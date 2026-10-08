# The graphical interface of SwarmUP. Run it with: python src/backend/interface/user_interface.py
# It is a small web server that only listens to this computer (127.0.0.1) and opens the interface in the web browser, so it works the same
# on Windows, macOS and Linux, with nothing to install. The pages are in the folder user-interface (index.html, style.css, icons.js, app.js).
# The interface follows the steps of the command line (src/backend/cli-tool/swarmup_cli.py) with clicks, from the same functions of tasks_library.py,
# models_library.py and the modules of swarm-utils: the mission, the task of each agent, its folder, its model, who waits for whom, and the run of
# the swarm, which is followed live. The user can also let the leader build the swarm (leader_utils.py): the user chooses the model of the
# leader and the folder of the mission, the leader proposes the agents, and the user approves. The agents speak to the user through the
# Session (notify, ask and askSecret), and the browser asks the Session for news (poll). The window holds several missions at once, each in a tab
# with its Session (Desk in session_desk.py). Passwords, API keys and tokens are only kept in memory,
# and they are never sent back to the browser.
import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted(Path(__file__).resolve().parents[1].iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
from interface_views import folderOf
from internet_cache import keepFresh
from session_desk import Desk
from web_server import HOST, makeServer


WINDOW_SIZE = (1380, 900)
WINDOW_MINIMUM = (980, 660)
WINDOW_BACKGROUND = "#0F1122"
# A browser that is already open takes the window over and its own process ends at once: then the program waits for Ctrl+C instead.
HANDOVER_SECONDS = 3


# ==============
# The desktop window. SwarmUP opens in a window of its own, like any program of the computer:
# 1. with pywebview (pip install pywebview), which uses the web engine of the system (WebView2 on Windows, WebKit on macOS, GTK or Qt on Linux)
#    and gives the dialogs of the system to choose folders and files,
# 2. otherwise in an application window of Edge, Chrome, Chromium or Brave (a window without tabs and address bar), if one is installed,
# 3. otherwise in a tab of the web browser.
# The program ends when its window is closed, and a swarm that runs is saved first, so it can be continued at the next start.
# ==============
def importWebview():
    try:
        import webview
    except ImportError:
        return None
    return webview


def findAppBrowser():
    names = ("msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "microsoft-edge-stable", "brave-browser", "chrome")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        roots = [os.environ.get(variable, "") for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        places = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        candidates = [Path(root) / place for root in roots if root for place in places]
    elif sys.platform == "darwin":
        candidates = [Path(f"/Applications/{app}.app/Contents/MacOS/{app}") for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser")]
    else:
        candidates = []
    return next((str(candidate) for candidate in candidates if candidate.is_file()), None)


# The dialogs of the system, given by pywebview. kind is folder, file (an existing file) or path (a file that may not exist yet).
def makeDialog(webview, window):
    dialogs = getattr(webview, "FileDialog", None)
    kinds = {"folder": dialogs.FOLDER, "file": dialogs.OPEN, "path": dialogs.SAVE} if dialogs else \
        {"folder": webview.FOLDER_DIALOG, "file": webview.OPEN_DIALOG, "path": webview.SAVE_DIALOG}
    def choose(kind, start):
        folder = str(folderOf(Path(start).expanduser())) if start else str(Path.home())
        name = Path(start).name if kind == "path" and start and not Path(start).is_dir() else ""
        chosen = window.create_file_dialog(kinds[kind], directory=folder, save_filename=name) if kind == "path" else window.create_file_dialog(kinds[kind], directory=folder)
        if isinstance(chosen, (list, tuple)):
            chosen = chosen[0] if chosen else None
        return str(chosen) if chosen else ""
    return choose


def openWebviewWindow(webview, session, address):
    window = webview.create_window("SwarmUP", address, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=WINDOW_MINIMUM, confirm_close=True,
                                   background_color=WINDOW_BACKGROUND, text_select=True)
    session.dialog = makeDialog(webview, window)
    webview.start(private_mode=False, storage_path=str(windowFolder("webview")))


def windowFolder(name):
    folder = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "swarm-up" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# An application window of a browser, with a profile of its own: the browser then runs until this window is closed.
def openAppWindow(browser, address):
    command = [browser, f"--app={address}", f"--user-data-dir={windowFolder('app-window')}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
               "--no-first-run", "--no-default-browser-check"]
    started = time.monotonic()
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if time.monotonic() - started < HANDOVER_SECONDS:
        print("The window was opened by a browser that was already running. Keep this terminal open while you use SwarmUP, "
              "and press Ctrl+C here to leave.", flush=True)
        threading.Event().wait()


def main():
    parser = argparse.ArgumentParser(description="The graphical interface of SwarmUP. It opens in a window of its own.")
    parser.add_argument("--port", type=int, default=0, help="the port to listen on (a free one by default)")
    parser.add_argument("--window", choices=("auto", "webview", "app", "browser", "none"), default="auto",
                        help="where to open the interface: auto (the best one available), webview (pywebview), app (an application window of Edge or Chrome), "
                             "browser (a tab of the web browser), or none (only print the address)")
    options = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    session = Desk()
    keepFresh()
    server = makeServer(session, options.port)
    address = f"http://{HOST}:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview = importWebview() if options.window in ("auto", "webview") else None
    browser = findAppBrowser() if options.window in ("auto", "app") and not webview else None
    if options.window == "webview" and not webview:
        print("pywebview is not installed, so SwarmUP cannot open its own window. Install it with: pip install pywebview", flush=True)
    print(f"SwarmUP is running at {address}", flush=True)
    try:
        if webview:
            openWebviewWindow(webview, session, address)
        elif browser:
            openAppWindow(browser, address)
        else:
            if options.window != "none":
                print("For a window of its own, install pywebview: pip install pywebview", flush=True)
                webbrowser.open(address)
            print("Keep this terminal open while you use SwarmUP. Press Ctrl+C here to leave (a running swarm is saved).", flush=True)
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    print("Leaving SwarmUP. A running swarm is saved: start the program again to continue it.", flush=True)
    session.close()
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
