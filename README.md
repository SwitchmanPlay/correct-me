# correct-me (MVP - stock Gemma 4 E2B)

Press `Home` (configurable) in any text field and the text is replaced with a
grammar/spelling/punctuation-corrected version. **No selection needed**: with
nothing selected, the whole input field is corrected - type your chat
message, press Home, hit send. Runs 100% locally - no cloud, no telemetry.

Since v5 it lives in the **system tray**: a status icon plus a settings window
(model picker, hotkey, toggles) - no console window needed.

This is Phase 1 (MVP) of the build plan: stock model, no fine-tuning yet.
The app talks to any **OpenAI-compatible local server**, so both **LM Studio**
and **Ollama** work - pick one below.

## Option A - LM Studio (default config)

1. In LM Studio, download **`google/gemma-4-e2b`** (the standard ~4.2 GB
   build - it is already a 4-bit quant). Want it smaller? Get
   **`google/gemma-4-e2b-qat`** instead: Google's official QAT build,
   trained specifically for 4-bit, smaller and closest to full quality.
2. Go to the **Developer** tab -> start the server (default `http://localhost:1234`).
3. Check the model identifier shown in the server/model list (e.g. `google/gemma-4-e2b`)
   and make sure `model` in `config.json` matches it exactly.
4. RAM vs speed trade-off: **just-in-time model loading** (server settings)
   saves RAM but adds seconds to the first correction after idle, because the
   model has to reload from disk. For fast corrections, keep the model loaded
   or raise the JIT idle TTL. See "Speed" below.

## Option B - Ollama

1. Install [Ollama](https://ollama.com/download), then: `ollama pull gemma4:e2b`
2. In `config.json` set:
   ```json
   "model": "gemma4:e2b",
   "base_url": "http://localhost:11434/v1"
   ```
3. Ollama keeps the model in RAM ~5 min after last use, then unloads it automatically.

**Which one?** Functionally identical for this app (same API, same GGUF-class
models, same speed). LM Studio = GUI app that must be open; Ollama = headless
background service, slightly nicer for an always-on hotkey tool. Since you
already use LM Studio, start with it - switch to Ollama later only if keeping
the LM Studio window open annoys you. Not worth switching for any other reason.

## Run

```
pip install -r requirements.txt
python test_model.py    # sanity-check the model + prompt first (no clipboard)
python main.py           # the actual hotkey app (tray icon)
python main.py --console # old v4-style console mode
```

Type a message anywhere (browser, Telegram, VS Code...) and just press
**Home** - no selecting needed; the app grabs the whole input field (Ctrl+A).
Select text first only when you want to correct part of it.
One high beep = done. Two low beeps = no text / error.

The **tray icon** shows status: green = ready, orange = correcting, red =
error (hover it for details, like timings). Right-click it for **Settings**
(server URL, model picker with a live list from the server, hotkey, toggles),
**Pause** and **Quit**; double-click opens Settings directly. Saved settings
are written to `config.json` and applied instantly - no restart.

## How it works

```
hotkey -> Ctrl+C (Ctrl+A first if nothing selected) -> prompt + glossary
       -> local server (Gemma 4 E2B, temp 0, thinking disabled)
       -> output cleaning -> guard rails -> simulated Ctrl+V -> clipboard restored
```

- **Guard rails** (`corrector.py`): if the model's output is too different from
  the input (length ratio or `difflib` similarity), the original text is kept.
  This neutralizes the classic small-model failures: answering questions found
  in the text, adding sentences, or rewriting instead of correcting.
- **Glossary** (`glossary.json`): words the model must never "correct" - slang,
  names, project terms. Edit it freely; it is injected into the system prompt.
- **RAM**: Gemma 4 E2B (Q4) is ~4 GB on disk (5.1B total params, 2.3B effective),
  loaded only while in use (see the
  auto-unload notes above).

## Config (`config.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `google/gemma-4-e2b` | Model id (LM Studio) or tag (Ollama, e.g. `gemma4:e2b`) |
| `base_url` | `http://localhost:1234/v1` | LM Studio; use `http://localhost:11434/v1` for Ollama |
| `hotkey` | `home` | Global hotkey - see "Changing the hotkey" below |
| `suppress_hotkey` | `true` | Swallow the key so it never reaches the app (required for Home/End/Page Up) |
| `no_selection` | `select_all` | With no selection, grab the whole field via Ctrl+A (`off` to disable) |
| `disable_thinking` | `true` | Suppress Gemma 4's hidden reasoning tokens (big latency win) |
| `max_chars` | `4000` | Refuse selections longer than this |
| `selection_probe_timeout` | `0.15` | How long to wait for a selection copy before falling back to Ctrl+A |
| `clipboard_timeout` | `1.0` | Max seconds to wait for the Ctrl+A copy to land (polled every 20 ms) |
| `paste_delay` | `0.05` | Pause between setting the clipboard and sending Ctrl+V |
| `restore_delay` | `0.2` | Pause after Ctrl+V before the old clipboard is restored (in background) |
| `restore_clipboard` | `true` | Put your old clipboard back after pasting |

## Changing the hotkey

Three ways:

- Easiest: right-click the tray icon -> **Settings**.
- Permanent: edit `"hotkey"` in `config.json`.
- One-off: `python main.py --hotkey "page up"`

Anything the Python [`keyboard`](https://github.com/boppreh/keyboard) library
understands works. Good low-conflict choices:

- Single keys: `home` (default), `insert`, `page up`, `pause`, `scroll lock`, `f8`, `f9`
- Combos: `ctrl+alt+g`, `ctrl+shift+space`

Note: since v3 the hotkey is **suppressed** - the app swallows the key, so it
no longer performs its normal action anywhere (Home won't move the cursor
while correct-me is running). This is required: keys like Home/End/Page Up
move the caret and collapse the text selection before the app can copy it -
exactly why v2 always reported "nothing selected". If you use a combo like
`ctrl+alt+g` and want the key back, set `"suppress_hotkey": false`.

## Speed

What the 4-5 s per correction was made of, and what to do:

- **Hidden thinking tokens**: Gemma 4 is a hybrid-thinking model and can
  silently "reason" for hundreds of tokens before answering (646 in one
  logged request!). The app requests thinking off (`disable_thinking`), but
  **LM Studio currently ignores that API field**, and its in-app toggle
  resets whenever the model reloads. Permanent fix: see Troubleshooting ->
  "Thinking turns itself back on". The app now prints a warning whenever
  the server thought anyway.
- **Model reload (biggest chunk)**: with JIT loading / auto-unload, the first
  correction after idle reloads the model from disk - several seconds. Keep
  the model loaded (or raise the idle TTL) and this disappears.
- **CPU instead of GPU**: check LM Studio shows full GPU offload for the
  model. CPU-only is a 5-10x slowdown - on your 4060 Ti there is no reason
  for it.
- **Fixed overhead (v5)**: pressing the hotkey with *nothing selected* used
  to burn the full 1.0 s `clipboard_timeout` before falling back to Ctrl+A -
  a 1-second tax on the main use case. v5 probes the selection for only
  `selection_probe_timeout` (0.15 s), trims the paste sleeps, and restores
  the clipboard in the background. Total is now roughly model time + ~0.4 s.
- **Generation itself**: the model re-types the whole selection, so latency
  scales with text length. Warm model + GPU offload: roughly 0.5-1 s for a
  short sentence, 2-4 s for a long paragraph.
- **Speculative decoding (best model-time win)**: in LM Studio, open the
  model's load settings and attach the Gemma 4 E2B **MTP drafter** as the
  draft model. Correction output is highly predictable (mostly the input
  re-typed), which is the ideal case for it - typically 1.5-2.5x faster
  generation.

Honest answer on "half a second": realistic for short sentences with a warm
model on GPU; not realistic for whole paragraphs - no consumer-hardware local
model re-types 200+ tokens in 0.5 s. The Phase-2 trick if this matters:
split long text into sentences and correct them in parallel requests.

About caching the instructions: this already happens automatically. llama.cpp
(the engine inside both LM Studio and Ollama) reuses the KV cache for a shared
prompt prefix, and our system prompt is byte-identical on every request - so
it is processed once per model load, not on every correction. The instructions
can grow much bigger without adding per-request latency.

## Model size

The default `google/gemma-4-e2b` LM Studio download (~4.2 GB) is already a
4-bit quant - that is as small as this model officially gets in a normal GGUF.
The options, all the *same* model:

- **LM Studio (default)**: `google/gemma-4-e2b`, ~4.2 GB. Works as-is.
- **Smaller official option**: `google/gemma-4-e2b-qat` - Google's
  Quantization-Aware Training build. QAT is *trained* for 4-bit, so it keeps
  quality closest to the full bf16 model at the smallest size. If you want to
  save disk/RAM, this is the one to pick, not a random small GGUF.
- **Ollama**: `ollama run gemma4:e2b-it-q4_K_M`.
- Avoid third-party "uncensored" or otherwise fine-tuned community GGUFs
  (e.g. 3.4 GB Q4_K_M re-uploads): they are unofficial fine-tunes and often
  follow instructions worse, which matters a lot for a corrector.
- "MTP" builds are **drafter models** for multi-token prediction
  (speculative decoding) - they assist a main model, they are not meant to be
  loaded as your model.

Don't go below Q4 (Q2/Q3) - correction quality visibly degrades. And there is
no better *smaller* model for multilingual correction right now: Gemma 4 E2B
is already the floor of its class, so shrink the quant, not the model.

## Troubleshooting

- **"nothing selected" everywhere (the v2 bug)**: fixed in v3. Home/End/Page
  Up move the caret and destroy the selection before Ctrl+C fires; v3
  suppresses the hotkey so the selection survives.
- **Still "nothing selected" in one specific app**: that app probably runs
  elevated (as administrator); Windows blocks keystroke injection into
  elevated windows. Run the script as administrator too. Otherwise admin is
  NOT needed.
- **Pressed Home outside a text field**: Ctrl+A may select a whole page;
  anything over `max_chars` is refused, so nothing happens - by design.
- **Pastes nothing or stale text**: a clipboard manager may interfere - try
  disabling it, or raise `clipboard_timeout`.
- **Two beeps + `[error]` in console**: server not running or wrong `model`
  id; the console message says which.
- **Thinking turns itself back on (slow again after a restart)**: LM
  Studio's "Enable Thinking" toggle is per-load and resets when the model
  reloads, and the API cannot override it. Permanent fix: **My Models ->
  the Gemma model -> Inference -> Prompt Template (Jinja) -> add
  `{%- set enable_thinking = false %}` as the FIRST line -> reload**. Then
  it stays off no matter how or when the model gets loaded.
- **Pressing the hotkey in an empty field pasted old clipboard text (v5
  bug)**: fixed in v5.1. Clearing the clipboard with an empty string is
  unreliable on Windows, so v5 could mistake your previous clipboard for
  the "selection" and paste a corrected copy of it. v5.1 uses a sentinel
  marker instead and quietly does nothing when no text is found.
- **The .exe closed instantly (v5 bug)**: fixed in v5.1. With `--noconsole`
  there is no stdout, and v5's status printing crashed on start. Rebuild
  the .exe from v5.1.

## Building a .exe

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name correct-me main.py
```

`dist/correct-me.exe` is a single file you can drop into autostart. Keep
`config.json` and `glossary.json` next to the .exe (the app looks for them
there when frozen). `--noconsole` is fine since v5: status and errors are
shown on the tray icon (hover it), and problems still beep twice.

## Known limitations (MVP)

- Windows-first. The hotkey/paste flow works on Linux/macOS in principle, but
  `keyboard` needs root on Linux, and macOS needs accessibility permissions
  (and Cmd instead of Ctrl) - not wired up yet.
- Apps that block programmatic paste (some terminals, password fields) won't work.
- Glossary is static; "learn words the user keeps re-sending" is Phase 5.

## Roadmap

See the build plan: MVP -> datasets + eval set -> LoRA fine-tunes
(0.5B / 1.5B / 2B) -> quantize -> benchmark table -> polish.
