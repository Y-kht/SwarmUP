# What the window shows: the models, the questions and the answers, the events of a swarm, the fields of the forms, and the
# folders of the computer.
import os
import shlex
import shutil
import string
import subprocess
import sys
from pathlib import Path

from agent_storehouse import folderProblem
from harness_utils import ConnectionLost, USER_NAME, describeError, listRules
from messengers import MessagingError
from model_support import ModelError, getApiKey, getHubFolder, isDownloaded
from models_library import API_KEYS, BITS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, PRICING_PAGES, isGated
from sources_library import MESSAGING_APPS, NEWS_OUTLETS, PAPER_PUBLISHERS
from tasks_library import DEFAULT_LOOPS, LEADER_TASK, NO_MESSENGER, SPECIALISED_TASKS, TASKS, getDefault, getHelp


BROWSE_LIMIT = 3000

NEEDS_FOLDER_KINDS = ("file", "path")
SKIPPED_STATUSES = ("waiting",)
# The errors whose message is written for the user. The browser shows them as they are.
USER_ERRORS = (ValueError, ModelError, MessagingError, ConnectionLost)


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
        problem = folderProblem(folder) or f"This folder cannot be read: {describeError(error)}."
    entries.sort(key=lambda entry: (not entry["folder"], entry["name"].lower()))
    parent = str(folder.parent) if folder.parent != folder else None
    return {"path": str(folder), "parent": parent, "entries": entries, "problem": problem, "places": listPlaces(), "separator": os.sep}


def catalog():
    tasks = [{"key": key, "label": task["label"], "role": task["role"], "info": task["info"], "folder": task["folder"], "name": task["name"], "specialised": key in SPECIALISED_TASKS}
             for key, task in TASKS.items()]
    providers = {key: {**details, "models": MODELS_API[key], "pricing": PRICING_PAGES.get(key, "")} for key, details in API_KEYS.items()}
    return {"tasks": tasks, "rules": listRules(), "providers": providers, "families": list(MODELS_LOCAL), "bits": list(BITS), "outlets": {group: list(names) for group, names in NEWS_OUTLETS.items()},
            "publishers": PAPER_PUBLISHERS, "messaging": {app: details["info"] for app, details in MESSAGING_APPS.items()}, "noMessenger": NO_MESSENGER,
            "defaultLoops": DEFAULT_LOOPS, "platform": {"os": sys.platform, "separator": os.sep, "home": str(Path.home())}, "user": USER_NAME,
            "codingAgents": {key: {"label": agent["label"], "company": agent["company"], "page": agent["page"], "models": agent["models"], "provider": agent["provider"]}
                             for key, agent in MODELS_CLI.items()}, "defaultCliModel": DEFAULT_CLI_MODEL,
            "leaderTask": {"key": "leader", "label": LEADER_TASK["label"], "role": LEADER_TASK["role"], "info": LEADER_TASK["info"], "folder": LEADER_TASK["folder"],
                           "name": LEADER_TASK["name"]}}
