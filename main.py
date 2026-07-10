"""correct-me v5 - hotkey text correction with a tray icon and settings GUI.

Press the hotkey (default: Home) in any text field. If text is selected, it
is corrected and replaced. If nothing is selected, the whole field is grabbed
via Ctrl+A and corrected - type your chat message, press Home, hit send.
Fully local, no cloud.

Runs as a system tray icon (right-click: Settings / Pause / Quit).

Run:  python main.py            (tray mode)
      python main.py --console  (v4-style console mode)
"""

import argparse
import json
import queue
import sys
import threading
import time

import keyboard
import pyperclip
import requests

import corrector
from corrector import CorrectionError, correct, load_config, load_glossary, warm_up

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

def _poll_clipboard(deadline: float) -> str:
    while time.monotonic() < deadline:
        try:
            text = pyperclip.paste()
        except pyperclip.PyperclipException:
            text = ""
        if text:
            return text
        time.sleep(0.02)
    return ""


def _copy_selection(timeout: float) -> str:
    """Send Ctrl+C and poll the clipboard until the copy lands (or timeout)."""
    pyperclip.copy("")  # so we can tell whether Ctrl+C actually copied anything
    time.sleep(0.03)
    keyboard.send("ctrl+c")
    return _poll_clipboard(time.monotonic() + timeout)


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
    ok = True
    detail = ""
    try:
        text, previous, used_select_all = grab_selection()
        if not text.strip():
            print(
                "[skip] no text found - empty field? (If the target app runs "
                "as administrator, run this app as administrator too.)"
            )
            pyperclip.copy(previous)
            beep(ok=False)
            ok = False
            detail = "no text found"
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
            ok = False
            detail = "error - check the local server (see Settings)"
            return

        if corrected == text:
            print(f"nothing to fix ({seconds:.1f}s)")
            if used_select_all:
                _deselect()
            pyperclip.copy(previous)
            beep(ok=True)
            detail = f"nothing to fix ({seconds:.1f}s)"
            return

        replace_selection(corrected, previous)
        total = time.monotonic() - started
        print(f"done (model {seconds:.1f}s, total {total:.1f}s)")
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
    hotkey = cfg.get("hotkey", "home")
    # suppress=True swallows the key so it cannot reach the target app.
    # Critical for keys like Home/End/Page Up, which would otherwise move the
    # caret and destroy the selection before we can copy it.
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
    print(f"[config] saved (hotkey: {cfg['hotkey']}, model: {cfg['model']})")


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
    hotkey_var = tk.StringVar(value=cfg.get("hotkey", "home"))
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
    ttk.Label(frm, text="e.g. home, insert, f8, ctrl+alt+g").grid(row=2, column=2, sticky="w")

    checks = [
        ("Swallow the hotkey (required for Home/End/Page Up)", suppress_var),
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

    ttk.Label(frm, text="Timing knobs (timeouts, delays) live in config.json.").grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(6, 0)
    )
    row += 1

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=3, pady=(10, 0))

    def do_save() -> None:
        new_cfg = dict(cfg)
        new_cfg["base_url"] = url_var.get().strip() or "http://localhost:1234/v1"
        new_cfg["model"] = model_var.get().strip() or cfg.get("model", "")
        new_cfg["hotkey"] = hotkey_var.get().strip() or "home"
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
        print("[paused]" if _paused else "[resumed]")
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
        f"Ready. Press {cfg.get('hotkey', 'home').upper()} in a text field "
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
        help="run without the tray icon (v4-style console mode)",
    )
    args = parser.parse_args()
    if args.hotkey:
        cfg["hotkey"] = args.hotkey  # session override; persisted only if you Save in Settings

    print(f"correct-me - model: {cfg['model']} @ {cfg['base_url']}")
    if cfg.get("warm_up_on_start", True):
        print("Warming up model (in background) ...")
        threading.Thread(target=warm_up, args=(cfg,), daemon=True).start()

    hotkey = register_hotkey()
    print(f"Hotkey: {hotkey.upper()} (selection optional)")

    if args.console:
        run_console()
        return
    try:
        run_tray_app()
    except ImportError:
        print("Tray dependencies missing - falling back to console mode.")
        print("For the tray icon: pip install pystray Pillow")
        run_console()


if __name__ == "__main__":
    main()
