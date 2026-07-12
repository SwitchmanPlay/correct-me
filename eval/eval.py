"""Run the correct-me eval set against the local model and score it.

Usage (from the correct-me/eval folder, with LM Studio running):
    python eval.py                    # full run (~10-15 min for 400 cases)
    python eval.py --limit 100        # quick run
    python eval.py --buckets typos,punct
    python eval.py --langs ru,uk

Uses the SAME prompt, guards and config as the hotkey app (imports
corrector.py from the parent folder), so scores reflect real app behavior.

Outputs:
    eval_results.json   summary metrics per bucket/language
    failures.jsonl      every failed case: input / expected / got

Metrics:
- fix rate          cases needing a change where output == expected (loose)
- changed rate      cases needing a change where the model changed anything
- false change      must-not-change cases where the model changed something
                    (style damage - the most important number to keep LOW)
- latency           mean / p50 / p95 model seconds
"""

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import corrector  # noqa: E402
from corrector import CorrectionError  # noqa: E402

_QUOTES = dict.fromkeys("\u00ab\u00bb\u201c\u201d\u201e", '"')
_QUOTES["\u2019"] = "'"


def normalize(s: str) -> str:
    """Loose comparison: ignore whitespace runs, quote style, dash style and
    one trailing period - those are not errors worth failing a case over."""
    s = "".join(_QUOTES.get(ch, ch) for ch in s)
    s = s.replace("\u2014", "-").replace("\u2013", "-").replace("\u2026", "...")
    s = " ".join(s.split()).strip()
    if s.endswith(".") and not s.endswith("..."):
        s = s[:-1]
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description="Score correct-me on the eval set")
    parser.add_argument("--evalset", default=str(HERE / "evalset.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="only run the first N cases")
    parser.add_argument("--buckets", default="", help="comma-separated bucket filter")
    parser.add_argument("--langs", default="", help="comma-separated language filter")
    parser.add_argument("--tag", default="baseline", help="label stored in the results file")
    args = parser.parse_args()

    cases = []
    with open(args.evalset, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
    if args.buckets:
        keep = set(args.buckets.split(","))
        cases = [c for c in cases if c["bucket"] in keep]
    if args.langs:
        keep = set(args.langs.split(","))
        cases = [c for c in cases if c["lang"] in keep]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("no cases selected")

    cfg = corrector.load_config()
    glossary = corrector.load_glossary()
    print(f"{len(cases)} cases | model {cfg['model']} | {cfg['base_url']}")
    corrector.warm_up(cfg)

    rows = []
    t0 = time.monotonic()
    for i, c in enumerate(cases, 1):
        try:
            got, seconds = corrector.correct(c["input"], cfg, glossary)
            error = ""
        except CorrectionError as exc:
            got, seconds, error = "", 0.0, str(exc)
        ok_exact = normalize(got) == normalize(c["expected"]) and not error
        changed = normalize(got) != normalize(c["input"]) if not error else False
        rows.append({**c, "got": got, "seconds": round(seconds, 2), "error": error,
                     "ok": ok_exact, "changed": changed})
        if i % 25 == 0 or i == len(cases):
            done_pct = 100 * i // len(cases)
            print(f"  {i}/{len(cases)} ({done_pct}%) elapsed {time.monotonic() - t0:.0f}s")

    def bucket_stats(items: list[dict]) -> dict:
        need_change = [r for r in items if not r["must_not_change"]]
        keep_same = [r for r in items if r["must_not_change"]]
        lat = sorted(r["seconds"] for r in items if not r["error"])
        stats = {
            "cases": len(items),
            "errors": sum(1 for r in items if r["error"]),
        }
        if need_change:
            stats["fix_rate"] = round(sum(r["ok"] for r in need_change) / len(need_change), 3)
            stats["changed_rate"] = round(sum(r["changed"] for r in need_change) / len(need_change), 3)
        if keep_same:
            stats["false_change_rate"] = round(
                sum(r["changed"] for r in keep_same) / len(keep_same), 3
            )
        if lat:
            stats["latency_mean"] = round(statistics.mean(lat), 2)
            stats["latency_p50"] = round(lat[len(lat) // 2], 2)
            stats["latency_p95"] = round(lat[int(len(lat) * 0.95) - 1], 2)
        return stats

    summary = {
        "tag": args.tag,
        "model": cfg["model"],
        "total": bucket_stats(rows),
        "by_bucket": {},
        "by_lang": {},
    }
    for bucket in sorted({r["bucket"] for r in rows}):
        summary["by_bucket"][bucket] = bucket_stats([r for r in rows if r["bucket"] == bucket])
    for lang in sorted({r["lang"] for r in rows}):
        summary["by_lang"][lang] = bucket_stats([r for r in rows if r["lang"] == lang])

    with open(HERE / "eval_results.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=True, indent=2)
    with open(HERE / "failures.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            if not r["ok"]:
                f.write(json.dumps(
                    {k: r[k] for k in ("id", "lang", "bucket", "must_not_change",
                                        "input", "expected", "got", "seconds", "error")},
                    ensure_ascii=True) + "\n")

    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    n_fail = sum(1 for r in rows if not r["ok"])
    print(f"\nwrote eval_results.json and failures.jsonl ({n_fail} failures)")


if __name__ == "__main__":
    main()
