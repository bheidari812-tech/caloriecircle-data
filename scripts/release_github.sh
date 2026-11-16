#!/bin/sh
# Builds a product database update and, with --upload, publishes it as a GitHub release.
#
# Usage: Scripts/off_index/release_github.sh <owner/repo> <work-dir> <private-key.pem> [--upload] [--serial <nn>]
#
#   <owner/repo>   a PUBLIC repository that only holds the data releases (the app downloads from it without login)
#   <work-dir>     folder with off-de-extract.jsonl.gz and extract-stats.json (written by extract.py)
#   --serial       two digits to tell apart several releases of the same export (default 00)
#
# Without --upload nothing leaves this computer: the files are written to <work-dir>/release and checked.
# The app is configured with
#   https://github.com/<owner/repo>/releases/latest/download/manifest.json
# which always points to the newest release; each package lives under its own tag v<version>.
set -eu
[ $# -ge 3 ] || { sed -n '2,12p' "$0" >&2; exit 2; }
repo=$1; work=$2; key=$3; shift 3
upload=no; serial=00
while [ $# -gt 0 ]; do
    case $1 in
        --upload) upload=yes ;;
        --serial) serial=$2; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
here=$(cd "$(dirname "$0")" && pwd)
[ -f "$work/off-de-extract.jsonl.gz" ] && [ -f "$work/extract-stats.json" ] || { echo "run extract.py into $work first" >&2; exit 1; }
[ -f "$key" ] || { echo "private key not found: $key" >&2; exit 1; }

date=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_last_modified"])' "$work/extract-stats.json")
version=$(printf '%s' "$date" | cut -c1-10 | tr -d -)$serial
case $version in ??????????) ;; *) echo "cannot derive a version from the export date \"$date\"" >&2; exit 1 ;; esac
release=$work/release
rm -rf "$release"

python3 -I "$here/build.py" "$work" --app "$work/OFFProducts.sqlite" --version "$version" > /dev/null
python3 -I "$here/publish.py" "$work/OFFProducts.sqlite" "$release" \
    --base-url "https://github.com/$repo/releases/download/v$version/" --key "$key" --stats "$work/build-stats-app.json" > /dev/null
# The method that produced the database travels with it (ODbL): the scripts themselves.
# No user or group names of this computer in the archive.
tar --uid 0 --gid 0 --uname "" --gname "" -czf "$release/pipeline-scripts.tar.gz" -C "$here" \
    extract.py build.py offindex.py publish.py release_github.sh
# The public key belongs to this release; the app must contain the same one.
openssl ec -in "$key" -pubout -outform DER 2>/dev/null | base64 > "$release/public-key.txt"
openssl ec -in "$key" -pubout -out "$release/public-key.pem" 2>/dev/null
base64 -d < "$release/manifest.json.sig" > "$release/.signature.der" 2>/dev/null || base64 -D < "$release/manifest.json.sig" > "$release/.signature.der"
openssl dgst -sha256 -verify "$release/public-key.pem" -signature "$release/.signature.der" "$release/manifest.json" > /dev/null \
    || { echo "signature check failed" >&2; exit 1; }
rm -f "$release/.signature.der" "$release/public-key.pem"
gzip -t "$release/OFFProducts-$version.sqlite.gz"

echo "Version $version (export of $date)"
ls -l "$release" | awk 'NR > 1 { printf "  %10d  %s\n", $5, $9 }'
echo "Manifest address for the app: https://github.com/$repo/releases/latest/download/manifest.json"
echo "Public key for the app:       $(cat "$release/public-key.txt")"

if [ "$upload" = yes ]; then
    gh release create "v$version" --repo "$repo" --latest --title "Produktdatenbank $version" --notes-file "$release/SOURCE.md" \
        "$release/OFFProducts-$version.sqlite.gz" "$release/manifest.json" "$release/manifest.json.sig" \
        "$release/SOURCE.md" "$release/pipeline-scripts.tar.gz"
else
    echo "Not uploaded (no --upload)."
fi
