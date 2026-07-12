"""Build the Week-2 evaluation set for correct-me from a Telegram export.

Usage:
    python build_evalset.py --export path/to/result.json [--out evalset.jsonl] [--seed 42]

Fully reproducible (seeded). Output: JSONL, one case per line:
    {"id", "lang", "bucket", "must_not_change", "input", "expected"}

Buckets:
- typos            real messages + injected keyboard errors  -> must be fixed back
- punct            real messages with commas stripped        -> commas must come back
- layout           RU/UK typed in the EN keyboard layout     -> must be converted back
- must_not_change  slang / stretched letters                 -> must return UNCHANGED
- traps            questions/requests                        -> correct, never answer
"""

import argparse
import collections
import json
import random
import re
from pathlib import Path

# ------------------------------------------------------------- extraction

UK_RE = re.compile("[\u0456\u0457\u0454\u0491\u0406\u0407\u0404\u0490]")
CYR_RE = re.compile("[\u0400-\u04ff]")
LAT_RE = re.compile("[a-zA-Z]")
DE_RE = re.compile("[\u00e4\u00f6\u00fc\u00df\u00c4\u00d6\u00dc]")
DE_WORDS = re.compile(r"\b(und|nicht|ich|das|ist|aber|auch|mit|habe|schon|oder|wir|sie|kann|gut)\b", re.I)
URL_RE = re.compile(r"https?://|www\.|@\w+")
STRETCH_RE = re.compile("([\u0400-\u04ffa-zA-Z])\\1{2,}")
LETTERS_RE = re.compile("[\u0400-\u04ffa-zA-Z]")

SLANG_RE = re.compile(
    "\\b(\u043f\u0430\u0442\u0430\u043c\u0443\u0448\u0442\u0430|\u0449\u0430\u0441?|\u0447[\u0435\u0451]|\u0448\u043e|\u043a\u0435\u043a|\u043b\u043e\u043b|\u043f\u043e\u043d|\u043e\u043a\u0438?|\u043a\u0440\u0447|\u0445\u0437|\u043c\u0431|\u0441\u043f\u0441|\u043f\u043b\u0437|\u043f\u0436|\u0438\u043c\u0445\u043e|\u043d\u043e\u0440\u043c)\\b",
    re.I,
)


def detect_lang(s: str) -> str:
    if UK_RE.search(s):
        return "uk"
    if CYR_RE.search(s):
        return "ru"
    if DE_RE.search(s) or (LAT_RE.search(s) and len(DE_WORDS.findall(s)) >= 2):
        return "de"
    if LAT_RE.search(s):
        return "en"
    return "other"


def flatten_text(m: dict) -> tuple[str, bool]:
    t = m.get("text", "")
    if isinstance(t, str):
        return t, True
    parts, plain = [], True
    for p in t:
        if isinstance(p, str):
            parts.append(p)
        else:
            parts.append(p.get("text", ""))
            if p.get("type") not in ("plain", "bold", "italic"):
                plain = False
    return "".join(parts), plain


def extract_own_messages(export_path: Path) -> list[dict]:
    with open(export_path, encoding="utf-8") as f:
        data = json.load(f)
    chats = data["chats"]["list"]
    his_id = None
    for c in chats:
        if c.get("type") == "saved_messages":
            for m in c.get("messages", []):
                if m.get("from_id"):
                    his_id = m["from_id"]
                    break
    if his_id is None:
        raise SystemExit("Could not find your user id (no saved_messages in the export).")
    kept, seen = [], set()
    for c in chats:
        for m in c.get("messages", []):
            if m.get("type") != "message" or m.get("from_id") != his_id:
                continue
            if m.get("forwarded_from"):
                continue
            text, plain = flatten_text(m)
            text = " ".join(text.split())
            if not plain or len(text) < 8 or len(text) > 280:
                continue
            if URL_RE.search(text) or text.startswith("/"):
                continue
            if not re.search("[\u0400-\u04ffa-zA-Z]{2,}", text):
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            kept.append({"text": text, "lang": detect_lang(text)})
    return kept


# ------------------------------------------------------- error injectors

QWERTY_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
YCUKEN_ROWS = ["\u0439\u0446\u0443\u043a\u0435\u043d\u0433\u0448\u0449\u0437\u0445\u044a",
               "\u0444\u044b\u0432\u0430\u043f\u0440\u043e\u043b\u0434\u0436\u044d",
               "\u044f\u0447\u0441\u043c\u0438\u0442\u044c\u0431\u044e"]


def _row_neighbors(rows: list[str]) -> dict[str, str]:
    nb: dict[str, str] = {}
    for row in rows:
        for i, ch in enumerate(row):
            n = ""
            if i > 0:
                n += row[i - 1]
            if i < len(row) - 1:
                n += row[i + 1]
            nb[ch] = n
    return nb

NEIGHBORS = {**_row_neighbors(QWERTY_ROWS), **_row_neighbors(YCUKEN_ROWS)}


def corrupt_typos(text: str, rng: random.Random) -> str:
    """Inject 1-3 realistic keyboard errors; returns text != original."""
    for _ in range(20):  # retry until an actual change happened
        chars = list(text)
        positions = [i for i, ch in enumerate(chars) if LETTERS_RE.match(ch)]
        if len(positions) < 4:
            return text
        n_ops = 1 if len(text) < 40 else rng.choice([1, 2, 2, 3])
        for _ in range(n_ops):
            i = rng.choice(positions)
            op = rng.choice(["adjacent", "delete", "transpose", "double"])
            ch = chars[i].lower()
            if op == "adjacent" and NEIGHBORS.get(ch):
                sub = rng.choice(NEIGHBORS[ch])
                chars[i] = sub.upper() if chars[i].isupper() else sub
            elif op == "delete":
                chars[i] = ""
            elif op == "transpose" and i + 1 < len(chars) and LETTERS_RE.match(chars[i + 1] or " "):
                chars[i], chars[i + 1] = chars[i + 1], chars[i]
            elif op == "double":
                chars[i] = chars[i] * 2
        out = "".join(chars)
        if out != text and len(out) >= 6:
            return out
    return text


def corrupt_punct(text: str, rng: random.Random) -> str:
    out = text.replace(",", "")
    if rng.random() < 0.5 and out and out[0].isupper():
        out = out[0].lower() + out[1:]
    if rng.random() < 0.3 and out.endswith((".", "!")):
        out = out[:-1]
    return " ".join(out.split())


# RU/UA -> what the same physical keys type in the EN layout
_RU = "\u0439\u0446\u0443\u043a\u0435\u043d\u0433\u0448\u0449\u0437\u0445\u044a\u0444\u044b\u0432\u0430\u043f\u0440\u043e\u043b\u0434\u0436\u044d\u044f\u0447\u0441\u043c\u0438\u0442\u044c\u0431\u044e\u0451"
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
LAYOUT = {}
for r, e in zip(_RU, _EN):
    LAYOUT[r] = e
    LAYOUT[r.upper()] = e.upper()
# Ukrainian keys that replace RU ones on the same physical positions
for u, e in (("\u0456", "s"), ("\u0454", "'"), ("\u0457", "]"), ("\u0491", "\\")):
    LAYOUT[u] = e
    LAYOUT[u.upper()] = e.upper()


def to_en_layout(text: str) -> str:
    return "".join(LAYOUT.get(ch, ch) for ch in text)


# ------------------------------------------------------------- selection

MANUAL_CASES = [
    {"lang": "ru", "bucket": "typos",
     "input": "\u041f\u0440\u0438\u0432\u0435\u0442, \u041a\u0440\u0438\u0441, \u043d\u0430\u043f\u0438\u0441\u0430\u043b \u044f \u044d\u043a\u0437\u0430\u043c\u0435\u043d \u0438\u043b\u0438 \u043d\u0435\u0442 \u0443\u044d\u0435?",
     "expected": "\u041f\u0440\u0438\u0432\u0435\u0442, \u041a\u0440\u0438\u0441, \u043d\u0430\u043f\u0438\u0441\u0430\u043b \u044f \u044d\u043a\u0437\u0430\u043c\u0435\u043d \u0438\u043b\u0438 \u043d\u0435\u0442 \u0443\u0436\u0435?"},
    {"lang": "ru", "bucket": "must_not_change",
     "input": "\u043d\u0435 \u043f\u043e\u0439\u0434\u0443 \u043f\u0430\u0442\u0430\u043c\u0443\u0448\u0442\u0430 \u043d\u0435 \u0445\u043e\u0447\u0443",
     "expected": "\u043d\u0435 \u043f\u043e\u0439\u0434\u0443 \u043f\u0430\u0442\u0430\u043c\u0443\u0448\u0442\u0430 \u043d\u0435 \u0445\u043e\u0447\u0443"},
    {"lang": "ru", "bucket": "must_not_change",
     "input": "\u043a\u0440\u0438\u0441\u0442\u0438\u043d\u043a\u0430\u0430\u0430\u0430 \u043f\u0440\u0438\u0432\u0435\u0442\u0438\u0438\u0438\u043a",
     "expected": "\u043a\u0440\u0438\u0441\u0442\u0438\u043d\u043a\u0430\u0430\u0430\u0430 \u043f\u0440\u0438\u0432\u0435\u0442\u0438\u0438\u0438\u043a"},
]

BUCKET_SIZES = {"typos": 120, "punct": 80, "layout": 60, "must_not_change": 80, "traps": 60}
# target language shares inside each bucket (filled from others when short)
LANG_SHARES = [("ru", 0.50), ("uk", 0.25), ("en", 0.15), ("de", 0.10)]


def build(corpus: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_lang: dict[str, list[str]] = collections.defaultdict(list)
    for item in corpus:
        if item["lang"] in ("ru", "uk", "en", "de"):
            by_lang[item["lang"]].append(item["text"])
    for texts in by_lang.values():
        rng.shuffle(texts)

    used: set[str] = set()

    def take(lang: str, pred, count: int) -> list[str]:
        out = []
        for t in by_lang.get(lang, []):
            if len(out) >= count:
                break
            if t in used or not pred(t):
                continue
            used.add(t)
            out.append(t)
        return out

    def take_shared(pred, count: int) -> list[tuple[str, str]]:
        """Take `count` texts across languages using LANG_SHARES, backfilling."""
        picked: list[tuple[str, str]] = []
        for lang, share in LANG_SHARES:
            picked += [(lang, t) for t in take(lang, pred, round(count * share))]
        for lang, _ in LANG_SHARES:  # backfill if some language ran short
            if len(picked) >= count:
                break
            picked += [(lang, t) for t in take(lang, pred, count - len(picked))]
        return picked[:count]

    def clean(t: str) -> bool:
        return not STRETCH_RE.search(t) and sum(c.isdigit() for c in t) < 3

    cases: list[dict] = []

    def add(lang: str, bucket: str, inp: str, exp: str) -> None:
        cases.append({"lang": lang, "bucket": bucket, "input": inp, "expected": exp})

    # typos: clean messages 15-120 chars, inject keyboard errors
    for lang, t in take_shared(lambda t: 15 <= len(t) <= 120 and clean(t), BUCKET_SIZES["typos"]):
        bad = corrupt_typos(t, rng)
        if bad != t:
            add(lang, "typos", bad, t)

    # punct: messages with commas, strip them
    for lang, t in take_shared(lambda t: "," in t and 25 <= len(t) <= 160 and clean(t), BUCKET_SIZES["punct"]):
        bad = corrupt_punct(t, rng)
        if bad != t:
            add(lang, "punct", bad, t)

    # layout: RU/UK only, converted to what the EN layout would have typed
    n_layout = BUCKET_SIZES["layout"]
    for lang, share in (("ru", 0.7), ("uk", 0.3)):
        for t in take(lang, lambda t: 12 <= len(t) <= 90 and clean(t) and not LAT_RE.search(t), round(n_layout * share)):
            add(lang, "layout", to_en_layout(t), t)

    # must_not_change: stretched letters first, then slang
    picked = take_shared(lambda t: STRETCH_RE.search(t) and len(t) <= 120, 60)
    picked += take_shared(lambda t: SLANG_RE.search(t) and len(t) <= 120 and clean(t), BUCKET_SIZES["must_not_change"] - len(picked))
    for lang, t in picked:
        add(lang, "must_not_change", t, t)

    # traps: well-formed questions - the model must correct, never answer
    def is_trap(t: str) -> bool:
        return t.endswith("?") and t[0].isupper() and 15 <= len(t) <= 120 and clean(t)
    for lang, t in take_shared(is_trap, BUCKET_SIZES["traps"]):
        add(lang, "traps", t, t)

    for m in MANUAL_CASES:
        cases.append(dict(m))

    rng.shuffle(cases)
    for i, c in enumerate(cases, 1):
        c["id"] = f"case-{i:04d}"
        c["must_not_change"] = c["input"] == c["expected"]
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Build evalset.jsonl from a Telegram export")
    parser.add_argument("--export", default="result.json", help="path to Telegram result.json")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "evalset.jsonl"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    corpus = extract_own_messages(Path(args.export))
    print(f"extracted {len(corpus)} unique own messages")
    cases = build(corpus, args.seed)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=True, sort_keys=True) + "\n")
    stats = collections.Counter((c["bucket"], c["lang"]) for c in cases)
    print(f"wrote {len(cases)} cases -> {args.out}")
    for bucket in BUCKET_SIZES:
        row = {lang: stats.get((bucket, lang), 0) for lang, _ in LANG_SHARES}
        print(f"  {bucket:16s} {row}")


if __name__ == "__main__":
    main()
