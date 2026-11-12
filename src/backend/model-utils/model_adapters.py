# The model adapters (docs/MCP_DESIGN_BOOKLET.md, section 4.2). The core loop of an agent (agent_conversation.py) speaks to every model the
# same way: adapter.converse(system, messages, tools) gives {"text", "calls", "raw", "cut"}, whatever the model. Every difference between the
# kinds of models stays here, at the edge:
# - an API model and a local model are adapters themselves (model_clients.py): they translate the tools of the MCP servers for their provider;
# - a model that only answers a prompt (PromptAdapter) gets the conversation and its tools as text, and writes its calls in its text, which
#   only this adapter reads (ADP-04);
# - a coding agent and the user who leads a swarm are adapters of their own (coding_agents.py, codex_agent.py, cli_models.py).
# An adapter may also have: attach(loop), called before each request; canShorten (False: its old messages are never shortened); and
# approvesItself (True: what it writes needs no approval, it is the user's own).
import json
import shlex
import uuid

import agent_prompts as prompts
from model_messages import readToolCalls


class PromptAdapter:
    canShorten = True

    def __init__(self, agent):
        self.agent = agent

    @property
    def approvesItself(self):
        return bool(getattr(self.agent, "approvesItself", False))

    def attach(self, loop):
        attach = getattr(self.agent, "attach", None)
        if callable(attach):
            attach(loop)

    def converse(self, system, messages, tools, onText=None):
        text = self.agent.input(renderPrompt(system, messages, tools))
        if not tools:
            return {"text": text, "calls": [], "raw": None, "cut": False}
        calls, rest = readToolCalls(text, tools)
        return {"text": rest, "calls": calls, "raw": None, "cut": False}


def writeCalls(calls):
    return [f"<tool_call>{json.dumps({'name': call['name'], 'arguments': call.get('arguments') or {}}, ensure_ascii=False)}</tool_call>" for call in calls]


# The conversation as one prompt: the system prompt, the tools (if any), then every message in order.
def renderPrompt(system, messages, tools):
    parts = [system] if system else []
    if tools:
        parts.append(prompts.TEXT_TOOLS_PROMPT.format(tools=json.dumps(tools, ensure_ascii=False)))
    for message in messages:
        if message["role"] == "user":
            parts.append(f"[SwarmUP]\n{message['content']}")
        elif message["role"] == "tool":
            parts.append(f"[Result of {message['name']}]\n{message['content']}")
        else:
            parts.append("[Your answer]\n" + "\n".join([message.get("content") or "", *writeCalls(message.get("calls") or [])]).strip())
    return "\n\n".join(part for part in parts if part)


# A person (or an AI agent) who leads at the command line calls a tool with a command: /files__read_file path=notes.md, or
# /files__read_file {"path": "notes.md"}. Anything else is the answer. It gives the calls, or None for an answer.
# \n starts a new line in key=value. In JSON, \n is already a new line of its string: writing it as one first made the JSON invalid.
def readCommand(text):
    text = text.strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    name, _, rest = text[1:].partition(" ")
    rest = rest.strip()
    if rest.startswith("{"):
        try:
            arguments = json.loads(rest, strict=False)
        except ValueError:
            return [{"id": f"call_{uuid.uuid4().hex[:12]}", "name": name, "arguments": {}, "problem": "the arguments are not valid JSON. Write them again."}]
    else:
        rest = rest.replace("\\n", "\n")
        try:
            words = shlex.split(rest)
        except ValueError:
            words = rest.split()
        arguments = {}
        for word in words:
            key, _, value = word.partition("=")
            try:
                arguments[key] = json.loads(value)
            except ValueError:
                arguments[key] = value
    return [{"id": f"call_{uuid.uuid4().hex[:12]}", "name": name, "arguments": arguments if isinstance(arguments, dict) else {}}]


# Who types a command cannot say that id=12 is a text: the tool says it. The rest is checked by the gateway, as for every call.
def typedArguments(name, arguments, tools):
    properties = next(((tool.get("parameters") or {}).get("properties") or {} for tool in tools or [] if tool["name"] == name), {})
    return {key: str(value) if (properties.get(key) or {}).get("type") == "string" and isinstance(value, (int, float)) and not isinstance(value, bool) else value
            for key, value in arguments.items()}


def describeCommands(tools):
    lines = []
    for tool in tools:
        properties = (tool.get("parameters") or {}).get("properties") or {}
        lines.append(f"/{tool['name']} " + " ".join(f"{key}=..." for key in properties) + f"  {tool.get('description', '').split('. ')[0].rstrip('.')}.")
    return "\n".join(lines)


def adapterOf(agent):
    return agent if callable(getattr(agent, "converse", None)) else PromptAdapter(agent)


# The user can lead the swarm instead of a model: a person in the window or at the terminal, or an AI agent that drives the command line
# (swarmup_cli.py send). What the swarm asks the model of the leader (its own task, which agents a follow-up concerns, what to keep in the long-term
# memory) is asked to the user, in the window of the leader, and the answer is the answer of the leader. The leader does not check or approve its own drafts.
USER_LEADER = {"name": "you", "local": False, "provider": None, "billions": None, "bits": None, "vram": 0.0, "cli": None, "user": True}


# What is new for someone who reads the conversation from outside: all of it the first time, then what came after their last answer.
def describeNews(system, messages):
    last = max((number for number, message in enumerate(messages) if message["role"] == "assistant"), default=-1)
    shown = [system] if last < 0 and system else []
    return shown + [message["content"] if message["role"] == "user" else f"[Result of {message['name']}]\n{message['content']}" for message in messages[last + 1:]]


# A model that runs the command line (Claude Code, for example) can be the only agent of a swarm: it then has the tools, the wheels and the
# records of SwarmUP. It is not asked by SwarmUP: its loop runs outside, so each step of the conversation becomes a turn on its desk (SoloDesk in
# cli_solo.py), which waits for its next call or its answer. Its own loop decides how often it repeats a call and who it asks.
class Driver:
    approvesItself, canShorten, repeatsFreely, hasOwnUser = True, False, True, True

    def __init__(self, desk, name):
        self.desk, self.name, self.usage = desk, name, {"calls": 0, "input": 0, "output": 0}

    def converse(self, system, messages, tools, onText=None):
        self.usage["calls"] += 1
        reply = self.desk.take({"kind": "work", "agent": self.name, "text": "\n\n".join(describeNews(system, messages)), "tools": tools or []})
        call = reply.get("call")
        calls = [{"id": f"call_{uuid.uuid4().hex[:12]}", "name": call["name"], "arguments": typedArguments(call["name"], call.get("arguments") or {}, tools)}] if call else []
        return {"text": "" if call else str(reply.get("answer") or ""), "calls": calls, "raw": None, "cut": False}

    def cancel(self):
        self.desk.release()


# The user is an adapter of its own (model_adapters.py): it sees only what is new in the conversation, and calls the tools of the leader with
# commands, like a model calls them (ADP-07). console is what the program that runs it gives: say(text, window) and ask(question, window).
class UserLeader:
    approvesItself = True

    def __init__(self, console, name):
        self.console, self.name, self.usage = console, name, {"calls": 0, "input": 0, "output": 0}
        self.listed = set()

    # One line answers; \n in it starts a new line, so a long answer (a report, JSON) fits in one line. A line that starts with / calls a tool.
    # The tools are told once each, so a wheel that comes while the agent works is told too.
    def converse(self, system, messages, tools, onText=None):
        self.usage["calls"] += 1
        shown = describeNews(system, messages)
        fresh = [tool for tool in tools or [] if tool["name"] not in self.listed]
        if fresh:
            self.listed |= {tool["name"] for tool in fresh}
            shown.append("Your tools, as commands (write /name key=value, or /name {JSON}):\n" + describeCommands(fresh))
        self.console.say("\n=== You lead the swarm. SwarmUP asks you what it would ask the model of the leader: ===\n" + "\n\n".join(shown) +
                         "\n=== Answer in one line (write \\n for a new line), or call a tool. ===", window=self.name)
        reply = self.console.ask("Your answer, as the leader:", window=self.name)
        calls = readCommand(reply) if tools else None
        for call in calls or []:
            call["arguments"] = typedArguments(call["name"], call["arguments"], tools)
        return {"text": "" if calls else reply.replace("\\n", "\n"), "calls": calls or [], "raw": None, "cut": False}
