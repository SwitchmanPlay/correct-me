# Week 3 - fine-tune your own correction model

Goal: replace the 4.2 GB Gemma with a ~0.5-1 GB specialist trained on YOUR
messages, and prove it with the Week-2 eval. Ship rule: the fine-tune replaces
Gemma only if on `eval.py` it beats fix_rate 0.337 AND keeps
false_change_hard_rate at or below 0.155 (the v7.3 numbers).

Everything runs locally on the RTX 4060 Ti. Expect the whole ladder to take
one evening plus GPU hours.

## 0. One-time setup (fresh venv, Python 3.11+)

```
python -m venv venv-train
venv-train\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements-train.txt
```

If bitsandbytes complains on Windows, upgrade it (`pip install -U bitsandbytes`)
- official Windows wheels exist since 0.43. Plan B: run everything in WSL2.

## 1. Data (already included)

`train.jsonl` (54,833 pairs) + `val.jsonl` (2,000) + `prompt.txt` are shipped
pre-generated from your corpus. Regenerate anytime:

```
python build_dataset.py --export path/to/result.json
```

Mix: typos 24k / punct-restore 10k / combo 2.8k / no-op 20k (~36%%).
No-ops teach "do not touch correct text" - the #1 small-model failure.
Eval-set messages are excluded from training (no benchmark contamination).

## 2. Train the ladder (start small)

```
python finetune.py --model Qwen/Qwen2.5-0.5B-Instruct --out lora-0.5b
python finetune.py --model Qwen/Qwen2.5-1.5B-Instruct --out lora-1.5b
```

~1 h for 0.5B, ~2.5-4 h for 1.5B (1 epoch). OOM -> `--batch 4 --accum 4`.

## 3. Merge + convert to GGUF + quantize

```
python merge_lora.py --model Qwen/Qwen2.5-0.5B-Instruct --lora lora-0.5b --out merged-0.5b

git clone https://github.com/ggml-org/llama.cpp
pip install -r llama.cpp/requirements/requirements-convert_hf_to_gguf.txt
python llama.cpp/convert_hf_to_gguf.py merged-0.5b --outfile correct-me-0.5b-f16.gguf --outtype f16
```

Quantize with the `llama-quantize` binary (download a llama.cpp release build):

```
llama-quantize correct-me-0.5b-f16.gguf correct-me-0.5b-Q4_K_M.gguf Q4_K_M
```

Result: ~0.4 GB (0.5B) / ~1.0 GB (1.5B).

## 4. Serve it in LM Studio

Copy the .gguf into the LM Studio models folder as
`models/danya/correct-me-0.5b/correct-me-0.5b-Q4_K_M.gguf`, load it in the
server tab, then point the app at it in `config.json`:
`"model": "danya/correct-me-0.5b"`.

## 5. Judge it (the CV table)

```
cd ../eval
python eval.py --tag ft-0.5b
python eval.py --tag ft-1.5b
```

Compare against the stock-Gemma baseline (fix_rate 0.337,
false_change_hard_rate 0.155, punct 0.19, typos 0.31). Each row of
{base 0.5B, ft-0.5B, base 1.5B, ft-1.5B, Gemma-4-E2B} on the same 403 cases
is exactly the results table the README/writeup needs.

## Troubleshooting

- `eval_strategy` error -> your transformers version drifted; reinstall
  pinned versions from requirements-train.txt.
- bf16 not supported error -> replace `bf16=True` with `fp16=True` in
  finetune.py (4060 Ti supports bf16, so this should not happen).
- Loss ~0 immediately or garbage output -> check prompt.txt exists; the
  chat template comes from the tokenizer automatically.
