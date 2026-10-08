# The swarm: a team of loops that work together (see the description of Swarm below). Its parts are in swarm_team.py,
# swarm_review.py and swarm_run.py, and its saved state in saved_swarms.py.
import os
import threading
import traceback
from datetime import datetime

from harness_utils import STATE_LOCK, STATE_VERSION
from mission_costs import MissionCosts
from saved_swarms import saveSwarmState
from swarm_review import SwarmReview
from swarm_rounds import SwarmRounds
from swarm_run import SwarmRun
from swarm_team import SwarmTeam


# What is saved of an agent of a swarm, apart from its loop and the lists.
SAVED_FIELDS = ("role", "task", "boss", "model", "recipe", "status", "result", "error", "mode", "review", "draft", "problem", "revision", "started", "sitsOut")


# ==============
# MAJOR: Swarm harness. Multiple harnesses/models interacting with each other.
# An agent of the swarm is any loop above. The first agent added is the leader.
# The leader decides who waits for whom (waitsFor, or planWithLeader). The agents that wait for nobody
# work at the same time. An agent only starts after the agents it waits for finished, and receives their results.
# The leader works last. Each agent reports to the leader when it is done. getStages shows the groups of agents that work at the same time.
# The status of an agent is waiting, working, done or failed. Only the agents that are working are active.
# The get methods are made for a user interface to show the swarm as a tree and to follow it while it runs.
# When the user clicks an agent of the tree (the leader too), sendUserMessage gives it a message without stopping it.
# The message is logged with the sender USER_NAME, so getMessages(USER_NAME, name) shows what the user told an agent.
# All the messages are cleared when the swarm runs again.
#
# The whole swarm is in plan mode or in execute mode (setMode), and each agent plans or executes its own task.
# In both modes an agent writes a draft (its plan, or what it is about to do) and waits for the user. Nothing is done before the approval.
# The review of an agent is "ready" while its draft waits for the user (a light a user interface can turn green), then approved or rejected.
# The model of an agent (getModelInfo in models_library.py) is chosen when it is added. Every local model needs VRAM, and the swarm needs
# the sum of them. checkModel tells before the choice if the GPUs can host the model, and getVramStatus shows the total while the swarm is built.
# A model that does not fit in the GPUs cannot be chosen (the user changes to a smaller one with setModel, or uses an API model).
# One that fits but not in the memory that is free now can be chosen with a warning, and run refuses until the memory is free.
# The user can approve (approveDraft), reject (rejectDraft) or correct (correctDraft) an agent on its own by clicking on its name.
# An approved agent goes on right away, without waiting for the others, and the agents that wait for it start when it is done.
# When no agent is busy, the leader summarises where every agent stands, what the user already approved included, and the user
# approves, rejects or corrects the summary as a whole, or types the name of an agent to decide about it alone.
# A correction of the summary is sent by the leader to the agents it concerns. A correction of one agent (correctDraft, or a message
# to it) only changes the draft of that agent. In both cases the leader summarises again afterwards.
# A user interface follows the swarm live with addListener: every change (the status or the review of an agent, a message, a summary)
# is given to the listeners as an event. getTree has, for every agent, who it waits for (waitsFor), who it still waits for (waitingOn),
# and the time it is scheduled for (startAt, for an agent with a time of its own, like the news briefer, which startNow wakes up).
#
# The work of the user is never lost. While the swarm runs its whole state is saved at every change (see saved_swarms.py), so a swarm that
# was interrupted is found again with findUnfinishedSwarms, brought back with restore (the user interface builds the agents again), and
# goes on where it stopped with resume. The agents remember what the user approved, so nothing is asked or sent twice.
# If the internet is lost while an agent works, the agent is paused (status paused), not failed, and the leader tells the user, who
# continues (continueWork, which tries again) or cancels. To cancel, the leader summarises every change that was made (summarizeChanges)
# and the user confirms (stopWork) or goes on. startInBackground runs the swarm in its own thread, followed with isRunning, wait and outcome.
# ==============
class Swarm(SwarmTeam, SwarmReview, SwarmRun, SwarmRounds):
    def __init__(self, mission):
        self.mission = mission
        self.members = {}
        self.leader = None
        self.messages = []
        self.mode = "execute"
        self.summary = ""
        self.listeners = []
        # Everything about the drafts waiting for the user changes under this condition, which the waiting threads sleep on.
        self.changed = threading.Condition()
        self.id = f"{datetime.now():%Y%m%d_%H%M%S}_{os.urandom(2).hex()}"
        self.stopped = False
        self.interruption = None
        # active is True while the swarm runs. The snapshots are numbered, so an old one is never saved over a newer one.
        self.active = False
        self.snapshots = 0
        self.saved = 0
        self.saveFailed = False
        self.heartbeat = threading.Event()
        self.thread = None
        self.outcome = {}
        # The agents taken out of the swarm (removeAgent): departed holds those whose thread did not end yet, removed is the history.
        self.departed = {}
        self.removed = []
        # The group of agents that works now (runStage), and the events of the workers that finished (execute mode).
        self.stage = None
        self.finished = {}
        # The leader manages the swarm when a manager reads what it writes (see LeaderManager in leader_manager.py).
        self.manager = None
        # What the models of the mission spent. A user interface gives the swarm the costs of the whole mission (its building included).
        self.costs = MissionCosts()
        # The memory of the session, shared by the agents (WorkSession in agent_storehouse.py), and the name of the run, where the results are saved.
        self.session = None
        self.runName = ""
        # A mission has rounds: the first request of the user, then each follow-up. requests holds them all, {round, text, time}.
        self.round = 1
        self.requests = []
        # What the leader gave each agent to do in this round, after the first ({agent: part}), None before it decided.
        self.roundParts = None

    def getMember(self, name):
        if name not in self.members:
            raise ValueError(f"There is no agent called {name} in the swarm.")
        return self.members[name]

    # A listener is a function that takes an event: a dictionary with the kind (status, review, message, summary, scheduled, run, finished,
    # connectionLost, resumed or stopped), the agent it is about, the time, and details. A listener that fails is reported but never stops the swarm.
    def addListener(self, listener):
        self.listeners.append(listener)

    def emit(self, kind, agent="", **details):
        event = {"kind": kind, "agent": agent, "time": f"{datetime.now():%H:%M:%S}", **details}
        self.checkpoint()
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                traceback.print_exc()

    # What is saved of every agent. The loop of the agent saves its own state, and the recipe is what the user interface needs to build it again.
    def describeState(self):
        members = {}
        for name, member in self.members.items():
            start = member["startAt"]
            members[name] = {**{field: member.get(field) for field in SAVED_FIELDS}, "waitsFor": list(member["waitsFor"]),
                             "startAt": f"{start:%Y-%m-%d %H:%M}" if start else None, "state": member["agent"].getState()}
        return {"version": STATE_VERSION, "id": self.id, "mission": self.mission, "mode": self.mode, "leader": self.leader,
                "state": "paused" if self.interruption else "running", "heartbeat": datetime.now().timestamp(), "pid": os.getpid(),
                "savedAt": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "summary": self.summary, "messages": list(self.messages), "members": members,
                "removed": list(self.removed), "managed": self.manager is not None, "costs": self.costs.state(), "runName": self.runName,
                "round": self.round, "requests": list(self.requests), "roundParts": self.roundParts}

    # Writes the state to the disk. Whatever goes wrong, the swarm goes on, and the user is told once that the work cannot be continued after a stop.
    # The user is told from another thread, because speaking to the user can wait for a long time, and the locks of the swarm may be held here.
    def checkpoint(self):
        if not self.active:
            return
        failure = None
        try:
            with self.changed:
                self.snapshots += 1
                number, state = self.snapshots, self.describeState()
            with STATE_LOCK:
                if not self.active or number <= self.saved:
                    return
                self.saved = number
                saveSwarmState(self.id, state)
        except Exception as error:
            failure = error
        if failure and not self.saveFailed:
            self.saveFailed = True
            warning = f"Warning: the state of the swarm could not be saved ({failure}). If the program stops, its work cannot be continued."
            threading.Thread(target=self.getMember(self.leader)["agent"].notifyUser, args=(warning,), daemon=True).start()

    # Writes in the memory of the mission (MissionMemory in mission_memory.py) while the swarm runs, for example
    # record("addUserPrompt", round, text). The memory is only a record: a problem with it never stops the swarm.
    def record(self, name, *details, **options):
        if self.session is None:
            return
        try:
            getattr(self.session.memory, name)(*details, **options)
        except Exception:
            traceback.print_exc()

    # Brings back a swarm found by findUnfinishedSwarms. makeAgent(name, saved) gives the loop of an agent, built again from saved["recipe"]
    # and saved["model"] (the passwords are asked again, because they are never saved). Then resume() goes on where the swarm stopped.
    @classmethod
    def restore(cls, saved, makeAgent):
        swarm = cls(saved["mission"])
        swarm.id, swarm.mode, swarm.leader = saved["id"], saved["mode"], saved["leader"]
        swarm.summary, swarm.messages = saved.get("summary", ""), list(saved.get("messages", []))
        swarm.removed = list(saved.get("removed", []))
        swarm.runName = saved.get("runName", "")
        swarm.round, swarm.requests, swarm.roundParts = saved.get("round", 1), list(saved.get("requests", [])), saved.get("roundParts")
        swarm.costs.restore(saved.get("costs") or {})
        for name, data in saved["members"].items():
            agent = makeAgent(name, data)
            agent.setState(data.get("state", {}))
            member = swarm.newMember(name, agent, data["role"], data["task"], data["boss"], data["waitsFor"], data["model"], data.get("recipe"))
            member.update({field: data[field] for field in SAVED_FIELDS if field in data})
            member["resumeStart"] = datetime.strptime(data["startAt"], "%Y-%m-%d %H:%M") if data.get("startAt") else None
            swarm.settle(member)
            swarm.members[name] = member
            swarm.costs.track(name, data["model"], getattr(agent, "agent", None))
        return swarm

    # Goes on with a swarm that was interrupted (one that was restored, or one whose run was cut short): what is done stays done,
    # and the agents that did not finish start again, remembering what the user approved.
    def resume(self):
        return self.run(resume=True)
