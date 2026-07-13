"""Generates a personal layout.py (vocab + bigram data baked in) from your own
Telegram messages, so the wrong-layout fixer and vocab protection are tuned
to YOUR vocabulary. Not shipped with data: everyone generates their own.

Usage:
    python gen_layout.py --export result.json            # Telegram Desktop export
    python gen_layout.py --corpus corpus.jsonl           # or a prebuilt corpus
"""
import argparse
import json
import re
import collections
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--corpus", help="corpus.jsonl with {text, lang} lines")
ap.add_argument("--export", help="Telegram Desktop result.json export")
ap.add_argument("--out", default="layout.py", help="output file (default: layout.py)")
args = ap.parse_args()

if args.corpus:
    corpus = [json.loads(l) for l in open(args.corpus, encoding="utf-8")]
elif args.export:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
    from build_evalset import extract_own_messages
    corpus = extract_own_messages(args.export)
else:
    ap.error("pass --export result.json or --corpus corpus.jsonl")
WORD_RE = re.compile("[\u0430-\u044f\u0451\u0456\u0457\u0454\u0491']+")

freq = {"ru": collections.Counter(), "uk": collections.Counter()}
bigrams = collections.Counter()
firsts = collections.Counter()
for item in corpus:
    lang = item["lang"]
    for w in WORD_RE.findall(item["text"].lower()):
        if len(w) > 20:
            continue
        if lang in freq:
            freq[lang][w] += 1
        if lang in ("ru", "uk"):
            ww = "^" + w + "$"
            for a, b in zip(ww, ww[1:]):
                bigrams[a + b] += 1
                firsts[a] += 1

vocab_ru = [w for w, c in freq["ru"].most_common(4000) if c >= 3]
vocab_uk = [w for w, c in freq["uk"].most_common(4000) if c >= 3]

TEMPLATE = '''"""Deterministic wrong-keyboard-layout fixer (Punto Switcher style).

Detects text typed with the wrong layout active (Russian/Ukrainian typed while
the keyboard was in English mode, e.g. "ghbdtn" -> "privet" keys) and converts
it back BEFORE the text is sent to the model. Zero latency, zero hallucination
risk: pure table lookup plus a plausibility score.

The vocabulary and character-bigram statistics baked into this file were
generated from the owner\'s own Telegram message corpus (build: gen_layout.py),
so the detector is tuned to their vocabulary in Russian and Ukrainian.
All data is stored ASCII-escaped; no personal message text is embedded, only
word frequencies.
"""

import json
import math
import re

# Physical key correspondence: RU letters on the same keys as EN characters.
_RU = "@@RU_ROW@@"
_EN = "qwertyuiop[]asdfghjkl;\'zxcvbnm,.`"

EN2RU = {}
for _r, _e in zip(_RU, _EN):
    EN2RU[_e] = _r
    if _e.upper() != _e:
        EN2RU[_e.upper()] = _r.upper()

# Ukrainian layout: same keys, four differences.
EN2UK = dict(EN2RU)
for _u, _e in (("\\u0456", "s"), ("\\u0454", "\'"), ("\\u0457", "]"), ("\\u0491", "\\\\")):
    EN2UK[_e] = _u
    if _e.upper() != _e:
        EN2UK[_e.upper()] = _u.upper()

# Characters that are both real punctuation AND letters on the RU/UK layout
# (e.g. "," is the key for Cyrillic "b"). Resolved per word by scoring.
_AMBIG = set(",.;\'[]`\\\\")

_VOCAB_RU = set(json.loads("""@@VOCAB_RU@@"""))
_VOCAB_UK = set(json.loads("""@@VOCAB_UK@@"""))
_BIGRAMS = json.loads("""@@BIGRAMS@@""")
_FIRSTS = json.loads("""@@FIRSTS@@""")

_EN_COMMON = set(
    "the be to of and a in that have i it for not on with he as you do at this "
    "but his by from they we say her she or an will my one all would there "
    "their what so up out if about who get which go me when make can like time "
    "no just him know take people into year your good some could them see other "
    "than then now look only come its over think also back after use two how "
    "our work first well way even new want because any these give day most us "
    "is are was were been has had did am ok yes no hi hello thanks bro lol im "
    "dont cant lets gonna wanna u ur".split()
)

_CYR_CORE = re.compile("[\\u0430-\\u044f\\u0451\\u0456\\u0457\\u0454\\u0491\']+")

THRESHOLD = 1.2


def _bg_logp(word: str) -> float:
    """Average per-character bigram log-probability under the owner corpus."""
    ww = "^" + word + "$"
    total = 0.0
    n = 0
    for a, b in zip(ww, ww[1:]):
        total += math.log10((_BIGRAMS.get(a + b, 0) + 1) / (_FIRSTS.get(a, 0) + 40))
        n += 1
    return total / max(n, 1)


def _plaus_en(text: str) -> float:
    words = re.findall(r"[a-z\']+", text.lower())
    if not words:
        return 0.0
    letters = [c for w in words for c in w]
    vr = sum(c in "aeiouy" for c in letters) / max(len(letters), 1)
    hit = sum(w.strip("\'") in _EN_COMMON for w in words) / len(words)
    return hit * 2 + (0.5 if 0.25 <= vr <= 0.6 else 0.0)


def _score_word(w: str, vocab: set) -> tuple[float, bool]:
    cores = _CYR_CORE.findall(w.lower())
    if not cores:
        return 0.0, False
    core = max(cores, key=len)
    if core in vocab:
        return 2.0 + min(len(core), 8) * 0.15, len(core) >= 2
    return 2.0 + _bg_logp(core), False


def _convert_word(tok: str, mapping: dict, vocab: set) -> tuple[str, float, bool]:
    """Convert one token; ambiguous punctuation chars are tried both ways
    (converted to a letter vs kept as punctuation) and the best-scoring
    variant wins. E.g. "gjghj,e." -> "poproby" + ","->"b" and "."->"yu"."""
    idxs = [i for i, c in enumerate(tok) if c in _AMBIG and c in mapping][:3]
    base = [mapping.get(c, c) if c not in _AMBIG else c for c in tok]
    best = None
    for mask in range(1 << len(idxs)):
        chars = list(base)
        internal_kept = 0
        for k, i in enumerate(idxs):
            if mask >> k & 1:
                chars[i] = mapping[tok[i]]
            elif 0 < i < len(tok) - 1:
                internal_kept += 1
        cand = "".join(chars)
        score, hit = _score_word(cand, vocab)
        score -= 0.6 * internal_kept
        if best is None or score > best[1]:
            best = (cand, score, hit)
    return best


def _convert_message(text: str, mapping: dict, vocab: set) -> tuple[str, float, int]:
    out, scores, hits = [], [], 0
    for tok in text.split(" "):
        if not re.search("[A-Za-z]", tok):
            out.append(tok)
            continue
        cand, score, hit = _convert_word(tok, mapping, vocab)
        out.append(cand)
        scores.append(score)
        hits += hit
    if not scores:
        return text, -9.0, 0
    return " ".join(out), sum(scores) / len(scores), hits


def fix_layout(text: str) -> str:
    """Return the layout-corrected text, or the input unchanged if the text
    does not confidently look like wrong-layout Cyrillic."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 6:
        return text
    # Nearly all letters must be plain ASCII (umlauts, Cyrillic etc. bail out).
    if sum(c.isascii() for c in letters) / len(letters) < 0.9:
        return text
    # Code, IDs, hex hashes: digit-heavy or symbol-bearing text is never a
    # wrong-layout message.
    alnum = [c for c in text if c.isalnum()]
    if alnum and sum(c.isdigit() for c in alnum) / len(alnum) > 0.25:
        return text
    if any(c in "{}=|<>#$%^&*_~+" for c in text):
        return text
    # Real English (two or more common words) is left alone.
    en_words = re.findall(r"[a-z\']+", text.lower())
    if sum(w.strip("\'") in _EN_COMMON for w in en_words) >= 2 and _plaus_en(text) >= 0.8:
        return text
    ru, ru_s, ru_h = _convert_message(text, EN2RU, _VOCAB_RU)
    uk, uk_s, uk_h = _convert_message(text, EN2UK, _VOCAB_UK)
    conv, score, hits = (uk, uk_s, uk_h) if uk_s > ru_s else (ru, ru_s, ru_h)
    if score >= THRESHOLD and hits >= 1:
        return conv
    return text
'''

out = TEMPLATE
out = out.replace("@@RU_ROW@@", "".join("\\u%04x" % ord(c) for c in "\u0439\u0446\u0443\u043a\u0435\u043d\u0433\u0448\u0449\u0437\u0445\u044a\u0444\u044b\u0432\u0430\u043f\u0440\u043e\u043b\u0434\u0436\u044d\u044f\u0447\u0441\u043c\u0438\u0442\u044c\u0431\u044e\u0451"))
out = out.replace("@@VOCAB_RU@@", json.dumps(vocab_ru, ensure_ascii=True))
out = out.replace("@@VOCAB_UK@@", json.dumps(vocab_uk, ensure_ascii=True))
out = out.replace("@@BIGRAMS@@", json.dumps(dict(bigrams), ensure_ascii=True))
out = out.replace("@@FIRSTS@@", json.dumps(dict(firsts), ensure_ascii=True))

with open(args.out, "w", encoding="utf-8") as f:
    f.write(out)
print("wrote " + args.out + ",", len(out), "bytes, vocab ru/uk:", len(vocab_ru), len(vocab_uk))
