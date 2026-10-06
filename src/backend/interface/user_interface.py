# The graphical interface of SwarmUP. Run it with: python src/backend/interface/user_interface.py
# It is a small web server that only listens to this computer (127.0.0.1) and opens the interface in the web browser, so it works the same
# on Windows, macOS and Linux, with nothing to install. The pages are in the folder user-interface (index.html, style.css, icons.js, app.js).
# The interface follows the steps of tests/full_command_line_user_test.py with clicks, from the same functions of tasks_library.py,
# models_library.py and harness_utils.py: the mission, the task of each agent, its folder, its model, who waits for whom, and the run of
# the swarm, which is followed live. The user can also let the leader build the swarm (leader_utils.py): the user chooses the model of the
# leader and the folder of the mission, the leader proposes the agents, and the user approves. The agents speak to the user through the
# Session (notify, ask and askSecret), and the browser asks the Session for news (poll). Passwords, API keys and tokens are only kept in memory,
# and they are never sent back to the browser.
import argparse
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted(Path(__file__).resolve().parents[1].iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
from harness_utils import (FETCH_ERRORS, USER_NAME, ConnectionLost, Loop, MessagingError, Swarm, checkEmailLogin, checkMessenger, checkVram, describeError,
                           findPublishers, findTelegramChats, findUnfinishedSwarms, getModelCost, nextOccurrence, readGpus)
from leader_utils import LeaderCatalog, LeaderManager, designSwarm
from model_clients import (ModelError, CodexLogin, checkCodex, createModel, findMissingPackages, getApiKey, getHubFolder, isDownloaded, listCodexModels,
                           lookupHuggingFace, readCodexAccount)
from models_library import (API_KEYS, BITS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, PRICING_PAGES, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo,
                            isGated)
from sources_library import MESSAGING_APPS, NEWS_OUTLETS, PAPER_PUBLISHERS
from tasks_library import (ADVANCED_FIELDS, DEFAULT_LOOPS, LEADER_TASK, NO_MESSENGER, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getFields,
                           getHelp, getTask, isAsked, messengerSettings, parseAnswer, publicAnswers, readAnswers, restoreAnswers, secretFields, suggestFolder, suggestName)

# The pages of the window: src/user-interface.
INTERFACE_FOLDER = Path(__file__).resolve().parents[2] / "user-interface"
ASSETS = {"style.css": "text/css; charset=utf-8", "icons.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          "logo.svg": "image/svg+xml"}
HOST = "127.0.0.1"
TOKEN_HEADER = "X-SwarmUP-Token"
TOKEN_PLACEHOLDER = "__SWARMUP_TOKEN__"
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
POLL_SECONDS = 20
FEED_LIMIT = 600
CONTEXT_LIMIT = 6
BODY_LIMIT = 2000000
BROWSE_LIMIT = 3000
TEST_PROMPT = "Reply with the single word OK."
SHUTDOWN_DELAY = 0.5
WINDOW_SIZE = (1380, 900)
WINDOW_MINIMUM = (980, 660)
WINDOW_BACKGROUND = "#0F1122"
# A browser that is already open takes the window over and its own process ends at once: then the program waits for Ctrl+C instead.
HANDOVER_SECONDS = 3
NEEDS_FOLDER_KINDS = ("file", "path")
SKIPPED_STATUSES = ("waiting",)
# The errors whose message is written for the user. The browser shows them as they are.
USER_ERRORS = (ValueError, ModelError, MessagingError, ConnectionLost)
# The actions that change what the user built run one at a time. The others only read, ask the internet, wait for a dialog of the system,
# or answer the swarm, so they never wait behind a slow one.
FREE_ACTIONS = ("checkEmail", "checkMessenger", "findChats", "searchPublishers", "browse", "pickPath", "lookupModel", "price", "testModel", "modelCatalog", "answer",
                "approve", "reject", "correct", "message", "startNow", "openLink", "openFolder", "refreshUnfinished", "quit", "taskForm",
                "codexAccount")


# An answer of a form that is not right. errors is {key of the field: what is wrong}, shown under each field.
class FormError(ValueError):
    def __init__(self, errors, message="Some answers need your attention."):
        super().__init__(message)
        self.errors = errors


# ==============
# What the browser shows of the models, the questions and the events of a swarm.
# ==============
def describeModel(info):
    if not info:
        return None
    cli = info.get("cli")
    if cli:
        agent = MODELS_CLI[cli]
        model = "its default model" if info["name"] == DEFAULT_CLI_MODEL else info["name"]
        return {**info, "cli": cli, "company": agent["company"], "gated": False, "label": f"{agent['label']} · {model}"}
    company = API_KEYS[info["provider"]]["company"] if info["provider"] else ""
    label = f"{info['name']} · {info['vram']} GB of VRAM" if info["local"] else f"{info['name']} · {company}"
    return {**info, "cli": None, "company": company, "gated": isGated(info["name"]) if info["local"] else False, "label": label}


# Whether a model can be made without asking anything more: a local model, Codex (signed in through Codex), or a model with its API key.
def hasCredentials(info, keys):
    return bool(info["local"] or info.get("cli") == "codex" or keys.get(info["provider"]) or getApiKey(info["provider"]))


# What kind of question the agents ask, and the buttons that answer it. The text box stays for every question that takes words.
# A coding agent asks for a permission (kind permission) or asks its own questions (kind form): those are answered with buttons and choices.
PERMISSION_REPLIES = [{"label": "Allow once", "value": "once", "style": "primary"}, {"label": "Allow until the next run", "value": "run", "style": "ghost"},
                      {"label": "Deny", "value": "deny", "style": "danger"}]


def describeQuestion(text, secret=False, kind=None):
    low = text.strip().lower()
    if kind == "permission":
        return "permission", PERMISSION_REPLIES
    if kind == "proposal":
        return "proposal", []
    if kind == "form":
        return "form", []
    if secret:
        return "secret", []
    if "type continue to try again" in low:
        return "connection", [{"label": "Try again", "value": "continue", "style": "primary"}, {"label": "Cancel the swarm", "value": "cancel", "style": "danger"}]
    if "type stop to stop here" in low:
        return "stop", [{"label": "Go on until the end", "value": "continue", "style": "primary"}, {"label": "Stop here", "value": "stop", "style": "danger"}]
    if low.startswith("do you approve?"):
        approve = "Approve all" if "approve all" in low else "Approve"
        return "review", [{"label": approve, "value": "yes", "style": "primary"}, {"label": "Reject" + (" all" if approve == "Approve all" else ""), "value": "no", "style": "danger"}]
    if "type yes to approve" in low or "is this good to go?" in low:
        return "approval", [{"label": "Approve", "value": "yes", "style": "primary"}, {"label": "Reject", "value": "no", "style": "danger"}]
    if "(yes/no)" in low:
        return "yesno", [{"label": "Yes", "value": "yes", "style": "primary"}, {"label": "No", "value": "no", "style": "ghost"}]
    if low.startswith("username for"):
        return "text", [{"label": "Skip, I have no account", "value": "", "style": "ghost"}]
    return "text", []


# What the user answered, as the conversation shows it. A secret is never shown.
def describeAnswer(question, answer):
    if question["kind"] == "permission" and isinstance(answer, dict):
        detail = (question["payload"] or {}).get("detail", "")
        words = {"once": "Allowed once", "run": "Allowed until the next run", "deny": "Denied"}.get(answer.get("decision"), "Denied")
        return f"{words}: {detail}" + (f"\nWhy: {answer['message']}" if answer.get("message") else "")
    if question["kind"] == "proposal" and isinstance(answer, dict):
        approved = answer.get("decision") == "approve"
        what = "the swarm of the leader" if (question["payload"] or {}).get("action") == "build" else (question["payload"] or {}).get("text", "the proposal")
        return f"{'Approved' if approved else 'Rejected'}: {what}" + (f"\nWhat to change: {answer['message']}" if answer.get("message") else "")
    if question["kind"] == "form" and isinstance(answer, dict):
        secret = {item["id"] for item in question["payload"] or [] if item.get("secret")}
        return "\n".join(f"{key}: {'••••••' if key in secret else ', '.join(values)}" for key, values in answer.items()) or "(no answer)"
    if question["secret"] and answer:
        return "••••••"
    return str(answer) if answer else "(no answer)"


def formatEvent(event):
    kind, agent = event["kind"], event["agent"]
    if kind == "run":
        return f"The swarm starts in {'plan' if event['mode'] == 'plan' else 'execute'} mode.", "start"
    if kind == "finished":
        return ("The swarm finished." if event["ok"] else "The swarm ended without a result."), "success" if event["ok"] else "warning"
    if kind == "status" and event["status"] not in SKIPPED_STATUSES:
        words = {"working": "started working", "done": "is done", "failed": "did not finish", "paused": "is paused: the connection was lost"}
        return f"{agent} {words.get(event['status'], 'is now ' + event['status'])}.", {"done": "success", "failed": "danger", "paused": "warning"}.get(event["status"], "info")
    if kind == "review":
        words = {"ready": ("is ready and waits for you.", "attention"), "approved": ("was approved.", "success"), "rejected": ("was rejected.", "danger"),
                 "": ("is writing its draft again.", "info")}
        text, tone = words[event["review"]]
        return f"{agent} {text}", tone
    if kind == "scheduled":
        return f"{agent} waits until {event['startAt']} to start. You can start it earlier from its card.", "info"
    if kind == "connectionLost":
        return f"{agent} lost its connection and is paused.", "warning"
    if kind == "resumed":
        return "The connection is back: the swarm goes on.", "success"
    if kind == "stopped":
        return "The swarm was stopped.", "warning"
    if kind == "joined":
        return f"{agent} joined the swarm ({event.get('role', '')}). Every agent was told.", "success"
    if kind == "removed":
        return f"{agent} left the swarm. Why: {(event.get('reason') or 'not given').rstrip('.')}. Every agent was told.", "warning"
    if kind == "retired":
        return f"{agent} is gone: what it left half done was put back, and its model freed its memory.", "info"
    if kind == "model":
        return f"{agent} now uses {event.get('model')}.", "info"
    return None, None


def describeRawValue(field, value):
    kind = field["kind"]
    if kind in ("secret", "accounts"):
        return [{"host": host, "user": login[0], "password": ""} for host, login in (value or {}).items()] if kind == "accounts" else ""
    if kind == "command":
        return (subprocess.list2cmdline(value) if os.name == "nt" else shlex.join(value)) if value else ""
    if kind in ("choice", "choices", "outlets", "publishers"):
        return value
    return "" if value is None else str(value)


def describeField(field, answers, previous=None):
    kind, default = field["kind"], getDefault(field, answers)
    view = {"key": field["key"], "ask": field["ask"], "kind": kind, "required": bool(field.get("required")), "help": getHelp(field, answers),
            "options": field.get("options"), "default": describeRawValue(field, default) if kind != "secret" else ""}
    if kind in ("secret", "accounts"):
        view["saved"] = bool(previous and previous.get(field["key"]))
    return view


def folderOf(path):
    folder = Path(path)
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return folder


# Where the model of a local agent is downloaded, and if there is room for it. The files are always the 16-bit ones, even for a compressed model.
def describeDownload(info):
    folder = getHubFolder()
    try:
        free = round(shutil.disk_usage(folderOf(folder)).free / 1e9)
    except OSError:
        free = None
    return {"downloaded": isDownloaded(info["name"]), "folder": str(folder), "size": round(info["billions"] * 2, 1), "free": free}


def describePrice(cost):
    if "error" in cost:
        return {"model": cost["model"], "error": cost["error"], "page": cost["page"]}
    return {key: cost.get(key) for key in ("model", "input", "output", "cachedInput", "context", "note", "page")}


def openInFileManager(path):
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def listPlaces():
    home = Path.home()
    places = [{"name": "Home", "path": str(home)}]
    places += [{"name": name, "path": str(home / name)} for name in ("Desktop", "Documents", "Downloads") if (home / name).is_dir()]
    if os.name == "nt":
        places += [{"name": f"Drive {letter}:", "path": f"{letter}:\\"} for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    else:
        places.append({"name": "Computer", "path": "/"})
    return places


# The folders (and files) inside a folder, for the browser of folders: a web page cannot read the paths of the computer by itself.
def browseFolder(text, files=False, hidden=False):
    folder = Path(text).expanduser() if text and text.strip() else Path.home()
    folder = folder.parent if folder.is_file() else folder
    folder = folderOf(folder).resolve()
    if not folder.is_dir():
        folder = Path.home()
    entries, problem = [], ""
    try:
        for entry in folder.iterdir():
            if (entry.name.startswith(".") and not hidden) or len(entries) >= BROWSE_LIMIT:
                continue
            try:
                isFolder = entry.is_dir()
            except OSError:
                continue
            if isFolder or (files and entry.is_file()):
                entries.append({"name": entry.name, "path": str(entry), "folder": isFolder})
    except OSError as error:
        problem = f"This folder cannot be read: {describeError(error)}."
    entries.sort(key=lambda entry: (not entry["folder"], entry["name"].lower()))
    parent = str(folder.parent) if folder.parent != folder else None
    return {"path": str(folder), "parent": parent, "entries": entries, "problem": problem, "places": listPlaces(), "separator": os.sep}


def catalog():
    tasks = [{"key": key, "label": task["label"], "role": task["role"], "info": task["info"], "folder": task["folder"], "name": task["name"]} for key, task in TASKS.items()]
    providers = {key: {**details, "models": MODELS_API[key], "pricing": PRICING_PAGES.get(key, "")} for key, details in API_KEYS.items()}
    return {"tasks": tasks, "providers": providers, "families": list(MODELS_LOCAL), "bits": list(BITS), "outlets": {group: list(names) for group, names in NEWS_OUTLETS.items()},
            "publishers": PAPER_PUBLISHERS, "messaging": {app: details["info"] for app, details in MESSAGING_APPS.items()}, "noMessenger": NO_MESSENGER,
            "defaultLoops": DEFAULT_LOOPS, "platform": {"os": sys.platform, "separator": os.sep, "home": str(Path.home())}, "user": USER_NAME,
            "codingAgents": {key: {"label": agent["label"], "company": agent["company"], "page": agent["page"], "models": agent["models"], "provider": agent["provider"]}
                             for key, agent in MODELS_CLI.items()}, "defaultCliModel": DEFAULT_CLI_MODEL,
            "leaderTask": {"key": "leader", "label": LEADER_TASK["label"], "role": LEADER_TASK["role"], "info": LEADER_TASK["info"], "folder": LEADER_TASK["folder"],
                           "name": LEADER_TASK["name"]}}


# ==============
# The session: everything the user built and everything the swarm says, for one window of the interface.
# The builder keeps the agents as specs: {id, task, name, answers (the folder included), model, client, waitsFor, description, missing}.
# The Swarm is built from them when it is about to run, and kept afterwards, so a plan that was approved can be executed.
# Rule for the locks: the lock of the session is never held while calling a method of the Swarm that takes its own lock,
# because the Swarm calls the listener of the session with its lock held.
# ==============
class Session:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.acting = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.feed = []
        self.feedNumber = 0
        self.questions = {}
        self.questionNumber = 0
        self.context = {}
        self.jobs = {}
        self.keys = {}
        self.tokens = {}
        self.agentNumber = 0
        self.unfinished = []
        self.cancelling = None
        self.closing = False
        self.dialog = None
        self.codexLogin = None
        self.reset()
        self.refreshUnfinished()
        self.actions = {name: getattr(self, name) for name in (
            "setMission", "taskForm", "saveAgent", "removeAgent", "undoRemove", "moveAgent", "checkEmail", "checkMessenger", "findChats", "searchPublishers",
            "browse", "pickPath", "setFolder", "openFolder", "modelCatalog", "lookupModel", "price", "chooseModel", "testModel", "checkPackages", "setOrder", "planOrder",
            "setMode", "start", "execute", "answer", "approve", "reject", "correct", "message", "startNow", "prepareStop", "stop", "clearJob", "newSwarm",
            "resumeForm", "resume", "prepareCancel", "abandon", "refreshUnfinished", "openLink", "codexAccount", "codexSignIn", "codexCancel", "joinLive",
            "removeLive", "setBuildMode", "buildWithLeader", "quit")}

    def reset(self):
        self.mission = ""
        self.buildMode = "manual"
        self.specs = []
        self.swarm = None
        self.dirty = True
        self.mode = "plan"
        self.order = "together"
        self.runInfo = {}
        self.trash = None

    def touch(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def addFeed(self, speaker, text, tone="info", kind="note", receiver=None):
        with self.lock:
            self.feedNumber += 1
            self.feed.append({"id": self.feedNumber, "time": f"{datetime.now():%H:%M:%S}", "speaker": speaker, "text": str(text), "tone": tone, "kind": kind,
                              "receiver": receiver})
            del self.feed[:-FEED_LIMIT]
            self.touch()

    # ---------- What the agents say and ask. Every loop of the swarm is connected to these. ----------
    def notify(self, name, message):
        found = re.match(r"^\[([^\]\n]+)\]\s*(.*)$", str(message), re.DOTALL)
        speaker, text = (found.group(1), found.group(2)) if found else (name, str(message))
        low = text.lower()
        tone = "warning" if low.startswith(("warning", "skipping", "could not", "stopped after")) or "could not" in low[:80] else "info"
        with self.lock:
            self.context.setdefault(speaker, []).append(text)
            del self.context[speaker][:-CONTEXT_LIMIT]
            self.addFeed(speaker, text, tone)

    # payload is what a coding agent asks: the permission it wants (kind permission), or its questions (kind form). The answer is then a dictionary.
    def ask(self, name, text, secret=False, kind=None, payload=None):
        kind, replies = describeQuestion(text, secret, kind)
        shown = self.readyRevisions() if kind == "review" else {}
        if kind == "review" and not shown:
            return "yes"
        event = threading.Event()
        # Once the swarm is stopped, a coding agent may still finish its turn and ask again: it is refused at once, so the swarm can end.
        if self.swarm is not None and self.swarm.stopped and self.swarm.isRunning():
            return ""
        with self.lock:
            if self.closing:
                return ""
            self.questionNumber += 1
            question = {"id": self.questionNumber, "speaker": name, "text": str(text), "secret": secret, "kind": kind, "replies": replies, "payload": payload,
                        "context": self.context.pop(name, []), "shown": shown, "event": event, "answer": "", "time": f"{datetime.now():%H:%M:%S}"}
            self.questions[question["id"]] = question
            self.touch()
        event.wait()
        return question["answer"]

    def connect(self, loop, name):
        loop.name = name
        loop.notifyUser = lambda message: self.notify(name, message)
        loop.askUser = lambda question: self.ask(name, question)
        loop.askSecret = lambda question: self.ask(name, question, secret=True)
        loop.askPermission = lambda request: self.ask(name, f"{name} wants to {request['action']}.", kind="permission", payload=request)
        loop.askQuestions = lambda questions: self.ask(name, f"{name} has {'a question' if len(questions) == 1 else 'questions'} for you.", kind="form", payload=questions)
        loop.askProposal = lambda proposal: self.ask(name, f"{name} proposes a swarm for your mission." if proposal["action"] == "build" else f"{name} proposes: {proposal['text']}.",
                                                     kind="proposal", payload=proposal)

    def readyRevisions(self):
        swarm = self.swarm
        if not swarm:
            return {}
        return {name: info["revision"] for name, info in ((name, swarm.getInfo(name)) for name in swarm.getAgents()) if info["review"] == "ready"}

    def release(self, question, answer, shown=True):
        question["answer"] = answer
        if shown:
            self.addFeed(USER_NAME, describeAnswer(question, answer), "info", "answer")
        question["event"].set()

    # A summary waits for an answer about drafts that the user already decided one by one. Nothing is left for it to decide, so it is
    # answered by itself and the leader goes on (the answer only applies to the drafts that still wait at the revision the summary showed).
    def dropAnsweredReviews(self):
        swarm = self.swarm
        if not swarm:
            return
        with self.lock:
            for question in list(self.questions.values()):
                if question["kind"] != "review" or not question["shown"]:
                    continue
                waiting = [name for name, revision in question["shown"].items() if swarm.getInfo(name)["review"] == "ready" and swarm.getInfo(name)["revision"] == revision]
                if not waiting:
                    self.questions.pop(question["id"])
                    self.release(question, "yes", shown=False)
            self.touch()

    def onEvent(self, event):
        if event["kind"] == "removed":
            self.dropSpec(event["agent"])
        if event["kind"] == "finished":
            self.dropProposals()
        text, tone = formatEvent(event)
        if event["kind"] == "message":
            self.addFeed(event["sender"], event["message"], "info", "message" if event["sender"] != USER_NAME else "answer", event["receiver"])
        elif text:
            self.addFeed("", text, tone, "event")
        if event["kind"] == "review":
            self.dropAnsweredReviews()
        self.touch()

    def dropProposals(self):
        with self.lock:
            for question in list(self.questions.values()):
                if question["kind"] == "proposal" and (question["payload"] or {}).get("action") != "build":
                    self.questions.pop(question["id"])
                    self.release(question, {"decision": "reject", "message": ""}, shown=False)

    # ---------- Long tasks run in their own thread, and the browser follows them in the state. ----------
    def runJob(self, name, title, work):
        with self.lock:
            if self.jobs.get(name, {}).get("state") == "running":
                raise ValueError("This is already in progress, please wait.")
            self.jobs[name] = {"title": title, "state": "running", "result": None, "error": ""}
            self.touch()
        def target():
            result, error = None, ""
            try:
                result = work()
            except USER_ERRORS as problem:
                error = str(problem)
            except Exception as problem:
                traceback.print_exc()
                error = f"Something went wrong: {type(problem).__name__}: {problem}"
            with self.lock:
                self.jobs[name].update(state="failed" if error else "done", result=result, error=error)
                self.touch()
        threading.Thread(target=target, daemon=True).start()

    def clearJob(self, payload):
        with self.lock:
            self.jobs.pop(payload.get("name"), None)

    def isBusy(self):
        return any(job["state"] == "running" for job in self.jobs.values()) or bool(self.swarm and self.swarm.isRunning())

    # An agent that is being added to the swarm while it runs (pending) can be prepared like in the steps: only it can change then.
    def checkIdle(self, spec=None):
        if self.swarm and self.swarm.isRunning() and not (spec and spec.get("pending")):
            raise ValueError("The swarm is running. Add or remove agents from the live view, or wait until it finishes to change it.")

    # The swarm that the agents of the steps are in, when it exists: agents join it and leave it directly, so its approved plans are kept.
    def liveSwarm(self):
        return self.swarm if self.swarm is not None and not self.dirty else None

    def dropSpec(self, name):
        with self.lock:
            self.specs = [spec for spec in self.specs if spec["name"] != name]
            for spec in self.specs:
                spec["waitsFor"] = [waited for waited in spec["waitsFor"] if waited != name]

    # ---------- The state that the browser draws. ----------
    def findSpec(self, agentId):
        spec = next((spec for spec in self.specs if spec["id"] == agentId), None)
        if spec is None:
            raise ValueError("This agent does not exist anymore.")
        return spec

    def describeSpec(self, spec, index):
        task, answers = getTask(spec["task"]), spec["answers"]
        return {"id": spec["id"], "name": spec["name"], "task": spec["task"], "label": task["label"], "role": task["role"], "description": spec["description"],
                "folder": answers.get("folder"), "folderNote": task["folder"], "suggestion": suggestFolder(answers), "model": describeModel(spec["model"]),
                "missing": spec["missing"], "isLeader": index == 0, "waitsFor": spec["waitsFor"], "loops": answers.get("numberOfLoops", DEFAULT_LOOPS),
                "tested": spec.get("tested", ""), "pending": bool(spec.get("pending")), "builder": spec["task"] == "leader", "why": spec.get("why", "")}

    def describeKeys(self):
        return {provider: {"company": details["company"], "variable": details["variable"], "page": details["page"],
                           "source": "typed" if self.keys.get(provider) else "environment" if getApiKey(provider) else None} for provider, details in API_KEYS.items()}

    def describeRun(self):
        swarm = self.swarm
        if not swarm or not self.runInfo:
            return None
        agents = []
        for name in swarm.getAgents():
            info, member = swarm.getInfo(name), swarm.getMember(name)
            client = member["agent"].agent
            agents.append({**info, "model": describeModel(info["model"]), "task": (member.get("recipe") or {}).get("task"), "description": info["task"],
                           "usage": dict(client.usage) if hasattr(client, "usage") else None, "removable": name != swarm.getLeader()})
        try:
            stages = swarm.getStages()
        except ValueError:
            stages = [[name for name in swarm.getAgents() if name != swarm.getLeader()]]
        running = swarm.isRunning()
        outcome = swarm.outcome
        if running:
            state = "running"
        elif "error" in outcome:
            state = "error"
        elif swarm.stopped:
            state = "stopped"
        elif outcome.get("result") is not None:
            state = "succeeded"
        else:
            state = "unfinished"
        return {"mission": swarm.mission, "mode": swarm.getMode(), "leader": swarm.getLeader(), "agents": agents, "stages": stages, "running": running, "state": state,
                "error": describeError(outcome["error"]) if "error" in outcome and not isinstance(outcome["error"], USER_ERRORS) else str(outcome.get("error", "")),
                "summary": swarm.getSummary(), "interruption": swarm.getInterruption(), "connections": swarm.getConnections(),
                "messages": swarm.getMessages()[-80:], "ready": swarm.getReadyAgents(), "resumed": self.runInfo.get("resume", False), **self.runInfo,
                "removed": list(swarm.removed), "canJoin": not running or self.canJoin(swarm),
                "vram": swarm.getVramStatus() if swarm.getNeededVram() else None}

    def describeGpus(self):
        needed = round(sum(spec["model"]["vram"] for spec in self.specs if spec["model"]), 1)
        gpus = readGpus()
        return checkVram(needed, gpus) if gpus or needed else None

    def describe(self):
        with self.lock:
            version = self.version
            questions = [{key: value for key, value in question.items() if key not in ("event", "answer", "shown")} for question in self.questions.values()]
            jobs = {name: dict(job) for name, job in self.jobs.items()}
            agents = [self.describeSpec(spec, index) for index, spec in enumerate(self.specs)]
            cancelling = {key: value for key, value in self.cancelling.items() if key != "swarm"} if self.cancelling else None
            codexLogin = {key: value for key, value in self.codexLogin.items() if key != "login"} if self.codexLogin else None
        return {"version": version, "mission": self.mission, "buildMode": self.buildMode, "agents": agents, "mode": self.mode, "order": self.order, "keys": self.describeKeys(),
                "gpu": self.describeGpus(), "run": self.describeRun(), "questions": questions, "jobs": jobs, "unfinished": self.unfinished, "busy": self.isBusy(),
                "nativeDialogs": self.dialog is not None, "trash": self.trash["spec"]["name"] if self.trash else None, "cancelling": cancelling,
                "codexLogin": codexLogin}

    # Waits until something changed after the version the browser has, then gives the state and the new lines of the feed.
    def poll(self, version, feedAfter):
        with self.changed:
            self.changed.wait_for(lambda: self.version != version or self.closing, timeout=POLL_SECONDS)
            feed = [item for item in self.feed if item["id"] > feedAfter]
        return {"state": self.describe(), "feed": feed}

    def act(self, name, payload):
        if name not in self.actions:
            raise ValueError(f"Unknown action: {name}.")
        if name in FREE_ACTIONS:
            result = self.actions[name](payload or {}) or {}
        else:
            with self.acting:
                result = self.actions[name](payload or {}) or {}
        self.touch()
        return {"ok": True, **result, "state": self.describe()}

    # ---------- Step 1: the mission and the agents. ----------
    def setMission(self, payload):
        mission = str(payload.get("mission", "")).strip()
        if not mission:
            raise FormError({"mission": "Write the mission of the swarm in a sentence or two."})
        if mission != self.mission:
            self.mission, self.dirty = mission, True
            leader = self.leaderSpec()
            if leader:
                leader["answers"]["mission"] = mission

    # The answers so far, as they are typed, to know which questions are asked and what their defaults and help are.
    def roughAnswers(self, task, values):
        answers = {}
        for field in getFields(task):
            if isAsked(field, answers):
                raw = values.get(field["key"])
                answers[field["key"]] = getDefault(field, answers) if raw in (None, "") else raw
        return answers

    def rawValues(self, spec):
        return {field["key"]: describeRawValue(field, spec["answers"].get(field["key"])) for field in getFields(spec["task"]) if field["key"] in spec["answers"]}

    def taskForm(self, payload):
        task = payload.get("task")
        if task == "leader":
            raise ValueError("The leader has no questions: it builds the swarm from your mission. Change the mission, its folder or its model instead.")
        if task not in TASKS:
            raise ValueError("Choose one of the tasks.")
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        previous = spec["answers"] if spec and spec["task"] == task else None
        values = payload.get("values")
        if values is None:
            values = self.rawValues(spec) if previous else {}
        answers = self.roughAnswers(task, values)
        fields = [{**describeField(field, answers, previous), "visible": isAsked(field, answers)} for field in TASKS[task]["fields"]]
        taken = [other["name"] for other in self.specs if other is not spec]
        return {"form": {"task": task, "label": TASKS[task]["label"], "info": TASKS[task]["info"], "fields": fields, "values": values,
                         "advanced": [describeField(field, answers) for field in ADVANCED_FIELDS], "name": spec["name"] if spec else suggestName(task, taken),
                         "agentId": spec["id"] if spec else None}}

    # The Telegram chat is found from the messages the user sent to the bot, when the user did not give it.
    def findTelegramChat(self, answers, errors):
        chat = answerKey("Telegram", "chat")
        if answers.get("messenger") != "Telegram" or answers.get(chat) or errors:
            return
        try:
            chats = findTelegramChats(answers[answerKey("Telegram", "token")])
        except (MessagingError, ConnectionLost) as error:
            errors[chat] = str(error)
            return
        if len(chats) == 1:
            answers[chat] = str(chats[0]["id"])
        else:
            errors[chat] = ("Open your bot in Telegram, press Start and send it any message, then press Find my chat." if not chats else
                            "Several chats wrote to your bot: press Find my chat and choose yours.")

    # live (from the live view) prepares an agent that joins the swarm afterwards (joinLive): until then it is pending, and the swarm does not change.
    def saveAgent(self, payload):
        task = payload.get("task")
        if task not in TASKS:
            raise ValueError("Choose one of the tasks.")
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        live = (spec.get("pending") if spec else bool(payload.get("live"))) and self.liveSwarm() is not None
        if not live:
            self.checkIdle()
        previous = spec["answers"] if spec and spec["task"] == task else None
        answers, errors = readAnswers(task, payload.get("values") or {}, previous)
        taken = [other["name"] for other in self.specs if other is not spec]
        name = str(payload.get("name") or "").strip() or suggestName(task, taken)
        if checkAgentName(name, taken):
            errors["name"] = checkAgentName(name, taken)
        self.findTelegramChat(answers, errors)
        if errors:
            raise FormError(errors)
        folder, warning = (spec["answers"].get("folder") if spec else None), ""
        try:
            loop = buildLoop(task, None, {**answers, "folder": folder})
        except ValueError as problem:
            if not folder:
                raise FormError({}, str(problem))
            folder, warning = None, f"The folder of this agent was removed: {problem}"
            loop = buildLoop(task, None, {**answers, "folder": None})
        answers["folder"] = folder
        if spec is None:
            self.agentNumber += 1
            spec = {"id": f"agent{self.agentNumber}", "model": None, "client": None, "waitsFor": [], "missing": [], "pending": live}
            self.specs.append(spec)
        elif spec["name"] != name:
            for other in self.specs:
                other["waitsFor"] = [name if waited == spec["name"] else waited for waited in other["waitsFor"]]
        spec.update(task=task, name=name, answers=answers, description=describeLoop(loop))
        self.dirty = self.dirty or not live
        return {"agentId": spec["id"], "description": spec["description"], "warning": warning}

    def removeAgent(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        if spec["task"] == "leader":
            raise ValueError("This leader builds your swarm. To build it yourself instead, choose it in the step of the mission.")
        if spec.get("pending"):
            self.specs.remove(spec)
            if hasattr(spec["client"], "unload"):
                spec["client"].unload()
            return
        self.checkIdle()
        index = self.specs.index(spec)
        self.specs.remove(spec)
        for other in self.specs:
            other["waitsFor"] = [name for name in other["waitsFor"] if name != spec["name"]]
        self.trash = {"spec": spec, "index": index}
        self.dirty = True
        self.sanitizeWaits()

    def undoRemove(self, payload):
        self.checkIdle()
        if not self.trash:
            raise ValueError("There is nothing to bring back.")
        spec, index = self.trash["spec"], self.trash["index"]
        if checkAgentName(spec["name"], [other["name"] for other in self.specs]):
            raise ValueError(f"Another agent is called {spec['name']} now, so it cannot come back.")
        self.specs.insert(min(index, len(self.specs)), spec)
        self.trash, self.dirty = None, True
        self.sanitizeWaits()

    # The first agent is the leader. It works last, so nobody can wait for it, and it waits for nobody.
    def sanitizeWaits(self):
        if not self.specs:
            return
        leader = self.specs[0]["name"]
        names = [spec["name"] for spec in self.specs]
        self.specs[0]["waitsFor"] = []
        for spec in self.specs[1:]:
            spec["waitsFor"] = [name for name in spec["waitsFor"] if name in names and name not in (leader, spec["name"])]

    def moveAgent(self, payload):
        self.checkIdle()
        spec = self.findSpec(payload.get("agentId"))
        position = max(0, min(int(payload.get("position", 0)), len(self.specs) - 1))
        if self.leaderSpec() and (spec["task"] == "leader" or position == 0):
            raise ValueError("The leader that builds the swarm stays first: it leads it.")
        self.specs.remove(spec)
        self.specs.insert(position, spec)
        self.dirty = True
        self.sanitizeWaits()

    def checkEmail(self, payload):
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        answers, errors = readAnswers("email", payload.get("values") or {}, spec["answers"] if spec and spec["task"] == "email" else None)
        needed = {key: error for key, error in errors.items() if key in ("provider", "sender", "password", "smtp", "imap")}
        if needed:
            raise FormError(needed, "Fill in the account first.")
        return {"problem": checkEmailLogin(answers["sender"], answers["password"], answers["smtp"], answers["imap"])}

    def messengerAnswers(self, payload):
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        answers, errors = readAnswers("news", payload.get("values") or {}, spec["answers"] if spec and spec["task"] == "news" else None)
        app = answers.get("messenger")
        if app not in MESSAGING_APPS:
            raise ValueError("Choose a messaging app first.")
        return app, answers, errors

    def checkMessenger(self, payload):
        app, answers, errors = self.messengerAnswers(payload)
        needed = {key: error for key, error in errors.items() if key.startswith(app.lower())}
        if needed:
            raise FormError(needed, f"Fill in what {app} needs first.")
        return {"problem": checkMessenger(app, messengerSettings(answers))}

    def findChats(self, payload):
        app, answers, errors = self.messengerAnswers(payload)
        token = answerKey("Telegram", "token")
        if token in errors:
            raise FormError({token: errors[token]}, "Give the token of your bot first.")
        return {"chats": [{"id": str(chat["id"]), "name": chat["name"]} for chat in findTelegramChats(answers[token])]}

    def searchPublishers(self, payload):
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Write the name of the publisher.")
        try:
            found = findPublishers(name)
        except FETCH_ERRORS as error:
            raise ValueError(f"Crossref could not be asked: {describeError(error)}.") from None
        return {"publishers": [{"name": publisher["name"], "id": str(publisher["id"]), "papers": publisher["papers"]} for publisher in found]}

    def browse(self, payload):
        return {"browse": browseFolder(str(payload.get("path") or ""), bool(payload.get("files")), bool(payload.get("hidden")))}

    # The dialog of the system, when the window has one. The browser shows its own browser of folders otherwise.
    def pickPath(self, payload):
        kind = payload.get("kind")
        if not self.dialog or kind not in ("folder", "file", "path"):
            raise ValueError("The dialogs of the system are not available in this window.")
        return {"path": self.dialog(kind, str(payload.get("path") or ""))}

    # ---------- Step 2: the folder of each agent. ----------
    def setFolder(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        self.checkIdle(spec)
        text, folder = str(payload.get("folder") or "").strip(), None
        if text:
            folder, error = parseAnswer({"key": "folder", "ask": "", "kind": "folder"}, text)
            if error:
                raise FormError({"folder": error}, error)
        try:
            loop = buildLoop(spec["task"], None, {**spec["answers"], "folder": folder})
        except ValueError as problem:
            raise FormError({"folder": str(problem)}, str(problem)) from None
        spec["answers"]["folder"], spec["description"] = folder, describeLoop(loop)
        self.dirty = self.dirty or not spec.get("pending")

    def openFolder(self, payload):
        path = str(payload.get("path") or "")
        known = {str(spec["answers"].get("folder")) for spec in self.specs if spec["answers"].get("folder")}
        known |= {suggestFolder(spec["answers"]) for spec in self.specs if suggestFolder(spec["answers"])}
        if path not in known or not Path(path).is_dir():
            raise ValueError("Only the folders of the agents can be opened.")
        openInFileManager(path)

    # ---------- Step 3: the model of each agent. ----------
    # A swarm with the models of the other agents, to check a model with the same rules as the swarm that will run.
    def sizingSwarm(self, excluding=None):
        swarm = Swarm(self.mission or "sizing")
        for spec in self.specs:
            if spec["id"] != excluding and spec["model"]:
                swarm.members[spec["name"]] = swarm.newMember(spec["name"], Loop(None), "", "", None, [], spec["model"], None)
        return swarm

    def localEntry(self, sizing, name, bits, billions=None):
        info = getModelInfo(name, bits=bits, billions=billions)
        check = sizing.checkModel(info)
        status = "fits" if check["allowed"] and not check["message"] else "busy" if check["allowed"] else "tooBig"
        return {"name": name, "billions": info["billions"], "vram": info["vram"], "bits": bits, "gated": isGated(name), "status": status,
                "message": check["message"], "downloaded": isDownloaded(name)}

    def modelCatalog(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        bits = int(payload.get("bits") or 16)
        if bits not in BITS:
            raise ValueError(f"The models are used with {', '.join(str(option) for option in BITS)} bits.")
        sizing = self.sizingSwarm(spec["id"])
        recommend = getTask(spec["task"])["recommend"]
        families = {family: [self.localEntry(sizing, name, bits) for name in models] for family, models in MODELS_LOCAL.items()}
        local = [self.localEntry(sizing, name, bits) for name in RECOMMENDED_LOCAL[recommend]]
        api = [{"name": name, "provider": provider, "company": API_KEYS[provider]["company"]} for name in RECOMMENDED_API[recommend]
               for provider in [next(key for key, models in MODELS_API.items() if name in models)]]
        gpu = checkVram(sizing.getNeededVram(), readGpus())
        cli = {"claude-code": {"missing": findMissingPackages(getModelInfo("", cli="claude-code")), "problem": ""}, "codex": checkCodex()}
        return {"catalog": {"agentId": spec["id"], "bits": bits, "local": local, "families": families, "api": api, "gpu": gpu, "isLeader": self.specs[0] is spec,
                            "hfToken": bool(os.environ.get("HF_TOKEN")), "current": describeModel(spec["model"]), "cli": cli}}

    def lookupModel(self, payload):
        name = str(payload.get("name", "")).strip()
        if name.count("/") != 1 or " " in name:
            raise FormError({"name": "It is written owner/name, like Qwen/Qwen3-8B."}, "It is written owner/name, like Qwen/Qwen3-8B.")
        listed = next((family[name] for family in MODELS_LOCAL.values() if name in family), None)
        found = {"billions": listed, "gated": isGated(name)} if listed else lookupHuggingFace(name)
        return {"model": {"name": name, "billions": found["billions"], "gated": found["gated"] or isGated(name)}}

    def price(self, payload):
        name = str(payload.get("name", "")).strip()
        if payload.get("cli") == "codex":
            raise ValueError("Codex uses your ChatGPT plan: there is no price per use, only the limits of your plan.")
        info = getModelInfo(name, provider=payload.get("provider"), cli=payload.get("cli"))
        if info["local"]:
            raise ValueError("Local models are free to use: they run on your GPUs.")
        return {"price": describePrice(getModelCost(info["provider"], name))}

    def chooseModel(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        self.checkIdle(spec)
        name = str(payload.get("name", "")).strip()
        if payload.get("cli"):
            info = getModelInfo(name, cli=payload["cli"])
            if info["cli"] == "codex":
                self.checkCodexReady()
        elif payload.get("local"):
            billions = float(payload["billions"]) if payload.get("billions") not in (None, "") else None
            if billions is not None and billions <= 0:
                raise FormError({"billions": "Write a number above 0, like 8.2."})
            info = getModelInfo(name, bits=int(payload.get("bits") or 16), billions=billions)
        else:
            info = getModelInfo(name, provider=payload.get("provider"))
            if info["local"]:
                raise ValueError(f"{name} is a model of Hugging Face: choose it in the local models.")
        # An agent about to join is checked against the swarm as it is (the agents that left freed their memory).
        check = (self.liveSwarm() if spec.get("pending") else self.sizingSwarm(spec["id"])).checkModel(info)
        if not check["allowed"]:
            raise ValueError(check["message"])
        if info["local"]:
            token = str(payload.get("hfToken") or "").strip()
            if token:
                self.tokens[name] = token
        elif info["provider"]:
            key = str(payload.get("apiKey") or "").strip()
            if key:
                self.keys[info["provider"]] = key
            if not (self.keys.get(info["provider"]) or getApiKey(info["provider"])):
                company = API_KEYS[info["provider"]]["company"]
                raise FormError({"apiKey": f"{company} needs an API key. Create one at {API_KEYS[info['provider']]['page']}."}, f"{company} needs an API key.")
        client = createModel(info, self.keys, token=self.tokens.get(name), report=lambda message: self.notify(spec["name"], message))
        if hasattr(spec["client"], "unload"):
            spec["client"].unload()
        spec.update(model=info, client=client, missing=findMissingPackages(info), tested="")
        self.dirty = self.dirty or not spec.get("pending")
        return {"warning": check["message"] if info["local"] else "", "missing": spec["missing"], "download": describeDownload(info) if info["local"] else None}

    # ---------- Agents that join or leave the swarm while it runs (or between two runs, keeping what was approved). ----------
    # A new agent joins the group that works now (see Swarm.addAgent): it can wait for agents already there, and nobody waits for it.
    def canJoin(self, swarm):
        stage = swarm.stage
        return bool(stage and stage["open"] and (stage["plan"] or swarm.getLeader() not in stage["names"]))

    def joinLive(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        swarm = self.liveSwarm()
        if not spec.get("pending"):
            raise ValueError(f"{spec['name']} is already in the swarm.")
        if swarm is None:
            raise ValueError("There is no swarm to join: add the agent in the steps instead.")
        if not spec["model"]:
            raise FormError({"model": f"Choose a model for {spec['name']} first."}, f"Choose a model for {spec['name']} first.")
        if payload.get("folder") is not None:
            self.setFolder({"agentId": spec["id"], "folder": payload.get("folder")})
        waits = [name for name in payload.get("waitsFor") or [] if name in swarm.getAgents() and name != swarm.getLeader()]
        loop = buildLoop(spec["task"], spec["client"], spec["answers"])
        self.connect(loop, spec["name"])
        recipe = {"task": spec["task"], "answers": publicAnswers(spec["task"], spec["answers"])}
        swarm.addAgent(spec["name"], loop, TASKS[spec["task"]]["role"], describeLoop(loop), waitsFor=waits, model=spec["model"], recipe=recipe)
        spec.update(pending=False, waitsFor=waits, description=describeLoop(loop))

    # The agent leaves at once (see Swarm.removeAgent); its model frees its memory as soon as its last step ended.
    def removeLive(self, payload):
        swarm = self.liveSwarm()
        if swarm is None:
            raise ValueError("There is no swarm: remove the agent in the steps instead.")
        name = str(payload.get("agent") or "")
        reason = str(payload.get("reason") or "").strip() or "The user removed it."
        swarm.removeAgent(name, reason)
        self.dropSpec(name)

    def testModel(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        if not spec["client"]:
            raise ValueError("Choose a model first.")
        if spec["model"]["local"]:
            raise ValueError("A local model is tested when the swarm starts: it is loaded on the GPUs then.")
        if spec["model"].get("cli") == "codex":
            account = self.checkCodexReady()
            return {"problem": "", "answer": f"Codex is signed in{' as ' + account['email'] if account.get('email') else ''}."}
        try:
            answer = spec["client"].input(TEST_PROMPT)
        except ModelError as error:
            spec["tested"] = "failed"
            return {"problem": str(error)}
        spec["tested"] = "ok"
        return {"problem": "", "answer": " ".join(str(answer).split())[:80]}

    # ---------- Codex: signed in with the ChatGPT plan of the user, through Codex itself. ----------
    # The sign-in runs in its own thread: the page (or the code) is shown to the user, and the state says when Codex received the answer.
    def checkCodexReady(self):
        status = checkCodex()
        if status["problem"]:
            raise ValueError(status["problem"])
        account = readCodexAccount()
        if not account["signedIn"]:
            raise FormError({"codex": "Sign in to Codex with your ChatGPT account first."}, "Sign in to Codex with your ChatGPT account first.")
        return account

    def codexAccount(self, payload):
        status = checkCodex()
        if status["problem"]:
            return {"codex": {**status, "account": None, "models": []}}
        account = readCodexAccount()
        models = listCodexModels() if account["signedIn"] else []
        return {"codex": {**status, "account": account, "models": models}}

    def codexSignIn(self, payload):
        method = "code" if payload.get("method") == "code" else "browser"
        with self.lock:
            if self.codexLogin and self.codexLogin["state"] == "waiting":
                self.codexLogin["login"].cancel()
        login = CodexLogin()
        try:
            target = login.start(method)
        except ModelError:
            login.cancel()
            raise
        state = {"state": "waiting", "method": method, "url": target["url"], "code": target["code"], "error": "", "account": None, "login": login}
        with self.lock:
            self.codexLogin = state
        if method == "browser":
            webbrowser.open(target["url"])
        def wait():
            error = login.wait()
            account = None
            if not error:
                try:
                    account = readCodexAccount()
                except ModelError as problem:
                    error = str(problem)
            with self.lock:
                if self.codexLogin is state:
                    state.update(state="failed" if error else "done", error=error, account=account)
                self.touch()
        threading.Thread(target=wait, daemon=True).start()

    def codexCancel(self, payload):
        with self.lock:
            state, self.codexLogin = self.codexLogin, None
        if state and state["state"] == "waiting":
            state["login"].cancel()

    def checkPackages(self, payload):
        for spec in self.specs:
            if spec["model"]:
                spec["missing"] = findMissingPackages(spec["model"])

    # ---------- The leader builds the swarm, and manages it while it runs (leader_utils.py). ----------
    # In the mode leader, the first agent is the leader of LEADER_TASK: the user gives it a model and the folder of the mission (setFolder and
    # chooseModel, like any agent), then the leader proposes the other agents. Once the user approved, they are agents of the steps like any
    # other, so the user can still look at them and change them before the run.
    def leaderSpec(self):
        return self.specs[0] if self.specs and self.specs[0]["task"] == "leader" else None

    def setBuildMode(self, payload):
        self.checkIdle()
        mode = payload.get("mode")
        if mode not in ("manual", "leader"):
            raise ValueError("Choose who builds the swarm: you, or the leader.")
        if mode == self.buildMode:
            return
        if any(job["state"] == "running" for name, job in self.jobs.items() if name == "leader"):
            raise ValueError("The leader is building the swarm: wait until it proposes it.")
        self.buildMode, self.dirty = mode, True
        if mode == "leader":
            self.agentNumber += 1
            answers = {"mission": self.mission, "folder": None, "numberOfLoops": DEFAULT_LOOPS}
            self.specs.insert(0, {"id": f"agent{self.agentNumber}", "task": "leader", "name": suggestName("leader", [spec["name"] for spec in self.specs]), "answers": answers,
                                  "model": None, "client": None, "waitsFor": [], "missing": [], "pending": False,
                                  "description": describeLoop(buildLoop("leader", None, answers))})
        else:
            leader = self.leaderSpec()
            if leader:
                self.specs.remove(leader)
                if hasattr(leader["client"], "unload"):
                    leader["client"].unload()
        self.trash = None
        self.sanitizeWaits()

    def buildWithLeader(self, payload):
        self.checkIdle()
        leader = self.leaderSpec()
        if leader is None:
            raise ValueError("Choose to let the leader build the swarm first.")
        if not self.mission:
            raise FormError({"mission": "Write the mission of the swarm first."}, "Write the mission of the swarm first.")
        if not leader["answers"].get("folder"):
            raise FormError({"folder": "Choose the folder of the mission: the agents work in it."}, "Choose the folder of the mission first.")
        if not leader["model"]:
            raise FormError({"model": f"Choose the model of {leader['name']} first."}, f"Choose the model of {leader['name']} first.")
        self.makeClients()
        catalog = LeaderCatalog(self.mission, leader["answers"]["folder"], leader["name"], leader["model"], self.keys, self.tokens)
        loop = buildLoop("leader", leader["client"], {**leader["answers"], "mission": self.mission})
        self.connect(loop, leader["name"])
        def work():
            agents = designSwarm(loop, catalog)
            if agents is None:
                return None
            self.applyPlan(leader, agents)
            return {"agents": [agent["name"] for agent in agents]}
        self.runJob("leader", f"{leader['name']} is building the swarm", work)

    # The agents the user approved replace the ones of the steps (the leader stays). Their models are made now when nothing more is needed.
    def applyPlan(self, leader, agents):
        made = []
        for agent in agents:
            info = agent["model"]
            client = createModel(info, self.keys, token=self.tokens.get(info["name"]), report=lambda message, name=agent["name"]: self.notify(name, message)) \
                if hasCredentials(info, self.keys) else None
            loop = buildLoop(agent["task"], None, agent["answers"])
            self.agentNumber += 1
            made.append({"id": f"agent{self.agentNumber}", "task": agent["task"], "name": agent["name"], "answers": agent["answers"], "model": info, "client": client,
                         "waitsFor": list(agent["waitsFor"]), "missing": findMissingPackages(info), "description": describeLoop(loop), "pending": False, "tested": "",
                         "why": agent["why"]})
        with self.lock:
            if self.leaderSpec() is not leader or (self.swarm and self.swarm.isRunning()):
                for spec in made:
                    if hasattr(spec["client"], "unload"):
                        spec["client"].unload()
                raise ValueError("The swarm changed while the leader was building it, so its proposal was not used.")
            for spec in self.specs[1:]:
                if hasattr(spec["client"], "unload"):
                    spec["client"].unload()
            self.specs = [leader, *made]
            self.order = "custom" if any(spec["waitsFor"] for spec in made) else "together"
            self.dirty, self.trash = True, None
            self.touch()

    def manage(self, swarm, leader):
        LeaderManager(swarm, LeaderCatalog(self.mission, leader["answers"].get("folder"), leader["name"], leader["model"], self.keys, self.tokens), self)

    # What the manager of the leader needs: the loop of a new agent, or of an agent with another model (LeaderManager in leader_utils.py).
    def makeLoop(self, agent):
        info = agent["model"]
        client = createModel(info, self.keys, token=self.tokens.get(info["name"]), report=lambda message: self.notify(agent["name"], message))
        loop = buildLoop(agent["task"], client, agent["answers"])
        self.connect(loop, agent["name"])
        return loop

    def joined(self, agent, loop):
        with self.lock:
            self.agentNumber += 1
            self.specs.append({"id": f"agent{self.agentNumber}", "task": agent["task"], "name": agent["name"], "answers": agent["answers"], "model": agent["model"],
                               "client": loop.agent, "waitsFor": list(agent["waitsFor"]), "missing": findMissingPackages(agent["model"]), "description": describeLoop(loop),
                               "pending": False, "tested": "", "why": agent["why"]})
        self.touch()

    def remakeLoop(self, name, model):
        spec = next((spec for spec in self.specs if spec["name"] == name), None)
        if spec is None:
            raise ValueError(f"{name} is not an agent of the swarm anymore.")
        return self.makeLoop({"name": name, "task": spec["task"], "answers": spec["answers"], "model": model})

    def remade(self, name, model, loop):
        with self.lock:
            for spec in self.specs:
                if spec["name"] == name:
                    spec.update(model=model, client=loop.agent, missing=findMissingPackages(model), tested="")
        self.touch()

    # ---------- Step 4: who waits for whom. ----------
    def setOrder(self, payload):
        self.checkIdle()
        order = payload.get("order")
        if order not in ("together", "custom", "leader"):
            raise ValueError("Choose how the agents work together.")
        names = [spec["name"] for spec in self.specs]
        waits = {spec["name"]: [] for spec in self.specs[1:]}
        if order == "custom":
            for name, others in (payload.get("waits") or {}).items():
                if name in waits:
                    waits[name] = [other for other in others if other in waits and other != name]
        elif order == "leader":
            waits = {spec["name"]: list(spec["waitsFor"]) for spec in self.specs[1:]}
        try:
            Swarm("").findStages(waits)
        except ValueError as error:
            raise ValueError(f"{error}. Change who waits for whom.") from None
        for spec in self.specs[1:]:
            spec["waitsFor"] = waits[spec["name"]]
        self.order, self.dirty = order, True
        return {"stages": Swarm("").findStages(waits) if names else []}

    def buildSwarm(self):
        if not self.specs:
            raise ValueError("Add at least one agent.")
        if not self.mission:
            raise ValueError("Write the mission of the swarm first.")
        missing = [spec["name"] for spec in self.specs if not spec["model"]]
        if missing:
            raise ValueError(f"Choose a model for {', '.join(missing)} first.")
        self.makeClients()
        leader = self.leaderSpec()
        if leader and not leader["answers"].get("folder"):
            raise ValueError(f"Choose the folder of the mission for {leader['name']} first.")
        if leader and len(self.specs) == 1:
            raise ValueError(f"Ask {leader['name']} to build the swarm first, in the step of the mission.")
        swarm = Swarm(self.mission)
        for spec in self.specs:
            loop = buildLoop(spec["task"], spec["client"], spec["answers"])
            self.connect(loop, spec["name"])
            recipe = {"task": spec["task"], "answers": publicAnswers(spec["task"], spec["answers"])}
            swarm.addAgent(spec["name"], loop, getTask(spec["task"])["role"], describeLoop(loop), model=spec["model"], recipe=recipe)
        for spec in self.specs[1:]:
            swarm.setWaitsFor(spec["name"], spec["waitsFor"])
        swarm.addListener(self.onEvent)
        if leader:
            self.manage(swarm, leader)
        return swarm

    # A model of the steps that has no client yet (the leader chose it before its API key was given) gets one now.
    def makeClients(self):
        for spec in self.specs:
            if spec["model"] and spec["client"] is None:
                if not hasCredentials(spec["model"], self.keys):
                    company = API_KEYS[spec["model"]["provider"]]["company"]
                    raise ValueError(f"{spec['name']} needs an API key of {company}: choose its model again in the step of the models to give it.")
                spec["client"] = createModel(spec["model"], self.keys, token=self.tokens.get(spec["model"]["name"]), report=lambda message, name=spec["name"]: self.notify(name, message))

    def currentSwarm(self):
        if self.swarm is None or self.dirty:
            self.swarm, self.runInfo, self.dirty = self.buildSwarm(), {}, False
        return self.swarm

    def planOrder(self, payload):
        self.checkIdle()
        if len(self.specs) < 2:
            raise ValueError("With one agent, nobody waits for anybody.")
        # A swarm that was stopped refuses the questions of its agents, so the leader plans in a new one.
        self.dirty = self.dirty or bool(self.swarm and self.swarm.stopped)
        swarm = self.currentSwarm()
        def work():
            approved = swarm.planWithLeader()
            if approved:
                for spec in self.specs[1:]:
                    spec["waitsFor"] = list(swarm.getMember(spec["name"])["waitsFor"])
                self.order = "leader"
            return approved
        self.runJob("order", "The leader is working out who waits for whom", work)

    # ---------- Step 5: the run of the swarm. ----------
    def setMode(self, payload):
        mode = payload.get("mode")
        if mode not in ("plan", "execute"):
            raise ValueError("The mode is plan or execute.")
        self.mode = mode

    def watchRun(self, swarm):
        swarm.wait()
        with self.lock:
            if self.swarm is swarm:
                self.runInfo["finishedAt"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            self.touch()

    def launch(self, swarm, resume=False):
        swarm.getStages()
        swarm.checkGpus()
        with self.lock:
            self.runInfo = {"startedAt": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "finishedAt": None, "resume": resume}
            self.context = {}
        swarm.startInBackground(resume)
        threading.Thread(target=self.watchRun, args=(swarm,), daemon=True).start()

    def start(self, payload):
        if self.isBusy():
            raise ValueError("Wait until the work in progress is finished.")
        if payload.get("mode"):
            self.setMode(payload)
        swarm = self.currentSwarm()
        swarm.setMode(self.mode)
        self.launch(swarm)

    # The plans were approved: the same swarm executes them, so every agent follows its approved plan.
    def execute(self, payload):
        if not self.swarm or self.isBusy():
            raise ValueError("There is no finished swarm to execute.")
        self.mode = "execute"
        self.swarm.setMode("execute")
        self.launch(self.swarm)

    def answer(self, payload):
        with self.lock:
            question = self.questions.get(int(payload.get("id", 0)))
            if question is None:
                raise ValueError("This question was already answered.")
            answer = payload.get("answer", "")
            # A coding agent receives a dictionary (its permission or its answers), and so does the leader for its proposals. The other questions
            # receive a text. A wrong answer leaves the question waiting.
            if question["kind"] in ("permission", "form", "proposal"):
                if not isinstance(answer, dict):
                    raise ValueError("This question needs a choice.")
                if question["kind"] == "proposal" and answer.get("decision") not in ("approve", "reject"):
                    raise ValueError("Approve the proposal, or reject it.")
            else:
                answer = str(answer)
            self.questions.pop(question["id"])
            self.release(question, answer)

    def runningSwarm(self):
        if not self.swarm:
            raise ValueError("There is no swarm.")
        return self.swarm

    def approve(self, payload):
        self.runningSwarm().approveDraft(payload.get("agent"), payload.get("revision"))

    def reject(self, payload):
        self.runningSwarm().rejectDraft(payload.get("agent"), payload.get("revision"))

    def correct(self, payload):
        self.runningSwarm().correctDraft(payload.get("agent"), str(payload.get("text", "")))

    def message(self, payload):
        self.runningSwarm().sendUserMessage(payload.get("agent"), str(payload.get("text", "")))

    def startNow(self, payload):
        self.runningSwarm().startNow(payload.get("agent"))

    # The user wants to stop the swarm: the leader first says what was already changed, and the user confirms with stop.
    def prepareStop(self, payload):
        swarm = self.runningSwarm()
        if not swarm.isRunning():
            raise ValueError("The swarm is not running.")
        self.runJob("changes", "The leader is listing what the swarm already changed", swarm.summarizeChanges)

    # The questions that wait are answered with nothing, so the agents that wait for the user see that the swarm stopped.
    def stop(self, payload):
        swarm = self.runningSwarm()
        swarm.stopWork()
        with self.lock:
            for question in list(self.questions.values()):
                self.questions.pop(question["id"])
                self.release(question, "", shown=False)
            self.jobs.pop("changes", None)

    def newSwarm(self, payload):
        self.checkIdle()
        if any(job["state"] == "running" for job in self.jobs.values()):
            raise ValueError("Wait until the work in progress is finished.")
        self.unloadModels()
        with self.lock:
            self.reset()
            self.jobs, self.feed, self.context = {}, [], {}
        self.refreshUnfinished()

    def unloadModels(self):
        for spec in self.specs:
            if hasattr(spec["client"], "unload"):
                spec["client"].unload()

    # ---------- The swarms that were interrupted. ----------
    def refreshUnfinished(self, payload=None):
        found = []
        for saved in findUnfinishedSwarms():
            members = [{"name": name, "role": member["role"], "status": member["status"], "task": (member.get("recipe") or {}).get("task")}
                       for name, member in saved["members"].items()]
            found.append({"id": saved["id"], "mission": saved["mission"], "savedAt": saved["savedAt"], "mode": saved["mode"], "running": saved["running"],
                          "reason": "The connection was lost" if saved["state"] == "paused" else "The program or the computer stopped",
                          "canResume": all(member.get("recipe") for member in saved["members"].values()), "members": members, "leader": saved["leader"]})
        self.unfinished = found

    def findSaved(self, swarmId):
        saved = next((saved for saved in findUnfinishedSwarms() if saved["id"] == swarmId), None)
        if saved is None:
            raise ValueError("This swarm is not saved anymore.")
        if saved["running"]:
            raise ValueError("This swarm is working in another window of the program. If that window was closed a few seconds ago, try again in half a minute.")
        return saved

    # What the user must give again to continue a swarm: the secrets of every agent (never saved), the API keys and the Hugging Face tokens.
    def resumeForm(self, payload):
        saved = self.findSaved(payload.get("id"))
        if not all(member.get("recipe") for member in saved["members"].values()):
            raise ValueError("This swarm was made by another program, so it cannot be continued here.")
        agents, providers, gated, codex = [], {}, [], False
        for name, member in saved["members"].items():
            task, answers, finished = member["recipe"]["task"], member["recipe"]["answers"], member["status"] in ("done", "failed")
            fields = [{**describeField(field, answers), "required": field.get("required", False) and not finished} for field in secretFields(task, answers)]
            info = member["model"]
            agents.append({"name": name, "role": member["role"], "task": task, "status": member["status"], "finished": finished, "fields": fields,
                           "accounts": task == "literature" and not finished, "model": describeModel(info), "missing": findMissingPackages(info) if info else []})
            if info and not info["local"] and info["provider"] and not finished:
                providers[info["provider"]] = self.describeKeys()[info["provider"]]
            codex = codex or bool(info and info.get("cli") == "codex" and not finished)
            if info and info["local"] and isGated(info["name"]) and not os.environ.get("HF_TOKEN") and not finished:
                gated.append(info["name"])
        return {"resume": {"id": saved["id"], "mission": saved["mission"], "mode": saved["mode"], "agents": agents, "providers": providers, "gated": gated,
                           "codex": codex}}

    def rebuildAgent(self, name, data, secretValues, rebuilt):
        recipe, finished = data["recipe"], data["status"] in ("done", "failed")
        task, values = recipe["task"], secretValues.get(name) or {}
        secrets, errors = {}, {}
        for field in secretFields(task, recipe["answers"]):
            text = str(values.get(field["key"]) or "")
            if not text and field.get("required") and not finished:
                errors[f"{name}.{field['key']}"] = "This is needed to continue."
            secrets[field["key"]] = text
        if task == "literature":
            secrets["accounts"] = {str(row.get("host", "")).strip(): (str(row.get("user", "")).strip(), str(row.get("password", ""))) for row in values.get("accounts") or []
                                   if str(row.get("host", "")).strip() and str(row.get("user", "")).strip()}
        info, client = data["model"], None
        usable = info and hasCredentials(info, self.keys)
        if info and not usable and not finished:
            errors[f"key.{info['provider']}"] = f"{API_KEYS[info['provider']]['company']} needs an API key."
        if errors:
            raise FormError(errors)
        if usable:
            client = createModel(info, self.keys, token=self.tokens.get(info["name"]), report=lambda message: self.notify(name, message))
        answers = restoreAnswers(task, recipe["answers"], secrets)
        loop = buildLoop(task, client, answers)
        self.connect(loop, name)
        self.agentNumber += 1
        rebuilt.append({"id": f"agent{self.agentNumber}", "task": task, "name": name, "answers": answers, "model": info, "client": client,
                        "waitsFor": list(data["waitsFor"]), "missing": findMissingPackages(info) if info else [], "description": describeLoop(loop)})
        return loop

    def rememberKeys(self, payload):
        self.keys.update({provider: str(key).strip() for provider, key in (payload.get("keys") or {}).items() if provider in API_KEYS and str(key).strip()})
        self.tokens.update({name: str(token).strip() for name, token in (payload.get("tokens") or {}).items() if str(token).strip()})

    def resume(self, payload):
        if self.isBusy():
            raise ValueError("Wait until the work in progress is finished.")
        saved = self.findSaved(payload.get("id"))
        self.rememberKeys(payload)
        if any((member.get("model") or {}).get("cli") == "codex" and member["status"] not in ("done", "failed") for member in saved["members"].values()):
            self.checkCodexReady()
        rebuilt = []
        swarm = Swarm.restore(saved, lambda name, data: self.rebuildAgent(name, data, payload.get("secrets") or {}, rebuilt))
        swarm.addListener(self.onEvent)
        self.unloadModels()
        self.reset()
        rebuilt.sort(key=lambda spec: spec["name"] != saved["leader"])
        self.mission, self.specs, self.mode, self.swarm, self.dirty = saved["mission"], rebuilt, saved["mode"], swarm, False
        if saved.get("managed") and self.leaderSpec():
            self.buildMode = "leader"
            self.manage(swarm, self.leaderSpec())
        self.order = "custom" if any(spec["waitsFor"] for spec in rebuilt) else "together"
        self.cancelling = None
        self.launch(swarm, resume=True)
        self.refreshUnfinished()

    # Before a saved swarm is cancelled, the leader says what it already changed. It writes that with its model if it can be made again.
    def prepareCancel(self, payload):
        saved = self.findSaved(payload.get("id"))
        self.rememberKeys(payload)
        def rebuild(name, data):
            recipe = data.get("recipe") or {}
            try:
                loop = buildLoop(recipe["task"], None, {**restoreAnswers(recipe["task"], recipe["answers"]), "folder": None})
            except (KeyError, ValueError, OSError):
                loop = Loop(None)
            self.connect(loop, name)
            return loop
        swarm = Swarm.restore(saved, rebuild)
        info = saved["members"][saved["leader"]]["model"]
        if info and not payload.get("plain") and hasCredentials(info, self.keys):
            swarm.getMember(swarm.getLeader())["agent"].agent = createModel(info, self.keys, token=self.tokens.get(info["name"]))
        def work():
            summary = swarm.summarizeChanges()
            with self.lock:
                self.cancelling = {"id": saved["id"], "mission": saved["mission"], "summary": summary, "swarm": swarm}
            return summary
        self.runJob("cancel", "The leader is listing what the swarm already did", work)

    def abandon(self, payload):
        if not self.cancelling or self.cancelling["id"] != payload.get("id"):
            raise ValueError("Read the summary of the leader before you stop this swarm.")
        self.cancelling["swarm"].abandon()
        self.cancelling = None
        self.jobs.pop("cancel", None)
        self.refreshUnfinished()

    # The pages of the providers (API keys, prices, licences) open in the web browser of the user, outside the window of SwarmUP.
    def openLink(self, payload):
        url = str(payload.get("url") or "")
        if not url.startswith(("https://", "http://")):
            raise ValueError("Only web addresses can be opened.")
        webbrowser.open(url)

    # ---------- Leaving the program. ----------
    def close(self):
        with self.lock:
            self.closing = True
            self.touch()
        if self.swarm:
            self.swarm.saveForExit()
        self.unloadModels()

    def quit(self, payload):
        threading.Timer(SHUTDOWN_DELAY, self.shutdown).start()
        return {"message": "SwarmUP is closing. Your swarm is saved: start the program again to continue it."}

    def shutdown(self):
        self.close()
        os._exit(0)


# ==============
# The web server. It only answers this computer, checks the name it is called with (so a website cannot reach it through a DNS trick),
# and every action needs the token of the session, which only the page it served knows.
# ==============
class Handler(BaseHTTPRequestHandler):
    server_version = "SwarmUP"

    def log_message(self, format, *args):
        pass

    def send(self, status, body, contentType):
        self.send_response(status)
        self.send_header("Content-Type", contentType)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def sendJson(self, status, data):
        self.send(status, json.dumps(data, default=str, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def allowed(self):
        return self.headers.get("Host", "") in self.server.hosts

    def authorized(self):
        return hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), self.server.session.token)

    def do_GET(self):
        if not self.allowed():
            return self.sendJson(403, {"ok": False, "error": "Forbidden."})
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            page = (INTERFACE_FOLDER / "index.html").read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, self.server.session.token)
            return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if url.path.startswith("/assets/") and url.path[len("/assets/"):] in ASSETS:
            name = url.path[len("/assets/"):]
            return self.send(200, (INTERFACE_FOLDER / name).read_bytes(), ASSETS[name])
        if url.path.startswith("/api/") and not self.authorized():
            return self.sendJson(403, {"ok": False, "error": "This page is not allowed to use SwarmUP. Open the address the program printed."})
        if url.path == "/api/catalog":
            return self.sendJson(200, catalog())
        if url.path == "/api/poll":
            query = parse_qs(url.query)
            version, feed = int(query.get("version", ["-1"])[0]), int(query.get("feed", ["0"])[0])
            return self.sendJson(200, self.server.session.poll(version, feed))
        self.sendJson(404, {"ok": False, "error": "Not found."})

    def do_POST(self):
        if not self.allowed() or not self.authorized():
            return self.sendJson(403, {"ok": False, "error": "This page is not allowed to use SwarmUP. Open the address the program printed."})
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if not url.path.startswith("/api/") or length > BODY_LIMIT:
            return self.sendJson(404, {"ok": False, "error": "Not found."})
        session = self.server.session
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.sendJson(200, session.act(url.path[len("/api/"):], payload))
        except FormError as error:
            self.sendJson(400, {"ok": False, "error": str(error), "errors": error.errors, "state": session.describe()})
        except USER_ERRORS as error:
            self.sendJson(400, {"ok": False, "error": str(error), "state": session.describe()})
        except Exception as error:
            traceback.print_exc()
            self.sendJson(500, {"ok": False, "error": f"Something went wrong: {type(error).__name__}: {error}", "state": session.describe()})


def makeServer(session, port=0):
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    server.session = session
    server.hosts = {f"{HOST}:{server.server_port}", f"localhost:{server.server_port}"}
    return server


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
    session = Session()
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
