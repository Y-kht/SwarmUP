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

from architect_loop import MAX_STEPS, ArchitectDesk, ArchitectLoop, pickArchitect, sharedMember
from architect_prompts import ARCHITECT_FIX_PROMPT, ARCHITECT_FOCUS_PROMPT, ARCHITECT_PASS_PROMPT, ARCHITECT_PASS_REASONS
from harness_utils import ARCHITECT_NAME, SwarmStopped
from mcp_gateway import Gateway
from miner_service import MINERS
from pattern_digest import FOCUS_CHARS, describeWheels, draftForStreak, focusPatterns, isFix, renderBrief, renderFix, renderFocus, renderPattern
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
LAST_PASS_SECONDS = 3600
GUIDED_PATTERNS = 3
GUIDED_STEPS = 6
GUIDED_TOOLS = {"check_wheel", "save_wheel", "save_note", "dismiss_pattern", "read_wheel", "retire_wheel"}


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

    # The last pass of a run when a pattern just came to repeat, however short the run was (many short missions repeat too: in a real run of 27
    # entries the pass never came), when a wheel went wrong again since the architect last looked, or when a run that did something can still
    # learn from what repeats. Only what the architect can decide about counts: a pass with nothing to decide costs a model for nothing.
    def worthEnd(self):
        store = self.store()
        if store is None or self.swarm.journal is None:
            return False
        if store.pendingSignals():
            return True
        focus = focusPatterns(store, GUIDED_PATTERNS, project=self.project)
        looked = (store.lastPass() or {}).get("ended") or 0
        if any(isFix(row) and row["lastSeen"] > looked for row in focus):
            return True
        return self.swarm.journal.written >= MIN_ACTIVITY and any(not isFix(row) for row in focus)

    def mayRunNow(self, reason):
        if self.cancelled.is_set() or self.overBudget():
            return False
        info, how = pickArchitect(self.swarm, self.choice)
        if info is None:
            return False
        if reason == "signal":
            if how in ("share", "driver") or self.passesThisRun >= MAX_PASSES_WHILE_WORKING or time.time() - self.lastPass < PASS_GAP_SECONDS:
                return False
        return True

    # ---------- One pass. ----------
    def ensureLoop(self):
        info, how = pickArchitect(self.swarm, self.choice)
        shared = sharedMember(self.swarm, self.choice)["agent"].agent if how == "share" else None
        if self.loop is not None and (shared is None or self.loop.agent is shared):
            return self.loop
        if how == "share":
            client = shared
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
        prompt = ARCHITECT_PASS_PROMPT.format(why=ARCHITECT_PASS_REASONS.get(reason, ""), brief=brief, steps=MAX_STEPS,
                                              focus=renderFocus(store, claimedBy=self.desk.passId, project=self.project))
        self.desk.listings = 0
        focus = focusPatterns(store, 1, self.desk.passId, self.project)
        self.desk.focus = f"#{focus[0]['id']}" if focus else ""
        outcome, report = "done", ""
        try:
            report = (self.guidedPass(loop, store) if (self.info or {}).get("local") else loop.askAgent(prompt, own=True)) or "Nothing worth automating in this pass."
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
        self.unloadModel()

    # A local model of its own is only needed during a pass: the agents need the memory of the GPU the rest of the time. It loads again for the next pass.
    # The same goes for the model of an agent it shares, unless it is the one of the leader, which still works.
    def unloadModel(self):
        leader = self.swarm.members.get(self.swarm.leader) or {}
        if hasattr(self.client, "unload") and (self.how == "own" or self.client is not getattr(leader.get("agent"), "agent", None)):
            self.client.unload()

    # A small local model gets lost in an open task: it decides about the best patterns one at a time, each in a short conversation of its own, with
    # only the tools of a decision. A wheel to repair comes first, with its code and what went wrong. A capable model of an API works freely on the whole brief.
    def guidedPass(self, loop, store):
        scope, steps, reports = loop.toolScope, loop.maxSteps, []
        loop.toolScope, loop.maxSteps = {"architect": GUIDED_TOOLS}, GUIDED_STEPS
        try:
            for row in focusPatterns(store, GUIDED_PATTERNS, self.desk.passId, self.project):
                self.desk.focus, loop.conversation = f"#{row['id']}", None
                reports.append(f"#{row['id']}: {loop.askAgent(self.guidedPrompt(store, row), own=True)}")
        finally:
            loop.toolScope, loop.maxSteps, loop.conversation = scope, steps, None
        return "\n".join(reports)

    def guidedPrompt(self, store, row):
        if isFix(row):
            return ARCHITECT_FIX_PROMPT.format(steps=GUIDED_STEPS, id=f"#{row['id']}", fix=renderFix(store, row, self.project))
        wheels = "\n".join(describeWheels(store, self.project)) or "- none yet"
        return ARCHITECT_FOCUS_PROMPT.format(steps=GUIDED_STEPS, pattern=renderPattern(store, str(row["id"]), examples=2)[:FOCUS_CHARS], wheels=wheels, draft=draftForStreak(row))

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
    # A shared model works for its agent too, so a stop of the swarm does not end what it writes. model ends it all the same: no agent works anymore.
    def cancel(self, model=False):
        self.cancelled.set()
        self.desk.stopped.set()
        if self.gateway is not None:
            self.gateway.cancelAll()
        client = self.client
        if client is not None and hasattr(client, "cancel") and (self.how == "own" or model):
            threading.Thread(target=client.cancel, daemon=True).start()

    # The last pass of a run is what the swarm learned. It says if the architect is still at work, and finish waits for it (a stop ends it at once).
    def isWorking(self):
        thread = self.thread
        return thread is not None and thread.is_alive()

    def finish(self, seconds=LAST_PASS_SECONDS):
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(seconds)

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
