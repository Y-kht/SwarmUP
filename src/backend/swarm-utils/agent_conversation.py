# The core loop of an agent (docs/MCP_DESIGN_BOOKLET.md, section 8). Every agent works the same way, whatever its model: SwarmUP and the
# model talk turn after turn, the model calls the tools of the MCP servers of SwarmUP through the gateway (mcp_gateway.py), SwarmUP gives back
# what they found, and so on until the model answers. The model is reached through its adapter (model_adapters.py), so this loop never asks
# what kind of model it speaks to. The conversation goes on for the whole run of the agent: a correction of the user is the next message of
# the same conversation, and the model still knows what it read and did.
# The messages of the user and of the other agents arrive between two steps, so the agent reads them while it works, and never in the middle
# of a call. The conversation is saved in the session of the swarm after each step, so a swarm that goes on after a stop goes on with it.
#
# The messages are written the same way for every model, and each adapter translates them:
# {"role": "user", "content": text}, {"role": "assistant", "content": text, "calls": [{"id", "name", "arguments"}], "raw": what the provider sent},
# and {"role": "tool", "id", "name", "content": text, "error": True if the tool failed}.
# An adapter gives back {"text", "calls", "raw", "cut": True if the answer was cut before its calls were complete}.
import contextlib
import json
import time

import agent_prompts as prompts
from agent_storehouse import agentWorkspace, describeTree, safeName
from harness_utils import STOPPED_MESSAGE, SwarmStopped
from mcp_gateway import LOOSE_GATEWAY, gatewayOf
from memory_cache import describeMemory
from model_adapters import adapterOf


MAX_STEPS = 25
CONTEXT_CHARS = 600000
KEEP_RECENT = 12
CONVERSATIONS_FOLDER = "conversations"
BAD_CALLS = 3
# The times a model may make exactly the same call in one request; after that it is told to use the result it has.
SAME_CALLS = 2
TEXT_SECONDS = 0.25


# What the agent knows of itself for the whole conversation: who it is, where it works and how, its rules, its approved plan, and the memory
# (its notes included) with the temp folder.
def describeAgent(loop, readOnly, own):
    root = agentWorkspace(loop)
    workplace = prompts.FOLDER_WORKPLACE.format(folder=root, tree=describeTree(root)) if loop.folder else prompts.SCRATCH_WORKPLACE.format(folder=root)
    parts = [prompts.AGENT_SYSTEM_PROMPT.format(name=loop.name, task=loop.describeTask() or "the requests of SwarmUP below.", workplace=workplace)]
    if readOnly:
        parts.append(prompts.LOOK_ONLY_PROMPT)
    if own and loop.rules:
        parts.append(f"THE RULES OF YOUR TASK. Follow them strictly:\n{loop.rules}")
    if own and loop.approvedPlan:
        parts.append(prompts.APPROVED_PLAN_PROMPT.format(plan=loop.approvedPlan))
    if loop.session:
        parts.append(describeMemory(loop.session.memory, loop.name))
    return "\n\n".join(parts)


# The link of the agent to the servers, for one request. The gateway of a swarm keeps it open; an agent alone has it for one request.
@contextlib.contextmanager
def linkOf(loop):
    gateway = gatewayOf(loop)
    link = gateway.link(loop)
    try:
        yield link
    finally:
        if gateway is LOOSE_GATEWAY:
            gateway.drop(loop)


# The text a model writes, given to the loop while it writes it (UX-04), at most every TEXT_SECONDS, so the user sees the work at once.
class LiveText:
    def __init__(self, loop):
        self.loop, self.text, self.told = loop, "", 0.0

    def add(self, piece):
        self.text += piece
        now = time.monotonic()
        if now - self.told >= TEXT_SECONDS:
            self.told = now
            self.loop.recordText(self.text)

    def end(self):
        if self.text:
            self.loop.recordText(self.text, done=True)


# What an adapter receives besides the conversation, by what it can do: the text while it writes it, and the tools to run itself.
def adapterOptions(adapter, loop, link, readOnly, live):
    options = {}
    if getattr(adapter, "streams", False):
        options["onText"] = live.add
    if getattr(adapter, "runsTools", False) and link is not None:
        options["runTool"] = lambda name, arguments: link.call(name, arguments, readOnly)
    return options


class Conversation:
    # own is False for a conversation that only serves one request of a leader (building the swarm): it only looks, and it is not kept.
    def __init__(self, loop, own=True):
        self.loop, self.own = loop, own
        self.system, self.messages, self.delivered = "", [], [0, 0]
        self.badCalls = 0
        self.sameCalls = {}

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

    # A long conversation drops the old results of its tools, except for a model whose old messages must stay as they were (Claude).
    def shorten(self, adapter):
        if not getattr(adapter, "canShorten", True) or sum(len(str(message.get("content") or "")) for message in self.messages) <= CONTEXT_CHARS:
            return
        for message in self.messages[:-KEEP_RECENT]:
            if message["role"] == "tool" and message["content"] != prompts.OLD_RESULT:
                message["content"] = prompts.OLD_RESULT

    def checkStop(self):
        team = getattr(self.loop, "team", None)
        if team is not None and team.shouldStop(self.loop.name):
            raise SwarmStopped(STOPPED_MESSAGE)

    # ADP-05: a model that cannot write a correct call is told the correct format, and after BAD_CALLS in a row its leader and the user know it.
    def noteBadCall(self, bad):
        self.badCalls = self.badCalls + 1 if bad else 0
        if self.badCalls != BAD_CALLS:
            return
        loop, team = self.loop, getattr(self.loop, "team", None)
        text = f"{loop.name} wrote {BAD_CALLS} tool calls in a row that SwarmUP could not read. Its model may be too small for its task."
        if team is not None and team.leader != loop.name and team.leader in team.members:
            team.members[team.leader]["agent"].receive("SwarmUP", text)
        loop.notifyUser(f"[{loop.name}] {text}")

    # A small model can call the same tool with the same arguments again and again. The third time, the call is not run: the model is told
    # to use the result it already has, so the request goes on instead of turning in a circle.
    def repeated(self, call):
        key = (call["name"], json.dumps(call.get("arguments") or {}, sort_keys=True, ensure_ascii=False))
        self.sameCalls[key] = self.sameCalls.get(key, 0) + 1
        if self.sameCalls[key] <= SAME_CALLS:
            return ""
        return (f"you already made this same call {self.sameCalls[key] - 1} times in this request, and its result is above. Use it, do something "
                "else, or give your answer now.")

    # One request: the model works with its tools for MAX_STEPS steps at most, then its answer is given back.
    def ask(self, prompt):
        loop, readOnly = self.loop, not self.own
        adapter = adapterOf(loop.agent)
        self.checkStop()
        with linkOf(loop) as link:
            link.resume()
            link.stale = True
            if not self.system:
                self.system = describeAgent(loop, readOnly, self.own)
            news = self.news()
            self.messages.append({"role": "user", "content": f"{news}\n\n{prompt}" if news else prompt})
            reply, self.sameCalls = {"text": ""}, {}
            for step in range(MAX_STEPS):
                self.checkStop()
                self.shorten(adapter)
                tools = link.tools(readOnly)
                if hasattr(adapter, "attach"):
                    adapter.attach(loop)
                live = LiveText(loop)
                options = adapterOptions(adapter, loop, link, readOnly, live)
                try:
                    reply = loop.callModel(lambda: adapter.converse(self.system, self.messages, tools, **options), self.own)
                finally:
                    live.end()
                calls = reply.get("calls") or []
                self.messages.append({"role": "assistant", "content": reply.get("text") or "", "calls": calls, "raw": reply.get("raw")})
                if not calls:
                    break
                loop.recordActivity(", ".join(describeCall(call["name"], call.get("arguments")) for call in calls))
                for call in calls:
                    problem = call.get("problem") or (prompts.CUT_CALL if reply.get("cut") else "") or self.repeated(call)
                    self.noteBadCall(bool(call.get("problem")))
                    text, error = (f"Not done: {problem}", True) if problem else link.call(call["name"], call.get("arguments"), readOnly)
                    self.messages.append({"role": "tool", "id": call["id"], "name": call["name"], "content": text, "error": error})
                news = self.news()
                if step == MAX_STEPS - 2:
                    news = f"{news}\n\n{prompts.LAST_STEP}" if news else prompts.LAST_STEP
                if news:
                    self.messages.append({"role": "user", "content": news})
                self.save()
        self.save()
        return reply.get("text") or ""


def describeCall(name, arguments):
    shown = {key: (value if len(str(value)) <= 80 else str(value)[:77] + "...") for key, value in (arguments or {}).items() if key not in ("content", "new_text", "old_text")}
    return f"{name}({json.dumps(shown, ensure_ascii=False)[1:-1]})"


# A request that needs no tool (a check, a summary, a route): one message, with what the agent must know written before it.
def answerOnce(loop, prompt, own):
    if own and loop.approvedPlan:
        prompt = prompts.APPROVED_PLAN_PROMPT.format(plan=loop.approvedPlan) + f"\n\n{prompt}"
    if loop.userMessages:
        prompt = prompts.USER_MESSAGES_PROMPT.format(messages="\n".join(f"- {message}" for message in loop.userMessages)) + f"\n\n{prompt}"
    if loop.inbox:
        prompt = "Messages from the other agents of your swarm:\n" + "\n".join(loop.inbox) + f"\n\n{prompt}"
    if own and loop.rules:
        prompt = f"Follow these rules strictly:\n{loop.rules}\n\n{prompt}"
    adapter = adapterOf(loop.agent)
    if hasattr(adapter, "attach"):
        adapter.attach(loop)
    live = LiveText(loop)
    options = adapterOptions(adapter, loop, None, True, live)
    try:
        reply = loop.callModel(lambda: adapter.converse("", [{"role": "user", "content": prompt}], [], **options), own)
    finally:
        live.end()
    return reply.get("text") or ""


# Asks the model of the loop. With tools it works in its conversation (own: its own work, kept for the whole run; otherwise a request of a
# leader that is not part of its own work, in a conversation of its own that only looks). Without tools it answers one message.
def talk(loop, prompt, own, tools=True):
    if not tools:
        return answerOnce(loop, prompt, own)
    if not own:
        return Conversation(loop, own=False).ask(prompt)
    if loop.conversation is None:
        loop.conversation = Conversation.load(loop) if loop.resumed else Conversation(loop)
    return loop.conversation.ask(prompt)
