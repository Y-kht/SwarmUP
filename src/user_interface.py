# The graphical interface of SwarmUP. Run it with: python src/user_interface.py
# It is a small web server that only listens to this computer (127.0.0.1) and opens the interface in the web browser, so it works the same
# on Windows, macOS and Linux, with nothing to install. The pages are in the folder user-interface (index.html, style.css, icons.js, app.js).
# The interface follows the steps of tests/full_command_line_user_test.py with clicks, from the same functions of tasks_library.py,
# models_library.py and harness_utils.py: the mission, the task of each agent, its folder, its model, who waits for whom, and the run of
# the swarm, which is followed live. The agents speak to the user through the Session (notify, ask and askSecret), and the browser asks
# the Session for news (poll). Passwords, API keys and tokens are only kept in memory, and they are never sent back to the browser.
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

from harness_utils import (FETCH_ERRORS, USER_NAME, ConnectionLost, Loop, MessagingError, Swarm, checkEmailLogin, checkMessenger, checkVram, describeError,
                           findPublishers, findTelegramChats, findUnfinishedSwarms, getModelCost, nextOccurrence, readGpus)
from model_clients import ModelError, LocalModel, createModel, findMissingPackages, getApiKey, getHubFolder, isDownloaded, lookupHuggingFace
from models_library import API_KEYS, BITS, MODELS_API, MODELS_LOCAL, PRICING_PAGES, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, isGated
from sources_library import MESSAGING_APPS, NEWS_OUTLETS, PAPER_PUBLISHERS
from tasks_library import (ADVANCED_FIELDS, DEFAULT_LOOPS, NO_MESSENGER, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getFields, getHelp,
                           isAsked, messengerSettings, parseAnswer, publicAnswers, restoreAnswers, secretFields, suggestFolder, suggestName)

INTERFACE_FOLDER = Path(__file__).resolve().parent / "user-interface"
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
                "approve", "reject", "correct", "message", "startNow", "openLink", "openFolder", "refreshUnfinished", "quit", "taskForm")


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
    company = API_KEYS[info["provider"]]["company"] if info["provider"] else ""
    label = f"{info['name']} · {info['vram']} GB of VRAM" if info["local"] else f"{info['name']} · {company}"
    return {**info, "company": company, "gated": isGated(info["name"]) if info["local"] else False, "label": label}


# What kind of question the agents ask, and the buttons that answer it. The text box stays for every question that takes words.
def describeQuestion(text, secret=False):
    low = text.strip().lower()
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
            "defaultLoops": DEFAULT_LOOPS, "platform": {"os": sys.platform, "separator": os.sep, "home": str(Path.home())}, "user": USER_NAME}


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
        self.reset()
        self.refreshUnfinished()
        self.actions = {name: getattr(self, name) for name in (
            "setMission", "taskForm", "saveAgent", "removeAgent", "undoRemove", "moveAgent", "checkEmail", "checkMessenger", "findChats", "searchPublishers",
            "browse", "pickPath", "setFolder", "openFolder", "modelCatalog", "lookupModel", "price", "chooseModel", "testModel", "checkPackages", "setOrder", "planOrder",
            "setMode", "start", "execute", "answer", "approve", "reject", "correct", "message", "startNow", "prepareStop", "stop", "clearJob", "newSwarm",
            "resumeForm", "resume", "prepareCancel", "abandon", "refreshUnfinished", "openLink", "quit")}

    def reset(self):
        self.mission = ""
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

    def ask(self, name, text, secret=False):
        kind, replies = describeQuestion(text, secret)
        shown = self.readyRevisions() if kind == "review" else {}
        if kind == "review" and not shown:
            return "yes"
        event = threading.Event()
        with self.lock:
            if self.closing:
                return ""
            self.questionNumber += 1
            question = {"id": self.questionNumber, "speaker": name, "text": str(text), "secret": secret, "kind": kind, "replies": replies,
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

    def readyRevisions(self):
        swarm = self.swarm
        if not swarm:
            return {}
        return {name: info["revision"] for name, info in ((name, swarm.getInfo(name)) for name in swarm.getAgents()) if info["review"] == "ready"}

    def release(self, question, answer, shown=True):
        question["answer"] = answer
        if shown:
            self.addFeed(USER_NAME, "••••••" if question["secret"] and answer else answer or "(no answer)", "info", "answer")
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
        text, tone = formatEvent(event)
        if event["kind"] == "message":
            self.addFeed(event["sender"], event["message"], "info", "message" if event["sender"] != USER_NAME else "answer", event["receiver"])
        elif text:
            self.addFeed("", text, tone, "event")
        if event["kind"] == "review":
            self.dropAnsweredReviews()
        self.touch()

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

    def checkIdle(self):
        if self.swarm and self.swarm.isRunning():
            raise ValueError("The swarm is running. Wait until it finishes, or stop it, before you change it.")

    # ---------- The state that the browser draws. ----------
    def findSpec(self, agentId):
        spec = next((spec for spec in self.specs if spec["id"] == agentId), None)
        if spec is None:
            raise ValueError("This agent does not exist anymore.")
        return spec

    def describeSpec(self, spec, index):
        task, answers = TASKS[spec["task"]], spec["answers"]
        return {"id": spec["id"], "name": spec["name"], "task": spec["task"], "label": task["label"], "role": task["role"], "description": spec["description"],
                "folder": answers.get("folder"), "folderNote": task["folder"], "suggestion": suggestFolder(answers), "model": describeModel(spec["model"]),
                "missing": spec["missing"], "isLeader": index == 0, "waitsFor": spec["waitsFor"], "loops": answers.get("numberOfLoops", DEFAULT_LOOPS),
                "tested": spec.get("tested", "")}

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
                           "usage": dict(client.usage) if hasattr(client, "usage") else None})
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
        return {"version": version, "mission": self.mission, "agents": agents, "mode": self.mode, "order": self.order, "keys": self.describeKeys(),
                "gpu": self.describeGpus(), "run": self.describeRun(), "questions": questions, "jobs": jobs, "unfinished": self.unfinished, "busy": self.isBusy(),
                "nativeDialogs": self.dialog is not None, "trash": self.trash["spec"]["name"] if self.trash else None, "cancelling": cancelling}

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

    # Turns the values of the form into the answers of the task, field after field, like the command line asks them.
    # A secret left empty keeps the one given before, so a password is never sent back to the browser to edit an agent.
    def parseForm(self, task, values, previous=None):
        answers, errors = {}, {}
        for field in getFields(task):
            if not isAsked(field, answers):
                continue
            key, kind, raw = field["key"], field["kind"], values.get(field["key"])
            value, error = None, ""
            if kind == "choice":
                value = raw or getDefault(field, answers)
                if value not in field["options"]:
                    value, error = None, "Choose one of the options."
            elif kind == "choices":
                value = [option for option in field["options"] if option in (raw if raw is not None else getDefault(field, answers) or [])]
            elif kind == "outlets":
                value = [str(outlet).strip() for outlet in raw or [] if str(outlet).strip()]
                wrong = [outlet for outlet in value if "://" in outlet and not outlet.startswith(("http://", "https://"))]
                error = "Choose at least one outlet." if field.get("required") and not value else f"{wrong[0]} must start with http:// or https://." if wrong else ""
            elif kind == "publishers":
                value = {str(name): str(number) for name, number in (raw or {}).items()}
            elif kind == "accounts":
                value, old = {}, (previous or {}).get(key) or {}
                for row in raw or []:
                    host, user, password = (str(row.get(part, "")).strip() for part in ("host", "user", "password"))
                    if not host and not user:
                        continue
                    password = password or (old.get(host, ("", ""))[1] if old.get(host, ("", ""))[0] == user else "")
                    if not host or "/" in host or not user or not password:
                        error = "Every account needs the website (like ieeexplore.ieee.org, without https://), a username and a password."
                    value[host] = (user, password)
            else:
                text = "" if raw is None else str(raw)
                if kind == "secret" and not text and previous and previous.get(key):
                    value = previous[key]
                else:
                    value, error = parseAnswer(field, text, answers)
            if error:
                errors[key] = error
            answers[key] = value
        return answers, errors

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

    def saveAgent(self, payload):
        self.checkIdle()
        task = payload.get("task")
        if task not in TASKS:
            raise ValueError("Choose one of the tasks.")
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        previous = spec["answers"] if spec and spec["task"] == task else None
        answers, errors = self.parseForm(task, payload.get("values") or {}, previous)
        taken = [other["name"] for other in self.specs if other is not spec]
        name = str(payload.get("name") or "").strip() or suggestName(task, taken)
        if checkAgentName(name, taken):
            errors["name"] = checkAgentName(name, taken)
        if task == "literature" and not (answers.get("searches") or answers.get("publishers")) and "searches" not in errors:
            errors["searches"] = "The survey needs a place to search: choose at least a search engine or a publisher."
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
            spec = {"id": f"agent{self.agentNumber}", "model": None, "client": None, "waitsFor": [], "missing": []}
            self.specs.append(spec)
        elif spec["name"] != name:
            for other in self.specs:
                other["waitsFor"] = [name if waited == spec["name"] else waited for waited in other["waitsFor"]]
        spec.update(task=task, name=name, answers=answers, description=describeLoop(loop))
        self.dirty = True
        return {"agentId": spec["id"], "description": spec["description"], "warning": warning}

    def removeAgent(self, payload):
        self.checkIdle()
        spec = self.findSpec(payload.get("agentId"))
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
        self.specs.remove(spec)
        self.specs.insert(position, spec)
        self.dirty = True
        self.sanitizeWaits()

    def checkEmail(self, payload):
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        answers, errors = self.parseForm("email", payload.get("values") or {}, spec["answers"] if spec and spec["task"] == "email" else None)
        needed = {key: error for key, error in errors.items() if key in ("provider", "sender", "password", "smtp", "imap")}
        if needed:
            raise FormError(needed, "Fill in the account first.")
        return {"problem": checkEmailLogin(answers["sender"], answers["password"], answers["smtp"], answers["imap"])}

    def messengerAnswers(self, payload):
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        answers, errors = self.parseForm("news", payload.get("values") or {}, spec["answers"] if spec and spec["task"] == "news" else None)
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
        self.checkIdle()
        spec = self.findSpec(payload.get("agentId"))
        text, folder = str(payload.get("folder") or "").strip(), None
        if text:
            folder, error = parseAnswer({"key": "folder", "ask": "", "kind": "folder"}, text)
            if error:
                raise FormError({"folder": error}, error)
        try:
            loop = buildLoop(spec["task"], None, {**spec["answers"], "folder": folder})
        except ValueError as problem:
            raise FormError({"folder": str(problem)}, str(problem)) from None
        spec["answers"]["folder"], spec["description"], self.dirty = folder, describeLoop(loop), True

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
        recommend = TASKS[spec["task"]]["recommend"]
        families = {family: [self.localEntry(sizing, name, bits) for name in models] for family, models in MODELS_LOCAL.items()}
        local = [self.localEntry(sizing, name, bits) for name in RECOMMENDED_LOCAL[recommend]]
        api = [{"name": name, "provider": provider, "company": API_KEYS[provider]["company"]} for name in RECOMMENDED_API[recommend]
               for provider in [next(key for key, models in MODELS_API.items() if name in models)]]
        gpu = checkVram(sizing.getNeededVram(), readGpus())
        return {"catalog": {"agentId": spec["id"], "bits": bits, "local": local, "families": families, "api": api, "gpu": gpu, "isLeader": self.specs[0] is spec,
                            "hfToken": bool(os.environ.get("HF_TOKEN")), "current": describeModel(spec["model"])}}

    def lookupModel(self, payload):
        name = str(payload.get("name", "")).strip()
        if name.count("/") != 1 or " " in name:
            raise FormError({"name": "It is written owner/name, like Qwen/Qwen3-8B."}, "It is written owner/name, like Qwen/Qwen3-8B.")
        listed = next((family[name] for family in MODELS_LOCAL.values() if name in family), None)
        found = {"billions": listed, "gated": isGated(name)} if listed else lookupHuggingFace(name)
        return {"model": {"name": name, "billions": found["billions"], "gated": found["gated"] or isGated(name)}}

    def price(self, payload):
        name = str(payload.get("name", "")).strip()
        info = getModelInfo(name, provider=payload.get("provider"))
        if info["local"]:
            raise ValueError("Local models are free to use: they run on your GPUs.")
        return {"price": describePrice(getModelCost(info["provider"], name))}

    def chooseModel(self, payload):
        self.checkIdle()
        spec = self.findSpec(payload.get("agentId"))
        name = str(payload.get("name", "")).strip()
        if payload.get("local"):
            billions = float(payload["billions"]) if payload.get("billions") not in (None, "") else None
            if billions is not None and billions <= 0:
                raise FormError({"billions": "Write a number above 0, like 8.2."})
            info = getModelInfo(name, bits=int(payload.get("bits") or 16), billions=billions)
        else:
            info = getModelInfo(name, provider=payload.get("provider"))
            if info["local"]:
                raise ValueError(f"{name} is a model of Hugging Face: choose it in the local models.")
        check = self.sizingSwarm(spec["id"]).checkModel(info)
        if not check["allowed"]:
            raise ValueError(check["message"])
        if info["local"]:
            token = str(payload.get("hfToken") or "").strip()
            if token:
                self.tokens[name] = token
        else:
            key = str(payload.get("apiKey") or "").strip()
            if key:
                self.keys[info["provider"]] = key
            if not (self.keys.get(info["provider"]) or getApiKey(info["provider"])):
                company = API_KEYS[info["provider"]]["company"]
                raise FormError({"apiKey": f"{company} needs an API key. Create one at {API_KEYS[info['provider']]['page']}."}, f"{company} needs an API key.")
        client = createModel(info, self.keys, token=self.tokens.get(name), report=lambda message: self.notify(spec["name"], message))
        if isinstance(spec["client"], LocalModel):
            spec["client"].unload()
        spec.update(model=info, client=client, missing=findMissingPackages(info), tested="")
        self.dirty = True
        return {"warning": check["message"] if info["local"] else "", "missing": spec["missing"], "download": describeDownload(info) if info["local"] else None}

    def testModel(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        if not spec["client"]:
            raise ValueError("Choose a model first.")
        if spec["model"]["local"]:
            raise ValueError("A local model is tested when the swarm starts: it is loaded on the GPUs then.")
        try:
            answer = spec["client"].input(TEST_PROMPT)
        except ModelError as error:
            spec["tested"] = "failed"
            return {"problem": str(error)}
        spec["tested"] = "ok"
        return {"problem": "", "answer": " ".join(str(answer).split())[:80]}

    def checkPackages(self, payload):
        for spec in self.specs:
            if spec["model"]:
                spec["missing"] = findMissingPackages(spec["model"])

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
        swarm = Swarm(self.mission)
        for spec in self.specs:
            loop = buildLoop(spec["task"], spec["client"], spec["answers"])
            self.connect(loop, spec["name"])
            recipe = {"task": spec["task"], "answers": publicAnswers(spec["task"], spec["answers"])}
            swarm.addAgent(spec["name"], loop, TASKS[spec["task"]]["role"], describeLoop(loop), model=spec["model"], recipe=recipe)
        for spec in self.specs[1:]:
            swarm.setWaitsFor(spec["name"], spec["waitsFor"])
        swarm.addListener(self.onEvent)
        return swarm

    def currentSwarm(self):
        if self.swarm is None or self.dirty:
            self.swarm, self.runInfo, self.dirty = self.buildSwarm(), {}, False
        return self.swarm

    def planOrder(self, payload):
        self.checkIdle()
        if len(self.specs) < 2:
            raise ValueError("With one agent, nobody waits for anybody.")
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
            question = self.questions.pop(int(payload.get("id", 0)), None)
            if question is None:
                raise ValueError("This question was already answered.")
            self.release(question, str(payload.get("answer", "")))

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
            if isinstance(spec["client"], LocalModel):
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
        agents, providers, gated = [], {}, []
        for name, member in saved["members"].items():
            task, answers, finished = member["recipe"]["task"], member["recipe"]["answers"], member["status"] in ("done", "failed")
            fields = [{**describeField(field, answers), "required": field.get("required", False) and not finished} for field in secretFields(task, answers)]
            info = member["model"]
            agents.append({"name": name, "role": member["role"], "task": task, "status": member["status"], "finished": finished, "fields": fields,
                           "accounts": task == "literature" and not finished, "model": describeModel(info), "missing": findMissingPackages(info) if info else []})
            if info and not info["local"] and not finished:
                providers[info["provider"]] = self.describeKeys()[info["provider"]]
            if info and info["local"] and isGated(info["name"]) and not os.environ.get("HF_TOKEN") and not finished:
                gated.append(info["name"])
        return {"resume": {"id": saved["id"], "mission": saved["mission"], "mode": saved["mode"], "agents": agents, "providers": providers, "gated": gated}}

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
        usable = info and (info["local"] or self.keys.get(info["provider"]) or getApiKey(info["provider"]))
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
        rebuilt = []
        swarm = Swarm.restore(saved, lambda name, data: self.rebuildAgent(name, data, payload.get("secrets") or {}, rebuilt))
        swarm.addListener(self.onEvent)
        self.unloadModels()
        self.reset()
        rebuilt.sort(key=lambda spec: spec["name"] != saved["leader"])
        self.mission, self.specs, self.mode, self.swarm, self.dirty = saved["mission"], rebuilt, saved["mode"], swarm, False
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
        if info and not payload.get("plain") and (info["local"] or self.keys.get(info["provider"]) or getApiKey(info["provider"])):
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
