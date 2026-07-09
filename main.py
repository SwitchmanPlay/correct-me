"""correct-me - MVP with stock Gemma 4 E2B via LM Studio or Ollama.

Select text anywhere, press the hotkey (default: Insert), and the selection
is replaced with a corrected version. Fully local, no cloud.

Run:  python main.py
"""

import argparse
import sys
import threading
import time

import keyboard
import pyperclip

from corrector import CorrectionError, correct, load_config, load_glossary, warm_up

cfg = load_config()
glossary = load_glossary()
_busy = threading.Lock()

IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    import winsound


def beep(ok: bool = True) -> None:
    """Tiny audio feedback: one high beep = done, two low beeps = problem."""
    if not IS_WINDOWS:
        print("\a", end="", flush=True)
        return
    if ok:
        winsound.Beep(1200, 90)
    else:
        winsound.Beep(400, 120)
        winsound.Beep(400, 120)


def grab_selection() -> tuple[str, str]:
    """Copy the current selection. Returns (selection, previous_clipboard)."""
    previous = ""
    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        pass
    pyperclip.copy("")  # so we can tell whether Ctrl+C actually copied anything
    keyboard.send("ctrl+c")
    time.sleep(cfg.get("clipboard_delay", 0.25))
    return pyperclip.paste(), previous


def replace_selection(corrected: str, previous_clipboard: str) -> None:
    pyperclip.copy(corrected)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    if cfg.get("restore_clipboard", True):
        # Give the target app a moment to read the clipboard before restoring.
        time.sleep(0.4)
        pyperclip.copy(previous_clipboard)


def on_hotkey() -> None:
    if not _busy.acquire(blocking=False):
        return  # a correction is already running
    try:
        text, previous = grab_selection()
        if not text.strip():
            print("[skip] nothing selected")
            pyperclip.copy(previous)
            beep(ok=False)
            return

        print(f"[fixing] {len(text)} chars ...", end=" ", flush=True)
        try:
            corrected, seconds = correct(text, cfg, glossary)
        except CorrectionError as exc:
            print(f"\n[error] {exc}")
            pyperclip.copy(previous)
            beep(ok=False)
            return

        if corrected == text:
            print(f"nothing to fix ({seconds:.1f}s)")
            pyperclip.copy(previous)
            beep(ok=True)
            return

        replace_selection(corrected, previous)
        print(f"done ({seconds:.1f}s)")
        beep(ok=True)
    finally:
        _busy.release()


def main() -> None:
    import sys
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(description="correct-me")
    parser.add_argument(
        "--hotkey",
        help='override the hotkey from config.json, e.g. --hotkey "page up" or --hotkey f8',
    )
    args = parser.parse_args()
    hotkey = args.hotkey or cfg.get("hotkey", "insert")
    print(f"correct-me - model: {cfg['model']} @ {cfg['base_url']}")
    if cfg.get("warm_up_on_start", True):
        print("Warming up model ...")
        warm_up(cfg)
    # Run corrections off the keyboard-callback thread so hotkeys stay responsive.
    keyboard.add_hotkey(hotkey, lambda: threading.Thread(target=on_hotkey, daemon=True).start())
    print(f"Ready. Select text anywhere and press {hotkey.upper()}. Ctrl+C here to quit.")
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
