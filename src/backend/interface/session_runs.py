import threading
from datetime import datetime

from interface_views import FormError, hasCredentials
from leader_catalog import LeaderCatalog
from leader_manager import LeaderManager
from leader_utils import designSwarm
from model_support import findMissingPackages
from models_library import API_KEYS
from swarm_harness import Swarm
from tasks_library import DEFAULT_LOOPS, buildLoop, describeLoop, getTask, publicAnswers, suggestName
from user_settings import loadSettings


# Building and running the swarm (Session in session_core.py): the leader that builds it, who waits for whom, the run, and what
# the user does while it runs.
class SessionRuns:
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
        catalog = self.leaderCatalog(leader)
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
            client = self.makeClient(info, agent["name"]) if hasCredentials(info, self.keys) else None
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

    def leaderCatalog(self, leader):
        return LeaderCatalog(self.mission, leader["answers"].get("folder"), leader["name"], leader["model"], self.keys, self.tokens, self.costs, loadSettings()["maxAgents"])

    def manage(self, swarm, leader):
        LeaderManager(swarm, self.leaderCatalog(leader), self)

    # What the manager of the leader needs: the loop of a new agent, or of an agent with another model (LeaderManager in leader_manager.py).
    def makeLoop(self, agent):
        info = agent["model"]
        client = self.makeClient(info, agent["name"])
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
        silent = [spec["name"] for spec in self.specs if spec["task"] == "agent" and not str(spec["answers"].get("prompt") or "").strip()]
        if silent:
            raise ValueError(f"Write what {', '.join(silent)} must do, in the step of the agents.")
        self.makeClients()
        leader = self.leaderSpec()
        if leader and not leader["answers"].get("folder"):
            raise ValueError(f"Choose the folder of the mission for {leader['name']} first.")
        if leader and len(self.specs) == 1:
            raise ValueError(f"Ask {leader['name']} to build the swarm first, in the step of the mission.")
        swarm = Swarm(self.mission)
        swarm.costs = self.costs
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
                spec["client"] = self.makeClient(spec["model"], spec["name"])

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
