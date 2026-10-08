# Codex as a coding agent: its app-server, its sign-in with ChatGPT, its models, and the approval of what it wants to do.
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

from coding_agents import CODING_AGENT_RULES, agentWorkspace, askPermission, askQuestions, isInside, placeRule
from harness_utils import isOnline
from internet_cache import remember
from model_support import ModelConnectionError, ModelError, recordCall
from models_library import DEFAULT_CLI_MODEL


# ---------- Codex, through its app-server: JSON-RPC messages on its standard input and output. ----------
# OpenAI calls the app-server experimental, so SwarmUP only accepts the versions it was tested with (CODEX_TESTED: same major and minor
# version), unless SWARMUP_ALLOW_UNTESTED_CODEX is set. Codex uses the ChatGPT plan of the user: it is signed in through Codex itself
# (CodexLogin), which keeps the sign-in the way it does for its own window. Every thread is ephemeral (nothing is kept by Codex), works in the
# folder of the agent, and asks before every command (approval policy untrusted). SwarmUP answers alone only for a command that only reads
# inside the folder: one program among CODEX_READERS, without any sign of the shell, whose paths all stay inside the folder, and that Codex
# itself describes as reading. The web search of Codex (which never asks), its sub-agents and the MCP servers of the user are switched off.
CODEX_TESTED = (0, 160)
CODEX_SETTINGS = ["-c", 'web_search="disabled"', "-c", "features.multi_agent=false", "-c", "mcp_servers={}"]
CODEX_CLIENT = {"name": "swarmup", "title": "SwarmUP", "version": "1.0"}
CODEX_REQUEST_TIMEOUT = 60
CODEX_READERS = ("cat", "head", "tail", "ls", "wc", "grep", "rg")
CODEX_READ_ACTIONS = ("read", "listFiles", "search")
SHELL_SIGNS = set(";&|<>$`(){}[]\\~!#*?\n\r'\"")
DANGEROUS_OPTIONS = ("--pre", "-z", "--search-zip", "-f", "--follow")
CODEX_PROBLEMS = {"usageLimitExceeded": "Your ChatGPT plan reached its Codex limit for now. Try again later, or choose another model for this agent.",
                  "unauthorized": "Codex is not signed in anymore. Sign in with ChatGPT again in the step of the models.",
                  "contextWindowExceeded": "The task was too long for the model of Codex."}


def findCodex():
    return os.environ.get("SWARMUP_CODEX") or shutil.which("codex")


# Whether Codex is installed and has a version SwarmUP was tested with: {"path", "version", "problem"}.
def checkCodex():
    path = findCodex()
    if not path:
        return {"path": None, "version": None, "problem": "Codex is not installed. Install it with: npm install -g @openai/codex (it needs Node.js)."}
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        return {"path": path, "version": None, "problem": f"Codex could not be started: {error}"}
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", output.stdout + output.stderr)
    if not found:
        return {"path": path, "version": None, "problem": "The version of Codex could not be read."}
    version = tuple(int(part) for part in found.groups())
    tested = ".".join(str(part) for part in CODEX_TESTED)
    problem = ""
    if version[:2] != CODEX_TESTED and not os.environ.get("SWARMUP_ALLOW_UNTESTED_CODEX"):
        problem = (f"SwarmUP was tested with Codex {tested}.x, and this computer has {found.group(0)}. The connection of SwarmUP to Codex is experimental "
                   f"for OpenAI, so another version may not work. Install the tested one with: npm install -g @openai/codex@{tested}")
    return {"path": path, "version": found.group(0), "problem": problem}


# One app-server of Codex. onRequest(method, params) answers the questions of Codex (its result, or None if SwarmUP does not handle them),
# and onNotify(method, params) receives its news. The questions are answered in their own thread, because the user can take long.
class CodexConnection:
    def __init__(self, path, onRequest=None, onNotify=None):
        self.onRequest = onRequest or (lambda method, params: None)
        self.onNotify = onNotify or (lambda method, params: None)
        self.pending = {}
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()
        self.closed = False
        try:
            self.process = subprocess.Popen([path, "app-server", *CODEX_SETTINGS], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                            encoding="utf-8", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise ModelError(f"Codex could not be started: {error}") from None
        threading.Thread(target=self.read, daemon=True).start()
        self.request("initialize", {"clientInfo": CODEX_CLIENT})
        self.send({"method": "initialized", "params": {}})

    def send(self, message):
        try:
            with self.lock:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError):
            raise ModelError("Codex stopped unexpectedly.") from None

    def read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "method" in message and "id" in message:
                threading.Thread(target=self.answer, args=(message,), daemon=True).start()
            elif "method" in message:
                self.onNotify(message["method"], message.get("params") or {})
            elif message.get("id") in self.pending:
                holder = self.pending.pop(message["id"])
                holder[1] = message
                holder[0].set()
        self.closed = True
        for holder in list(self.pending.values()):
            holder[0].set()
        self.onNotify("connection/closed", {})

    def request(self, method, params, timeout=CODEX_REQUEST_TIMEOUT):
        number, holder = next(self.numbers), [threading.Event(), None]
        self.pending[number] = holder
        self.send({"method": method, "id": number, "params": params})
        if not holder[0].wait(timeout):
            self.pending.pop(number, None)
            raise ModelError(f"Codex did not answer in time ({method}).")
        if holder[1] is None:
            raise ModelError("Codex stopped unexpectedly.")
        if "error" in holder[1]:
            raise ModelError(f"Codex refused {method}: {holder[1]['error'].get('message', holder[1]['error'])}")
        return holder[1].get("result") or {}

    def answer(self, message):
        try:
            result = self.onRequest(message["method"], message.get("params") or {})
        except Exception as error:
            result, problem = None, str(error)
        else:
            problem = "SwarmUP does not handle this request."
        try:
            self.send({"id": message["id"], "result": result} if result is not None else {"id": message["id"], "error": {"code": -32601, "message": problem}})
        except ModelError:
            pass

    def close(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def openCodex(onRequest=None, onNotify=None):
    status = checkCodex()
    if status["problem"]:
        raise ModelError(status["problem"])
    return CodexConnection(status["path"], onRequest, onNotify)


# Who is signed in to Codex: {"signedIn", "type" (chatgpt or apiKey), "email", "plan"}.
def readCodexAccount():
    connection = openCodex()
    try:
        account = connection.request("account/read", {}).get("account")
    finally:
        connection.close()
    if not account:
        return {"signedIn": False, "type": None, "email": None, "plan": None}
    return {"signedIn": True, "type": account.get("type"), "email": account.get("email"), "plan": account.get("planType")}


# The models the plan of the user can use in Codex: [{"id", "name", "description", "isDefault"}]. Codex is asked every time, and when it cannot
# answer (no internet), the list of the last time is given (see remember in internet_cache.py).
def listCodexModels():
    def fetch():
        connection = openCodex()
        try:
            models = connection.request("model/list", {"limit": 100}).get("data") or []
        finally:
            connection.close()
        return [{"id": model["id"], "name": model.get("displayName") or model["id"], "description": model.get("description") or "", "isDefault": bool(model.get("isDefault"))}
                for model in models if not model.get("hidden")]
    return remember("codex-models", fetch, 0, errors=(ModelError, OSError))


# A sign-in with ChatGPT, through Codex. start gives what the user must open: {"url"} for the browser (Codex receives the answer on
# this computer), or {"url", "code"} for a code typed on the page (works when the browser cannot come back to this computer).
# wait gives "" when the user is signed in, or what went wrong. cancel stops it.
class CodexLogin:
    def __init__(self):
        self.done = threading.Event()
        self.error = ""
        self.loginId = None
        self.connection = openCodex(onNotify=self.notice)

    def notice(self, method, params):
        if method == "account/login/completed" and params.get("loginId") in (None, self.loginId):
            self.error = "" if params.get("success") else params.get("error") or "The sign-in did not finish."
            self.done.set()
        elif method == "connection/closed" and not self.done.is_set():
            self.error = "Codex stopped during the sign-in."
            self.done.set()

    def start(self, kind="browser"):
        result = self.connection.request("account/login/start", {"type": "chatgptDeviceCode" if kind == "code" else "chatgpt"})
        self.loginId = result.get("loginId")
        return {"url": result.get("verificationUrl") or result.get("authUrl"), "code": result.get("userCode")}

    def wait(self, timeout=None):
        self.done.wait(timeout)
        self.connection.close()
        return self.error if self.done.is_set() else "The sign-in took too long."

    def cancel(self):
        if self.loginId and not self.done.is_set():
            try:
                self.connection.request("account/login/cancel", {"loginId": self.loginId})
            except ModelError:
                pass
        self.error = self.error or "The sign-in was cancelled."
        self.done.set()
        self.connection.close()


# The command inside the shell that Codex wraps it in (/bin/bash -lc '...').
def unwrapCommand(command):
    try:
        words = shlex.split(command or "")
    except ValueError:
        return command or ""
    if len(words) == 3 and Path(words[0]).name in ("bash", "sh", "zsh") and words[1] in ("-lc", "-c"):
        return words[2]
    return command or ""


# A command that only reads inside the folder, which SwarmUP lets run without asking. On Windows every command is asked.
def isSafeRead(params, folder):
    if os.name == "nt":
        return False
    actions = params.get("commandActions") or []
    if not actions or any(action.get("type") not in CODEX_READ_ACTIONS for action in actions):
        return False
    command = unwrapCommand(params.get("command"))
    if not command or any(sign in SHELL_SIGNS for sign in command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    cwd = params.get("cwd") or str(folder)
    if not words or words[0] not in CODEX_READERS or not isInside(folder, cwd):
        return False
    for word in words[1:]:
        if word.startswith(DANGEROUS_OPTIONS):
            return False
        value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else None if word.startswith("-") else word
        if value is not None and not isInside(folder, value, cwd):
            return False
    return True


def describeCodexPermissions(permissions):
    parts = []
    files = (permissions or {}).get("fileSystem") or {}
    for entry in files.get("entries") or []:
        parts.append(json.dumps(entry, ensure_ascii=False))
    parts += [f"read {path}" for path in files.get("read") or []] + [f"write {path}" for path in files.get("write") or []]
    if ((permissions or {}).get("network") or {}).get("enabled"):
        parts.append("use the internet")
    return "; ".join(parts) or json.dumps(permissions, ensure_ascii=False)


class CodexModel:
    def __init__(self, name, report=None):
        self.name = name or DEFAULT_CLI_MODEL
        self.report = report or (lambda message: None)
        self.usage = {"calls": 0, "input": 0, "output": 0}
        # How Codex is signed in, read when it connects: chatgpt (the plan pays, unless the plan is billed by use) or apiKey (OpenAI bills every
        # use), see priceOfModel.
        self.accountType = "chatgpt"
        self.planType = ""
        self.loop = None
        self.connection = None
        self.turn = None
        self.runCommands = set()
        self.runKinds = set()
        self.lock = threading.Lock()

    def attach(self, loop):
        self.loop = loop

    def newRun(self):
        self.runCommands, self.runKinds = set(), set()

    def connect(self):
        if self.connection is None or self.connection.closed:
            self.connection = openCodex(self.answer, self.notice)
            account = self.connection.request("account/read", {})
            if not account.get("account") and account.get("requiresOpenaiAuth", True):
                self.close()
                raise ModelError("Codex is not signed in. Sign in with ChatGPT in the step of the models, then start again.")
            self.accountType = (account.get("account") or {}).get("type") or "other"
            self.planType = (account.get("account") or {}).get("planType") or ""
        return self.connection

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def unload(self):
        self.close()

    def notice(self, method, params):
        turn = self.turn
        if turn is None or (params.get("threadId") and params["threadId"] != turn["thread"]):
            return
        if method == "item/started" and (params.get("item") or {}).get("type") == "fileChange":
            item = params["item"]
            turn["files"][item.get("id")] = [change.get("path") for change in item.get("changes") or [] if isinstance(change, dict) and change.get("path")]
        elif method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
            turn["messages"].append(params["item"].get("text") or "")
        elif method == "thread/tokenUsage/updated":
            turn["usage"] = (params.get("tokenUsage") or {}).get("total") or turn["usage"]
        elif method == "error" and params.get("willRetry"):
            self.report(f"Codex: {(params.get('error') or {}).get('message', 'a problem')}. It tries again.")
        elif method == "turn/completed":
            turn["result"] = params.get("turn") or {}
            turn["done"].set()
        elif method == "connection/closed":
            turn["done"].set()

    def answer(self, method, params):
        folder = agentWorkspace(self.loop)
        if method == "item/commandExecution/requestApproval":
            command = unwrapCommand(params.get("command"))
            if isSafeRead(params, folder) or command in self.runCommands:
                return {"decision": "accept"}
            network = params.get("networkApprovalContext")
            action = f"connect to {network.get('host')}" if network else "run a command"
            decision = askPermission(self.loop, {"action": action, "detail": command, "folder": params.get("cwd") or str(folder), "reason": params.get("reason") or ""})
            if decision["decision"] == "run":
                self.runCommands.add(command)
            return {"decision": "decline" if decision["decision"] == "deny" else "accept"}
        if method == "item/fileChange/requestApproval":
            if "fileChange" in self.runKinds:
                return {"decision": "accept"}
            files = (self.turn or {}).get("files", {}).get(params.get("itemId")) or []
            rules = [placeRule(self.loop, path, True) for path in files]
            if files and not params.get("grantRoot") and all(rule == "free" for rule in rules):
                return {"decision": "accept"}
            refused = next((rule for rule in rules if rule not in ("free", "ask")), None)
            if refused:
                self.loop.notifyUser(f"[{self.loop.name}] {refused}")
                return {"decision": "decline"}
            detail = ", ".join(files) or params.get("reason") or "files of its folder"
            action = f"write anywhere in {params['grantRoot']}" if params.get("grantRoot") else "change files"
            decision = askPermission(self.loop, {"action": action, "detail": detail, "folder": str(folder), "reason": params.get("reason") or ""})
            if decision["decision"] == "run":
                self.runKinds.add("fileChange")
            return {"decision": "decline" if decision["decision"] == "deny" else "accept"}
        if method == "item/permissions/requestApproval":
            decision = askPermission(self.loop, {"action": "get more permissions", "detail": describeCodexPermissions(params.get("permissions")),
                                                 "folder": params.get("cwd") or str(folder), "reason": params.get("reason") or ""})
            if decision["decision"] == "deny":
                return {"permissions": {}}
            return {"permissions": params.get("permissions") or {}, "scope": "session" if decision["decision"] == "run" else "turn"}
        if method == "item/tool/requestUserInput":
            questions = [{"id": question["id"], "header": question.get("header", ""), "question": question.get("question", ""),
                          "options": [{"label": option.get("label", ""), "description": option.get("description", "")} for option in question.get("options") or []],
                          "multiple": False, "secret": bool(question.get("isSecret"))} for question in params.get("questions") or []]
            answers = askQuestions(self.loop, questions)
            return {"answers": {question["id"]: {"answers": [str(value) for value in answers.get(question["id"]) or []]} for question in questions}}
        return None

    def input(self, prompt):
        with self.lock:
            connection = self.connect()
            folder = agentWorkspace(self.loop)
            thread = connection.request("thread/start", {"cwd": str(folder), "approvalPolicy": "untrusted", "sandbox": "workspace-write", "ephemeral": True,
                                                         "model": None if self.name == DEFAULT_CLI_MODEL else self.name, "developerInstructions": CODING_AGENT_RULES})
            self.turn = {"thread": thread["thread"]["id"], "done": threading.Event(), "messages": [], "files": {}, "usage": None, "result": None}
            connection.request("turn/start", {"threadId": self.turn["thread"], "input": [{"type": "text", "text": prompt}]})
            self.turn["done"].wait()
            turn, self.turn = self.turn, None
        usage = turn["usage"] or {}
        cached = min(usage.get("cachedInputTokens") or 0, usage.get("inputTokens") or 0)
        recordCall(self.usage, input=(usage.get("inputTokens") or 0) - cached, cachedInput=cached, output=usage.get("outputTokens"), missing=not usage)
        result = turn["result"]
        if result is None:
            self.close()
            raise ModelError("Codex stopped unexpectedly.")
        if result.get("status") != "completed":
            error = result.get("error") or {}
            if not isOnline():
                raise ModelConnectionError("Codex cannot reach OpenAI: the internet connection is lost.")
            kind = error.get("codexErrorInfo")
            raise ModelError(CODEX_PROBLEMS.get(kind) if isinstance(kind, str) and kind in CODEX_PROBLEMS else f"Codex could not finish: {error.get('message') or result.get('status')}")
        return turn["messages"][-1] if turn["messages"] else ""
