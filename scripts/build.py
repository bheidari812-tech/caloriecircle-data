#!/usr/bin/env python3
"""Step 2: build the SQLite index from the German extract (see extract.py).

Usage:  python3 -I Scripts/off_index/build.py <out-dir> [--app <OFFProducts.sqlite> [--version <yyyymmddnn>]]
Reads <out-dir>/off-de-extract.jsonl.gz, writes <out-dir>/off-de.sqlite and <out-dir>/build-stats.json.
With --app it also writes the file that is bundled with CalorieCircle: only products whose unit is confirmed
(statistics in <out-dir>/build-stats-app.json). The build is deterministic: the same extract gives the same file.
With --version <yyyymmddnn> the app file records that version; this is required for a downloadable update
(see publish.py). The file shipped inside the app was built without it.

Tables
  product  one row per barcode; `code` (barcode without leading zeros) is the primary key and stable id;
           unit_confirmed = 0 marks values that are only assumed to be per 100 g
  search   contentless FTS5 index over folded name, brand and alternative names (rowid = code)
  vocab    indexed words with document counts, for spelling correction
  meta     source, date, licence, counts
Open Food Facts data stays in this separate file; the BLS and Swiss catalogues are not touched.
"""
import collections
import gzip
import hashlib
import json
import resource
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offindex  # noqa: E402

RULES = ("invalid_barcode", "obsolete", "missing_name", "prepared_only", "not_per_100", "incomplete_nutrients",
         "implausible_nutrients", "quality_errors", "unclear_unit", "unit_conflict", "duplicate_barcode")


def build(extract, target, source_info=None, fts_options="detail=column", confirmed_only=False, version=None):
    """Returns the statistics; `extract` is an iterable of product dicts.
    `confirmed_only` leaves out products whose unit is only assumed (the file shipped with the app)."""
    stats = collections.OrderedDict(read=0, **{rule: 0 for rule in RULES}, imported=0)
    rows = {}
    for product in extract:
        stats["read"] += 1
        row = offindex.evaluate(product)
        if isinstance(row, str):
            stats[row] += 1
            continue
        other = rows.get(row["code"])
        if other is not None:
            # The same barcode with and without leading zeros: keep the better-known, newer record.
            stats["duplicate_barcode"] += 1
            if (other["scans"], other["modified"]) >= (row["scans"], row["modified"]):
                continue
        rows[row["code"]] = row
    stats["imported"] = len(rows)
    stats["unit_assumed_left_out"] = 0
    if confirmed_only:
        stats["unit_assumed_left_out"] = sum(not r["unit_confirmed"] for r in rows.values())
        rows = {code: row for code, row in rows.items() if row["unit_confirmed"]}
        stats["imported"] = len(rows)
    stats.update(with_brand=sum(bool(r["brand"]) for r in rows.values()),
                 german_name=sum(r["german_name"] for r in rows.values()),
                 per_100_ml=sum(r["unit"] == offindex.MILLILITER for r in rows.values()),
                 unit_confirmed=sum(r["unit_confirmed"] for r in rows.values()),
                 unit_assumed_gram=sum(not r["unit_confirmed"] for r in rows.values()),
                 unit_assumed_gram_drink_like=sum(not r["unit_confirmed"] and offindex.drink_like(r["name"]) for r in rows.values()),
                 with_scans=sum(r["scans"] > 0 for r in rows.values()),
                 package_size_missing=sum(not r["quantity"] for r in rows.values()))

    target = Path(target)
    target.unlink(missing_ok=True)
    db = sqlite3.connect(target)
    db.executescript(f"""
        PRAGMA page_size = 4096;
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE product (
            code INTEGER PRIMARY KEY, name TEXT NOT NULL, brand TEXT NOT NULL, unit INTEGER NOT NULL,
            kcal REAL NOT NULL, protein REAL NOT NULL, carbs REAL NOT NULL, fat REAL NOT NULL, fiber REAL,
            quantity TEXT NOT NULL, scans INTEGER NOT NULL, german_name INTEGER NOT NULL,
            unit_confirmed INTEGER NOT NULL);
        CREATE VIRTUAL TABLE search USING fts5(name, brand, alt, content='', {fts_options});
        CREATE TABLE vocab (term TEXT PRIMARY KEY, docs INTEGER NOT NULL) WITHOUT ROWID;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
    """)
    ordered = sorted(rows.values(), key=lambda r: r["code"])
    db.executemany("INSERT INTO product VALUES (:code,:name,:brand,:unit,:kcal,:protein,:carbs,:fat,:fiber,:quantity,"
                   ":scans,:german_name,:unit_confirmed)", ordered)
    vocabulary = collections.Counter()

    def documents():
        for row in ordered:
            name, brand = offindex.fold(row["name"]), offindex.fold(row["brand"])
            # Brands written as one word ("cocacola", "redbull") are found through the alternative column.
            brand_words = offindex.first_brand(row["brand"]).split()
            alt = row["alt"] + (" " + "".join(brand_words) if len(brand_words) > 1 else "")
            vocabulary.update(set(name.split()) | set(brand.split()))
            yield row["code"], name, brand, alt.strip()
    db.executemany("INSERT INTO search(rowid, name, brand, alt) VALUES (?,?,?,?)", documents())
    # Words that occur once are mostly typing errors in the source; they are not offered as corrections.
    db.executemany("INSERT INTO vocab VALUES (?,?)", sorted((t, n) for t, n in vocabulary.items() if n >= 2 and len(t) >= 3))
    meta = {"schema": offindex.SCHEMA_VERSION, "products": len(ordered), "confirmed_units_only": int(confirmed_only),
            "source": "Open Food Facts (https://openfoodfacts.org), products listed for Germany",
            "licence": "Open Database License (ODbL) 1.0; contents: Database Contents License (DbCL) 1.0",
            "attribution": "Contains data from Open Food Facts, available under the Open Database License",
            **(source_info or {})}
    if version is not None:
        # Downloadable updates carry their version; the app compares it with the database it already has.
        meta["version"] = int(version)
    db.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    db.commit()
    db.execute("INSERT INTO search(search) VALUES ('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    return stats


def read_extract(path):
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            yield json.loads(line)


def main():
    arguments = [a for a in sys.argv[1:] if a != "--app"]
    version = None
    if "--version" in arguments:
        at = arguments.index("--version")
        version = int(arguments[at + 1])
        del arguments[at:at + 2]
    if len(arguments) not in (1, 2) or ("--app" in sys.argv) != (len(arguments) == 2):
        sys.exit(__doc__)
    out_dir = Path(arguments[0])
    info = {}
    if (out_dir / "extract-stats.json").exists():
        extract_stats = json.loads((out_dir / "extract-stats.json").read_text())
        info = {k: extract_stats[k] for k in ("source_file", "source_bytes", "source_last_modified") if k in extract_stats}
    jobs = [(out_dir / "off-de.sqlite", out_dir / "build-stats.json", False)]
    if len(arguments) == 2:
        # The file bundled with the app: confirmed units only.
        jobs.append((Path(arguments[1]), out_dir / "build-stats-app.json", True))
    for target, stats_file, confirmed_only in jobs:
        started = time.monotonic()
        stats = build(read_extract(out_dir / "off-de-extract.jsonl.gz"), target, info, confirmed_only=confirmed_only,
                      version=version if confirmed_only else None)
        stats["seconds"] = round(time.monotonic() - started, 1)
        stats["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)
        stats["sqlite_bytes"] = target.stat().st_size
        stats["sqlite_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        stats_file.write_text(json.dumps(stats, indent=2) + "\n")
        print(target, json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
