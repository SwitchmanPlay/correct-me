# Week 2 - evaluation kit

Measures how good the corrector actually is, on YOUR real messages, so any
future change (prompt tweak, layout pre-pass, Week-3 fine-tuned model) can be
proven better or worse with numbers instead of feelings.

## Files

| File | What |
| --- | --- |
| `evalset.jsonl` | ~400 test cases built from your Telegram export (5 buckets x 4 languages) |
| `build_evalset.py` | Rebuilds `evalset.jsonl` from `result.json` (seeded, reproducible) |
| `eval.py` | Runs the set against the local model, writes scores + failures |

Buckets:

- `typos` - your real messages with realistic keyboard errors injected
  (adjacent key, dropped/doubled/swapped letter) - must be fixed back.
- `punct` - your real messages with commas stripped - must be restored.
- `layout` - RU/UK messages as if typed in the EN layout (`ghbdtn` style) -
  must come back as the original. Expect ~0% at baseline: the model can't do
  this. SOLVED in v7.2: the app now ships a deterministic layout pre-pass
  (`layout.py`), so this bucket should jump from ~0% to ~85%+.
- `must_not_change` - your slang and stretched letters ("\u043f\u0430\u0442\u0430\u043c\u0443\u0448\u0442\u0430",
  "\u043f\u0440\u0438\u0432\u0435\u0442\u0438\u0438\u0438\u043a") - must return UNCHANGED. `false_change_rate` here is the
  style-damage number; keeping it low matters more than fixing every typo.
- `traps` - well-formed questions - the model must correct, never answer.

## Run the baseline

LM Studio running, model loaded, then from this folder:

```
python eval.py --limit 100   # quick sanity run (~3-4 min)
python eval.py               # full run (~10-15 min)
```

Results land in `eval_results.json` (scores) and `failures.jsonl` (every
miss with input/expected/got). Send both files back for analysis.

## Notes

- `eval.py` imports `corrector.py` from the parent folder: same prompt, same
  guards, same config as the hotkey app. Keep the `eval` folder inside
  `correct-me`.
- Comparison is "loose": whitespace runs, quote style, dash style and one
  trailing period are ignored.
- A case counts as fixed only if the output matches what you actually wrote,
  so the metric is anchored to your style, not generic textbook grammar.
- Rebuild with different size/mix: edit `BUCKET_SIZES` / `LANG_SHARES` in
  `build_evalset.py`, then
  `python build_evalset.py --export path/to/result.json`.

## Reading v7.2+ results

- `false_change_hard_rate` (new) ignores punctuation- and case-only edits.
  Adding a missing comma to a slangy message is arguably a FIX, not damage,
  so the number that must stay near zero is the HARD rate: word swaps,
  de-stretching, e/yo rewrites.
- Compare runs by tag: `python eval.py --tag v7.2` and diff against the
  baseline eval_results.json.
- The style-restore guard in corrector.py now deterministically reverts
  stretched-letter collapses and e/yo swaps, so those failure classes should
  disappear regardless of what the model outputs.
