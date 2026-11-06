# SwarmUP.exe --self-test: everything that can go wrong on a computer that is not the one where the application was built. It runs a real agent
# through the real gateway, with a scripted model, so it needs no key, no model and no internet for its main part. It writes a report in the logs of
# the application, which the user can send. Each check gives what it found, or why it failed; one that fails never stops the others. The window of the
# system is only a note: without it SwarmUP opens in an application window of Edge or Chrome, or in a tab of the browser.
import importlib
import importlib.metadata
import json
import platform
import ssl
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path


REPORT = "self_test.txt"
PAGES = ("index.html", "app.js", "style.css", "logo.svg")
PACKAGES = ("mcp", "pydantic", "uvicorn", "anthropic", "openai", "pypdf", "certifi", "cryptography")
WEB_SECONDS = 15
RUN_SECONDS = 60
WHEEL = '''WHEEL = {"name": "count_files", "summary": "Counts the files of the folder and gives their number, with the Python it ran on.",
         "kind": "script", "inputs": {}, "required": [], "changes": False, "network": False, "timeout": 30}


import json
import os
import sys


def run(args, folder):
    return json.dumps({"files": len(os.listdir(folder)), "python": sys.version.split()[0]})
'''


class ScriptedModel:
    approvesItself = True

    def __init__(self, calls):
        self.calls, self.results, self.usage = list(calls), [], {"calls": 0, "input": 0, "output": 0}

    def converse(self, system, messages, tools, onText=None):
        if messages[-1]["role"] == "tool":
            self.results.append(messages[-1]["content"])
        if self.calls:
            name, arguments = self.calls.pop(0)
            return {"text": "", "calls": [{"id": f"c{len(self.calls)}", "name": name, "arguments": arguments}], "raw": None, "cut": False}
        return {"text": "The self-test is done.", "calls": [], "raw": None, "cut": False}


def checkModules(root):
    backend = root / "src" / "backend"
    names = [path.stem for folder in sorted(backend.iterdir()) if folder.is_dir() for path in sorted(folder.glob("*.py"))
             if path.stem not in ("__init__", "standalone_app", "user_interface", "swarmup_cli", "wheel_runner")]
    for folder in sorted(backend.iterdir()):
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.append(str(folder))
    failed = {}
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as error:
            failed[name] = f"{type(error).__name__}: {error}"
    if failed:
        raise RuntimeError(json.dumps(failed, indent=1))
    return f"{len(names)} modules"


def checkPackages():
    for name in PACKAGES:
        importlib.import_module(name)
    return {name: importlib.metadata.version(name) for name in PACKAGES}


def checkPages():
    from web_server import INTERFACE_FOLDER
    missing = [name for name in PAGES if not (INTERFACE_FOLDER / name).is_file()]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} missing in {INTERFACE_FOLDER}")
    return str(INTERFACE_FOLDER)


def checkRules():
    from harness_utils import AGENT_RULES, listRules
    return f"{len(listRules())} rules files in {AGENT_RULES}"


def checkWindow():
    guilib = importlib.import_module("webview.guilib")
    return f"pywebview {importlib.metadata.version('pywebview')} on {guilib.initialize(None).__name__}"


def checkWebviewEngine():
    if sys.platform != "win32":
        return "only for Windows"
    from webview.platforms import winforms
    return {"renderer": winforms.renderer, "chromium": bool(winforms.is_chromium)}


def checkHttps():
    with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=WEB_SECONDS, context=ssl.create_default_context()) as answer:
        return f"status {answer.status}"


def checkPip():
    import subprocess
    done = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode:
        raise RuntimeError(done.stderr[-500:])
    return done.stdout.strip()


# One agent alone, with its tools through the gateway: a file written and read, a command, a wheel in a process of its own, and the journal of it all.
def checkAgent():
    from general_loop import GeneralLoop
    from model_adapters import USER_LEADER
    from project_identity import projectFor
    from swarm_harness import Swarm
    from wheel_store import retireWheel, saveWheel
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary).resolve()
        project = projectFor(folder)[0]
        saveWheel("count_files", "script", WHEEL, "Counts the files of the folder.", ["self-test"], "project", project, {"mission": "self-test"})
        try:
            model = ScriptedModel([("files__write_file", {"path": "made.txt", "content": "made by the self-test", "reason": "self-test"}), ("files__read_file", {"path": "made.txt"}),
                                   ("processes__run_command", {"command": f'"{sys.executable}" -c "print(6 * 7)"', "reason": "self-test"}), ("wheels__count_files", {})])
            swarm = Swarm("The self-test of the application")
            loop = GeneralLoop(model, "The self-test of the application")
            loop.setFolder(folder)
            loop.approvals = "auto"
            loop.notifyUser = lambda message: None
            swarm.addAgent("Agent", loop, "agent", "The self-test", model=dict(USER_LEADER), recipe={"task": "agent", "answers": {}})
            swarm.startInBackground()
            swarm.wait(RUN_SECONDS)
            if swarm.gateway is not None:
                swarm.gateway.close()
        finally:
            retireWheel("count_files", "the self-test is over")
        text = "\n".join(model.results)
        if "error" in swarm.outcome or swarm.outcome.get("result") is None:
            raise RuntimeError(f"The run failed: {swarm.outcome}. What it read: {text}")
        for part in ("made by the self-test", "42", '\\"files\\"', ):
            if part not in text and part.replace("\\", "") not in text:
                raise RuntimeError(f"{part} is not in what the tools gave: {text}")
        return "a file, a command and a wheel worked through the gateway"


def runSelfTest(home, root):
    checks = [("the modules of SwarmUP", lambda: checkModules(root)), ("its packages", checkPackages), ("the pages of the window", checkPages), ("the rules files", checkRules),
              ("https", checkHttps), ("pip, for the install button", checkPip), ("one agent, its tools and a wheel", checkAgent)]
    notes = [("the window of the system", checkWindow), ("the engine of the window", checkWebviewEngine)]
    lines = [f"SwarmUP self-test, {time.strftime('%Y-%m-%d %H:%M:%S')}", f"System: {platform.platform()} on {platform.machine()}", f"Python: {sys.version.split()[0]} at {sys.executable}",
             f"Data: {home}", f"Application: {root}", ""]
    failed = 0
    for name, check in checks:
        started = time.monotonic()
        try:
            lines.append(f"OK    {name}: {check()} ({time.monotonic() - started:.1f} s)")
        except BaseException:
            failed += 1
            lines.append(f"FAIL  {name}:\n{traceback.format_exc()}")
    for name, check in notes:
        try:
            lines.append(f"OK    {name}: {check()}")
        except BaseException as error:
            lines.append(f"NOTE  {name} cannot open, so SwarmUP will use an application window of Edge or Chrome, or a tab of the browser: {type(error).__name__}: {str(error)[:300]}")
    lines += ["", "Everything works." if not failed else f"{failed} checks failed. Send this file to the people who make SwarmUP."]
    report = home / "logs" / REPORT
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0
