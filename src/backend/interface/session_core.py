# The session of the window: the state it draws, the questions of the agents and the actions of the user. Its steps are in
# session_steps.py, session_models.py, session_runs.py and session_saved.py.
import re
import secrets
import threading
import traceback
from datetime import datetime

from gpu_check import checkVram, readGpus
from harness_utils import USER_NAME, describeError
from interface_views import FormError, USER_ERRORS, describeAnswer, describeModel, describeQuestion, formatEvent
from internet_cache import describeCached
from mission_costs import MissionCosts
from model_clients import createModel
from model_support import getApiKey
from models_library import API_KEYS
from session_models import SessionModels
from session_runs import SessionRuns
from session_saved import SessionSaved
from session_steps import SessionSteps
from tasks_library import DEFAULT_LOOPS, getTask, suggestFolder
from user_settings import MAX_AGENTS_LIMIT, loadSettings, saveSettings


POLL_SECONDS = 20
FEED_LIMIT = 600
CONTEXT_LIMIT = 6

# The actions that change what the user built run one at a time. The others only read, ask the internet, wait for a dialog of the system,
# or answer the swarm, so they never wait behind a slow one.
FREE_ACTIONS = ("checkEmail", "checkMessenger", "findChats", "searchPublishers", "browse", "pickPath", "lookupModel", "price", "testModel", "modelCatalog", "answer",
                "approve", "reject", "correct", "message", "startNow", "openLink", "openFolder", "refreshUnfinished", "quit", "taskForm",
                "codexAccount")


# ==============
# The session: everything the user built and everything the swarm says, for one window of the interface.
# The builder keeps the agents as specs: {id, task, name, answers (the folder included), model, client, waitsFor, description, missing}.
# The Swarm is built from them when it is about to run, and kept afterwards, so a plan that was approved can be executed.
# Rule for the locks: the lock of the session is never held while calling a method of the Swarm that takes its own lock,
# because the Swarm calls the listener of the session with its lock held.
# ==============
class Session(SessionSteps, SessionModels, SessionRuns, SessionSaved):
    # A window with several missions (Desk in session_desk.py) gives every tab the same keys and tokens, and onTouch, called after each change.
    def __init__(self, keys=None, tokens=None, onTouch=None):
        self.token = secrets.token_urlsafe(24)
        self.onTouch = onTouch
        self.tab = None
        self.lock = threading.RLock()
        # The agents of this mission take turns to speak to the user, without waiting for the missions of the other tabs (Loop.userLock).
        self.userLock = threading.RLock()
        self.acting = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.feed = []
        self.feedNumber = 0
        self.questions = {}
        self.questionNumber = 0
        self.context = {}
        self.jobs = {}
        self.keys = {} if keys is None else keys
        self.tokens = {} if tokens is None else tokens
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
            "removeLive", "setBuildMode", "buildWithLeader", "setBudget", "saveSettings", "quit", "addAgents", "setSwarmFolder", "followUp")}

    def reset(self):
        self.mission = ""
        self.costs = MissionCosts()
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
        if self.onTouch:
            self.onTouch()

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
        loop.name, loop.userLock = name, self.userLock
        loop.notifyUser = lambda message: self.notify(name, message)
        loop.askUser = lambda question: self.ask(name, question)
        loop.askSecret = lambda question: self.ask(name, question, secret=True)
        loop.askPermission = lambda request: self.ask(name, f"{name} wants to {request['action']}.", kind="permission", payload=request)
        loop.askQuestions = lambda questions: self.ask(name, f"{name} has {'a question' if len(questions) == 1 else 'questions'} for you.", kind="form", payload=questions)
        loop.onUsage = self.usageChanged
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

    # ---------- The cost of the mission: every client of a model is counted, from the building of the swarm to its last run. ----------
    def makeClient(self, info, name):
        client = createModel(info, self.keys, token=self.tokens.get(info["name"]), report=lambda message: self.notify(name, message))
        self.costs.track(name, info, client)
        return client

    # After every call of a model: the window shows the new cost, and the user is told when the mission reaches 80% and 100% of its budget.
    def usageChanged(self):
        for warning in self.costs.warnings():
            self.addFeed("", warning, "warning", "event")
        self.touch()

    # The agents of the mission now, for the budget: those of the swarm while it exists (they finish), and those of the steps otherwise.
    def describeTeam(self, excluding=None):
        swarm = self.liveSwarm()
        if swarm is not None:
            team = swarm.describeTeam()
            names = {member["agent"] for member in team}
            team += [{"agent": spec["name"], "model": spec["model"], "client": spec["client"], "finished": False} for spec in self.specs if spec["name"] not in names]
        else:
            team = [{"agent": spec["name"], "model": spec["model"], "client": spec["client"], "finished": False} for spec in self.specs]
        return [member for member in team if member["agent"] != excluding]

    def describeCosts(self):
        return self.costs.report(self.describeTeam())

    def setBudget(self, payload):
        try:
            self.costs.setBudget(payload.get("budget"))
        except ValueError as error:
            raise FormError({"budget": str(error)}, str(error)) from None

    def saveSettings(self, payload):
        try:
            return {"settings": saveSettings({"maxAgents": payload.get("maxAgents")})}
        except (ValueError, OSError) as error:
            raise FormError({"maxAgents": str(error)}, str(error)) from None

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
                "tested": spec.get("tested", ""), "pending": bool(spec.get("pending")), "builder": spec["task"] == "leader", "why": spec.get("why", ""),
                "prompt": answers.get("prompt", ""), "rules": answers.get("rules") or []}

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
                           "usage": {key: client.usage.get(key, 0) for key in ("calls", "input", "output")} if hasattr(client, "usage") else None,
                           "removable": name != swarm.getLeader()})
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
        elif self.runInfo.get("reopened") and not outcome:
            state = "reopened"
        else:
            state = "unfinished"
        return {"mission": swarm.mission, "mode": swarm.getMode(), "leader": swarm.getLeader(), "agents": agents, "stages": stages, "running": running, "state": state,
                "error": describeError(outcome["error"]) if "error" in outcome and not isinstance(outcome["error"], USER_ERRORS) else str(outcome.get("error", "")),
                "summary": swarm.getSummary(), "interruption": swarm.getInterruption(), "connections": swarm.getConnections(),
                "messages": swarm.getMessages()[-80:], "ready": swarm.getReadyAgents(), "resumed": self.runInfo.get("resume", False), **self.runInfo,
                "removed": list(swarm.removed), "canJoin": not running or self.canJoin(swarm), "round": swarm.round, "requests": swarm.requests[-10:],
                "missionId": swarm.id, "canFollowUp": not running and not self.isBusy(),
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
            folders = {spec["answers"].get("folder") for spec in self.specs if spec["task"] != "leader"}
            cancelling = {key: value for key, value in self.cancelling.items() if key != "swarm"} if self.cancelling else None
            codexLogin = {key: value for key, value in self.codexLogin.items() if key != "login"} if self.codexLogin else None
        return {"version": version, "mission": self.mission, "buildMode": self.buildMode, "costs": self.describeCosts(), "settings": loadSettings(),
                "prices": describeCached("model-prices"),
                "maxAgentsLimit": MAX_AGENTS_LIMIT, "agents": agents, "mode": self.mode, "order": self.order, "keys": self.describeKeys(),
                "gpu": self.describeGpus(), "run": self.describeRun(), "questions": questions, "jobs": jobs, "unfinished": self.unfinished, "busy": self.isBusy(),
                "nativeDialogs": self.dialog is not None, "trash": self.trash["spec"]["name"] if self.trash else None, "cancelling": cancelling,
                "codexLogin": codexLogin, "swarmFolder": next(iter(folders)) if len(folders) == 1 else None, "mixedFolders": len(folders) > 1}

    # Waits until something changed after the version the browser has, then gives the state and the new lines of the feed.
    def poll(self, version, feedAfter, tab=None):
        with self.changed:
            self.changed.wait_for(lambda: self.version != version or self.closing, timeout=POLL_SECONDS)
            feed = [item for item in self.feed if item["id"] > feedAfter]
        return {"state": self.describe(), "feed": feed}

    def act(self, name, payload, tab=None):
        if name not in self.actions:
            raise ValueError(f"Unknown action: {name}.")
        if name in FREE_ACTIONS:
            result = self.actions[name](payload or {}) or {}
        else:
            with self.acting:
                result = self.actions[name](payload or {}) or {}
        self.touch()
        return {"ok": True, **result, "state": self.describe()}
