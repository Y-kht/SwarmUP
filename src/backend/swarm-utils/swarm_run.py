import threading
import time
import traceback
from datetime import datetime
from functools import partial

from agent_storehouse import WorkSession, clearSession, makeRunName
from harness_utils import STATE_LOCK, STOPPED_MESSAGE, SwarmStopped
from saved_swarms import clearSwarmState, saveSwarmState


HEARTBEAT_SECONDS = 5

STOP_TIMEOUT = 10


# How a swarm (Swarm in swarm_harness.py) runs: the groups of agents that work at the same time, the leader last, and what is saved
# while it runs.
class SwarmRun:
    # Ends for good a swarm that is not running (one found on the disk): what the agents that did not finish changed is undone,
    # and the saved state is deleted.
    def abandon(self):
        if self.isRunning():
            raise ValueError("The swarm is running. Stop it first.")
        for member in self.members.values():
            if member["status"] != "done":
                member["agent"].rollback()
        clearSwarmState(self.id)
        clearSession(self.id)
        self.emit("stopped")

    def isRunning(self):
        return self.active or (self.thread is not None and self.thread.is_alive())

    # For a program that is about to stop while the swarm runs: the state is saved as interrupted, so the next start finds it at once.
    # A run that is still getting ready (its thread started, but it is not active yet) is waited for, so it is saved too.
    def saveForExit(self):
        deadline = time.monotonic() + STOP_TIMEOUT
        while not self.active and self.thread is not None and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.active:
            self.closeRun(False)

    # Runs the swarm in its own thread, so the program (or the user interface) stays free while it works. wait() gives the outcome:
    # {"result": ...} or {"error": ...}.
    def startInBackground(self, resume=False):
        if self.isRunning():
            raise ValueError("The swarm is already running.")
        self.outcome = {}
        self.thread = threading.Thread(target=self.runForOutcome, args=(resume,), daemon=True)
        self.thread.start()

    def runForOutcome(self, resume):
        try:
            self.outcome["result"] = self.run(resume)
        except Exception as error:
            self.outcome["error"] = error

    def wait(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return self.outcome

    # An agent is busy while it writes, acts, or is about to start. One that waits for another agent, or for the user, or is finished, is not.
    def isBusy(self, name, stage):
        member, thread = self.members[name], stage["threads"][name]
        if not thread.is_alive():
            return False
        if member["status"] == "waiting":
            needed = [] if stage["plan"] else member["waitsFor"]
            return not member["startAt"] and all(self.members[other]["status"] in ("done", "failed") for other in needed if other in self.members)
        return member["status"] == "working" and member["review"] != "ready"

    # Runs while a group of agents works. Each time none of them is busy, the leader summarises where they stand and the user decides.
    # An agent the user decided about on its own goes on without waiting for the others, and so do the agents that wait for it.
    # It returns when no draft waits anymore and all the agents are finished. An agent that sleeps until its time is not finished:
    # nothing is left to do until it wakes up (or is started), and then it is looked after like the others.
    # A lost connection comes before everything else: the user decides to continue or to cancel. A stopped swarm returns at once.
    # The agents of the stage are read again at every step, because an agent can join it or leave it while it works. The stage closes in the
    # same step that sees nothing left to do, so an agent cannot join a stage that is already over.
    def reviewStage(self, stage):
        request = ""
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.stopped or not any(self.isBusy(name, stage) for name in stage["names"]))
                if self.stopped:
                    stage["open"] = False
                    return
                interruption = self.interruption
                shown = {name: self.members[name]["revision"] for name in stage["names"] if self.members[name]["review"] == "ready"}
                if not shown and not interruption and any(stage["threads"][name].is_alive() for name in stage["names"]):
                    self.changed.wait_for(lambda: any(self.isBusy(name, stage) or self.members[name]["review"] == "ready" for name in stage["names"])
                                          or self.interruption or self.stopped or not any(stage["threads"][name].is_alive() for name in stage["names"]))
                    continue
                if not shown and not interruption:
                    stage["open"] = False
                    return
            if interruption:
                self.askAboutInterruption(interruption)
            else:
                request = self.reviewRound(shown, request)

    # An agent with a time of its own (the news briefer) sleeps until then, without holding the others back. startNow wakes it up.
    # An agent that was already at work when the swarm was interrupted does not wait again, and one that was waiting for its time keeps it.
    def waitForStart(self, name):
        member = self.anyMember(name)
        if member["started"]:
            return
        start = member["resumeStart"] or member["agent"].startTime()
        member["resumeStart"] = None
        seconds = (start - datetime.now()).total_seconds() if start else 0
        if seconds > 0:
            member["wake"].clear()
            member["startAt"] = start
            self.emit("scheduled", name, startAt=f"{start:%Y-%m-%d %H:%M}")
            with self.changed:
                self.changed.notify_all()
            member["wake"].wait(seconds)
            member["startAt"] = None
            with self.changed:
                self.changed.notify_all()

    def startNow(self, name):
        member = self.getMember(name)
        if not member["startAt"]:
            raise ValueError(f"{name} is not waiting for a time to start.")
        member["wake"].set()

    def runLeader(self, name):
        self.waitForStart(name)
        self.runMember(name)

    # One failing agent never stops the swarm. Its error is kept and reported to the leader.
    # An agent that is already done or failed, because the swarm was interrupted after that, is not run again, and neither is one that was removed.
    def runMember(self, name):
        member = self.anyMember(name)
        if member["status"] in ("done", "failed") or member.get("removed"):
            return
        if self.stopped:
            member["error"] = STOPPED_MESSAGE
            self.setStatus(name, "failed")
            return
        member["started"] = True
        self.setStatus(name, "working")
        try:
            member["result"] = member["agent"].makePlan(member["task"]) if member["mode"] == "plan" else member["agent"].run()
        except SwarmStopped as stopped:
            member["error"] = str(stopped) or STOPPED_MESSAGE
        except Exception as error:
            member["error"] = f"{type(error).__name__}: {error}"
        if member["result"] is None and not member["error"]:
            member["error"] = "It did not finish, for example because the user did not approve."
        # What an agent that did not finish changed with its tools is put back (a removed agent is put back when it retires).
        if member["result"] is None and not member.get("removed"):
            try:
                member["agent"].rollback()
            except Exception:
                traceback.print_exc()
        self.setStatus(name, "done" if member["result"] is not None else "failed")

    # A result is delivered once: after a resume, the agent already has the results it received before the swarm stopped.
    def deliverResult(self, sender, receiver):
        result = str(self.members[sender]["result"])
        if not any(message["sender"] == sender and message["receiver"] == receiver and message["message"] == result for message in self.messages):
            self.communicate(sender, receiver, result)

    # Runs in its own thread. It waits for the agents it needs, works, and then reports to the leader. The agents it waits for are read again
    # after each wait, because one of them may leave the swarm meanwhile (it is then not waited for anymore). An agent that left does not report.
    def runWorker(self, name):
        member = self.anyMember(name)
        try:
            if member["status"] in ("done", "failed") or member.get("removed"):
                return
            for other in list(member["waitsFor"]):
                if other in self.finished:
                    self.finished[other].wait()
            if member.get("removed"):
                return
            waited = [other for other in member["waitsFor"] if other in self.members]
            missing = [other for other in waited if self.members[other]["result"] is None]
            if missing:
                member["error"] = f"{', '.join(missing)} did not finish, so {name} could not start."
                self.setStatus(name, "failed")
            else:
                for other in waited:
                    self.deliverResult(other, name)
                self.waitForStart(name)
                self.runMember(name)
            if not member.get("removed"):
                outcome = member["result"] if member["status"] == "done" else f"FAILED. {member['error']}"
                self.communicate(name, self.leader, f"{member['role']}: {outcome}")
        finally:
            if name in self.finished:
                self.finished[name].set()

    # Everything of the last run is cleared, except the plans approved in plan mode, which the agents follow when they execute, and the notes of
    # the session. A resumed run keeps it all: what is done stays done, and the agents that did not finish remember what they did.
    # Each new run saves its results in a folder of its own, and a resumed run in the same one.
    def prepareRun(self, resume=False):
        self.stopped, self.interruption, self.saveFailed = False, None, False
        if not (resume and self.runName):
            self.runName = makeRunName(self.mission)
        self.session = WorkSession(self.id, self.runName)
        if not resume:
            self.messages, self.summary = [], ""
        for name, member in self.members.items():
            if resume:
                self.settle(member)
            else:
                member.update(status="waiting", result=None, error="", mode=self.mode, review="", draft="", problem="", decision=None, revision=0,
                              startAt=None, started=False, resumeStart=None)
            member["wake"].clear()
            agent = member["agent"]
            if not resume:
                agent.inbox, agent.userMessages, agent.progress, agent.actions = [], [], {}, []
                agent.conversation, agent.activity = None, []
                if self.mode == "plan":
                    agent.approvedPlan = ""
            self.connectAgent(name, member, resume)
        self.active = True
        self.startHeartbeat()
        self.emit("run", mode=self.mode)
        if not resume:
            for name in self.members:
                if name != self.leader:
                    self.communicate(self.leader, name, self.describe(name))
            self.getMember(self.leader)["agent"].receive("swarm", self.describe(self.leader))

    # What a swarm gives to the loop of an agent while it runs: who reviews its drafts, what happens when the connection is lost, when it is saved,
    # the memory of the session, and the swarm itself, which its tools reach. What the user allowed its tools until the swarm runs again ends here.
    # What the user allowed a coding agent "until the swarm runs again" ends here. The leader of a managed swarm has what it writes read by the manager.
    def connectAgent(self, name, member, resume):
        agent = member["agent"]
        agent.resumed = resume
        if hasattr(agent.agent, "newRun"):
            agent.agent.newRun()
        agent.name, agent.reviewer, agent.session = name, partial(self.waitForReview, name), self.session
        agent.team, agent.allowed, agent.onActivity = self, set(), partial(self.noteActivity, name)
        agent.onConnectionLost, agent.onProgress = partial(self.waitForResume, name), self.checkpoint
        agent.onAnswer = self.manager.readOutput if self.manager is not None and name == self.leader else None

    # While the swarm runs its state is also written every few seconds. A saved state that is not renewed is of a program that stopped.
    def startHeartbeat(self):
        self.heartbeat = threading.Event()
        threading.Thread(target=self.beat, args=(self.heartbeat,), daemon=True).start()

    def beat(self, stop):
        while not stop.wait(HEARTBEAT_SECONDS):
            self.checkpoint()

    # The run is over. A swarm that ended (or that the user stopped) has nothing left to continue, so its saved state is deleted.
    # One that was cut short by an error keeps it, marked as interrupted, so the next start finds it at once.
    def closeRun(self, ended):
        self.heartbeat.set()
        try:
            with self.changed:
                state = self.describeState()
        except Exception:
            state = None
        with STATE_LOCK:
            self.active = False
            try:
                if ended or self.stopped:
                    clearSwarmState(self.id)
                elif state:
                    saveSwarmState(self.id, {**state, "state": "interrupted"})
            except OSError:
                traceback.print_exc()

    def runInThread(self, work, name):
        try:
            work(name)
        finally:
            with self.changed:
                self.changed.notify_all()

    # A group of agents that works together (all of them in plan mode, the workers then the leader in execute mode). Each one starts when the
    # agents it waits for are done (never in plan mode), and the user is looked after (reviewStage) until all of them are finished.
    # The stage is kept in self.stage, so an agent can join it (addAgent) or leave it (removeAgent) while it works.
    def runStage(self, names, work, plan=False):
        stage = {"names": [], "threads": {}, "work": work, "plan": plan, "open": True}
        with self.changed:
            self.stage = stage
            for name in names:
                self.startInStage(stage, name)
        self.reviewStage(stage)
        for thread in list(stage["threads"].values()):
            thread.join(timeout=STOP_TIMEOUT if self.stopped else None)

    def startInStage(self, stage, name):
        thread = threading.Thread(target=self.runInThread, args=(stage["work"], name), daemon=True)
        stage["names"].append(name)
        stage["threads"][name] = thread
        thread.start()

    # Plan mode: all the agents, the leader too, write their plans at the same time. It returns the summary plan, or None if a plan was not approved.
    # If the user approved every plan on its own and never saw a summary, the leader writes one now.
    def runPlanning(self):
        self.runStage(list(self.members), self.runMember, plan=True)
        if any(member["status"] != "done" for member in self.members.values()):
            return None
        if not self.summary:
            self.getMember(self.leader)["agent"].notifyUser(f"[{self.leader}] Summary of the approved plans:\n{self.writeSummary('')}")
        return self.summary if len(self.members) > 1 else self.members[self.leader]["result"]

    # Execute mode: each agent starts when the agents it waits for are done, then the leader works last.
    def runExecution(self):
        workers = [name for name in self.members if name != self.leader]
        self.finished = {name: threading.Event() for name in workers}
        self.runStage(workers, self.runWorker)
        if self.stopped:
            return None
        self.runStage([self.leader], self.runLeader)
        return self.members[self.leader]["result"]

    # With resume=True the swarm goes on where it was interrupted (see resume) instead of starting again.
    def run(self, resume=False):
        self.getStages()
        self.checkGpus()
        self.prepareRun(resume)
        result, ended = None, False
        try:
            result = self.runPlanning() if self.mode == "plan" else self.runExecution()
            ended = True
            return result
        finally:
            for member in self.members.values():
                agent = member["agent"]
                agent.reviewer = agent.onConnectionLost = agent.onProgress = agent.onAnswer = agent.session = agent.team = agent.onActivity = None
                agent.resumed = False
            self.stage = None
            self.closeRun(ended)
            self.emit("finished", mode=self.mode, ok=result is not None)
