"""Build Week-3 fine-tuning data for correct-me from your own Telegram messages.

Generates (broken, clean) instruction pairs with the same corruption logic as
the eval set, plus no-op pairs that teach the model to leave correct text
alone. Eval-set messages are excluded so the benchmark stays honest.

Usage (either input works):
    python build_dataset.py --export path/to/result.json
    python build_dataset.py --corpus path/to/corpus.jsonl

Outputs (into this folder): train.jsonl, val.jsonl, prompt.txt
Pair format per line: {"task", "input", "output"}
finetune.py turns pairs into chat examples using prompt.txt as the system
prompt, so training matches what the app sends at runtime.

Design notes:
- layout errors are NOT trained: layout.py fixes them deterministically
  before the model ever sees the text.
- no-op pool only contains messages that already carry punctuation or are
  too short/expressive to need any. Raw comma-less sentences are kept OUT of
  the no-op pool on purpose: training on them as "already correct" would
  teach the model to stop adding the commas the author skips while typing.
"""

import argparse
import collections
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "eval"))
sys.path.insert(0, str(HERE.parent))

from build_evalset import (  # noqa: E402
    SLANG_RE,
    STRETCH_RE,
    corrupt_punct,
    corrupt_typos,
    extract_own_messages,
)
import corrector  # noqa: E402

MIX = {"punct": 13000, "typos": 24000, "combo": 6000, "noop": 20000}
VAL_SIZE = 2000
MAX_LEN = 200
PUNCT_RE = re.compile(r"[,.!?;:]")


def load_corpus(args) -> list[str]:
    if args.corpus:
        texts = [json.loads(l)["text"] for l in open(args.corpus, encoding="utf-8")]
    else:
        texts = [m["text"] for m in extract_own_messages(Path(args.export))]
    seen, out = set(), []
    for t in texts:
        t = " ".join(t.split())
        if 8 <= len(t) <= MAX_LEN and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def eval_texts(evalset_path: Path) -> set[str]:
    banned = set()
    if evalset_path.exists():
        for line in open(evalset_path, encoding="utf-8"):
            c = json.loads(line)
            banned.add(c["input"])
            banned.add(c["expected"])
    return banned


def build(texts: list[str], banned: set[str], rng: random.Random) -> list[dict]:
    texts = [t for t in texts if t not in banned]
    rng.shuffle(texts)

    def clean(t: str) -> bool:
        return not STRETCH_RE.search(t) and sum(c.isdigit() for c in t) < 3

    pools: dict[str, list[str]] = {k: [] for k in MIX}
    for t in texts:
        if "," in t and len(t) >= 25 and clean(t) and len(pools["punct"]) < MIX["punct"]:
            pools["punct"].append(t)
        elif PUNCT_RE.search(t) and len(t) >= 25 and clean(t) and len(pools["combo"]) < MIX["combo"]:
            pools["combo"].append(t)
        elif (
            PUNCT_RE.search(t)
            or len(t.split()) <= 4
            or STRETCH_RE.search(t)
            or SLANG_RE.search(t)
        ) and len(pools["noop"]) < MIX["noop"]:
            pools["noop"].append(t)
        elif 15 <= len(t) <= 160 and clean(t) and len(pools["typos"]) < MIX["typos"]:
            pools["typos"].append(t)

    pairs: list[dict] = []
    for t in pools["punct"]:
        bad = corrupt_punct(t, rng)
        if bad != t:
            pairs.append({"task": "punct", "input": bad, "output": t})
    for t in pools["typos"]:
        bad = corrupt_typos(t, rng)
        if bad != t:
            pairs.append({"task": "typos", "input": bad, "output": t})
    for t in pools["combo"]:
        bad = corrupt_typos(corrupt_punct(t, rng), rng)
        if bad != t:
            pairs.append({"task": "combo", "input": bad, "output": t})
    for t in pools["noop"]:
        pairs.append({"task": "noop", "input": t, "output": t})

    rng.shuffle(pairs)
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser(description="Build train/val pairs from your messages")
    ap.add_argument("--export", default=None, help="Telegram result.json")
    ap.add_argument("--corpus", default=None, help="pre-extracted corpus.jsonl")
    ap.add_argument("--evalset", default=str(HERE.parent / "eval" / "evalset.jsonl"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    if not args.export and not args.corpus:
        raise SystemExit("need --export result.json or --corpus corpus.jsonl")

    rng = random.Random(args.seed)
    texts = load_corpus(args)
    banned = eval_texts(Path(args.evalset))
    print(f"corpus: {len(texts)} usable messages; {len(banned)} eval texts excluded")
    pairs = build(texts, banned, rng)

    val, train = pairs[:VAL_SIZE], pairs[VAL_SIZE:]
    for name, rows in (("train.jsonl", train), ("val.jsonl", val)):
        with open(HERE / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(HERE / "prompt.txt", "w", encoding="utf-8") as f:
        f.write(corrector._build_system_prompt([]))
    stats = collections.Counter(p["task"] for p in pairs)
    print(f"train {len(train)} / val {len(val)}; mix: {dict(stats)}")


if __name__ == "__main__":
    main()
