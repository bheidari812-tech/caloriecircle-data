# CalorieCircle – Produktdatenbank

Dieses Repository stellt die Offline-Produktdatenbank der iOS-App CalorieCircle öffentlich bereit: Markenprodukte,
die bei [Open Food Facts](https://openfoodfacts.org) für Deutschland eingetragen sind, mit vollständigen, plausiblen
Nährwerten je 100 g oder 100 ml.

Die Datenbanken liegen als [Releases](../../releases) vor. Jedes Release enthält:

| Datei | Inhalt |
|---|---|
| `OFFProducts-<Version>.sqlite.gz` | die Datenbank (SQLite, gzip-komprimiert) |
| `manifest.json` | Version, Datenstand, Größen und SHA-256-Prüfsummen |
| `manifest.json.sig` | Signatur des Manifests (ECDSA P-256, base64) |
| `SOURCE.md` | verwendeter Open-Food-Facts-Export, Zählungen, Ausschlüsse, Herstellungsweg |
| `pipeline-scripts.tar.gz` | die Skripte, mit denen die Datenbank erzeugt wurde |

## Lizenz und Quelle

Die Datenbank ist eine abgeleitete Datenbank von Open Food Facts und steht unter der
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1.0/). Rechte an einzelnen Inhalten
stehen unter der [Database Contents License (DbCL) 1.0](https://opendatacommons.org/licenses/dbcl/1.0/).

> Contains data from Open Food Facts, available under the Open Database License.

Open Food Facts wird von Freiwilligen gepflegt. Die Angaben können unvollständig oder fehlerhaft sein und ersetzen
nicht die Angaben auf der Verpackung. Sie dürfen nicht für medizinische Zwecke verwendet werden. Produktbilder sind
nicht enthalten.

Die Skripte in `scripts/` dürfen frei verwendet werden, um die Datenbank nachzubauen oder zu prüfen.

## Herstellung

Aus dem offiziellen Export `https://static.openfoodfacts.org/data/openfoodfacts-products.jsonl.gz`, nur mit Python 3
(Standardbibliothek) und `openssl`:

```bash
curl -sL https://static.openfoodfacts.org/data/openfoodfacts-products.jsonl.gz \
  | python3 -I scripts/extract.py - out <Datum des Exports>
python3 -I scripts/build.py out --app out/OFFProducts.sqlite --version <jjjjmmttnn>
python3 -I scripts/publish.py out/OFFProducts.sqlite release --base-url <https://…/>
```

Derselbe Export ergibt Byte für Byte dieselbe Datenbank und dasselbe Paket. Aufgenommen werden nur Produkte mit
numerischem Barcode, Namen, vollständigen Kernnährwerten vom Etikett (keine geschätzten Werte), plausibler Energie
und bestätigter Einheit: Die Deklaration „je 100 g“ bzw. „je 100 ml“ muss zur Verkaufseinheit der Packung passen.
Es wird nie zwischen g und ml umgerechnet. Die genauen Zahlen jedes Releases stehen in dessen `SOURCE.md`.

## Prüfen

```bash
# Signatur des Manifests
base64 -d public-key.txt > public-key.der && openssl ec -pubin -inform DER -in public-key.der -out public-key.pem
base64 -d manifest.json.sig > manifest.sig
openssl dgst -sha256 -verify public-key.pem -signature manifest.sig manifest.json

# Prüfsummen von Paket und Datenbank (Vergleich mit manifest.json)
shasum -a 256 OFFProducts-*.sqlite.gz
gzip -dc OFFProducts-*.sqlite.gz | shasum -a 256
```

Der öffentliche Schlüssel steht in [`public-key.txt`](public-key.txt). Die App akzeptiert nur ein Manifest, das mit
dem zugehörigen privaten Schlüssel signiert ist.

---

**English summary:** Offline product database of the CalorieCircle iOS app – brand products listed for Germany,
derived from Open Food Facts and made available under the ODbL 1.0 (contents: DbCL 1.0). Releases contain the SQLite
database, a signed manifest, source notes and the scripts that produced it.
