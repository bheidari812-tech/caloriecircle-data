"""Shared rules of the Open Food Facts offline index prototype: text folding, nutrient validation
(a port of OpenFoodFactsClient.evaluate), variant grouping and the search itself.

Standard library only. The SQL and the ranking are written so that they can be ported 1:1 to Swift
(SQLite with FTS5 ships with iOS); nothing here depends on Python-specific SQLite features.
"""
import math
import re
import sqlite3
import unicodedata

SCHEMA_VERSION = 2
CANDIDATES = 400          # best full-text matches that are ranked in code
GRAM, MILLILITER = 0, 1

# ---------------------------------------------------------------- text

_DIGRAPHS = (("ae", "a"), ("oe", "o"), ("ue", "u"))


def fold(text):
    """Lowercase, ß→ss, diacritics removed, ae/oe/ue→a/o/u, everything else to single spaces.

    Index and query use the same folding, so "Hähnchen", "Haehnchen" and "Hahnchen" meet. The digraph
    rule also shortens words like "blue"→"blu" – harmless, because both sides are folded alike.
    """
    text = unicodedata.normalize("NFKD", (text or "").lower().replace("ß", "ss"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    for long, short in _DIGRAPHS:
        text = text.replace(long, short)
    return text


def canonical_barcode(code):
    """Digits without leading zeros as an integer (the stable product id), or None."""
    code = (code or "").strip()
    if not code or not code.isascii() or not code.isdigit() or len(code) > 14:
        return None
    value = int(code)
    return value if value > 0 else None


# ---------------------------------------------------------------- validation (port of the Swift rules)

_VOLUME = re.compile(r"[0-9]\s*(?:ml|cl|dl|l)\b")
_MASS = re.compile(r"^[0-9]+(?:[.,][0-9]+)?\s*(?:(?:x|×)\s*[0-9]+(?:[.,][0-9]+)?\s*)?(?:g|gr|gramm|gram|kg)\.?(?:\s*(?:℮|e))?$")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def core_values(product):
    """(kcal, protein, carbs, fat, fibre, declared) from either export layout, or a rejection reason.
    `declared` is what the record says its values refer to: "100g", "100ml" or something else.

    New layout (nutrition.aggregated_set): values that Open Food Facts estimated from ingredients are
    not label values and are never used. Old layout (nutriments): the "_100g" fields.
    """
    aggregated = product.get("aggregated")
    if aggregated and aggregated.get("nutrients"):
        if aggregated.get("preparation") not in (None, "as_sold"):
            return "prepared_only"
        per = aggregated.get("per")
        if per not in ("100g", "100ml"):
            return "not_per_100"
        nutrients = aggregated["nutrients"]

        def value(key):
            entry = nutrients.get(key) or {}
            return None if entry.get("source") == "estimate" else _number(entry.get("value"))
        kcal = value("energy-kcal")
        if kcal is None:
            kj = value("energy-kj")
            kj = value("energy") if kj is None else kj
            kcal = None if kj is None else kj / 4.184
        values = (kcal, value("proteins"), value("carbohydrates"), value("fat"), value("fiber"))
        declared = per
    else:
        old = product.get("nutriments") or {}
        kcal = _number(old.get("energy-kcal_100g"))
        if kcal is None:
            kj = _number(old.get("energy-kj_100g"))
            kj = _number(old.get("energy_100g")) if kj is None else kj
            kcal = None if kj is None else kj / 4.184
        values = (kcal, _number(old.get("proteins_100g")), _number(old.get("carbohydrates_100g")),
                  _number(old.get("fat_100g")), _number(old.get("fiber_100g")))
        declared = (product.get("nutrition_data_per") or "").lower().replace(" ", "")
    if any(v is None for v in values[:4]):
        return "incomplete_nutrients"
    return values + (declared,)


_VOLUME_IN_NAME = re.compile(r"(?<![a-z0-9])\d+(?:[.,]\d+)?\s*(?:ml|cl|l|liter)\b", re.IGNORECASE)
_DRINK_WORDS = ("drink", "getrank", "saft", "cola", "limo", "wasser", "schorle", "tee", "bier", "wein", "smoothie",
                "energy", "milch", "sirup", "kaffee", "nektar")


def reference_unit(product, declared):
    """(unit, confirmed), or "unclear_unit" / "unit_conflict". No conversion between g and ml.

    Open Food Facts stores values "per 100 g, or per 100 ml for liquids" in the same fields. Two independent
    statements exist: what the record declares ("100g" / "100ml") and how the package is sold (g or ml).
    A package size alone proves nothing: 4,576 German products are sold in ml but declare values per 100 g.

    confirmed   both statements agree, or the record explicitly declares "100ml" and has no package size
    assumed     no package size, no drink category, declared "100g" – which is also the default a contributor
                never touched, so a drink entered without size and category ends up here
    conflict    the statements contradict each other
    unclear     the package size cannot be read, or a drink without any size"""
    unit = str(product.get("product_quantity_unit") or "").strip().lower()
    if unit:
        basis = "mass" if unit in ("g", "kg") else "volume" if unit in ("ml", "cl", "dl", "l") else "unknown"
    else:
        quantity = str(product.get("quantity") or "").strip().lower()
        if not quantity:
            basis = "missing"
        elif _VOLUME.search(quantity):
            basis = "volume"
        elif _MASS.match(quantity):
            basis = "mass"
        else:
            basis = "unknown"
    if basis == "unknown":
        return "unclear_unit"
    if basis == "mass":
        return "unit_conflict" if declared == "100ml" else (GRAM, declared == "100g")
    if basis == "volume":
        return (MILLILITER, True) if declared == "100ml" else "unit_conflict"
    if declared == "100ml":
        return (MILLILITER, True)
    return "unclear_unit" if product.get("liquid") else (GRAM, False)


def drink_like(name):
    """Does the name suggest a drink? Only used to estimate how many assumed-gram products are doubtful."""
    folded = fold(name)
    return any(word in folded for word in _DRINK_WORDS)


def evaluate(product):
    """A row for the index, or the name of the first rule the product fails (rules in a fixed order)."""
    code = canonical_barcode(product.get("code"))
    if code is None:
        return "invalid_barcode"
    if product.get("obsolete"):
        return "obsolete"
    german = (product.get("product_name_de") or "").strip()
    name = german or (product.get("product_name") or "").strip()
    if not name:
        return "missing_name"
    core = core_values(product)
    if isinstance(core, str):
        return core
    kcal, protein, carbs, fat, fibre, declared = core
    if not (0 <= kcal <= 1000) or not all(0 <= v <= 100 for v in (protein, carbs, fat)) or protein + carbs + fat > 102:
        return "implausible_nutrients"
    usable_fibre = fibre if fibre is not None and 0 <= fibre <= 100 else None
    expected = 4 * protein + 4 * carbs + 9 * fat + 2 * (usable_fibre or 0)
    if kcal > expected * 1.5 + 50 or kcal < expected * 0.5 - 50:
        return "implausible_nutrients"
    if product.get("data_quality_errors_tags"):
        return "quality_errors"
    reference = reference_unit(product, declared)
    if isinstance(reference, str):
        return reference
    unit, confirmed = reference
    if unit == GRAM and _VOLUME_IN_NAME.search(name):
        return "unit_conflict"      # "… 250 ml" in the name, but everything else says per 100 g
    brand = ", ".join(part.strip() for part in str(product.get("brands") or "").split(",") if part.strip())
    alternatives = [product.get(k) for k in ("product_name", "generic_name_de", "abbreviated_product_name_de")]
    scans = product.get("unique_scans_n")
    return {
        "code": code, "name": " ".join(name.split()), "brand": brand,
        "alt": " ".join(sorted({fold(a) for a in alternatives if a and fold(a) != fold(name)})),
        "unit": unit, "unit_confirmed": int(confirmed), "kcal": round(kcal, 2), "protein": protein, "carbs": carbs, "fat": fat, "fiber": usable_fibre,
        "quantity": " ".join(str(product.get("quantity") or "").split()),
        "scans": int(scans) if isinstance(scans, (int, float)) else 0,
        # Is the shown name known to be German? (explicit German name, or German main language)
        "german_name": 1 if german or product.get("lc") == "de" else 0,
        "modified": int(product.get("last_modified_t") or 0),
    }


# ---------------------------------------------------------------- variants

_SIZE = re.compile(r"^(?:\d+(?:x\d+)?|\d*(?:g|kg|ml|cl|l|er|x|stuck|pack|st|pet|dose|flasche|mehrweg|einweg|pfand))$")


def first_brand(brand):
    return fold(brand.split(",")[0]) if brand else ""


def core_name(name, brand):
    """Folded name without brand words and without package-size words ("1,5 l", "6x330ml", "Dose")."""
    brand_words = set(first_brand(brand).split())
    sized = [w for w in fold(name).split() if not _SIZE.match(w)]
    words = [w for w in sized if w not in brand_words]
    return " ".join(words or sized) or fold(name)


_GENERIC_BRAND_WORDS = {"gmbh", "co", "kg", "ag", "the", "company", "sa", "nv", "srl", "services", "technical", "und", "and"}


def brand_words(brand):
    return {w for w in fold(brand).split() if w not in _GENERIC_BRAND_WORDS}


def same_variant(a, b):
    """Would two products look identical in a result list? Same core name and unit, the same brand family
    (a shared brand word, or the same brand written together) and energy within 4 % (at least 3 kcal).
    Different package sizes of one drink are the same variant; "Zero" and "Light" are not.
    A product without a brand is never merged into a branded one."""
    if a["unit"] != b["unit"] or a["core"] != b["core"]:
        return False
    if abs(a["kcal"] - b["kcal"]) > max(3.0, 0.04 * max(a["kcal"], b["kcal"])):
        return False
    words_a, words_b = brand_words(a["brand"]), brand_words(b["brand"])
    if not words_a or not words_b:
        return not words_a and not words_b
    return bool(words_a & words_b) or "".join(sorted(words_a)) == "".join(sorted(words_b)) \
        or fold(a["brand"]).replace(" ", "") == fold(b["brand"]).replace(" ", "")


# ---------------------------------------------------------------- search

def distance(a, b, limit):
    """Damerau-Levenshtein distance (adjacent transposition counts 1), or limit + 1 when above the limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2, previous = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            if previous2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cost = min(cost, previous2[j - 2] + 1)
            current.append(cost)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.db.execute("PRAGMA query_only = 1")

    def close(self):
        self.db.close()

    def barcode(self, text):
        code = canonical_barcode(text)
        if code is None:
            return None
        return self._row(self.db.execute(_SELECT + " WHERE p.code = ?", (code,)).fetchone())

    @staticmethod
    def _row(r):
        if r is None:
            return None
        keys = ("code", "name", "brand", "unit", "kcal", "protein", "carbs", "fat", "fiber", "quantity", "scans",
                "german_name", "unit_confirmed")
        return dict(zip(keys, r))

    @staticmethod
    def match_expression(tokens, alternatives=None):
        """All words must occur; the last one may be a prefix. Adjacent words may also be written together
        ("coca cola" finds "cocacola"). `alternatives` maps a word to corrected spellings."""
        alternatives = alternatives or {}
        parts = []
        for i, token in enumerate(tokens):
            last = i == len(tokens) - 1
            options = [f'"{token}"' + ("*" if last and len(token) >= 2 else "")]
            options += [f'"{other}"' for other in alternatives.get(token, [])]
            parts.append("(" + " OR ".join(options) + ")")
        expression = " AND ".join(parts)
        if len(tokens) >= 2:
            joined = [tokens[:i] + [tokens[i] + tokens[i + 1]] + tokens[i + 2:] for i in range(len(tokens) - 1)]
            for variant in joined:
                words = [f'"{t}"' + ("*" if i == len(variant) - 1 else "") for i, t in enumerate(variant)]
                expression += " OR (" + " AND ".join(words) + ")"
        return expression

    def known(self, token, prefix):
        """Is the word in the index (for the last word: does any indexed word start with it)?"""
        if prefix:
            return self.db.execute("SELECT 1 FROM vocab WHERE term >= ? AND term < ? LIMIT 1", (token, token + "\uffff")).fetchone() is not None
        return self.db.execute("SELECT 1 FROM vocab WHERE term = ?", (token,)).fetchone() is not None

    def corrections(self, token):
        """Indexed words within edit distance 1 (2 for long words) that start with the same letter."""
        if len(token) < 4 or token.isdigit():
            return []
        limit = 1 if len(token) < 8 else 2
        rows = self.db.execute(
            "SELECT term, docs FROM vocab WHERE term >= ? AND term < ? AND length(term) BETWEEN ? AND ?",
            (token[0], token[0] + "￿", len(token) - limit, len(token) + limit)).fetchall()
        scored = [(distance(token, term, limit), -docs, term) for term, docs in rows if term != token]
        return [term for d, _, term in sorted(s for s in scored if s[0] <= limit)[:3]]

    def search(self, query, offset=0, limit=10):
        """One page of results: {"total", "groups", "corrected", "results": [...]}.

        `total` counts matching products, `groups` the distinct variants among the ranked candidates.
        A purely numeric query of 8+ digits is a barcode lookup.
        """
        digits = query.strip().replace(" ", "")
        if digits.isdigit() and len(digits) >= 8:
            row = self.barcode(digits)
            return {"total": int(row is not None), "groups": int(row is not None), "corrected": {},
                    "results": [dict(row, variants=1)] if row else []}
        tokens = fold(query).split()
        if not tokens or (len(tokens) == 1 and len(tokens[0]) < 2):
            return {"total": 0, "groups": 0, "corrected": {}, "results": []}
        total, ranked = self._ranked(tokens, {})
        corrected = {}
        if len(ranked) < 3:
            # Too little: allow corrected spellings for words the index does not know well.
            corrected = {t: c for i, t in enumerate(tokens)
                         if not self.known(t, prefix=i == len(tokens) - 1) and (c := self.corrections(t))}
            if corrected:
                fixed_total, fixed = self._ranked(tokens, corrected)
                if len(fixed) > len(ranked):
                    total, ranked = fixed_total, fixed
                else:
                    corrected = {}
        return {"total": total, "groups": len(ranked), "corrected": corrected, "results": ranked[offset:offset + limit]}

    def _ranked(self, tokens, alternatives):
        expression = self.match_expression(tokens, alternatives)
        total = self.db.execute("SELECT count(*) FROM search WHERE search MATCH ?", (expression,)).fetchone()[0]
        if total == 0:
            return 0, []
        # bm25 weights: name, brand, alternative names. The popularity term keeps often-scanned products
        # among the candidates when thousands of names contain the word.
        rows = self.db.execute(
            _SELECT + " JOIN (SELECT rowid AS id, bm25(search, 10.0, 5.0, 1.0) AS score FROM search"
            " WHERE search MATCH ?) m ON m.id = p.code"
            " ORDER BY m.score - 0.6 * min(p.scans, 200) / 200.0 LIMIT ?", (expression, CANDIDATES)).fetchall()
        accepted = [set([t] + alternatives.get(t, [])) for t in tokens]
        phrase = " ".join(tokens)
        scored = []
        for raw in rows:
            row = self._row(raw)
            name = fold(row["name"])
            brand = fold(row["brand"])
            name_words, both = name.split(), (name + " " + brand).split()
            row["core"] = core_name(row["name"], row["brand"])
            scored.append((self._tier(phrase, accepted, name, name_words, both, row["core"]),
                           1 - row["german_name"], not row["brand"], 1 - row["unit_confirmed"], -row["scans"], len(name),
                           row["code"], row))
        scored.sort(key=lambda s: s[:7])
        # One entry per variant: the best-ranked product represents the others (see same_variant).
        by_core, results = {}, []
        for *_, row in scored:
            shown = next((r for r in by_core.get(row["core"], ()) if same_variant(r, row)), None)
            if shown is not None:
                shown["variants"] += 1
            else:
                row["variants"] = 1
                by_core.setdefault(row["core"], []).append(row)
                results.append(row)
        return total, results

    @staticmethod
    def _tier(phrase, accepted, name, name_words, both, core):
        def covered(words, whole):
            joined = "".join(words)
            for i, options in enumerate(accepted):
                last = i == len(accepted) - 1
                if any(w == o for w in words for o in options):
                    continue
                if whole or not any(o in joined or (last and any(w.startswith(o) for w in words)) for o in options):
                    return False
            return True
        def exact(words):
            return len(words) == len(accepted) and all(w in options for w, options in zip(words, accepted))
        if exact(name_words) or exact(core.split()) or name.replace(" ", "") == phrase.replace(" ", ""):
            return 0            # exact product name
        if covered(name_words, whole=True):
            return 1            # name with additional words
        if covered(name_words, whole=False):
            return 2            # name contains the words as a prefix or written together
        if covered(both, whole=False):
            return 3            # brand + name
        return 4                # alternative names only


_SELECT = ("SELECT p.code, p.name, p.brand, p.unit, p.kcal, p.protein, p.carbs, p.fat, p.fiber, p.quantity,"
           " p.scans, p.german_name, p.unit_confirmed FROM product p")
