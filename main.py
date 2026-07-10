"""correct-me - MVP with stock Gemma 4 E2B via LM Studio or Ollama.

Press the hotkey (default: Home) in any text field. If text is selected, it
is corrected and replaced. If nothing is selected, the whole field is grabbed
via Ctrl+A and corrected - perfect for chat messages: type, press Home, send.
Fully local, no cloud.

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


def _copy_selection() -> str:
    """Send Ctrl+C and poll the clipboard until the copy lands (or timeout)."""
    pyperclip.copy("")  # so we can tell whether Ctrl+C actually copied anything
    time.sleep(0.05)
    keyboard.send("ctrl+c")
    deadline = time.monotonic() + cfg.get("clipboard_timeout", 1.0)
    while time.monotonic() < deadline:
        text = pyperclip.paste()
        if text:
            return text
        time.sleep(0.03)
    return ""


def grab_selection() -> tuple[str, str, bool]:
    """Copy the selection; with no selection, select the whole field (Ctrl+A).

    Returns (text, previous_clipboard, used_select_all).
    """
    previous = ""
    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        pass
    text = _copy_selection()
    if text:
        return text, previous, False
    if cfg.get("no_selection", "select_all") != "select_all":
        return "", previous, False
    keyboard.send("ctrl+a")
    time.sleep(0.05)
    return _copy_selection(), previous, True


def replace_selection(corrected: str, previous_clipboard: str) -> None:
    pyperclip.copy(corrected)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    if cfg.get("restore_clipboard", True):
        # Give the target app a moment to read the clipboard before restoring.
        time.sleep(0.4)
        pyperclip.copy(previous_clipboard)


def _deselect() -> None:
    """Collapse a Ctrl+A selection so the next keystroke can't wipe the text."""
    keyboard.send("right")


def on_hotkey() -> None:
    if not _busy.acquire(blocking=False):
        return  # a correction is already running
    started = time.monotonic()
    try:
        text, previous, used_select_all = grab_selection()
        if not text.strip():
            print(
                "[skip] no text found - empty field? (If the target app runs "
                "as administrator, run this script as administrator too.)"
            )
            pyperclip.copy(previous)
            beep(ok=False)
            return

        print(f"[fixing] {len(text)} chars ...", end=" ", flush=True)
        try:
            corrected, seconds = correct(text, cfg, glossary)
        except CorrectionError as exc:
            print(f"\n[error] {exc}")
            if used_select_all:
                _deselect()
            pyperclip.copy(previous)
            beep(ok=False)
            return

        if corrected == text:
            print(f"nothing to fix ({seconds:.1f}s)")
            if used_select_all:
                _deselect()
            pyperclip.copy(previous)
            beep(ok=True)
            return

        replace_selection(corrected, previous)
        total = time.monotonic() - started
        print(f"done (model {seconds:.1f}s, total {total:.1f}s)")
        beep(ok=True)
    finally:
        _busy.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="correct-me")
    parser.add_argument(
        "--hotkey",
        help='override the hotkey from config.json, e.g. --hotkey "page up" or --hotkey f8',
    )
    args = parser.parse_args()
    hotkey = args.hotkey or cfg.get("hotkey", "home")
    suppress = cfg.get("suppress_hotkey", True)
    print(f"correct-me - model: {cfg['model']} @ {cfg['base_url']}")
    if cfg.get("warm_up_on_start", True):
        print("Warming up model ...")
        warm_up(cfg)
    # suppress=True swallows the key so it cannot reach the target app.
    # Critical for keys like Home/End/Page Up, which would otherwise move the
    # caret and destroy the selection before we can copy it.
    keyboard.add_hotkey(
        hotkey,
        lambda: threading.Thread(target=on_hotkey, daemon=True).start(),
        suppress=suppress,
    )
    print(f"Ready. Press {hotkey.upper()} in a text field (selection optional). Ctrl+C here to quit.")
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
