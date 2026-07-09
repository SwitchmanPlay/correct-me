# Hotkey Proofreader (MVP — stock Gemma 4 E2B)

Select text in **any** app, press `Ctrl+Alt+G`, and the selection is replaced
with a grammar/spelling/punctuation-corrected version. Runs 100% locally —
no cloud, no telemetry.

This is Phase 1 (MVP) of the build plan: stock model, no fine-tuning yet.
The app talks to any **OpenAI-compatible local server**, so both **LM Studio**
and **Ollama** work — pick one below.

## Option A — LM Studio (default config)

1. In LM Studio, download **Gemma 4 E2B Instruct** (search `gemma-2-2b-it`, take a Q4_K_M GGUF).
2. Go to the **Developer** tab → start the server (default `http://localhost:1234`).
3. Check the model identifier shown in the server/model list (e.g. `google/gemma-2-2b-it`)
   and make sure `model` in `config.json` matches it.
4. Tip: enable **just-in-time model loading** in the server settings so the model
   loads on first request and auto-unloads when idle — you don't need to keep it
   loaded manually.

## Option B — Ollama

1. Install [Ollama](https://ollama.com/download), then: `ollama pull gemma2:2b`
2. In `config.json` set:
   ```json
   "model": "gemma2:2b",
   "base_url": "http://localhost:11434/v1"
   ```
3. Ollama keeps the model in RAM ~5 min after last use, then unloads it automatically.

**Which one?** Functionally identical for this app (same API, same GGUF-class
models, same speed). LM Studio = GUI app that must be open; Ollama = headless
background service, slightly nicer for an always-on hotkey tool. Since you
already use LM Studio, start with it — switch to Ollama later only if keeping
the LM Studio window open annoys you. Not worth switching for any other reason.

## Run

```
pip install -r requirements.txt
python test_model.py    # sanity-check the model + prompt first (no clipboard)
python main.py          # the actual hotkey app
```

Select some text anywhere (browser, Telegram, VS Code...), press **Ctrl+Alt+G**.
One high beep = done. Two low beeps = nothing selected / error (see console).

## How it works

```
hotkey -> simulated Ctrl+C -> prompt + glossary -> local server (Gemma 2 2B, temp 0)
       -> output cleaning -> guard rails -> simulated Ctrl+V -> clipboard restored
```

- **Guard rails** (`corrector.py`): if the model's output is too different from
  the input (length ratio or `difflib` similarity), the original text is kept.
  This neutralizes the classic small-model failures: answering questions found
  in the text, adding sentences, or rewriting instead of correcting.
- **Glossary** (`glossary.json`): words the model must never "correct" — slang,
  names, project terms. Edit it freely; it is injected into the system prompt.
- **RAM**: Gemma 2 2B (Q4) is ~1.6 GB, loaded only while in use (see the
  auto-unload notes above).

## Config (`config.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `google/gemma-4-E2b-it` | Model id (LM Studio) or tag (Ollama, e.g. `gemma4:e2b`) |
| `base_url` | `http://localhost:1234/v1` | LM Studio; use `http://localhost:11434/v1` for Ollama |
| `hotkey` | `ctrl+alt+g` | Global hotkey |
| `max_chars` | `4000` | Refuse selections longer than this |
| `clipboard_delay` | `0.25` | Seconds to wait after Ctrl+C (raise if selections come back empty) |
| `restore_clipboard` | `true` | Put your old clipboard back after pasting |

## Known limitations (MVP)

- Windows-first. The hotkey/paste flow works on Linux/macOS in principle, but
  `keyboard` needs root on Linux, and macOS needs accessibility permissions
  (and Cmd instead of Ctrl) — not wired up yet.
- Apps that block programmatic paste (some terminals, password fields) won't work.
- No tray icon yet — it's a console app by design (Handy-style minimalism).
- Glossary is static; "learn words the user keeps re-sending" is Phase 5.

## Roadmap

See the build plan: MVP -> datasets + eval set -> LoRA fine-tunes
(0.5B / 1.5B / 2B) -> quantize -> benchmark table -> polish.
