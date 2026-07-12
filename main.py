"""correct-me v7 - hotkey text correction with a tray icon, settings GUI and
a single JSONL log file (correct-me.log).

Two hotkeys:
- Insert (default): corrects the WHOLE field - Ctrl+A, copy, correct, paste.
- Alt+Insert: corrects only the text you selected first.

Why two modes instead of auto-detection: many editors "helpfully" copy the
current line (Notepad++/Scintilla) or the current block (Notion) when Ctrl+C
is pressed with nothing selected. v5/v6 probed for a selection with Ctrl+C
and mistook that auto-copy for a real selection, correcting random lines.
v7 never probes: field mode always selects first, selection mode requires a
real selection.

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

import applog
import corrector
from corrector import CorrectionError, correct, load_config, load_glossary, warm_up

APP_VERSION = "7.2"

cfg = load_config()
glossary = load_glossary()
_busy = threading.Lock()
_paused = False
_hotkey_handles: list = []
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
# with an empty string is unreliable on Windows, so a stale clipboard could
# otherwise be mistaken for the copy. The marker is VERIFIED before Ctrl+C
# is sent: if it never lands, nothing we read can be trusted and the run is
# aborted.
_PROBE = "[correct-me-probe-7f3a]"


def _copy_after_ctrl_c(timeout: float) -> str:
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

def on_hotkey(mode: str) -> None:
    """mode: "field" (Ctrl+A the whole field) or "selection" (need a real one)."""
    if _paused:
        return
    if not _busy.acquire(blocking=False):
        return  # a correction is already running
    started = time.monotonic()
    set_state("busy", "correcting ...")
    applog.log("hotkey_press", mode=mode)
    ok = True
    detail = ""
    made_selection = False
    try:
        previous = ""
        try:
            previous = pyperclip.paste()
        except pyperclip.PyperclipException:
            pass

        try:
            if mode == "field":
                # Select the whole field FIRST - never Ctrl+C on a bare caret
                # (Notepad++ copies the current line, Notion the current block).
                keyboard.send("ctrl+a")
                made_selection = True
                time.sleep(0.05)
                text = _copy_after_ctrl_c(cfg.get("clipboard_timeout", 1.0))
            else:
                text = _copy_after_ctrl_c(cfg.get("selection_copy_timeout", 0.3))
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
            if mode == "field":
                note = (
                    "empty field, or the target app blocked Ctrl+A/Ctrl+C "
                    "(elevated apps need correct-me to run as administrator too)"
                )
            else:
                note = "no text selected - selection mode needs a real selection"
            applog.log("skip_no_text", mode=mode, note=note)
            if made_selection:
                _deselect()
            pyperclip.copy(previous)
            ok = False
            detail = "no text found - nothing done"
            return

        applog.log("grabbed", mode=mode, chars=len(text), text=applog.short(text))
        try:
            corrected, seconds = correct(text, cfg, glossary)
        except CorrectionError as exc:
            applog.log("error", note=str(exc))
            if made_selection:
                _deselect()
            pyperclip.copy(previous)
            beep(ok=False)
            ok = False
            detail = "error - check the local server (see Settings)"
            return

        if corrected == text:
            applog.log("nothing_to_fix", model_seconds=round(seconds, 2))
            if made_selection:
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


def register_hotkeys() -> tuple[str, str]:
    """(Re)register both global hotkeys from cfg. Returns (field, selection)."""
    global _hotkey_handles
    for handle in _hotkey_handles:
        try:
            keyboard.remove_hotkey(handle)
        except (KeyError, ValueError):
            pass
    _hotkey_handles = []
    # suppress=True swallows the key so it cannot reach the target app.
    # Critical for keys like Insert/Home, which would otherwise toggle modes /
    # move the caret and destroy the selection before we can copy it.
    suppress = cfg.get("suppress_hotkey", True)
    field_key = cfg.get("hotkey", "insert")
    _hotkey_handles.append(
        keyboard.add_hotkey(
            field_key,
            lambda: threading.Thread(
                target=on_hotkey, args=("field",), daemon=True
            ).start(),
            suppress=suppress,
        )
    )
    sel_key = (cfg.get("hotkey_selection") or "").strip()
    if sel_key and sel_key != field_key:
        _hotkey_handles.append(
            keyboard.add_hotkey(
                sel_key,
                lambda: threading.Thread(
                    target=on_hotkey, args=("selection",), daemon=True
                ).start(),
                suppress=suppress,
            )
        )
    return field_key, sel_key


def apply_config(new_cfg: dict) -> None:
    """Apply + persist a new config. Raises ValueError on a bad hotkey."""
    old_cfg = dict(cfg)
    cfg.clear()
    cfg.update(new_cfg)
    try:
        register_hotkeys()
    except (ValueError, KeyError) as exc:
        cfg.clear()
        cfg.update(old_cfg)
        register_hotkeys()
        raise ValueError(str(exc))
    with open(corrector.BASE_DIR / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    applog.log(
        "config_saved",
        hotkey=cfg["hotkey"],
        hotkey_selection=cfg.get("hotkey_selection", ""),
        model=cfg["model"],
    )


def fetch_models(base_url: str) -> list[str]:
    """List model ids from the local server (LM Studio / Ollama)."""
    try:
        resp = corrector.SESSION.get(base_url.rstrip("/") + "/models", timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return sorted(item.get("id", "") for item in data if item.get("id"))
    except Exception:
        return []


def _keep_alive_loop() -> None:
    """Ping the model periodically so JIT loading / auto-unload never evicts
    it (kills the several-second reload on the first correction after idle)."""
    while True:
        minutes = cfg.get("keep_alive_minutes", 0)
        if not minutes or minutes <= 0:
            return
        time.sleep(minutes * 60)
        warm_up(cfg)


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
    sel_hotkey_var = tk.StringVar(value=cfg.get("hotkey_selection", "alt+insert"))
    suppress_var = tk.BooleanVar(value=bool(cfg.get("suppress_hotkey", True)))
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

    ttk.Label(frm, text="Hotkey (whole field)").grid(row=2, column=0, sticky="w")
    ttk.Entry(frm, textvariable=hotkey_var, width=20).grid(row=2, column=1, sticky="w", pady=2)
    ttk.Label(frm, text="e.g. insert, f8, ctrl+alt+g").grid(row=2, column=2, sticky="w")

    ttk.Label(frm, text="Hotkey (selection only)").grid(row=3, column=0, sticky="w")
    ttk.Entry(frm, textvariable=sel_hotkey_var, width=20).grid(row=3, column=1, sticky="w", pady=2)
    ttk.Label(frm, text="empty = disabled").grid(row=3, column=2, sticky="w")

    checks = [
        ("Swallow the hotkey (required for Insert/Home/End)", suppress_var),
        ("Restore the previous clipboard after pasting", restore_var),
        ("Disable Gemma thinking tokens (faster)", think_var),
        ("Warm up the model on start", warm_var),
    ]
    row = 4
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
        new_cfg["hotkey_selection"] = sel_hotkey_var.get().strip()
        new_cfg["suppress_hotkey"] = bool(suppress_var.get())
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
        f"Ready. {cfg.get('hotkey', 'insert').upper()} = correct the whole "
        f"field, {cfg.get('hotkey_selection', 'alt+insert').upper()} = correct "
        "the selection. Ctrl+C here to quit."
    )
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("\nBye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="correct-me")
    parser.add_argument(
        "--hotkey",
        help='override the whole-field hotkey, e.g. --hotkey "page up" or --hotkey f8',
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="run without the tray icon (console mode)",
    )
    args = parser.parse_args()
    if args.hotkey:
        cfg["hotkey"] = args.hotkey  # session override; persisted only if you Save in Settings

    field_key, sel_key = register_hotkeys()
    applog.log(
        "app_start",
        version=APP_VERSION,
        model=cfg["model"],
        base_url=cfg["base_url"],
        hotkey=field_key,
        hotkey_selection=sel_key,
        keep_alive_minutes=cfg.get("keep_alive_minutes", 0),
        frozen=bool(getattr(sys, "frozen", False)),
        log_file=str(applog.LOG_PATH),
    )
    if cfg.get("warm_up_on_start", True):
        threading.Thread(target=warm_up, args=(cfg,), daemon=True).start()
    if cfg.get("keep_alive_minutes", 0):
        threading.Thread(target=_keep_alive_loop, daemon=True).start()

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
