#!/usr/bin/env python3
"""Step 1: reduce the official Open Food Facts JSONL export to the products listed for Germany.

Source: https://static.openfoodfacts.org/data/openfoodfacts-products.jsonl.gz (Open Database Licence).
Usage:  python3 -I Scripts/off_index/extract.py <openfoodfacts-products.jsonl.gz | -> <out-dir> [<export date, e.g. 2026-11-15T05:36:39Z>]
        ("-" streams the export from standard input: curl -sL <url> | python3 -I extract.py - out <date>)

Writes <out-dir>/off-de-extract.jsonl.gz (only the fields the index needs, no quality filtering yet) and
<out-dir>/extract-stats.json. Only the standard library is used; the export is streamed, never loaded.
Nothing is filtered here except the country, so the quality rules in build.py can be changed and re-run
without reading the 13 GB export again.
"""
import gzip
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

TEXT_FIELDS = ("code", "lc", "product_name", "product_name_de", "generic_name_de", "abbreviated_product_name_de",
               "brands", "quantity", "product_quantity", "product_quantity_unit", "nutrition_data_per",
               "serving_size", "serving_quantity", "serving_quantity_unit", "unique_scans_n", "completeness",
               "last_modified_t", "obsolete", "countries_tags", "data_quality_errors_tags", "schema_version")
# Core values in the old layout ("nutriments") and the new one ("nutrition.aggregated_set", schema >= 1003).
OLD_KEYS = ("energy-kcal_100g", "energy-kj_100g", "energy_100g", "proteins_100g", "carbohydrates_100g",
            "fat_100g", "fiber_100g")
NEW_KEYS = ("energy-kcal", "energy-kj", "energy", "proteins", "carbohydrates", "fat", "fiber")
LIQUID = {"en:beverages", "en:drinks", "en:waters", "en:milks", "en:plant-based-milks", "en:dairy-drinks",
          "en:juices", "en:fruit-juices", "en:sodas", "en:energy-drinks", "en:alcoholic-beverages",
          "en:oils", "en:vegetable-oils", "en:syrups", "en:vinegars", "en:liquid-soups"}


def reduce(product):
    out = {k: product[k] for k in TEXT_FIELDS if product.get(k) not in (None, "", [])}
    out["liquid"] = any(c in LIQUID for c in product.get("categories_tags") or [])
    old = product.get("nutriments") or {}
    if old:
        out["nutriments"] = {k: old[k] for k in OLD_KEYS if k in old}
    aggregated = (product.get("nutrition") or {}).get("aggregated_set") or {}
    if aggregated:
        nutrients = aggregated.get("nutrients") or {}
        out["aggregated"] = {
            "per": aggregated.get("per"), "preparation": aggregated.get("preparation"),
            "nutrients": {k: {f: nutrients[k].get(f) for f in ("value", "unit", "source", "modifier")}
                          for k in NEW_KEYS if isinstance(nutrients.get(k), dict)}}
    return out


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    streamed = sys.argv[1] == "-"
    source = Path("openfoodfacts-products.jsonl.gz" if streamed else sys.argv[1])
    stats = {"source_file": source.name, "source_bytes": None if streamed else source.stat().st_size,
             "source_last_modified": sys.argv[3] if len(sys.argv) == 4 else "unknown", "lines": 0, "unreadable": 0,
             "germany": 0, "no_country": 0, "no_country_german_name": 0}
    # "-" reads the export from standard input, so an automated run never has to store the 13 GB file.
    unzip = subprocess.Popen(["gzip", "-dc"] + ([] if streamed else [str(source)]), stdin=sys.stdin.buffer if streamed else None,
                             stdout=subprocess.PIPE, bufsize=1 << 20)
    with gzip.open(out_dir / "off-de-extract.jsonl.gz", "wt", encoding="utf-8", compresslevel=6) as out:
        for line in unzip.stdout:
            stats["lines"] += 1
            germany = b'"en:germany"' in line
            # Products without any country are only counted, to judge whether they are worth a later look.
            unlisted = not germany and b'"countries_tags":[]' in line
            if not germany and not unlisted:
                continue
            try:
                product = json.loads(line)
            except ValueError:
                stats["unreadable"] += 1
                continue
            countries = product.get("countries_tags") or []
            if "en:germany" in countries:
                stats["germany"] += 1
                out.write(json.dumps(reduce(product), ensure_ascii=False, separators=(",", ":")) + "\n")
            elif not countries:
                stats["no_country"] += 1
                stats["no_country_german_name"] += bool(product.get("product_name_de"))
    if unzip.wait() != 0:
        sys.exit("gzip failed: the export is incomplete or damaged")
    stats["seconds"] = round(time.monotonic() - started, 1)
    stats["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)
    stats["extract_bytes"] = (out_dir / "off-de-extract.jsonl.gz").stat().st_size
    (out_dir / "extract-stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
