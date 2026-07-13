"""Merge a trained LoRA adapter into its base model (fp16) for GGUF export.

    python merge_lora.py --model Qwen/Qwen2.5-1.5B-Instruct --lora lora-1.5b --out merged-1.5b
"""

import argparse

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--lora", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    base = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.float16, device_map="cpu"
    )
    merged = PeftModel.from_pretrained(base, args.lora).merge_and_unload()
    merged.save_pretrained(args.out)
    AutoTokenizer.from_pretrained(args.model).save_pretrained(args.out)
    print("merged model saved to", args.out)


if __name__ == "__main__":
    main()
