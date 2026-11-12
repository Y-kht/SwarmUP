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
import hashlib
import json
import time

import agent_prompts as prompts
from agent_storehouse import agentWorkspace, describeTree, safeName
from harness_utils import STOPPED_MESSAGE, SwarmStopped
from mcp_gateway import LOOSE_GATEWAY, gatewayOf
from memory_cache import describeMemory
from model_adapters import adapterOf
from model_support import ModelMemoryError
from user_settings import currentSetting


CONTEXT_CHARS = 600000
KEEP_RECENT = 12
# A long answer cut at the limit of the tokens of a model goes on, this many times at most.
CUT_TURNS = 4
# When the GPUs run out of memory, the conversation keeps this share of itself and the model tries again, this many times at most.
MEMORY_SHARE = 0.6
MEMORY_TRIES = 3
KEPT_CHARS = 2000
KEEP_STEPS = 6
# After this many refused repetitions in a row, a request is stuck: it ends, instead of using all its steps.
STUCK_CALLS = 6
# After this many calls in a row that were cut for being too long, the model answers in words, which go on as long as they need.
CUT_CALLS = 2
LONG_CALL_CHARS = 2000
CONVERSATIONS_FOLDER = "conversations"
BAD_CALLS = 3
# The times a model may make exactly the same call in one request; after that it is told to use the result it has.
SAME_CALLS = 2
# A command or a wheel can give something new without the agent changing a file through its tools (a test that was fixed by a command): it has more tries.
SAME_RUNS = 5
RUNNING_TOOLS = ("processes__run_command", "wheels__")
# After one of these worked, what the agent read and ran may be different: the same call is a new call, like the tests it runs again after a fix.
CHANGING_TOOLS = ("files__write_file", "files__edit_file", "files__make_folder", "files__move_path", "files__delete_path", "transfer__copy_path", "transfer__paste_text")
TEXT_SECONDS = 0.25


# What the agent knows of itself for the whole conversation: who it is, where it works and how, its rules, its approved plan, and the memory
# (its notes included) with the temp folder.
def describeAgent(loop, readOnly, own):
    if hasattr(loop, "systemPrompt"):
        return loop.systemPrompt()
    root = agentWorkspace(loop)
    workplace = prompts.FOLDER_WORKPLACE.format(folder=root, tree=describeTree(root)) if loop.folder else prompts.SCRATCH_WORKPLACE.format(folder=root)
    alone = len(getattr(getattr(loop, "team", None), "members", ())) == 1
    asking = "" if getattr(adapterOf(loop.agent), "hasOwnUser", False) else prompts.ASK_RULE
    parts = [prompts.AGENT_SYSTEM_PROMPT.format(name=loop.name, task=loop.describeTask() or "the requests of SwarmUP below.", workplace=workplace,
                                                team=prompts.ALONE_RULE if alone else prompts.TEAM_RULE, asking=asking)]
    if readOnly:
        parts.append(prompts.LOOK_ONLY_PROMPT)
    if own and loop.rules:
        parts.append(f"THE RULES OF YOUR TASK. Follow them strictly:\n{loop.rules}")
    if own and loop.approvedPlan:
        parts.append(prompts.APPROVED_PLAN_PROMPT.format(plan=loop.approvedPlan))
    if own and loop.wheels:
        parts.append(prompts.WHEELS_AGENT_PROMPT.format(wheels=", ".join(f"wheels__{name}" for name in loop.wheels)))
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
        options["runTool"] = lambda name, arguments: link.call(name, arguments, readOnly, origin="bridge")
    return options


def hashOf(text):
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:12]


def usageOf(loop):
    records = ((getattr(loop.agent, "usage", None) or {}).get("records")) or []
    return len(records), records


# The tokens of the model calls made since mark (the records of the usage of the client, model_support.recordCall).
def tokensSince(loop, mark):
    count, records = usageOf(loop)
    added = records[mark:count] if count >= mark else []
    return {"in": sum(record.get("input") or 0 for record in added), "out": sum(record.get("output") or 0 for record in added)}


class Conversation:
    # own is False for a conversation that only serves one request of a leader (building the swarm): it only looks, and it is not kept.
    def __init__(self, loop, own=True):
        self.loop, self.own = loop, own
        self.system, self.messages, self.delivered = "", [], [0, 0]
        self.limit = None
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

    # The messages of the other agents and of the user that the model has not read yet. When one sender wrote several of them, the older ones
    # are marked: an agent that started late got three versions of the same contract at once in a real run, the oldest one wrong.
    def news(self):
        inbox, users = self.loop.inbox, self.loop.userMessages
        lines = []
        entries = [entry.partition(": ") for entry in inbox[self.delivered[0]:]]
        latest = {sender: number for number, (sender, _, _) in enumerate(entries)}
        for number, (sender, _, message) in enumerate(entries):
            older = f", older: a newer message from {sender} follows, and where they differ the newer one counts" if latest[sender] != number else ""
            lines.append(f"[Message from {sender}{older}] {message}")
        lines += [f"[Message from the user] {message}" for message in users[self.delivered[1]:]]
        self.delivered = [len(inbox), len(users)]
        return "\n".join(lines)

    def size(self):
        return sum(len(str(message.get("content") or "")) + len(str(message.get("calls") or "")) for message in self.messages)

    # A long conversation drops the old results of its tools, except for a model whose old messages must stay as they were (Claude).
    def shorten(self, adapter):
        limit = self.limit or CONTEXT_CHARS
        if getattr(adapter, "canShorten", True) and self.size() > limit:
            self.shrink(limit)

    # The old results go first, then the longest results that are left lose their end, and say so.
    def shrink(self, limit):
        for message in self.messages[:-KEEP_RECENT]:
            if message["role"] == "tool" and message["content"] != prompts.OLD_RESULT:
                message["content"] = prompts.OLD_RESULT
        results = [message for message in self.messages if message["role"] == "tool" and len(str(message["content"])) > KEPT_CHARS]
        for message in sorted(results, key=lambda item: len(str(item["content"])), reverse=True):
            excess = self.size() - limit
            if excess <= 0:
                break
            text = str(message["content"]).replace(prompts.CUT_RESULT, "")
            message["content"] = text[:max(KEPT_CHARS, len(text) - excess)] + prompts.CUT_RESULT
        self.dropOldSteps(limit)

    # When there is no long result to cut, the oldest steps leave, each with its call and its result, so what is left stays a correct conversation.
    # The first message (the task) and the latest steps stay.
    def dropOldSteps(self, limit):
        last = max(len(self.messages) - KEEP_STEPS, 1)
        dropped = False
        position = 1
        while self.size() > limit and position < last:
            message = self.messages[position]
            if message["role"] == "assistant" and (message.get("calls") or len(str(message.get("content") or "")) > KEPT_CHARS):
                end = position + 1
                while end < len(self.messages) and self.messages[end]["role"] == "tool":
                    end += 1
                del self.messages[position:end]
                last -= end - position
                dropped = True
            else:
                position += 1
        if dropped and not any(message["role"] == "user" and prompts.OLD_STEPS in str(message.get("content")) for message in self.messages):
            self.messages.insert(1, {"role": "user", "content": prompts.OLD_STEPS})

    # The GPUs ran out of memory on the conversation: it keeps less from now on, and the model tries again. It says if there was something to take out.
    def makeRoom(self):
        before = self.size()
        self.limit = int(before * MEMORY_SHARE)
        self.shrink(self.limit)
        return self.size() < before

    def converseWithRoom(self, adapter, tools, options):
        tries, loop = 0, self.loop
        while True:
            try:
                return loop.callModel(lambda: adapter.converse(self.system, self.messages, tools, **options), self.own)
            except ModelMemoryError as error:
                tries += 1
                if not error.tokens or tries > MEMORY_TRIES or not getattr(adapter, "canShorten", True) or not self.makeRoom():
                    raise
                loop.note("memory", own=self.own, tokens=error.tokens, left=self.size())
                loop.notifyUser(f"[{loop.name}] Its conversation of {error.tokens:,} tokens was too long for the memory of the GPUs: SwarmUP took out part of the results of its tools, and it goes on.")

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
    # A change of the files starts the count again: in a real run, an agent that fixed a bug was refused the third run of its tests.
    # An adapter that runs its own loop from outside (repeatsFreely: a model that drives the command line) decides this itself.
    def repeated(self, call):
        if getattr(adapterOf(self.loop.agent), "repeatsFreely", False):
            return ""
        key = (call["name"], json.dumps(call.get("arguments") or {}, sort_keys=True, ensure_ascii=False))
        self.sameCalls[key] = self.sameCalls.get(key, 0) + 1
        if self.sameCalls[key] <= (SAME_RUNS if call["name"].startswith(RUNNING_TOOLS) else SAME_CALLS):
            return ""
        return (f"you already made this same call {self.sameCalls[key] - 1} times in this request, and its result is above. Use it, do something "
                "else, or give your answer now.")

    # One request: the model works with its tools for the maxSteps setting steps at most (a loop can have fewer), then its answer is given back.
    # A request that used them all is told: the agent may have left its work unfinished, and the architect sees which requests hit the limit.
    def ask(self, prompt):
        loop, readOnly = self.loop, not self.own
        steps = getattr(loop, "maxSteps", None) or currentSetting("maxSteps")
        adapter = adapterOf(loop.agent)
        self.checkStop()
        with linkOf(loop) as link:
            link.resume()
            link.stale = True
            if not self.system:
                self.system = describeAgent(loop, readOnly, self.own)
            news = self.news()
            self.messages.append({"role": "user", "content": f"{news}\n\n{prompt}" if news else prompt})
            loop.note("request", own=self.own, look=readOnly, prompt=clipText(prompt, 1500, 500), promptHash=hashOf(prompt), systemHash=hashOf(self.system),
                      news=clipText(news, 1500))
            reply, self.sameCalls, stuck, cuts, cutCalls, written, last = {"text": ""}, {}, 0, 0, 0, [], False
            for step in range(steps):
                self.checkStop()
                self.shorten(adapter)
                # The last step of a request has no tools: the model can only answer, with what it has.
                last = last or step == steps - 1
                tools = [] if last else link.tools(readOnly)
                if hasattr(adapter, "attach"):
                    adapter.attach(loop)
                live = LiveText(loop)
                options = adapterOptions(adapter, loop, link, readOnly, live)
                started, mark = time.time(), usageOf(loop)[0]
                try:
                    reply = self.converseWithRoom(adapter, tools, options)
                finally:
                    live.end()
                calls = reply.get("calls") or []
                text = reply.get("text") or ""
                loop.note("step", own=self.own, n=step + 1, text=clipText(text, 2000, 1000), length=len(text), cut=bool(reply.get("cut")),
                          seconds=round(time.time() - started, 3), tokens=tokensSince(loop, mark),
                          calls=[{"id": call.get("id"), "name": call.get("name"), "args": clipArguments(call.get("arguments"))} for call in calls])
                self.messages.append({"role": "assistant", "content": reply.get("text") or "", "calls": calls, "raw": reply.get("raw")})
                if last and calls:
                    reply = {"text": text}
                    break
                if last and reply.get("cut") and cuts < CUT_TURNS:
                    calls = []
                if not calls:
                    if not reply.get("cut"):
                        break
                    written.append(text)
                    if cuts >= CUT_TURNS:
                        loop.recordActivity("its answer is still too long after it went on several times: the end of it is missing")
                        reply = {"text": ""}
                        break
                    cuts += 1
                    self.messages.append({"role": "user", "content": prompts.CUT_ANSWER})
                    self.save()
                    continue
                written = []
                cutCalls = cutCalls + 1 if reply.get("cut") or any(len(str(call.get("text") or "")) > LONG_CALL_CHARS for call in calls) else 0
                loop.recordActivity(", ".join(describeCall(call["name"], call.get("arguments")) for call in calls))
                for call in calls:
                    problem = call.get("problem") or (prompts.CUT_CALL if reply.get("cut") else "")
                    repeated = "" if problem else self.repeated(call)
                    problem = problem or repeated
                    stuck = stuck + 1 if repeated else 0
                    self.noteBadCall(bool(call.get("problem")))
                    if problem:
                        why = "parse" if call.get("problem") else "cut" if reply.get("cut") else "repeated"
                        loop.note("bad", id=call.get("id"), name=call.get("name"), why=why, problem=clipText(problem, 600), args=clipArguments(call.get("arguments")),
                                  text=clipText(call.pop("text", ""), 1000, 500))
                    text, error = (f"Not done: {problem}", True) if problem else link.call(call["name"], call.get("arguments"), readOnly, callId=call.get("id"))
                    self.messages.append({"role": "tool", "id": call["id"], "name": call["name"], "content": text, "error": error})
                    if not error and call["name"] in CHANGING_TOOLS:
                        self.sameCalls = {}
                news = self.news()
                if step == steps - 2:
                    news = f"{news}\n\n{prompts.LAST_STEP}" if news else prompts.LAST_STEP
                if news:
                    self.messages.append({"role": "user", "content": news})
                self.save()
                if stuck >= STUCK_CALLS:
                    loop.note("limit", own=self.own, steps=step + 1, calls=True, stuck=True)
                    loop.recordActivity("it kept repeating calls that it had already made: it must answer now")
                    self.messages.append({"role": "user", "content": prompts.LAST_STEP})
                    last = True
                if cutCalls >= CUT_CALLS and not last:
                    loop.note("limit", own=self.own, steps=step + 1, calls=True, cut=True)
                    loop.recordActivity("its calls are too long for its model: it gives its work as its answer")
                    self.messages.append({"role": "user", "content": prompts.CUT_CALLS})
                    last = True
            if step == steps - 1:
                loop.note("limit", own=self.own, steps=steps, calls=bool(calls))
                loop.recordActivity(f"stopped at the limit of {steps} steps of a request: its work may be unfinished")
        self.save()
        return joinPieces([*written, reply.get("text") or ""])


# The parts of an answer that was cut and went on. Each part was stripped, so words are kept apart, except where a part starts with punctuation.
def joinPieces(pieces):
    answer = ""
    for piece in pieces:
        answer += " " + piece if answer and piece and piece[0] not in ",.;:!?)]}" else piece
    return answer


def clipText(text, head, tail=0):
    text = "" if text is None else str(text)
    return text if len(text) <= head + tail else text[:head] + f" [... {len(text) - head - tail:,} characters ...] " + (text[-tail:] if tail else "")


def clipArguments(arguments):
    if not isinstance(arguments, dict):
        return clipText(arguments, 300)
    return {key: clipText(value, 300) if isinstance(value, str) else value for key, value in arguments.items()}


def describeCall(name, arguments):
    shown = {key: (value if len(str(value)) <= 80 else str(value)[:77] + "...") for key, value in (arguments or {}).items() if key not in ("content", "new_text", "old_text")}
    return f"{name}({json.dumps(shown, ensure_ascii=False)[1:-1]})"


# A request that needs no tool (a check, a summary, a route): one message, with what the agent must know written before it.
# alone: the prompt says all there is to know (the review of a draft), so nothing is written before it: in a real run, the messages of the swarm
# before a review told a small model that it was the agent again, and it gave no verdict.
def answerOnce(loop, prompt, own, alone=False):
    if alone:
        own = False
    if own and loop.approvedPlan:
        prompt = prompts.APPROVED_PLAN_PROMPT.format(plan=loop.approvedPlan) + f"\n\n{prompt}"
    if loop.userMessages and not alone:
        prompt = prompts.USER_MESSAGES_PROMPT.format(messages="\n".join(f"- {message}" for message in loop.userMessages)) + f"\n\n{prompt}"
    if loop.inbox and not alone:
        prompt = "Messages from the other agents of your swarm:\n" + "\n".join(loop.inbox) + f"\n\n{prompt}"
    if own and loop.rules:
        prompt = f"Follow these rules strictly:\n{loop.rules}\n\n{prompt}"
    adapter = adapterOf(loop.agent)
    if hasattr(adapter, "attach"):
        adapter.attach(loop)
    live = LiveText(loop)
    options = adapterOptions(adapter, loop, None, True, live)
    started = time.time()
    try:
        reply = loop.callModel(lambda: adapter.converse("", [{"role": "user", "content": prompt}], [], **options), own)
    finally:
        live.end()
    loop.note("answer", own=own, prompt=clipText(prompt, 600), reply=clipText(reply.get("text"), 1200), seconds=round(time.time() - started, 3))
    return reply.get("text") or ""


# Asks the model of the loop. With tools it works in its conversation (own: its own work, kept for the whole run; otherwise a request of a
# leader that is not part of its own work, in a conversation of its own that only looks). Without tools it answers one message.
def talk(loop, prompt, own, tools=True, alone=False):
    if not tools:
        return answerOnce(loop, prompt, own, alone)
    if not own:
        return Conversation(loop, own=False).ask(prompt)
    if loop.conversation is None:
        loop.conversation = Conversation.load(loop) if loop.resumed else Conversation(loop)
    return loop.conversation.ask(prompt)
