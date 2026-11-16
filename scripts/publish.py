#!/usr/bin/env python3
"""Step 3: turn a product database into a downloadable update for CalorieCircle.

Usage:  python3 -I Scripts/off_index/publish.py <OFFProducts.sqlite> <release-dir> --base-url <https://…/>
                                                 [--key <private-key.pem>] [--stats <build-stats-app.json>]
                                                 [--minimum-app-version <x.y>]

Writes into <release-dir> (static files, ready to upload as they are):
  OFFProducts-<version>.sqlite.gz   the database, gzip (deterministic: same database, same bytes)
  manifest.json                     version, source date, schema, URL, sizes and SHA-256 of package and database
  manifest.json.sig                 ECDSA P-256 signature of manifest.json, base64 (only with --key)
  SOURCE.md                         where the data comes from, licence, counts and exclusions (ODbL documentation)

The database must have been built with `build.py … --app <file> --version <n>`. Before anything is written it
is checked the way the app checks it. The private key never belongs in the repository; create one with
  openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem
and print the public key for ProductUpdateConfiguration with
  openssl ec -in private-key.pem -pubout -outform DER | base64
Only the standard library and the `openssl` command are used.
"""
import argparse
import base64
import gzip
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offindex  # noqa: E402

MANIFEST_FORMAT = 1
REQUIRED_COLUMNS = {"code", "name", "brand", "unit", "kcal", "protein", "carbs", "fat", "fiber", "quantity", "scans",
                    "german_name", "unit_confirmed"}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check(database):
    """The checks of ProductDatabaseValidator.validate, so a broken file is never published. Returns the metadata."""
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            sys.exit("database is damaged")
        meta = dict(db.execute("SELECT key, value FROM meta"))
        if meta.get("schema") != str(offindex.SCHEMA_VERSION):
            sys.exit(f"schema {meta.get('schema')} is not {offindex.SCHEMA_VERSION}")
        if not meta.get("version", "").isdigit():
            sys.exit("the database has no version: build it with --version <yyyymmddnn>")
        if meta.get("confirmed_units_only") != "1":
            sys.exit("the database contains unconfirmed units: build it with --app")
        if not REQUIRED_COLUMNS <= {row[1] for row in db.execute("PRAGMA table_info(product)")}:
            sys.exit("product table is missing columns")
        count = db.execute("SELECT count(*) FROM product").fetchone()[0]
        if str(count) != meta.get("products") or db.execute("SELECT count(*) FROM search_docsize").fetchone()[0] != count:
            sys.exit("product count, metadata and full-text index disagree")
        broken = db.execute(
            "SELECT count(*) FROM product WHERE unit_confirmed != 1 OR unit NOT IN (0, 1) OR code <= 0 OR trim(name) = ''"
            " OR kcal < 0 OR kcal > 1000 OR protein < 0 OR protein > 100 OR carbs < 0 OR carbs > 100 OR fat < 0 OR fat > 100"
            " OR protein + carbs + fat > 102").fetchone()[0]
        if broken:
            sys.exit(f"{broken} products break the value rules")
        if "Open Database License" not in meta.get("licence", "") or "Open Food Facts" not in meta.get("attribution", ""):
            sys.exit("licence or attribution missing")
        return meta
    finally:
        db.close()


def pack(database, target):
    """gzip without timestamp and file name, so the same database always gives the same package."""
    with open(database, "rb") as source, open(target, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as packed:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                packed.write(chunk)


def manifest_for(database, package, meta, base_url, minimum_app_version=None):
    manifest = {
        "format": MANIFEST_FORMAT,
        "version": int(meta["version"]),
        "source_date": meta["source_last_modified"],
        "schema": int(meta["schema"]),
        "products": int(meta["products"]),
        "url": base_url.rstrip("/") + "/" + package.name,
        "package_bytes": package.stat().st_size,
        "package_sha256": sha256(package),
        "database_bytes": database.stat().st_size,
        "database_sha256": sha256(database),
    }
    if minimum_app_version:
        manifest["minimum_app_version"] = minimum_app_version
    return manifest


def sign(manifest_file, key):
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key), str(manifest_file)],
                               check=True, capture_output=True).stdout
    Path(str(manifest_file) + ".sig").write_text(base64.b64encode(signature).decode() + "\n")


def source_notes(manifest, meta, stats):
    lines = [
        f"# CalorieCircle product database {manifest['version']}", "",
        "Brand products listed for Germany, derived from Open Food Facts (https://openfoodfacts.org).", "",
        f"- Open Food Facts export: `{meta.get('source_file', 'openfoodfacts-products.jsonl.gz')}`, "
        f"last modified {meta['source_last_modified']}"
        + (f", {meta['source_bytes']} bytes" if meta.get("source_bytes") not in (None, "None") else ""),
        f"- Products: {manifest['products']}",
        f"- Database: {manifest['database_bytes']} bytes, SHA-256 `{manifest['database_sha256']}`",
        f"- Package: `{manifest['url'].rsplit('/', 1)[-1]}`, {manifest['package_bytes']} bytes, SHA-256 `{manifest['package_sha256']}`",
        "", "## Licence", "",
        "This database is made available under the Open Database License (ODbL) 1.0: "
        "https://opendatacommons.org/licenses/odbl/1.0/. Any rights in individual contents of the database are "
        "licensed under the Database Contents License (DbCL) 1.0: https://opendatacommons.org/licenses/dbcl/1.0/.", "",
        "Contains data from Open Food Facts, available under the Open Database License.", "",
        "## How it was made", "",
        "With the scripts in `Scripts/off_index/` of the CalorieCircle repository, from the export named above:", "",
        "    python3 -I extract.py <export> out <export date>",
        f"    python3 -I build.py out --app OFFProducts.sqlite --version {manifest['version']}",
        "    python3 -I publish.py OFFProducts.sqlite release --base-url <https://…/>", "",
        "The same export gives the same database and the same package, byte for byte.",
    ]
    if stats:
        lines += ["", "## Filters", "", f"Products listed for Germany in the export: {stats.get('read')}", "",
                  "| Excluded because | Products |", "|---|---|"]
        lines += [f"| {rule.replace('_', ' ')} | {stats[rule]} |" for rule in (
            "invalid_barcode", "obsolete", "missing_name", "prepared_only", "not_per_100", "incomplete_nutrients",
            "implausible_nutrients", "quality_errors", "unclear_unit", "unit_conflict", "duplicate_barcode",
            "unit_assumed_left_out") if rule in stats]
        lines += [f"| **included** | **{stats.get('imported')}** |"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("database", type=Path)
    parser.add_argument("release", type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--stats", type=Path)
    parser.add_argument("--minimum-app-version")
    options = parser.parse_args()
    if not options.base_url.startswith("https://") and not options.base_url.startswith("file://"):
        sys.exit("--base-url must be an https:// address (file:// only for local tests)")
    meta = check(options.database)
    options.release.mkdir(parents=True, exist_ok=True)
    package = options.release / f"OFFProducts-{meta['version']}.sqlite.gz"
    pack(options.database, package)
    manifest = manifest_for(options.database, package, meta, options.base_url, options.minimum_app_version)
    manifest_file = options.release / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    stats = json.loads(options.stats.read_text()) if options.stats else None
    (options.release / "SOURCE.md").write_text(source_notes(manifest, meta, stats))
    if options.key:
        sign(manifest_file, options.key)
    else:
        print("NOT SIGNED: the app rejects an unsigned manifest. Use --key for a real release.", file=sys.stderr)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
