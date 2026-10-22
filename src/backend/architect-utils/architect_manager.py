# The architect of a swarm. Every swarm has one, working in the background for as long as the swarm runs: it never takes part in the work of
# the team, never waits for the user and never asks the user anything. While the agents work, the miner of the project (miner_service.py)
# reads their journal; when patterns start to repeat, it signals the architect, which does a pass: it reads the brief of the patterns, writes
# or updates wheels, and keeps notes (architect_loop.py, server_architect.py). At the end of each run it does a last pass over everything the
# run did. A pass is a new conversation of at most a few steps, on the model the user chose for the architect at the setup of the swarm.
# The architect spends at most its share of the budget of the mission (settings: architectShare, or architectDollars without a budget).
# Its pass never slows the agents: it has its own thread and its own gateway, a model of its own (or the local model of the leader, which it
# then uses only when the run is over), and a stop of the swarm stops it at its next step.
import queue
import threading
import time
import traceback

from architect_loop import MAX_STEPS, ArchitectDesk, ArchitectLoop, pickArchitect
from architect_prompts import ARCHITECT_PASS_PROMPT, ARCHITECT_PASS_REASONS
from harness_utils import ARCHITECT_NAME, SwarmStopped
from mcp_gateway import Gateway
from miner_service import MINERS
from pattern_digest import renderBrief
from project_identity import projectFor, readProject
from user_settings import currentSetting


DEBOUNCE_SECONDS = 45
MAX_PASSES_WHILE_WORKING = 3
PASS_GAP_SECONDS = 300
DRAIN_SECONDS = 30
MIN_ACTIVITY = 30
JOIN_SECONDS = 5
REPORTS = 20
CLAIM_SECONDS = 1200


class ArchitectManager:
    # choice: the model the user chose for the architect ({"same": agent} or the info of a model); makeClient(info) makes a client of its own;
    # say(text) shows what the architect does (its window in the command line, the feed of the window).
    def __init__(self, swarm, choice, makeClient=None, say=None):
        self.swarm, self.choice, self.makeClient, self.say = swarm, choice, makeClient, say
        self.queue, self.thread, self.service = queue.Queue(), None, None
        self.loop, self.client, self.info, self.how, self.gateway = None, None, None, "", None
        self.state, self.reports, self.wheels, self.notes = "idle", [], [], []
        self.passesThisRun, self.lastPass, self.passCount = 0, 0.0, 0
        self.cancelled = threading.Event()
        self.lock, self.running = threading.RLock(), False
        self.desk = ArchitectDesk(self)
        self.failure = ""
        swarm.architect = self
        swarm.addListener(self.onEvent)

    @property
    def project(self):
        return self.swarm.project or projectFor(None)[0]

    @property
    def projectName(self):
        return readProject(self.project).get("name") or self.project

    @property
    def missionId(self):
        return self.swarm.id

    @property
    def modelName(self):
        info = self.info or pickArchitect(self.swarm, self.choice)[0] or {}
        return info.get("name") or ""

    def describeState(self):
        return {"choice": self.choice, "passes": self.passCount, "wheels": self.wheels[-REPORTS:], "notes": self.notes[-REPORTS:]}

    def restoreState(self, saved):
        saved = saved or {}
        self.passCount = saved.get("passes") or 0
        self.wheels, self.notes = list(saved.get("wheels") or []), list(saved.get("notes") or [])

    # What the window and the command line show of the architect.
    def describe(self):
        info = pickArchitect(self.swarm, self.choice)[0]
        return {"state": self.state, "model": (info or {}).get("name") or "", "available": info is not None, "passes": self.passCount, "wheels": self.wheels[-REPORTS:],
                "notes": self.notes[-REPORTS:], "report": self.reports[-1] if self.reports else "", "spent": round(self.spent(), 4), "failure": self.failure}

    # ---------- The events of the swarm: the architect only takes note here (the swarm may hold its lock). ----------
    def onEvent(self, event):
        kind = event["kind"]
        if kind == "run":
            self.start()
        elif kind == "stopped":
            self.cancel()
        elif kind == "finished":
            with self.lock:
                self.running = False
            self.queue.put(("end", None))

    # A run starts: the architect follows it (its thread may still be busy with the last pass of the run before; it then goes on with this one).
    def start(self):
        with self.lock:
            self.cancelled.clear()
            self.desk.stopped.clear()
            self.passesThisRun, self.running = 0, True
            if self.service is None:
                self.service = MINERS.acquire(self.project, id(self))
                self.service.subscribe(self.onSignals)
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.work, args=(self.queue,), name=f"swarmup-architect-{self.missionId}", daemon=True)
            self.thread.start()

    def onSignals(self, signals):
        self.queue.put(("signal", [signal["id"] for signal in signals]))

    def work(self, items):
        while True:
            try:
                kind, data = items.get()
                if kind == "signal":
                    kind = self.debounce(items)
                    if kind == "signal" and self.mayRunNow("signal"):
                        self.runPass("signal")
                if kind == "end":
                    self.endRun()
            except Exception:
                traceback.print_exc()
                kind = "end"
            if kind in ("end", "stop"):
                with self.lock:
                    if kind == "end" and self.running:
                        continue
                    self.thread = None
                    self.releaseMiner()
                    return

    # Signals come in bursts: the architect waits a little for the others, unless the run ends or stops meanwhile.
    def debounce(self, items):
        deadline = time.monotonic() + DEBOUNCE_SECONDS
        while time.monotonic() < deadline and not self.cancelled.is_set():
            try:
                kind, data = items.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if kind in ("end", "stop"):
                return kind
        return "signal"

    def endRun(self):
        if self.service is not None:
            self.service.drain(DRAIN_SECONDS)
        if self.mayRunNow("end") and self.worthEnd():
            self.runPass("end")

    # The last pass of a run only when the run did something and something waits for the architect.
    def worthEnd(self):
        store = self.store()
        if store is None or self.swarm.journal is None:
            return False
        if self.swarm.journal.written < MIN_ACTIVITY:
            return False
        return bool(store.pendingSignals()) or bool(store.patterns(statuses=("repeating", "cross"), limit=1))

    def mayRunNow(self, reason):
        if self.cancelled.is_set() or self.overBudget():
            return False
        info, how = pickArchitect(self.swarm, self.choice)
        if info is None:
            return False
        if reason == "signal":
            if how == "share" or self.passesThisRun >= MAX_PASSES_WHILE_WORKING or time.time() - self.lastPass < PASS_GAP_SECONDS:
                return False
        return True

    # ---------- One pass. ----------
    def ensureLoop(self):
        if self.loop is not None:
            return self.loop
        info, how = pickArchitect(self.swarm, self.choice)
        if how == "share":
            client = self.swarm.members[self.choice["same"]]["agent"].agent
        else:
            client = self.makeClient(info) if self.makeClient else None
            if client is None:
                return None
            self.swarm.costs.track(ARCHITECT_NAME, info, client)
        self.client, self.info, self.how = client, info, how
        loop = ArchitectLoop(client)
        loop.desk = loop.team = self.desk
        loop.say = self.tell
        root = readProject(self.project).get("folder")
        if root:
            try:
                loop.setFolder(root)
            except ValueError:
                pass
        self.loop = loop
        return loop

    def store(self):
        return self.service.storeOf() if self.service is not None else None

    def runPass(self, reason):
        try:
            loop = self.ensureLoop()
        except Exception as error:
            self.failure = f"The model of the architect could not start: {error}"
            self.tell(self.failure)
            return
        store = self.store()
        if loop is None or store is None:
            return
        started = time.time()
        signals = store.pendingSignals()
        self.passCount += 1
        self.desk.passId, self.desk.since = f"{self.missionId}:{self.passCount}", self.lastPass or None
        store.claim([signal["pattern"] for signal in signals], self.desk.passId, CLAIM_SECONDS)
        self.gateway = self.gateway or Gateway(self.missionId, folder=self.callsFolder())
        loop.gateway, loop.conversation = self.gateway, None
        self.setState("working", f"pass {self.passCount}: {ARCHITECT_PASS_REASONS.get(reason, '')}")
        brief = renderBrief(store, self.project, self.desk.since, claimedBy=self.desk.passId)
        prompt = ARCHITECT_PASS_PROMPT.format(why=ARCHITECT_PASS_REASONS.get(reason, ""), brief=brief, steps=MAX_STEPS)
        outcome, report = "done", ""
        try:
            report = loop.askAgent(prompt, own=True) or "Nothing worth automating in this pass."
        except SwarmStopped:
            outcome, report = "stopped", "The pass was stopped."
        except Exception as error:
            outcome, report = "failed", f"The pass failed: {type(error).__name__}: {error}"
        finally:
            loop.conversation = None
            store.release(self.desk.passId)
            if signals:
                store.consumeSignals(signals[-1]["id"])
            self.lastPass = time.time()
            if reason == "signal":
                self.passesThisRun += 1
        self.reports = [*self.reports[-(REPORTS - 1):], {"time": time.strftime("%H:%M:%S"), "reason": reason, "outcome": outcome, "text": report[:3000]}]
        try:
            store.addPass(self.missionId, started, time.time(), outcome, self.spent(), [signal["pattern"] for signal in signals], report[:3000])
        except Exception:
            traceback.print_exc()
        self.tell(f"Pass {self.passCount} ({outcome}): {report}")
        self.setState("idle", "")

    def callsFolder(self):
        session = self.swarm.session
        return session.folder / "architect" if session is not None else None

    def setState(self, state, text):
        self.state = state
        self.swarm.emit("architect", ARCHITECT_NAME, state=state, text=text, passes=self.passCount)

    def tell(self, text):
        if self.say is not None:
            try:
                self.say(text)
            except Exception:
                traceback.print_exc()

    # ---------- The budget. ----------
    def spent(self):
        try:
            return (self.swarm.costs.spending().get(ARCHITECT_NAME) or {}).get("cost", 0.0)
        except Exception:
            return 0.0

    def cap(self):
        budget = self.swarm.costs.budget
        return budget * currentSetting("architectShare") / 100 if budget else float(currentSetting("architectDollars"))

    def overBudget(self):
        return self.how == "own" and self.spent() >= self.cap()

    # ---------- What the architect made: the swarm, the leader and the user know it. ----------
    def onWheel(self, action, entry, why):
        name = entry["name"]
        self.wheels = [*self.wheels[-(REPORTS - 1):], {"name": name, "version": entry["version"], "action": action, "summary": entry.get("summary") or "", "why": why or ""}]
        if self.swarm.journal is not None:
            self.swarm.journal.add("wheel", ARCHITECT_NAME, name=name, action=action, version=entry["version"], summary=entry.get("summary") or "")
        self.swarm.emit("wheel", ARCHITECT_NAME, action=action, name=name, version=entry["version"], summary=entry.get("summary") or "", why=why or "")

    def onNote(self, note):
        self.notes = [*self.notes[-(REPORTS - 1):], {"id": note["id"], "text": note["text"][:300]}]

    # ---------- Stop and close. ----------
    def cancel(self):
        self.cancelled.set()
        self.desk.stopped.set()
        if self.gateway is not None:
            self.gateway.cancelAll()
        client = self.client
        if client is not None and hasattr(client, "cancel") and self.how == "own":
            threading.Thread(target=client.cancel, daemon=True).start()

    # The architect stops for good: before the models are unloaded and when the program closes.
    def close(self):
        with self.lock:
            self.running = False
        self.cancel()
        self.queue.put(("stop", None))
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(JOIN_SECONDS)
        self.releaseMiner()
        if self.gateway is not None:
            self.gateway.close()
            self.gateway = None

    def releaseMiner(self):
        service, self.service = self.service, None
        if service is not None:
            service.unsubscribe(self.onSignals)
            MINERS.release(self.project, id(self))


# Gives every swarm its architect (the five places a swarm starts: built by the leader, built by the user, added to, resumed in the command
# line and in the window).
def attachArchitect(swarm, choice, makeClient=None, say=None):
    if swarm.architect is not None:
        swarm.architect.choice = choice or swarm.architect.choice
        swarm.architect.makeClient = makeClient or swarm.architect.makeClient
        swarm.architect.say = say or swarm.architect.say
        return swarm.architect
    return ArchitectManager(swarm, choice, makeClient, say)
