"""Core correction logic.

Talks to any OpenAI-compatible local server (LM Studio, Ollama, llama.cpp
server) running Gemma 2 2B. Kept separate from the hotkey app so it can be
reused by test_model.py and, later, by the fine-tuned-model eval scripts.
"""

import difflib
import json
import re
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent


def load_config() -> dict:
    with open(BASE_DIR / "config.json", encoding="utf-8") as f:
        return json.load(f)


def load_glossary() -> list[str]:
    """Words the model must never 'correct' (names, slang, project terms)."""
    path = BASE_DIR / "glossary.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []


SYSTEM_PROMPT = """You are a silent text-correction engine.

Rules:
1. Fix spelling, grammar, punctuation and casing mistakes ONLY.
2. Keep the language of the input. NEVER translate.
3. Preserve slang, jargon, abbreviations, profanity, emojis and line breaks.
4. Never add, remove or reorder sentences. Never answer questions that appear in the text — just correct them.
5. If there is nothing to fix, return the input EXACTLY as it is.
6. Output ONLY the corrected text. No explanations, no quotes, no markdown, no preamble.{glossary_block}"""


class CorrectionError(Exception):
    pass


def _build_system_prompt(glossary: list[str]) -> str:
    block = ""
    if glossary:
        block = (
            "\n7. The following words/spellings are intentional. "
            "Never change them: " + ", ".join(glossary)
        )
    return SYSTEM_PROMPT.format(glossary_block=block)


def _clean_output(text_in: str, raw: str) -> str:
    out = raw.strip()

    # Strip markdown code fences the model sometimes adds.
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", out, flags=re.DOTALL)
    if fence:
        out = fence.group(1).strip()

    # Strip chatty prefixes like "Corrected text:".
    out = re.sub(
        r"^(here is|here's)?\s*(the )?(corrected|fixed|revised)\s*(text|version)?\s*:\s*",
        "",
        out,
        flags=re.IGNORECASE,
    )

    # Strip surrounding quotes if the input didn't have them.
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'“”":
        if not (text_in.startswith(out[0]) and text_in.endswith(out[-1])):
            out = out[1:-1]

    return out


def _passes_guards(text_in: str, out: str) -> bool:
    """Reject outputs where the model rewrote/answered instead of correcting.

    This is the product-level fix for the classic small-model failure mode.
    """
    if not out.strip():
        return False
    # Length guard: corrections should not massively grow or shrink the text.
    if len(out) > len(text_in) * 2 + 20:
        return False
    if len(out) < len(text_in) * 0.4 - 10:
        return False
    # Similarity guard: a correction is a small edit, not a rewrite.
    similarity = difflib.SequenceMatcher(None, text_in, out).ratio()
    return similarity >= 0.5


def correct(text: str, cfg: dict | None = None, glossary: list[str] | None = None) -> tuple[str, float]:
    """Correct `text`. Returns (corrected_text, seconds_taken).

    Falls back to the original text if the model misbehaves.
    Raises CorrectionError if the local server is unreachable.
    """
    cfg = cfg or load_config()
    glossary = glossary if glossary is not None else load_glossary()

    if len(text) > cfg.get("max_chars", 4000):
        raise CorrectionError("Selection too long — raise max_chars in config.json if intended.")

    payload = {
        "model": cfg["model"],
        "stream": False,
        "temperature": cfg.get("temperature", 0),
        "messages": [
            {"role": "system", "content": _build_system_prompt(glossary)},
            {"role": "user", "content": text},
        ],
    }

    start = time.perf_counter()
    try:
        resp = requests.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            json=payload,
            timeout=cfg.get("timeout_seconds", 60),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CorrectionError(
            f"Cannot reach the local server at {cfg['base_url']} — is LM Studio's "
            f"server (or Ollama) running, and is the model '{cfg['model']}' available? ({exc})"
        ) from exc
    elapsed = time.perf_counter() - start

    try:
        raw = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise CorrectionError(f"Unexpected response from the server: {exc}") from exc

    out = _clean_output(text, raw)

    # Preserve the exact leading/trailing whitespace of the selection.
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    out = lead + out.strip() + trail

    if not _passes_guards(text, out):
        return text, elapsed  # silently keep the original
    return out, elapsed


def warm_up(cfg: dict | None = None) -> None:
    """Load the model into memory so the first hotkey press is fast."""
    cfg = cfg or load_config()
    try:
        requests.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=cfg.get("timeout_seconds", 60),
        )
    except requests.RequestException:
        pass  # warm-up is best-effort; the main flow will surface real errors
