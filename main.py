"""correct-me v6 - hotkey text correction with a tray icon, settings GUI and
a single JSONL log file (correct-me.log).

Press the hotkey (default: Insert) in any text field. If text is selected, it
is corrected and replaced. If nothing is selected, the whole field is grabbed
via Ctrl+A and corrected - type your chat message, press Insert, hit send.
Fully local, no cloud.

Runs as a system tray icon (right-click: Settings / Pause / Quit).

Run:  python main.py            (tray mode)
      python main.py --console  (console mode)
"""

import argparse
import json
import os
import queue
import sys
import threading
import time

# With pyinstaller --noconsole there is no console: stdout/stderr are None and
# any print() would crash the app on start. Route them to devnull instead.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import keyboard
import pyperclip
import requests

import applog
import corrector
from corrector import CorrectionError, correct, load_config, load_glossary, warm_up

APP_VERSION = "6.0"

cfg = load_config()
glossary = load_glossary()
_busy = threading.Lock()
_paused = False
_hotkey_handle = None
_events: "queue.Queue[str]" = queue.Queue()
_tray = None  # pystray.Icon once the tray is running
_ICON_FACTORY = None  # set in run_tray_app (needs PIL)
_settings_win = None

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


def set_state(state: str, detail: str = "") -> None:
    """Update the tray icon color and tooltip (no-op in console mode)."""
    if _tray is None or _ICON_FACTORY is None:
        return
    try:
        _tray.icon = _ICON_FACTORY(state)
        _tray.title = ("correct-me - " + (detail or state))[:120]
    except Exception:
        pass  # tray hiccups must never break a correction


# ---------------------------------------------------------------- clipboard

class ClipboardBusy(Exception):
    """The clipboard could not be prepared - another app is holding it."""


# Unique marker put on the clipboard before Ctrl+C. Clearing the clipboard
# with an empty string is unreliable on Windows, and even a non-empty write
# can be delayed or swallowed when another app (or a clipboard manager) holds
# the clipboard. The marker is therefore VERIFIED before Ctrl+C is sent: if
# it never lands, nothing we read can be trusted and the run is aborted.
# (v5/v5.1 could mistake stale clipboard content for the selection in that
# case - seen inside Notion, where it pasted an old clipboard back in.)
_PROBE = "[correct-me-probe-7f3a]"


def _copy_selection(timeout: float) -> str:
    """Send Ctrl+C and poll the clipboard until the copy lands (or timeout)."""
    try:
        pyperclip.copy(_PROBE)
    except pyperclip.PyperclipException as exc:
        raise ClipboardBusy(str(exc)) from exc
    deadline = time.monotonic() + 0.3
    while True:
        try:
            if pyperclip.paste() == _PROBE:
                break
        except pyperclip.PyperclipException:
            pass
        if time.monotonic() > deadline:
            raise ClipboardBusy("probe marker never reached the clipboard")
        time.sleep(0.02)
    keyboard.send("ctrl+c")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException:
            text = ""
        if text and text != _PROBE:
            return text
        time.sleep(0.02)
    return ""


def grab_selection() -> tuple[str, str, bool]:
    """Copy the selection; with no selection, select the whole field (Ctrl+A).

    The first probe uses a short timeout (selection_probe_timeout) so the
    common no-selection case does not burn the full clipboard_timeout.
    Returns (text, previous_clipboard, used_select_all).
    """
    previous = ""
    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        pass
    text = _copy_selection(cfg.get("selection_probe_timeout", 0.15))
    if text:
        return text, previous, False
    if cfg.get("no_selection", "select_all") != "select_all":
        return "", previous, False
    keyboard.send("ctrl+a")
    time.sleep(0.05)
    return _copy_selection(cfg.get("clipboard_timeout", 1.0)), previous, True


def replace_selection(corrected: str, previous_clipboard: str) -> None:
    pyperclip.copy(corrected)
    time.sleep(cfg.get("paste_delay", 0.05))
    keyboard.send("ctrl+v")
    if cfg.get("restore_clipboard", True):
        # Restore in the background so the user-visible latency ends at paste.
        delay = cfg.get("restore_delay", 0.2)

        def _restore() -> None:
            time.sleep(delay)
            try:
                pyperclip.copy(previous_clipboard)
            except pyperclip.PyperclipException:
                pass

        threading.Thread(target=_restore, daemon=True).start()


def _deselect() -> None:
    """Collapse a Ctrl+A selection so the next keystroke can't wipe the text."""
    keyboard.send("right")


# ------------------------------------------------------------------ hotkey

def on_hotkey() -> None:
    if _paused:
        return
    if not _busy.acquire(blocking=False):
        return  # a correction is already running
    started = time.monotonic()
    set_state("busy", "correcting ...")
    applog.log("hotkey_press")
    ok = True
    detail = ""
    try:
        try:
            text, previous, used_select_all = grab_selection()
        except ClipboardBusy as exc:
            applog.log(
                "clipboard_error",
                note=f"clipboard is locked or unresponsive ({exc}) - nothing done",
            )
            beep(ok=False)
            ok = False
            detail = "clipboard locked - nothing done"
            return

        if not text.strip():
            applog.log(
                "skip_no_text",
                used_select_all=used_select_all,
                note=(
                    "empty field, or the target app blocked Ctrl+C (elevated "
                    "apps need correct-me to run as administrator too)"
                ),
            )
            pyperclip.copy(previous)
            ok = False
            detail = "no text found - nothing done"
            return

        applog.log(
            "grabbed",
            chars=len(text),
            used_select_all=used_select_all,
            text=applog.short(text),
        )
        try:
            corrected, seconds = correct(text, cfg, glossary)
        except CorrectionError as exc:
            applog.log("error", note=str(exc))
            if used_select_all:
                _deselect()
            pyperclip.copy(previous)
            beep(ok=False)
            ok = False
            detail = "error - check the local server (see Settings)"
            return

        if corrected == text:
            applog.log("nothing_to_fix", model_seconds=round(seconds, 2))
            if used_select_all:
                _deselect()
            pyperclip.copy(previous)
            beep(ok=True)
            detail = f"nothing to fix ({seconds:.1f}s)"
            return

        replace_selection(corrected, previous)
        total = time.monotonic() - started
        applog.log(
            "done",
            model_seconds=round(seconds, 2),
            total_seconds=round(total, 2),
            text=applog.short(corrected),
        )
        beep(ok=True)
        detail = f"done (model {seconds:.1f}s, total {total:.1f}s)"
    finally:
        _busy.release()
        if _paused:
            set_state("paused")
        else:
            set_state("idle" if ok else "error", detail)


def register_hotkey() -> str:
    """(Re)register the global hotkey from cfg. Returns the hotkey string."""
    global _hotkey_handle
    if _hotkey_handle is not None:
        try:
            keyboard.remove_hotkey(_hotkey_handle)
        except (KeyError, ValueError):
            pass
        _hotkey_handle = None
    hotkey = cfg.get("hotkey", "insert")
    # suppress=True swallows the key so it cannot reach the target app.
    # Critical for keys like Home/End/Insert, which would otherwise move the
    # caret / toggle modes and destroy the selection before we can copy it.
    _hotkey_handle = keyboard.add_hotkey(
        hotkey,
        lambda: threading.Thread(target=on_hotkey, daemon=True).start(),
        suppress=cfg.get("suppress_hotkey", True),
    )
    return hotkey


def apply_config(new_cfg: dict) -> None:
    """Apply + persist a new config. Raises ValueError on a bad hotkey."""
    old_cfg = dict(cfg)
    cfg.clear()
    cfg.update(new_cfg)
    try:
        register_hotkey()
    except (ValueError, KeyError) as exc:
        cfg.clear()
        cfg.update(old_cfg)
        register_hotkey()
        raise ValueError(str(exc))
    with open(corrector.BASE_DIR / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    applog.log("config_saved", hotkey=cfg["hotkey"], model=cfg["model"])


def fetch_models(base_url: str) -> list[str]:
    """List model ids from the local server (LM Studio / Ollama)."""
    try:
        resp = requests.get(base_url.rstrip("/") + "/models", timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return sorted(item.get("id", "") for item in data if item.get("id"))
    except (requests.RequestException, ValueError, AttributeError):
        return []


# --------------------------------------------------------------- tray GUI

def open_settings(root) -> None:
    global _settings_win
    import tkinter as tk
    from tkinter import messagebox, ttk

    if _settings_win is not None:
        try:
            if _settings_win.winfo_exists():
                _settings_win.deiconify()
                _settings_win.lift()
                return
        except tk.TclError:
            pass

    win = tk.Toplevel(root)
    _settings_win = win
    win.title("correct-me settings")
    win.resizable(False, False)
    win.attributes("-topmost", True)

    frm = ttk.Frame(win, padding=12)
    frm.grid(row=0, column=0)

    url_var = tk.StringVar(value=cfg.get("base_url", "http://localhost:1234/v1"))
    model_var = tk.StringVar(value=cfg.get("model", ""))
    hotkey_var = tk.StringVar(value=cfg.get("hotkey", "insert"))
    suppress_var = tk.BooleanVar(value=bool(cfg.get("suppress_hotkey", True)))
    selectall_var = tk.BooleanVar(value=cfg.get("no_selection", "select_all") == "select_all")
    restore_var = tk.BooleanVar(value=bool(cfg.get("restore_clipboard", True)))
    think_var = tk.BooleanVar(value=bool(cfg.get("disable_thinking", True)))
    warm_var = tk.BooleanVar(value=bool(cfg.get("warm_up_on_start", True)))

    ttk.Label(frm, text="Server URL").grid(row=0, column=0, sticky="w")
    ttk.Entry(frm, textvariable=url_var, width=40).grid(
        row=0, column=1, columnspan=2, sticky="we", pady=2
    )

    ttk.Label(frm, text="Model").grid(row=1, column=0, sticky="w")
    model_box = ttk.Combobox(frm, textvariable=model_var, width=32)
    model_box.grid(row=1, column=1, sticky="we", pady=2)

    def _populate_models() -> None:
        ids = fetch_models(url_var.get().strip())
        if ids:
            try:
                model_box["values"] = ids
            except tk.TclError:
                pass

    def refresh_models() -> None:
        ids = fetch_models(url_var.get().strip())
        if ids:
            model_box["values"] = ids
        else:
            messagebox.showwarning(
                "correct-me",
                "Could not list models - is the local server running at this URL?",
                parent=win,
            )

    ttk.Button(frm, text="Refresh", command=refresh_models).grid(row=1, column=2, padx=(6, 0))
    threading.Thread(target=_populate_models, daemon=True).start()

    ttk.Label(frm, text="Hotkey").grid(row=2, column=0, sticky="w")
    ttk.Entry(frm, textvariable=hotkey_var, width=20).grid(row=2, column=1, sticky="w", pady=2)
    ttk.Label(frm, text="e.g. insert, home, f8, ctrl+alt+g").grid(row=2, column=2, sticky="w")

    checks = [
        ("Swallow the hotkey (required for Insert/Home/End)", suppress_var),
        ("Correct the whole field when nothing is selected (Ctrl+A)", selectall_var),
        ("Restore the previous clipboard after pasting", restore_var),
        ("Disable Gemma thinking tokens (faster)", think_var),
        ("Warm up the model on start", warm_var),
    ]
    row = 3
    for label, var in checks:
        ttk.Checkbutton(frm, text=label, variable=var).grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        row += 1

    ttk.Label(
        frm,
        text="Timing knobs live in config.json. Full activity: correct-me.log",
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
    row += 1

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=3, pady=(10, 0))

    def do_save() -> None:
        new_cfg = dict(cfg)
        new_cfg["base_url"] = url_var.get().strip() or "http://localhost:1234/v1"
        new_cfg["model"] = model_var.get().strip() or cfg.get("model", "")
        new_cfg["hotkey"] = hotkey_var.get().strip() or "insert"
        new_cfg["suppress_hotkey"] = bool(suppress_var.get())
        new_cfg["no_selection"] = "select_all" if selectall_var.get() else "off"
        new_cfg["restore_clipboard"] = bool(restore_var.get())
        new_cfg["disable_thinking"] = bool(think_var.get())
        new_cfg["warm_up_on_start"] = bool(warm_var.get())
        try:
            apply_config(new_cfg)
        except ValueError as exc:
            messagebox.showerror("correct-me", f"Invalid hotkey: {exc}", parent=win)
            return
        set_state("paused" if _paused else "idle")
        win.destroy()

    ttk.Button(btns, text="Save and apply", command=do_save).grid(row=0, column=0, padx=4)
    ttk.Button(btns, text="Cancel", command=win.destroy).grid(row=0, column=1, padx=4)


def run_tray_app() -> None:
    import tkinter as tk

    import pystray
    from PIL import Image, ImageDraw

    global _tray, _ICON_FACTORY

    colors = {
        "idle": (76, 175, 80, 255),     # green
        "busy": (255, 152, 0, 255),     # orange
        "error": (211, 47, 47, 255),    # red
        "paused": (158, 158, 158, 255), # gray
    }

    def make_image(state: str):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=colors.get(state, colors["idle"]))
        # white check mark
        d.line((20, 34, 29, 43), fill=(255, 255, 255, 255), width=6)
        d.line((29, 43, 46, 22), fill=(255, 255, 255, 255), width=6)
        return img

    _ICON_FACTORY = make_image

    root = tk.Tk()
    root.withdraw()  # settings windows are Toplevels; the root stays hidden

    def toggle_pause(icon=None, item=None) -> None:
        global _paused
        _paused = not _paused
        applog.log("paused" if _paused else "resumed")
        set_state("paused" if _paused else "idle")

    menu = pystray.Menu(
        pystray.MenuItem(
            "Settings...", lambda icon, item: _events.put("settings"), default=True
        ),
        pystray.MenuItem("Pause", toggle_pause, checked=lambda item: _paused),
        pystray.MenuItem("Quit", lambda icon, item: _events.put("quit")),
    )
    icon = pystray.Icon("correct-me", make_image("idle"), "correct-me - idle", menu)
    _tray = icon
    threading.Thread(target=icon.run, daemon=True).start()

    def poll_events() -> None:
        try:
            while True:
                evt = _events.get_nowait()
                if evt == "settings":
                    open_settings(root)
                elif evt == "quit":
                    applog.log("quit")
                    keyboard.unhook_all()
                    icon.stop()
                    root.quit()
                    return
        except queue.Empty:
            pass
        root.after(100, poll_events)

    root.after(100, poll_events)
    print("Tray icon running. Right-click it: Settings / Pause / Quit.")
    root.mainloop()
    print("Bye.")


def run_console() -> None:
    print(
        f"Ready. Press {cfg.get('hotkey', 'insert').upper()} in a text field "
        "(selection optional). Ctrl+C here to quit."
    )
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("\nBye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="correct-me")
    parser.add_argument(
        "--hotkey",
        help='override the hotkey from config.json, e.g. --hotkey "page up" or --hotkey f8',
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="run without the tray icon (console mode)",
    )
    args = parser.parse_args()
    if args.hotkey:
        cfg["hotkey"] = args.hotkey  # session override; persisted only if you Save in Settings

    hotkey = register_hotkey()
    applog.log(
        "app_start",
        version=APP_VERSION,
        model=cfg["model"],
        base_url=cfg["base_url"],
        hotkey=hotkey,
        frozen=bool(getattr(sys, "frozen", False)),
        log_file=str(applog.LOG_PATH),
    )
    if cfg.get("warm_up_on_start", True):
        threading.Thread(target=warm_up, args=(cfg,), daemon=True).start()

    if args.console:
        run_console()
        return
    try:
        run_tray_app()
    except ImportError:
        applog.log(
            "error",
            note="tray dependencies missing (pip install pystray Pillow) - console mode",
        )
        run_console()


if __name__ == "__main__":
    main()
