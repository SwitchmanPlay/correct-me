"""QLoRA fine-tune of a small instruct model on your correction pairs.

Run on the RTX 4060 Ti. Examples:
    python finetune.py --model Qwen/Qwen2.5-0.5B-Instruct --out lora-0.5b
    python finetune.py --model Qwen/Qwen2.5-1.5B-Instruct --out lora-1.5b

Rough wall-clock, 1 epoch, ~60k pairs, 4060 Ti: 0.5B ~1 h, 1.5B ~2.5-4 h.
If you hit CUDA out-of-memory: --batch 4 --accum 4 (same effective batch).
"""

import argparse
from pathlib import Path

import torch
from datasets import load_dataset
from peft import LoraConfig
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--train", default=str(HERE / "train.jsonl"))
    ap.add_argument("--val", default=str(HERE / "val.jsonl"))
    ap.add_argument("--prompt", default=str(HERE / "prompt.txt"))
    ap.add_argument("--out", default=str(HERE / "lora-out"))
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--ctx", type=int, default=384)
    args = ap.parse_args()

    system_prompt = Path(args.prompt).read_text(encoding="utf-8")

    def to_chat(row):
        return {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": row["input"]},
                {"role": "assistant", "content": row["output"]},
            ]
        }

    data = load_dataset("json", data_files={"train": args.train, "val": args.val})
    data = data.map(to_chat, remove_columns=data["train"].column_names)

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model, quantization_config=bnb, device_map="auto"
    )
    tok = AutoTokenizer.from_pretrained(args.model)

    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )

    cfg = SFTConfig(
        output_dir=args.out,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=25,
        eval_strategy="steps",
        eval_steps=500,
        save_steps=500,
        save_total_limit=2,
        bf16=True,
        max_seq_length=args.ctx,
        packing=False,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        report_to="none",
    )
    trainer = SFTTrainer(
        model=model,
        args=cfg,
        train_dataset=data["train"],
        eval_dataset=data["val"],
        peft_config=lora,
        processing_class=tok,
    )
    trainer.train()
    trainer.save_model(args.out)
    print("LoRA adapter saved to", args.out)


if __name__ == "__main__":
    main()
