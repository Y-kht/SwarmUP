# The leader while the swarm runs: it is asked if a change is needed, and every change it proposes is checked and shown to the
# user (see leader_utils.py).
import queue
import threading
import time
import traceback

from harness_utils import USER_NAME
from leader_parser import findSuggestions, isNoChange, normalizeKeys, parseOutput, squash
from leader_utils import askMissing
from model_support import ModelError
from tasks_library import TASKS, describeLoop, publicAnswers


MAX_SUPERVISIONS = 12

DEBOUNCE_SECONDS = 1.5

SENDER = "SwarmUP"


# ==============
# The leader manages the swarm while it runs. The manager is the listener of the swarm, and it reads every answer of the leader (readOutput,
# called by the loop of the leader): the blocks it finds are proposed to the user, the sentences that suggest a change are sent back to the
# leader to confirm, and the leader is asked if a change is needed when an agent finishes or the user writes to it.
# The work is done in a thread of its own, so the swarm never waits for the manager.
# maker is the program that runs the swarm (the user interface or the command line). It makes the loops of the agents:
# makeLoop(agent) gives the loop of a new agent, from agent["name"], ["task"], ["answers"] and ["model"], with the keys of the catalog;
# joined(agent, loop) is told once that agent is in the swarm; remakeLoop(name, model) gives the loop of an agent with another model,
# and remade(name, model, loop) is told once the swarm uses it.
# ==============
class LeaderManager:
    def __init__(self, swarm, catalog, maker):
        self.swarm, self.catalog, self.maker = swarm, catalog, maker
        self.queue = queue.Queue()
        self.thread = None
        self.rounds = 0
        self.rejected = {}
        self.decisions = []
        swarm.manager = self
        swarm.addListener(self.onEvent)

    # Called by the swarm, maybe with its lock held: it only takes note.
    def onEvent(self, event):
        kind, agent = event["kind"], event["agent"]
        if kind == "run":
            self.start()
        elif kind == "finished":
            self.queue.put(("end", None))
        elif kind == "status" and event["status"] in ("done", "failed") and agent != self.swarm.getLeader() and self.othersAtWork(agent):
            self.queue.put(("event", f"{agent} {'is done' if event['status'] == 'done' else 'did not finish'}."))
        elif kind == "message" and event["sender"] == USER_NAME and event["receiver"] == self.swarm.getLeader():
            self.queue.put(("event", f"The user wrote to you: {event['message']}"))

    # When the last agent finishes, the leader starts its own work at once: nothing is left to change, so the leader is not asked (it costs tokens).
    def othersAtWork(self, agent):
        return any(member["status"] not in ("done", "failed") for name, member in list(self.swarm.members.items()) if name not in (agent, self.swarm.getLeader()))

    def start(self):
        self.queue, self.rounds = queue.Queue(), 0
        self.thread = threading.Thread(target=self.work, args=(self.queue,), daemon=True, name="leader-manager")
        self.thread.start()

    # Every answer of the leader comes here (Loop.onAnswer). The blocks are taken out of the text, which goes on to the user without them.
    # What the manager asks the leader itself is read by the manager, so it is given back as it is.
    def readOutput(self, text):
        if threading.current_thread() is self.thread:
            return text
        found = parseOutput(text)
        suggestions = [] if found["blocks"] or found["problems"] else findSuggestions(found["text"], self.swarm.getAgents(), self.swarm.getLeader())
        if found["blocks"] or found["problems"] or suggestions:
            self.queue.put(("output", {**found, "suggestions": suggestions}))
        return found["text"]

    def work(self, items):
        while True:
            kind, data = items.get()
            batch = [(kind, data)]
            if kind != "end":
                time.sleep(DEBOUNCE_SECONDS)
                while not items.empty():
                    batch.append(items.get_nowait())
            ending = any(kind == "end" for kind, _ in batch)
            try:
                self.handle(batch, ending)
            except Exception:
                traceback.print_exc()
            if ending:
                return

    def isOpen(self):
        return self.swarm.active and not self.swarm.stopped

    def handle(self, batch, ending):
        for kind, data in batch:
            if kind == "output" and self.isOpen():
                self.act(data)
        events = [data for kind, data in batch if kind == "event"]
        if events and not ending:
            self.supervise(events)

    def leaderLoop(self):
        return self.swarm.getMember(self.swarm.getLeader())["agent"]

    def tell(self, text):
        self.leaderLoop().receive(SENDER, text)

    def ask(self, prompt):
        try:
            return self.leaderLoop().askAgent(prompt, own=False)
        except Exception as error:
            self.leaderLoop().notifyUser(f"The leader could not be asked about the swarm: {error}")
            return None

    # The blocks of an answer of the leader are proposed one after the other. What cannot be used is sent back to the leader once; a sentence
    # that suggests a change, without any block, is sent back to be confirmed.
    def act(self, found, again=True):
        problems = [problem["error"] for problem in found["problems"]]
        for block in found["blocks"]:
            problems += [f"{error} (in: {block['raw'][:300]})" for error in self.propose(block)]
        if problems and again:
            reply = self.ask(self.catalog.repairPrompt(problems, "\n".join(block["raw"] for block in found["blocks"] + found["problems"]) or found["text"], "the right blocks"))
            if reply is not None:
                self.act(parseOutput(reply), again=False)
        elif problems:
            self.tell("SwarmUP could not use your proposal, so the user was not asked: " + " ".join(problems))
        elif not found["blocks"] and found.get("suggestions") and again:
            reply = self.ask(self.catalog.confirmPrompt(self.swarm, [item["sentence"] for item in found["suggestions"]]))
            if reply is not None and not isNoChange(reply):
                self.act({**parseOutput(reply), "suggestions": []}, again=False)

    def supervise(self, events):
        stage = self.swarm.stage
        leaderWorks = bool(stage and not stage["plan"] and self.swarm.getLeader() in stage["names"])
        if self.rounds >= MAX_SUPERVISIONS or not self.isOpen() or self.swarm.interruption or leaderWorks:
            return
        self.rounds += 1
        reply = self.ask(self.catalog.supervisePrompt(self.swarm, events, "\n".join(f"- {decision}" for decision in self.decisions[-12:]) or "Nothing yet."))
        if reply is None:
            return
        found = parseOutput(reply)
        if found["blocks"] or found["problems"]:
            self.act({**found, "suggestions": []})
        elif not isNoChange(found["text"]):
            self.act({**found, "suggestions": findSuggestions(found["text"], self.swarm.getAgents(), self.swarm.getLeader())})

    # One change of the leader: checked, shown to the user with the reason of the leader, and made if the user approves it. It returns what
    # the leader must correct (the change was not shown to the user then), or [].
    def propose(self, block):
        if block["action"] == "build":
            known = {squash(name) for name in [*self.swarm.getAgents(), *(item["name"] for item in self.swarm.removed)]}
            if all(squash(normalizeKeys(agent)[0].get("name")) in known for agent in block["data"]["agents"]):
                return []
            return ["The swarm is already built: propose each new agent in its own <swarmup_add> block, and each agent to take out in a <swarmup_remove> block."]
        change, errors = self.catalog.checkChange(block["action"], block["data"], self.swarm)
        if errors:
            return errors
        if change["signature"] in self.rejected:
            self.tell(f"You proposed again what the user already rejected, so it was not shown to the user: {change['text']}.")
            return []
        leader = self.leaderLoop()
        answer = leader.askProposal(change["proposal"])
        if not self.isOpen():
            return []
        if not isinstance(answer, dict) or answer.get("decision") != "approve":
            message = str(answer.get("message") or "").strip() if isinstance(answer, dict) else ""
            self.rejected[change["signature"]] = message
            self.decisions.append(f"Rejected: {change['text']}." + (f" The user said: {message}" if message else ""))
            self.tell(f"The user rejected your proposal: {change['text']}." + (f" The user said: {message}" if message else ""))
            return []
        try:
            self.apply(change, leader)
        except (ValueError, ModelError, OSError) as error:
            leader.notifyUser(f"The change you approved could not be made: {error}")
            self.decisions.append(f"Approved, but it could not be made: {change['text']}. Why: {error}")
            self.tell(f"The user approved {change['text']}, but it could not be made: {error}")
            return []
        self.decisions.append(f"Approved and done: {change['text']}.")
        self.tell(f"The user approved your proposal, and it is done: {change['text']}.")
        return []

    def apply(self, change, leader):
        action = change["action"]
        if action == "remove":
            self.swarm.removeAgent(change["name"], change["why"] or f"Proposed by {self.swarm.getLeader()}.")
            return
        if action == "model":
            if not askMissing([{"name": change["name"], "needs": [], "key": change["key"], "token": change["token"], "task": None}], self.catalog, leader):
                raise ValueError(f"the API key for {change['name']} was not given")
            loop = self.maker.remakeLoop(change["name"], change["model"])
            try:
                self.swarm.setModel(change["name"], change["model"], loop)
            except ValueError:
                if hasattr(loop.agent, "unload"):
                    loop.agent.unload()
                raise
            self.maker.remade(change["name"], change["model"], loop)
            return
        agent = change["agent"]
        if not askMissing([agent], self.catalog, leader):
            raise ValueError(f"{agent['name']} still misses what only you can give")
        agent["answers"]["folder"] = str(self.catalog.folder) if self.catalog.folder else None
        loop = self.maker.makeLoop(agent)
        recipe = {"task": agent["task"], "answers": publicAnswers(agent["task"], agent["answers"])}
        try:
            self.swarm.addAgent(agent["name"], loop, TASKS[agent["task"]]["role"], describeLoop(loop), waitsFor=agent["waitsFor"], model=agent["model"], recipe=recipe)
        except ValueError:
            if hasattr(loop.agent, "unload"):
                loop.agent.unload()
            raise
        self.maker.joined(agent, loop)
