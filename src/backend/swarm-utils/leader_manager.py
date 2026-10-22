# The leader while the swarm runs: it is asked if a change is needed, and every change it proposes is checked and shown to the
# user (see leader_utils.py).
import queue
import threading
import time
import traceback

from harness_utils import USER_NAME
from leader_parser import normalizeKeys, squash
from leader_utils import askMissing
from model_support import ModelError
from tasks_library import TASKS, describeLoop, publicAnswers


MAX_SUPERVISIONS = 12

DEBOUNCE_SECONDS = 1.5

SENDER = "SwarmUP"


# ==============
# The leader manages the swarm while it runs. The manager is the listener of the swarm: the leader is asked if a change is needed when an agent
# finishes or the user writes to it, in a conversation with the tools of the leader (server_leader.py). Each change the leader proposes with a
# tool comes to propose: it is checked, shown to the user, and made if the user approves it; the leader gets the decision as the result of its
# call. The work is done in a thread of its own, so the swarm never waits for the manager.
# maker is the program that runs the swarm (the user interface or the command line). It makes the loops of the agents:
# makeLoop(agent) gives the loop of a new agent, from agent["name"], ["task"], ["answers"] and ["model"], with the keys of the catalog;
# joined(agent, loop) is told once that agent is in the swarm; remakeLoop(name, model) gives the loop of an agent with another model,
# and remade(name, model, loop) is told once the swarm uses it.
# ==============
# watchFinishes is False for a swarm the user built: the leader is only asked when an agent or the user writes to it (in a real run, two agents
# wrote to the leader that a file had no owner, and nobody read it until the end).
class LeaderManager:
    def __init__(self, swarm, catalog, maker, watchFinishes=True):
        self.swarm, self.catalog, self.maker, self.watchFinishes = swarm, catalog, maker, watchFinishes
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
        elif kind == "status" and self.watchFinishes and event["status"] in ("done", "failed") and agent != self.swarm.getLeader() and self.othersAtWork(agent):
            self.queue.put(("event", f"{agent} {'is done' if event['status'] == 'done' else 'did not finish'}."))
        elif kind == "message" and event["sender"] == USER_NAME and event["receiver"] == self.swarm.getLeader():
            self.queue.put(("event", f"The user wrote to you: {event['message']}"))
        elif kind == "message" and event.get("direct") and event["receiver"] == self.swarm.getLeader():
            self.queue.put(("event", f"{event['sender']} wrote to you: {event['message']}"))
        # A wheel the architect just wrote may save work to an agent that still works: the leader decides whether to give it.
        elif kind == "wheel" and event.get("action") in ("saved", "updated") and self.othersAtWork(""):
            self.queue.put(("event", f"The architect {event['action']} the wheel {event.get('name')}: {event.get('summary') or ''} Give it to an agent that still works "
                                     "and needs it (leader__give_wheels), or change nothing."))

    # When the last agent finishes, the leader starts its own work at once: nothing is left to change, so the leader is not asked (it costs tokens).
    def othersAtWork(self, agent):
        return any(member["status"] not in ("done", "failed") for name, member in list(self.swarm.members.items()) if name not in (agent, self.swarm.getLeader()))

    def start(self):
        self.queue, self.rounds = queue.Queue(), 0
        self.thread = threading.Thread(target=self.work, args=(self.queue,), daemon=True, name="leader-manager")
        self.thread.start()

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
        events = [data for kind, data in batch if kind == "event"]
        if events and not ending:
            self.supervise(events)

    def leaderLoop(self):
        return self.swarm.getMember(self.swarm.getLeader())["agent"]

    def tell(self, text):
        self.leaderLoop().receive(SENDER, text)

    # The leader decides in a conversation of its own, with its tools: it proposes each change with a call, or answers NO CHANGE.
    def ask(self, prompt):
        try:
            return self.leaderLoop().askAgent(prompt, own=False, tools=True)
        except Exception as error:
            self.leaderLoop().notifyUser(f"The leader could not be asked about the swarm: {error}")
            return None

    def supervise(self, events):
        stage = self.swarm.stage
        leaderWorks = bool(stage and not stage["plan"] and self.swarm.getLeader() in stage["names"])
        if self.rounds >= MAX_SUPERVISIONS or not self.isOpen() or self.swarm.interruption or leaderWorks:
            return
        self.rounds += 1
        self.ask(self.catalog.supervisePrompt(self.swarm, events, "\n".join(f"- {decision}" for decision in self.decisions[-12:]) or "Nothing yet."))

    # One change of the leader (a call of its tools): checked, shown to the user with the reason of the leader (ask, the proposal card), and made
    # if the user approves it. It gives (what the leader must correct, or [], and what happened, for the leader).
    def propose(self, block, ask=None):
        if not self.isOpen():
            return ["The swarm is not running now, so it cannot change."], ""
        if block["action"] == "build":
            known = {squash(name) for name in [*self.swarm.getAgents(), *(item["name"] for item in self.swarm.removed)]}
            if all(squash(normalizeKeys(agent)[0].get("name")) in known for agent in block["data"].get("agents") or []):
                return [], "These agents are already in the swarm."
            return ["The swarm is already built: propose each new agent with leader__add_agent, and each agent to take out with leader__remove_agent."], ""
        change, errors = self.catalog.checkChange(block["action"], block["data"], self.swarm)
        if errors:
            return errors, ""
        if change["signature"] in self.rejected:
            return [], f"You proposed again what the user already rejected, so it was not shown to the user: {change['text']}."
        leader = self.leaderLoop()
        answer = (ask or leader.askProposal)(change["proposal"])
        if not self.isOpen():
            return [], "The swarm stopped before the change could be made."
        if not isinstance(answer, dict) or answer.get("decision") != "approve":
            message = str(answer.get("message") or "").strip() if isinstance(answer, dict) else ""
            self.rejected[change["signature"]] = message
            self.decisions.append(f"Rejected: {change['text']}." + (f" The user said: {message}" if message else ""))
            return [], f"The user rejected your proposal: {change['text']}." + (f" The user said: {message}" if message else "")
        try:
            self.apply(change, leader)
        except (ValueError, ModelError, OSError) as error:
            leader.notifyUser(f"The change you approved could not be made: {error}")
            self.decisions.append(f"Approved, but it could not be made: {change['text']}. Why: {error}")
            return [], f"The user approved {change['text']}, but it could not be made: {error}"
        self.decisions.append(f"Approved and done: {change['text']}.")
        return [], f"The user approved your proposal, and it is done: {change['text']}."

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
        agent["answers"]["wheels"] = list(agent.get("wheels") or [])
        loop = self.maker.makeLoop(agent)
        recipe = {"task": agent["task"], "answers": publicAnswers(agent["task"], agent["answers"])}
        try:
            self.swarm.addAgent(agent["name"], loop, TASKS[agent["task"]]["role"], describeLoop(loop), waitsFor=agent["waitsFor"], model=agent["model"], recipe=recipe)
        except ValueError:
            if hasattr(loop.agent, "unload"):
                loop.agent.unload()
            raise
        self.maker.joined(agent, loop)
