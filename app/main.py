"""Entry point: start (or reuse) the local API server and open the UI in a desktop window.

There is only ever one backend. Its port is written to data/server.json; a second launch, the
browser mode and the Claude integration all attach to it instead of starting another one.
"""
import argparse
import atexit
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from werkzeug.serving import make_server

from . import paths

log = logging.getLogger("main")


def _setup_logging(verbose):
    # pythonw has no console: give stray prints somewhere harmless to go.
    if sys.stdout is None or sys.stderr is None:
        sink = open(paths.DATA / "console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink
    handlers = [logging.handlers.RotatingFileHandler(paths.LOG, maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.INFO)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def running_backend():
    """URL of an already running backend (another window, or one Claude started), if any."""
    try:
        info = json.loads(paths.SERVER_INFO.read_text("utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/"
        with urllib.request.urlopen(url + "api/ping", timeout=2) as r:
            if json.load(r).get("app") == "power-hour-maker":
                return url
    except (OSError, ValueError, KeyError):
        pass
    return None


def _register(port):
    paths.SERVER_INFO.write_text(json.dumps({"port": port, "pid": os.getpid(), "started": time.time()}), "utf-8")

    def cleanup():
        try:
            if json.loads(paths.SERVER_INFO.read_text("utf-8")).get("pid") == os.getpid():
                paths.SERVER_INFO.unlink()
        except (OSError, ValueError):
            pass

    atexit.register(cleanup)


class JsApi:
    """Functions the page can call through window.pywebview.api (desktop mode only)."""

    FILE_TYPES = {
        "video": ("Video files (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v;*.wmv;*.flv;*.mpg;*.mpeg;*.ts)",
                  "All files (*.*)"),
        "image": ("Images (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif)",),
        "audio": ("Audio (*.mp3;*.wav;*.m4a;*.aac;*.ogg;*.flac;*.opus;*.wma)",),
    }

    def pick_files(self, kind="video", multiple=False):
        import webview
        res = webview.windows[0].create_file_dialog(webview.FileDialog.OPEN, allow_multiple=bool(multiple),
                                                    file_types=self.FILE_TYPES.get(kind, ("All files (*.*)",)))
        return list(res) if res else []

    def pick_folder(self):
        import webview
        res = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        return res[0] if res else ""


def _set_window_icon(window):
    """WinForms windows otherwise show the generic Python icon in the title bar and taskbar."""
    try:
        from System import Action
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(str(paths.WEB / "icon.ico")))))
    except Exception:  # noqa: BLE001 - purely cosmetic
        log.debug("could not set window icon", exc_info=True)


def _open_window(url):
    """Blocks until the window is closed. False if no window could be shown (browser opened instead)."""
    try:
        import webview
        window = webview.create_window("Power Hour Maker", url, width=1480, height=940, min_size=(1100, 720),
                                       js_api=JsApi(), background_color="#0d0a14")
        window.events.shown += lambda: _set_window_icon(window)
        webview.start(private_mode=False, storage_path=str(paths.DATA / "webview"))
        return True
    except Exception:  # noqa: BLE001 - no WebView2 etc.: fall back to the browser
        log.exception("desktop window failed, falling back to browser")
        webbrowser.open(url)
        return False


def _wait_until_idle(server, idle_seconds):
    """Keep serving while anything (a window, Claude, a render, downloads) is using the backend."""
    while True:
        time.sleep(2)
        if not server.busy() and time.time() - server.last_activity[0] > idle_seconds:
            return


def main():
    if sys.platform == "win32":
        # Own taskbar group instead of being lumped in with other Python apps.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PowerHourMaker.App")
    ap = argparse.ArgumentParser(description="Power Hour Maker")
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of an app window")
    ap.add_argument("--serve", action="store_true", help="only run the backend (no window)")
    ap.add_argument("--idle-exit", type=float, default=0,
                    help="with --serve: quit after this many seconds without requests or work")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    _setup_logging(args.verbose)

    existing = None if (args.serve or args.port) else running_backend()
    if existing:
        log.info("attaching to running backend %s", existing)
        if args.browser:
            webbrowser.open(existing)
        else:
            _open_window(existing)
        return

    from . import server
    server.init()
    port = args.port or _free_port()
    httpd = make_server("127.0.0.1", port, server.app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    _register(port)
    url = f"http://127.0.0.1:{port}/"
    log.info("serving %s", url)

    try:
        if args.serve:
            print(f"Power Hour Maker backend at {url}", flush=True)
            if args.idle_exit:
                _wait_until_idle(server, args.idle_exit)
            else:
                threading.Event().wait()
        elif args.browser:
            webbrowser.open(url)
            print(f"Power Hour Maker running at {url}  (Ctrl+C to quit)", flush=True)
            _wait_until_idle(server, 120)
        elif _open_window(url):
            # Window closed: finish any render, and keep serving other windows / Claude while in use.
            _wait_until_idle(server, 20)
        else:
            _wait_until_idle(server, 120)
    except KeyboardInterrupt:
        pass
    server.renders.cancel()
    httpd.shutdown()
    log.info("backend stopped")
