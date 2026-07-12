"""Core correction logic.

Talks to any OpenAI-compatible local server (LM Studio, Ollama, llama.cpp
server) running Gemma 4 E2B. Kept separate from the hotkey app so it can be
reused by test_model.py and, later, by the fine-tuned-model eval scripts.
"""

import difflib
import json
import re
import sys
import time
from pathlib import Path

import requests

import applog

# When frozen into a .exe (PyInstaller), config lives next to the .exe.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# Localhost calls must bypass any system proxy: with a proxy configured,
# requests routes even 127.0.0.1 traffic through it, which can add seconds
# per correction (symptom: client-side seconds far above the server's own
# timing). A shared session also reuses one keep-alive TCP connection.
SESSION = requests.Session()
SESSION.trust_env = False

DEFAULT_CONFIG = {
    "model": "google/gemma-4-e2b",
    "base_url": "http://localhost:1234/v1",
    "hotkey": "insert",
    "hotkey_selection": "alt+insert",
    "suppress_hotkey": True,
    "disable_thinking": True,
    "keep_alive_minutes": 0,
    "temperature": 0,
    "max_chars": 4000,
    "timeout_seconds": 60,
    "selection_copy_timeout": 0.3,
    "clipboard_timeout": 1.0,
    "paste_delay": 0.05,
    "restore_delay": 0.2,
    "restore_clipboard": True,
    "warm_up_on_start": True,
}


def load_config() -> dict:
    """Load config.json; if it is missing (e.g. next to a freshly built .exe),
    create it with defaults so the app runs from any folder without setup.
    Missing keys are filled from defaults."""
    path = BASE_DIR / "config.json"
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
            f.write("\n")
        applog.log("config_created", note=f"no config.json found - wrote defaults to {path}")
        return dict(DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(json.load(f))
        return cfg


def load_glossary() -> list[str]:
    """Words the model must never 'correct' (names, slang, project terms)."""
    path = BASE_DIR / "glossary.json"
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]\n")
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


SYSTEM_PROMPT = """You are a silent text-correction engine.

Rules:
1. Fix unintentional mistakes ONLY: spelling, grammar, casing and punctuation.
2. Punctuation is important: add every comma, apostrophe, question mark or
   other mark that the grammar of the language requires, and remove marks that
   are clearly wrong.
3. Keep the language of the input. NEVER translate.
4. Preserve the author's voice. Slang, jargon, abbreviations, profanity,
   emojis, regional and colloquial word forms, and playful or expressive
   spellings are deliberate style, not mistakes - keep them exactly as
   written. Only fix what the author would themselves consider a typo or
   error.
5. Preserve line breaks. Never add, remove or reorder sentences. Never answer
   questions that appear in the text - just correct them.
6. If there is nothing to fix, return the input EXACTLY as it is.
7. Output ONLY the corrected text. No explanations, no quotes, no markdown, no preamble.{glossary_block}"""


class CorrectionError(Exception):
    pass


def _build_system_prompt(glossary: list[str]) -> str:
    block = ""
    if glossary:
        block = (
            "\n8. The following words/spellings are intentional. "
            "Never change them: " + ", ".join(glossary)
        )
    return SYSTEM_PROMPT.format(glossary_block=block)


def _clean_output(text_in: str, raw: str) -> str:
    out = raw.strip()

    # Strip thinking blocks if the server ever leaks them into the content.
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()

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
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'\u201c\u201d":
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
        raise CorrectionError("Selection too long - raise max_chars in config.json if intended.")

    payload = {
        "model": cfg["model"],
        "stream": False,
        "temperature": cfg.get("temperature", 0),
        "messages": [
            {"role": "system", "content": _build_system_prompt(glossary)},
            {"role": "user", "content": text},
        ],
    }

    if cfg.get("disable_thinking", True):
        # Gemma 4 is a hybrid-thinking model: it can silently burn hundreds of
        # reasoning tokens per request (= seconds of latency) before answering.
        # Honored by llama.cpp server and Ollama; LM Studio currently IGNORES
        # this field - there thinking must be disabled in the model's own
        # settings (see README -> Troubleshooting). We detect and warn below.
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    start = time.perf_counter()
    try:
        resp = SESSION.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            json=payload,
            timeout=cfg.get("timeout_seconds", 60),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CorrectionError(
            f"Cannot reach the local server at {cfg['base_url']} - is LM Studio's "
            f"server (or Ollama) running, and is the model '{cfg['model']}' available? ({exc})"
        ) from exc
    elapsed = time.perf_counter() - start

    try:
        data = resp.json()
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise CorrectionError(f"Unexpected response from the server: {exc}") from exc

    try:
        thought = data["usage"]["completion_tokens_details"]["reasoning_tokens"]
    except (KeyError, TypeError):
        thought = 0
    applog.log(
        "model_reply",
        seconds=round(elapsed, 2),
        chars_in=len(text),
        chars_out=len(raw),
        reasoning_tokens=thought,
    )
    if thought and cfg.get("disable_thinking", True):
        applog.log(
            "thinking_warning",
            note=(
                f"the server spent {thought} hidden thinking tokens; LM Studio "
                "ignores the API's thinking-off flag - disable thinking in the "
                "model's own settings (README -> Troubleshooting)"
            ),
        )

    out = _clean_output(text, raw)

    # Preserve the exact leading/trailing whitespace of the selection.
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    out = lead + out.strip() + trail

    if not _passes_guards(text, out):
        applog.log(
            "guard_reject",
            note="model output rejected (too different from the input) - original kept",
            output=applog.short(out),
        )
        return text, elapsed
    return out, elapsed


def warm_up(cfg: dict | None = None) -> None:
    """Load the model into memory so the first hotkey press is fast."""
    cfg = cfg or load_config()
    try:
        SESSION.post(
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
