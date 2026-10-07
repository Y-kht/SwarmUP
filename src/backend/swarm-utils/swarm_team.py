import json
import threading
import traceback
from collections import Counter
from datetime import datetime

import agent_prompts as prompts
from gpu_check import checkVram, readGpus
from harness_utils import SUMMARY_LENGTH, USER_NAME, stripFences


# The agents of a swarm (Swarm in swarm_harness.py): who joins and who leaves, their models and the VRAM they need, who waits for whom,
# and what a user interface reads of them.
class SwarmTeam:
    # An agent of the swarm, or one that was just removed and whose thread did not end yet.
    def anyMember(self, name):
        member = self.members.get(name) or self.departed.get(name)
        if member is None:
            raise ValueError(f"There is no agent called {name} in the swarm.")
        return member

    def setStatus(self, name, status):
        self.anyMember(name)["status"] = status
        if name in self.members:
            self.emit("status", name, status=status)

    # boss is the agent this one reports to in the tree (the leader if not given).
    # waitsFor lists the agents whose results this one needs before it can start. It is not used for the leader.
    # model is what getModelInfo gives for the model of the agent. A local model that does not fit in the GPUs is refused.
    # recipe is what the user interface needs to build the agent again after the program stopped (without passwords, they are never saved).
    # While the swarm runs, the new agent joins the group that works now: in plan mode it plans, in execute mode it works and reports to the
    # leader. It can wait for the agents already there (it receives their results), and nobody waits for it. Once the leader started its own
    # final work, nobody can join that run anymore.
    def addAgent(self, name, agent, role, task, boss=None, waitsFor=(), model=None, recipe=None):
        with self.changed:
            if name in self.members or name in self.departed:
                raise ValueError(f"There is already an agent called {name} in the swarm.")
            if name == USER_NAME:
                raise ValueError(f"{USER_NAME} is the name of the user, choose another name for the agent.")
            if boss:
                self.getMember(boss)
            unknown = [other for other in waitsFor if other not in self.members or other == self.leader]
            if unknown:
                raise ValueError(f"{name} cannot wait for {', '.join(unknown)}: they are not agents of the swarm, or it is the leader, who works last.")
            check = self.checkModel(model) if model else {"allowed": True}
            if not check["allowed"]:
                raise ValueError(check["message"])
            stage = self.stage
            if self.active and (stage is None or not stage["open"] or (not stage["plan"] and self.leader in stage["names"])):
                raise ValueError("The leader already started its final work, so no agent can join this run. Add it when the run is over.")
            member = self.newMember(name, agent, role, task, boss, waitsFor, model, recipe)
            self.members[name] = member
            self.leader = self.leader or name
            self.costs.track(name, model, getattr(agent, "agent", None))
            if not self.active:
                return
            self.connectAgent(name, member, False)
            agent.inbox, agent.userMessages, agent.progress, agent.actions = [], [], {}, []
            self.communicate(self.leader, name, self.describe(name))
            self.tellTeam(f"{name} joined the swarm: {role}. Task: {task}" + (f" It waits for {', '.join(waitsFor)}." if waitsFor else ""), exclude=name)
            if not stage["plan"]:
                self.finished[name] = threading.Event()
            self.startInStage(stage, name)
            self.changed.notify_all()
        self.emit("joined", name, role=role)

    def newMember(self, name, agent, role, task, boss, waitsFor, model, recipe):
        return {"name": name, "agent": agent, "role": role, "task": task, "boss": boss, "waitsFor": list(waitsFor), "model": model, "recipe": recipe,
                "status": "waiting", "result": None, "error": "", "mode": self.mode, "review": "", "draft": "", "problem": "",
                "decision": None, "revision": 0, "startAt": None, "started": False, "resumeStart": None, "wake": threading.Event()}

    # Takes an agent out of the swarm (never the leader), while it runs or not. One that finished first gives its result to the agents that wait
    # for it. One that did not finish is stopped at its next step, and the agents that wait for it go on without it. Its loop and its model are
    # let go in the background (retire) once its thread ended: what it left half done in the files is put back, and its model frees its memory.
    def removeAgent(self, name, reason=""):
        with self.changed:
            if name == self.leader:
                raise ValueError("The leader cannot be removed: it speaks to you for the swarm.")
            member = self.getMember(name)
            for other in list(self.members.values()):
                if name in other["waitsFor"]:
                    if member["result"] is not None:
                        self.deliverResult(name, other["name"])
                    other["waitsFor"] = [waited for waited in other["waitsFor"] if waited != name]
            del self.members[name]
            member["removed"] = True
            self.departed[name] = member
            thread = None
            if self.stage is not None and name in self.stage["names"]:
                self.stage["names"].remove(name)
                thread = self.stage["threads"].pop(name)
            if name in self.finished:
                self.finished[name].set()
            member["wake"].set()
            self.removed.append({"name": name, "role": member["role"], "task": member["task"], "status": member["status"],
                                 "result": None if member["result"] is None else str(member["result"])[:SUMMARY_LENGTH], "reason": reason,
                                 "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
            self.changed.notify_all()
        self.tellTeam(f"{name} ({member['role']}) left the swarm. Why: {(reason or 'not given').rstrip('.')}. Do not count on it or wait for it anymore"
                      + (": its result was already given to the agents that needed it." if member["result"] is not None else "."))
        self.emit("removed", name, reason=reason, status=member["status"])
        threading.Thread(target=self.retire, args=(name, member, thread, member["status"] == "done"), daemon=True).start()

    # Every agent of the swarm reads the news of the team with its next prompt (an agent that joined or left), so none of them counts on an
    # agent that left, and all of them know the one that joined.
    def tellTeam(self, news, exclude=None):
        for name, member in list(self.members.items()):
            if name != exclude:
                member["agent"].receive("swarm", news)

    # done is whether the agent had finished when it was removed: what it did after that is put back too.
    def retire(self, name, member, thread, done):
        if thread is not None:
            thread.join()
        try:
            if not done:
                member["agent"].rollback()
            model = member["agent"].agent
            if hasattr(model, "unload"):
                model.unload()
        except Exception:
            traceback.print_exc()
        self.departed.pop(name, None)
        self.emit("retired", name)

    def isRemoved(self, name):
        return name not in self.members and (name in self.departed or any(item["name"] == name for item in self.removed))

    # An agent that did not finish starts again, with what its loop remembers. The ones that finished (done or failed) stay as they are.
    def settle(self, member):
        if member["status"] not in ("done", "failed"):
            member.update(status="waiting", error="", review="", draft="", problem="", decision=None, startAt=None)

    def setLeader(self, name):
        self.getMember(name)
        self.leader = name

    # The mode is used the next time the swarm runs.
    def setMode(self, mode):
        if mode not in ("plan", "execute"):
            raise ValueError("The mode is plan or execute.")
        self.mode = mode

    def getMode(self):
        return self.mode

    # The VRAM in GB that the models of the agents need together, without the agent called replacing.
    def getNeededVram(self, replacing=None):
        return round(sum(member["model"]["vram"] for name, member in self.members.items() if member["model"] and name != replacing), 1)

    # What the user sees before choosing a model for an agent, or to replace the model of the agent called replacing.
    # A local model that does not fit in the GPUs is not allowed, and the message says why. One that fits, but not in the memory
    # that is free now, is allowed and the message is a warning. API models are always allowed, but they do not hide the warning of the swarm.
    def checkModel(self, model, replacing=None):
        taken = self.getNeededVram(replacing)
        after = round(taken + model["vram"], 1)
        status = checkVram(after, readGpus() if after else [])
        name, vram, total = model["name"], model["vram"], status["total"]
        if model["local"] and status["gpus"] and vram > total:
            message = f"{name} needs about {vram} GB of VRAM, but your GPUs only have {total} GB in total. Choose a smaller model or an API model."
        elif model["local"] and status["gpus"] and not status["fits"]:
            message = (f"{name} needs about {vram} GB of VRAM. The other agents already take {taken} GB and your GPUs have {total} GB in total, "
                       "so there is no room left. Choose a smaller model or an API model.")
        else:
            message = status["message"]
        return {"allowed": status["fits"] or not model["local"], "vram": vram, "needed": after, "message": message}

    # Changes the model of an agent, for example to a smaller one. agent is the new loop that uses it, if the user interface made one.
    # While the swarm runs, only an agent that did not start yet can change its model, and its old model frees its memory.
    def setModel(self, name, model, agent=None):
        with self.changed:
            member = self.getMember(name)
            if self.active and (member["started"] or member["status"] != "waiting"):
                raise ValueError(f"{name} already started, so its model cannot change now. Remove it and add a new agent instead.")
            check = self.checkModel(model, replacing=name)
            if not check["allowed"]:
                raise ValueError(check["message"])
            previous = member["agent"]
            member["model"] = model
            if agent is not None:
                member["agent"] = agent
                self.costs.track(name, model, agent.agent)
                if self.active:
                    # The new loop takes over what the old one already received (the mission, the results of the agents it waited for).
                    agent.inbox, agent.userMessages = list(previous.inbox), list(previous.userMessages)
                    self.connectAgent(name, member, False)
        if self.active and agent is not None and previous.agent is not agent.agent and hasattr(previous.agent, "unload"):
            threading.Thread(target=previous.agent.unload, daemon=True).start()
        if self.active:
            self.emit("model", name, model=model["name"], cli=model.get("cli"))

    # The agents of the swarm for the budget: those that did not finish still set aside the price of 1 million tokens of their model.
    def describeTeam(self):
        return [{"agent": name, "model": member["model"], "client": getattr(member["agent"], "agent", None), "finished": member["status"] in ("done", "failed")}
                for name, member in list(self.members.items())]

    def getCosts(self):
        return self.costs.report(self.describeTeam())

    # The VRAM the swarm needs so far next to what the GPUs have, for the user interface to show while the swarm is built.
    # The message is empty, or the warning that the swarm cannot run now.
    def getVramStatus(self):
        return checkVram(self.getNeededVram(), readGpus())

    def checkGpus(self):
        needed = self.getNeededVram()
        status = checkVram(needed, readGpus() if needed else [])
        if not status["runnable"]:
            raise ValueError(status["message"])

    def setWaitsFor(self, name, waitsFor):
        self.getMember(name)["waitsFor"] = list(waitsFor)

    def getLeader(self):
        return self.leader

    def getAgents(self):
        return list(self.members)

    def getStatuses(self):
        return {name: member["status"] for name, member in self.members.items()}

    def getStatus(self, name):
        return self.getMember(name)["status"]

    def getActiveAgents(self):
        return [name for name, status in self.getStatuses().items() if status == "working"]

    def getInactiveAgents(self):
        return [name for name, status in self.getStatuses().items() if status != "working"]

    # The agents with a draft waiting for the user, the ones a user interface shows with a green light.
    def getReadyAgents(self):
        return [name for name, member in self.members.items() if member["review"] == "ready"]

    # The latest summary of the leader, for the user interface to show next to the tree.
    def getSummary(self):
        return self.summary

    def getRole(self, name):
        return self.getMember(name)["role"]

    def getTask(self, name):
        return self.getMember(name)["task"]

    def getParent(self, name):
        member = self.getMember(name)
        return None if name == self.leader else (member["boss"] or self.leader)

    def getChildren(self, name):
        return [other for other in self.members if self.getParent(other) == name]

    # The agents this one is waiting for right now. The leader works last, so it waits for all the others.
    # In plan mode nobody waits for anybody.
    def getWaitingOn(self, name):
        member = self.getMember(name)
        if member["status"] != "waiting" or member["mode"] == "plan":
            return []
        others = [other for other in self.members if other != self.leader] if name == self.leader else member["waitsFor"]
        return [other for other in others if self.members[other]["status"] not in ("done", "failed")]

    def getStartAt(self, name):
        start = self.getMember(name)["startAt"]
        return f"{start:%Y-%m-%d %H:%M}" if start else ""

    # ---------- What an agent reaches of its swarm with its tools (agent_tools.py). ----------
    # The name of an agent of the swarm as it was written by another agent: without capitals, and "leader" for the leader.
    def findName(self, written):
        written = str(written).strip().lstrip("@")
        if written.lower() in ("leader", "the leader"):
            return self.leader
        return next((name for name in list(self.members) if name.lower() == written.lower()), None)

    # An agent writes to another one (or to the leader), which reads it with its next step. It gives back what the agent is told.
    def relay(self, sender, receiver, text):
        target, text = self.findName(receiver), str(text).strip()
        if not text:
            raise ValueError("The message is empty.")
        if target is None:
            gone = any(item["name"].lower() == str(receiver).strip().lower() for item in self.removed)
            raise ValueError(f"{receiver} left the swarm." if gone else f"There is no agent called {receiver}. The agents: {', '.join(self.members)}.")
        if target == sender:
            raise ValueError("You cannot write to yourself: use remember to keep a note.")
        if self.members[target]["status"] in ("done", "failed"):
            return f"{target} has finished, so it cannot read messages anymore. Read its result with read_result."
        self.members[target]["agent"].receive(sender, text)
        self.logMessage(sender, target, text, direct=True)
        return f"Sent to {target}. It reads it with its next step."

    def describeTeamFor(self, name):
        lines = [f"You are {name}. The leader is {self.leader}: it works last, and receives the result of every agent."]
        for other, member in list(self.members.items()):
            waits = f" It waits for {', '.join(member['waitsFor'])}." if member["waitsFor"] and other != self.leader else ""
            ready = " Its result is ready (read_result)." if member["result"] is not None and other != name else ""
            lines.append(f"- {other}{' (leader)' if other == self.leader else ''}{' (you)' if other == name else ''}: the {member['role']}, {member['status']}. "
                         f"Task: {member['task']}{waits}{ready}")
        lines += [f"- {item['name']} left the swarm." for item in self.removed]
        return "\n".join(lines)

    def resultOf(self, written):
        target = self.findName(written)
        if target is None:
            left = next((item for item in reversed(self.removed) if item["name"].lower() == str(written).strip().lower()), None)
            if left and left["result"] is not None:
                return f"The result of {left['name']}, which left the swarm:\n{left['result']}"
            raise ValueError(f"There is no agent called {written}. The agents: {', '.join(self.members)}.")
        member = self.members[target]
        if member["result"] is None:
            return f"{target} has no result yet: it is {member['status']}." + (f" {member['error']}" if member["error"] else "")
        return f"The result of {target}:\n{member['result']}"

    # An agent that was removed, or a swarm that was stopped, stops at its next step.
    def shouldStop(self, name):
        return self.stopped or self.isRemoved(name)

    def noteActivity(self, name, text):
        self.emit("activity", name, text=text)

    def getInfo(self, name):
        member = self.getMember(name)
        return {"name": name, "role": member["role"], "task": member["task"], "status": member["status"], "boss": self.getParent(name),
                "waitsFor": member["waitsFor"], "waitingOn": self.getWaitingOn(name), "startAt": self.getStartAt(name), "isLeader": name == self.leader,
                "result": member["result"], "error": member["error"], "model": member["model"], "mode": member["mode"], "review": member["review"],
                "draft": member["draft"], "problem": member["problem"], "revision": member["revision"], "plan": member["agent"].approvedPlan,
                "actions": list(member["agent"].actions), "activity": list(member["agent"].activity)}

    # Who lost the connection and why ({agent: reason}) while the swarm waits for the user to continue or to cancel, otherwise None.
    def getInterruption(self):
        return dict(self.interruption["agents"]) if self.interruption else None

    # The whole swarm as nested dictionaries, starting from the leader, ready to be drawn as a tree.
    def getTree(self, name=None):
        if not self.members:
            return {}
        name = name or self.leader
        member = self.getMember(name)
        return {"name": name, "role": member["role"], "task": member["task"], "status": member["status"], "review": member["review"], "model": member["model"],
                "waitsFor": [] if name == self.leader else member["waitsFor"], "waitingOn": self.getWaitingOn(name), "startAt": self.getStartAt(name),
                "children": [self.getTree(child) for child in self.getChildren(name)]}

    def getMessages(self, sender=None, receiver=None):
        return [message for message in self.messages if sender in (None, message["sender"]) and receiver in (None, message["receiver"])]

    # Who talks to whom and how many messages were sent, for drawing the arrows between the agents.
    def getConnections(self):
        counts = Counter((message["sender"], message["receiver"]) for message in self.messages if message["sender"] != USER_NAME)
        return [{"from": sender, "to": receiver, "count": count} for (sender, receiver), count in counts.items()]

    # waits is {agent: [agents it waits for]}. Returns the groups of agents that work at the same time, one group after the other.
    def findStages(self, waits):
        stages, done = [], set()
        while len(done) < len(waits):
            stage = [name for name in waits if name not in done and all(other in done for other in waits[name])]
            if not stage:
                raise ValueError(f"These agents wait for each other in a circle, so none of them can start: {[name for name in waits if name not in done]}")
            stages.append(stage)
            done.update(stage)
        return stages

    # The agents (without the leader, who works last) that can work at the same time, group after group.
    def getStages(self):
        if not self.members:
            raise ValueError("The swarm has no agents.")
        waits = {name: member["waitsFor"] for name, member in self.members.items() if name != self.leader}
        for name, others in waits.items():
            for other in others:
                if other not in waits:
                    raise ValueError(f"{name} cannot wait for {other}. It is not an agent of the swarm, or it is the leader, who works last.")
        return self.findStages(waits)

    def checkPlan(self, draft):
        workers = [name for name in self.members if name != self.leader]
        try:
            plan = json.loads(stripFences(draft))
            waits = {name: list(plan[name]) for name in workers}
        except (ValueError, KeyError, TypeError):
            return f"Reply only with JSON like {prompts.SWARM_PLAN_EXAMPLE}, with one entry for each of these agents: {workers}"
        unknown = [other for others in waits.values() for other in others if other not in workers]
        if unknown:
            return f"These are not agents you can wait for: {unknown}. Choose from {workers}"
        try:
            self.findStages(waits)
        except ValueError as error:
            return str(error)
        return ""

    # The leader decides who waits for whom, and the user approves its plan like any other draft.
    def planWithLeader(self):
        leader = self.getMember(self.leader)["agent"]
        agents = "\n".join(f"- {name}: {member['role']}. Task: {member['task']}" for name, member in self.members.items() if name != self.leader)
        plan = leader.reviewLoop(prompts.SWARM_PLAN_PROMPT.format(mission=self.mission, agents=agents, example=prompts.SWARM_PLAN_EXAMPLE), self.checkPlan, own=False)
        if plan is None:
            return False
        for name, others in json.loads(stripFences(plan)).items():
            if name != self.leader:
                self.setWaitsFor(name, others)
        return True

    # The picture of the swarm that an agent receives: the mission, its own role and task, and who else is in the swarm.
    def describe(self, name):
        member = self.getMember(name)
        team = "\n".join(f"- {other}{' (leader)' if other == self.leader else ''}: {info['role']}. Task: {info['task']}"
                         f"{' Waits for: ' + ', '.join(info['waitsFor']) + '.' if info['waitsFor'] and other != self.leader else ''}"
                         for other, info in self.members.items())
        return f"Mission of the swarm: {self.mission}\nYou are {name}, the {member['role']}. Your task: {member['task']}\nThe agents of the swarm:\n{team}"
