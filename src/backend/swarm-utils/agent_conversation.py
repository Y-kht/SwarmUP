# The conversation of an agent with its model. A model client that can hold a conversation (converse: the API models and the local models)
# works like Claude Code and Codex: SwarmUP and the model talk turn after turn, the model calls its tools (agent_tools.py), SwarmUP runs them and
# gives back what they found, and so on until the model answers. The conversation goes on for the whole run of the agent: a correction of the
# user is the next message of the same conversation, and the model still knows what it read and did.
# The messages of the user and of the other agents arrive between two steps, so the agent reads them while it works, and not only at its next draft.
# The conversation is saved in the session of the swarm after each step, so a swarm that goes on after a stop goes on with it.
# A client that cannot hold a conversation (a test agent, or one with tools of its own like Claude Code) is asked one prompt at a time instead
# (consultFolder in agent_storehouse.py).
#
# The messages are written the same way for every provider, and each client translates them (model_clients.py):
# {"role": "user", "content": text}, {"role": "assistant", "content": text, "calls": [{"id", "name", "arguments"}], "raw": what the provider sent},
# and {"role": "tool", "id", "name", "content": text, "error": True if the tool failed}.
# A client gives back {"text", "calls", "raw", "cut": True if the answer was cut before its calls were complete}.
import json

import agent_prompts as prompts
from agent_storehouse import describeTree, safeName
from agent_tools import Toolbox, describeCall
from harness_utils import STOPPED_MESSAGE, SwarmStopped
from memory_cache import describeMemory


MAX_STEPS = 25
CONTEXT_CHARS = 600000
KEEP_RECENT = 12
CONVERSATIONS_FOLDER = "conversations"


def canConverse(agent):
    return callable(getattr(agent, "converse", None))


# What the agent knows of itself for the whole conversation: who it is, where it works and how, its rules, its approved plan, and the memory
# (its notes included) with the temp folder.
def describeAgent(loop, toolbox, own):
    workplace = prompts.FOLDER_WORKPLACE.format(folder=toolbox.root, tree=describeTree(toolbox.root)) if loop.folder else \
        prompts.SCRATCH_WORKPLACE.format(folder=toolbox.root)
    parts = [prompts.AGENT_SYSTEM_PROMPT.format(name=loop.name, task=loop.describeTask() or "the requests of SwarmUP below.", workplace=workplace)]
    if toolbox.readOnly:
        parts.append(prompts.LOOK_ONLY_PROMPT)
    if own and loop.rules:
        parts.append(f"THE RULES OF YOUR TASK. Follow them strictly:\n{loop.rules}")
    if own and loop.approvedPlan:
        parts.append(prompts.APPROVED_PLAN_PROMPT.format(plan=loop.approvedPlan))
    if loop.session:
        parts.append(describeMemory(loop.session.memory, loop.name))
    return "\n\n".join(parts)


class Conversation:
    # own is False for a conversation that only serves one request of a leader (building the swarm): it only looks, and it is not kept.
    def __init__(self, loop, own=True):
        self.loop, self.own = loop, own
        self.system, self.messages, self.delivered = "", [], [0, 0]

    def path(self):
        return self.loop.session.folder / CONVERSATIONS_FOLDER / f"{safeName(self.loop.name)}.json"

    def save(self):
        if not (self.own and self.loop.session):
            return
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"system": self.system, "messages": self.messages, "delivered": self.delivered}, ensure_ascii=False, default=str), encoding="utf-8")

    # The conversation the agent had before the program stopped. Calls that did not get their result are answered, so the model can go on.
    @classmethod
    def load(cls, loop):
        conversation = cls(loop)
        if loop.session and conversation.path().exists():
            saved = json.loads(conversation.path().read_text(encoding="utf-8"))
            conversation.system, conversation.messages, conversation.delivered = saved["system"], saved["messages"], saved["delivered"]
            answered = {message["id"] for message in conversation.messages if message["role"] == "tool"}
            last = next((message for message in reversed(conversation.messages) if message["role"] == "assistant"), None)
            for call in (last or {}).get("calls") or []:
                if call["id"] not in answered:
                    conversation.messages.append({"role": "tool", "id": call["id"], "name": call["name"], "content": "Not done: the program stopped before it ran.", "error": True})
        return conversation

    # The messages of the other agents and of the user that the model has not read yet.
    def news(self):
        inbox, users = self.loop.inbox, self.loop.userMessages
        lines = []
        for entry in inbox[self.delivered[0]:]:
            sender, _, message = entry.partition(": ")
            lines.append(f"[Message from {sender}] {message}")
        lines += [f"[Message from the user] {message}" for message in users[self.delivered[1]:]]
        self.delivered = [len(inbox), len(users)]
        return "\n".join(lines)

    # A long conversation drops the old results of its tools, except for a provider that needs the conversation to stay as it was (Claude).
    def shorten(self):
        if getattr(self.loop.agent, "appendOnly", False) or sum(len(str(message.get("content") or "")) for message in self.messages) <= CONTEXT_CHARS:
            return
        for message in self.messages[:-KEEP_RECENT]:
            if message["role"] == "tool" and message["content"] != prompts.OLD_RESULT:
                message["content"] = prompts.OLD_RESULT

    def checkStop(self):
        team = getattr(self.loop, "team", None)
        if team is not None and team.shouldStop(self.loop.name):
            raise SwarmStopped(STOPPED_MESSAGE)

    # One request: the model works with its tools for MAX_STEPS steps at most, then its answer is given back.
    def ask(self, prompt):
        loop, toolbox = self.loop, Toolbox(self.loop, readOnly=not self.own)
        if not self.system:
            self.system = describeAgent(loop, toolbox, self.own)
        news = self.news()
        self.messages.append({"role": "user", "content": f"{news}\n\n{prompt}" if news else prompt})
        reply = {"text": ""}
        for step in range(MAX_STEPS):
            self.checkStop()
            self.shorten()
            reply = loop.callModel(lambda: loop.agent.converse(self.system, self.messages, toolbox.schemas()), self.own)
            calls = reply.get("calls") or []
            self.messages.append({"role": "assistant", "content": reply.get("text") or "", "calls": calls, "raw": reply.get("raw")})
            if not calls:
                break
            loop.recordActivity(", ".join(describeCall(call["name"], call.get("arguments")) for call in calls))
            for call in calls:
                problem = call.get("problem") or (prompts.CUT_CALL if reply.get("cut") else "")
                text, error = (f"Not done: {problem}", True) if problem else toolbox.run(call["name"], call.get("arguments"))
                self.messages.append({"role": "tool", "id": call["id"], "name": call["name"], "content": text, "error": error})
            news = self.news()
            if step == MAX_STEPS - 2:
                news = f"{news}\n\n{prompts.LAST_STEP}" if news else prompts.LAST_STEP
            if news:
                self.messages.append({"role": "user", "content": news})
            self.save()
        self.save()
        return reply.get("text") or ""


# Asks the model of the loop in its conversation. own is False for a request of a leader that is not part of its own work: it gets a
# conversation of its own, which only looks.
def talk(loop, prompt, own):
    if not own:
        return Conversation(loop, own=False).ask(prompt)
    if loop.conversation is None:
        loop.conversation = Conversation.load(loop) if loop.resumed else Conversation(loop)
    return loop.conversation.ask(prompt)
