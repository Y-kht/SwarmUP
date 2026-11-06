# Solo: a model that runs the command line (Claude Code, for example) is the only agent of a swarm, so it works with the tools, the wheels and the
# records of SwarmUP. Its own loop runs outside: each step of its conversation is a turn on the desk of the session, and its next call or its
# answer ends the turn. The same desk serves the commands (swarmup_cli.py solo ...) and the MCP server (solo_mcp_server.py).
#   solo start MISSION --folder F --approvals auto|model|manual [--plan] [--rules R...] [--mission ID]
#   solo call SESSION TOOL key=value...|{JSON} [--args-file F] | answer SESSION TEXT [--file F] | next SESSION TEXT [--execute]
#   solo approve|always|forever|deny SESSION [WHY] | tools|status|wait|close [SESSION]
# When its work is over, the model is its own architect: its next turn is the brief of what repeats, with the tools of the architect.
# The user chooses at the start how its actions are approved (APPROVALS). Nothing else differs from any other agent: every call goes through the
# gateway, with its records, its journal and its undo, and only what is done with these tools teaches the architect.
import json
import os
import shlex
import sys
import threading
from pathlib import Path

from architect_manager import attachArchitect
from cli_console import shorten
from cli_program import buildAgent, report, runSwarm
from cli_resume import restoreSwarm
from cli_sessions import findSession, killSession, listSessions, newSessionId, request, startDaemon
from harness_utils import ARCHITECT_NAME, STATE_LOCK
from mission_history import loadMission
from model_adapters import USER_LEADER, Driver, describeCommands, readCommand
from saved_swarms import UNFINISHED_STATES
from swarm_harness import Swarm
from tasks_library import DEFAULT_LOOPS
from user_settings import MAX_STEPS_LIMIT


AGENT_NAME = "Agent"
APPROVALS = {"auto": "calling a tool is the approval, so nothing waits (every call is still recorded and can be put back)",
             "model": "each action comes back to the model as a card, which it answers with approve, always or deny",
             "manual": "each action waits for a person, who answers in another terminal (swarmup_cli.py attach)"}
WAIT_SECONDS = 100
REPLY_MARGIN = 15
DECISIONS = {"approve": "once", "always": "run", "forever": "always", "deny": "deny"}
FINISHED = ("over", "ended")


class SoloDesk:
    def __init__(self):
        self.changed = threading.Condition()
        self.state, self.turn, self.reply, self.number = "starting", None, None, 0
        self.notes, self.person, self.result, self.wanted, self.released = [], "", "", None, False
        self.listed, self.lastAgent = set(), AGENT_NAME

    # ---------- The side of the swarm. ----------
    # A turn waits for the model: {kind: work or card, agent, text, tools}. It gives what the model replied.
    def take(self, turn):
        with self.changed:
            self.number += 1
            fresh = [tool for tool in turn.get("tools") or [] if tool["name"] not in self.listed]
            self.listed |= {tool["name"] for tool in fresh}
            opens = turn["agent"] != self.lastAgent
            self.lastAgent = turn["agent"]
            self.turn, self.reply = {**turn, "id": self.number, "fresh": fresh, "opens": opens}, None
            self.changed.notify_all()
            while self.reply is None and not self.released:
                self.changed.wait()
            reply, self.reply, self.released = self.reply, None, False
            if self.turn is not None and self.turn["id"] == self.number:
                self.turn = None
            self.changed.notify_all()
            return reply or {}

    # A stop of the swarm: the turn that waits ends without an answer.
    def release(self):
        with self.changed:
            if self.turn is not None:
                self.released = True
                self.changed.notify_all()

    def note(self, text):
        with self.changed:
            self.notes.append(str(text))
            self.changed.notify_all()

    def setState(self, state, result=None):
        with self.changed:
            self.state = state
            self.result = self.result if result is None else str(result)
            self.changed.notify_all()

    def setPerson(self, text):
        with self.changed:
            self.person = text
            self.changed.notify_all()

    # The round is over: it waits for the next request of the model. None: the session is closed.
    def waitForNext(self):
        with self.changed:
            while self.wanted is None:
                self.changed.wait()
            wanted, self.wanted = self.wanted, None
            self.state = "working"
            return wanted

    # ---------- The side of the model. ----------
    def view(self):
        turn = self.turn
        notes, self.notes = self.notes, []
        shown = {key: turn[key] for key in ("id", "kind", "agent", "text", "opens")} if turn else None
        if turn:
            shown["fresh"] = [{"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("parameters")} for tool in turn["fresh"]]
            shown["tools"] = [tool["name"] for tool in turn.get("tools") or []]
        return {"type": "solo", "state": self.state, "turn": shown, "notes": notes, "person": self.person, "result": self.result if self.state in FINISHED else ""}

    def waitForNews(self, seconds):
        with self.changed:
            self.changed.wait_for(lambda: self.turn is not None or self.person or self.state in FINISHED, timeout=seconds)
            return self.view()

    def submit(self, reply):
        self.reply = reply
        self.turn = None
        self.changed.notify_all()

    def refuse(self, text):
        return {**self.view(), "type": "error", "message": text}

    def handle(self, message):
        operation, seconds = message.get("op"), min(float(message.get("seconds") or WAIT_SECONDS), WAIT_SECONDS)
        with self.changed:
            turn = self.turn
            if operation == "view":
                return self.view()
            if operation == "wait":
                return self.waitForNews(seconds)
            if operation == "tools":
                return {"type": "tools", "tools": list(turn.get("tools") or []) if turn else []}
            if operation == "next":
                return self.takeNext(message, seconds)
            if operation == "call":
                problem = self.checkCall(turn, message)
                if problem:
                    return self.refuse(problem)
                self.submit({"call": {"name": message["name"], "arguments": message.get("arguments") or {}}})
            elif operation == "answer":
                if not turn or turn["kind"] != "work":
                    return self.refuse(self.describeWaiting())
                if not str(message.get("text") or "").strip():
                    return self.refuse("Write the answer: an empty one would be saved as the result.")
                self.submit({"answer": str(message["text"])})
            elif operation == "decide":
                if not turn or turn["kind"] != "card":
                    return self.refuse(self.describeWaiting())
                self.submit({"decision": DECISIONS.get(message.get("decision"), "deny"), "message": str(message.get("why") or "")})
            else:
                return self.refuse(f"There is no operation {operation}.")
            return self.waitForNews(seconds)

    def takeNext(self, message, seconds):
        if self.state != "over":
            return self.refuse("The round is not over yet. " + self.describeWaiting())
        self.wanted = {"request": str(message.get("text") or "").strip(), "execute": bool(message.get("execute"))}
        if not self.wanted["request"] and not self.wanted["execute"]:
            self.wanted = None
            return self.refuse("Write the next request.")
        self.state = "working"
        self.changed.notify_all()
        return self.waitForNews(seconds)

    def checkCall(self, turn, message):
        if not turn or turn["kind"] != "work":
            return self.describeWaiting()
        names = [tool["name"] for tool in turn.get("tools") or []]
        if message.get("name") not in names:
            return f"You have no tool {message.get('name')} now. Your tools: {', '.join(names) or 'none: this turn only takes an answer'}."
        return ""

    def describeWaiting(self):
        turn = self.turn
        if self.person:
            return f"A person must answer first, in another terminal (swarmup_cli.py attach): {self.person}"
        if turn and turn["kind"] == "card":
            return "A card waits for your decision: solo approve, always or deny."
        if turn:
            return "This turn takes a tool call or an answer."
        return "The round is over." if self.state == "over" else "Nothing waits for you now: the agent is still at work." if self.state not in FINISHED else "The session has ended."


# ==============
# The swarm of one agent, in the daemon of its session.
# ==============
# What the model must decide or know comes to its desk, and the rest goes on as in every session.
def connectDesk(loop, desk, approvals):
    say = loop.notifyUser
    def notify(message):
        say(message)
        desk.note(message)
    loop.notifyUser = notify
    loop.maxSteps = MAX_STEPS_LIMIT
    if approvals == "model":
        loop.askPermission = lambda card: askCard(desk, loop, card)
    elif approvals == "manual":
        loop.askPermission = lambda card, ask=loop.askPermission: askPerson(desk, card, ask)


def describeCard(name, card):
    why = f"\nWhy: {card['reason']}" if card.get("reason") else ""
    return f"{name} wants to {card.get('action')}:\n{card.get('detail')}\nIn: {card.get('folder') or 'its folder'}{why}"


def askCard(desk, loop, card):
    reply = desk.take({"kind": "card", "agent": loop.name, "text": describeCard(loop.name, card), "tools": []})
    return {"decision": reply.get("decision") or "deny", "message": reply.get("message") or ""}


def askPerson(desk, card, ask):
    desk.setPerson(f"{card.get('action')}: {shorten(str(card.get('detail') or ''), 200)}")
    try:
        return ask(card)
    finally:
        desk.setPerson("")


def buildSolo(console, desk, spec):
    swarm = Swarm(spec["mission"])
    answers = {"prompt": spec["mission"], "rules": spec.get("rules") or [], "folder": spec["folder"], "numberOfLoops": DEFAULT_LOOPS, "approvals": spec["approvals"]}
    model = Driver(desk, AGENT_NAME)
    loop = buildAgent(console, swarm, {"task": "agent", "name": AGENT_NAME, "answers": answers}, dict(USER_LEADER, driver=True), model)
    connectDesk(loop, desk, spec["approvals"])
    return swarm, {AGENT_NAME: model}


# A mission of the history: it goes on where it stopped, or it waits for the next request.
def restoreSolo(console, desk, missionId):
    saved, models = loadMission(missionId), {}
    swarm = restoreSwarm(console, saved, models)
    if swarm is None:
        raise ValueError("This mission cannot be continued here.")
    for name in swarm.getAgents():
        member = swarm.getMember(name)
        connectDesk(member["agent"], desk, (member["recipe"].get("answers") or {}).get("approvals"))
    return swarm, models, saved["state"] in UNFINISHED_STATES


# What the architect says goes to its window. The model only reads its reports: it knows the calls it just made.
def tellArchitect(console, desk, text):
    console.say(text, window=ARCHITECT_NAME)
    if not str(text).startswith(f"[{ARCHITECT_NAME}]"):
        desk.note(text)


def describeRound(swarm, outcome):
    if "error" in outcome:
        return f"The swarm could not run: {outcome['error']}"
    info = swarm.getInfo(swarm.getLeader())
    return shorten(str(info["result"]), 600) if info["result"] is not None else f"It did not finish: {info['error'] or 'no result'}"


def runSolo(console, desk, spec):
    console.desk = desk
    try:
        if spec.get("missionId"):
            swarm, models, resume = restoreSolo(console, desk, spec["missionId"])
        else:
            (swarm, models), resume = buildSolo(console, desk, spec), False
        swarm.driver = desk
        attachArchitect(swarm, {"driver": True}, lambda info: Driver(desk, ARCHITECT_NAME), lambda text: tellArchitect(console, desk, text))
    except (ValueError, OSError) as error:
        desk.setState("ended", f"The mission could not start: {error}")
        return
    runRounds(console, desk, swarm, models, spec, resume)


# The rounds of the mission. The last pass of the architect comes before the end of the round, because the model does it too.
def runRounds(console, desk, swarm, models, spec, resume):
    mode = "plan" if spec.get("plan") and not resume else swarm.getMode() if resume else "execute"
    fresh = bool(spec.get("missionId")) and not resume
    while True:
        if not fresh:
            desk.setState("working")
            swarm.setMode(mode)
            outcome = runSwarm(console, swarm, resume)
            resume = False
            report(console, swarm, outcome, models)
            swarm.architect.finish()
            desk.setState("over", describeRound(swarm, outcome))
        else:
            desk.setState("over", "This mission waits for your next request.")
        fresh = False
        wanted = desk.waitForNext()
        if wanted["request"]:
            with STATE_LOCK:
                swarm.followUp(wanted["request"])
            mode = "plan" if wanted.get("plan") else "execute"
        else:
            mode = "execute"


# ==============
# The commands: what the model types, and what it reads.
# ==============
def addSoloArguments(commands):
    solo = commands.add_parser("solo", help="a model that runs this command line is the only agent of a swarm: it gets the tools and the wheels of SwarmUP")
    actions = solo.add_subparsers(dest="action", metavar="action", required=True)
    start = actions.add_parser("start", help="start a mission, and show its first turn")
    start.add_argument("mission", nargs="*", help="what must be done")
    start.add_argument("--folder", default="", help="the folder of the work (the current folder by default)")
    start.add_argument("--approvals", choices=tuple(APPROVALS), help="how the actions are approved: the user chooses it. " + "; ".join(f"{key}: {text}" for key, text in APPROVALS.items()))
    start.add_argument("--plan", action="store_true", help="only plan in this round: nothing changes until the plan is done")
    start.add_argument("--rules", nargs="*", default=[], help="the rules files it follows, like Coder")
    start.add_argument("--open", default="", help="continue a mission of the history (its id) instead")
    for name, text in (("tools", "the tools of this turn, with their arguments"), ("status", "the turn that waits, and where the session stands"),
                       ("wait", "wait for the next turn"), ("close", "close the session: the mission stays in the history")):
        actions.add_parser(name, help=text).add_argument("session", nargs="?")
    call = actions.add_parser("call", help="call a tool, and show the next turn")
    call.add_argument("session")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="*", help="key=value..., or one JSON object")
    call.add_argument("--args-file", default="", help="a file with the arguments, as JSON (for long ones, like the code of a wheel)")
    answer = actions.add_parser("answer", help="give the answer of this turn: the result of the agent, or the report of a pass of the architect")
    answer.add_argument("session")
    answer.add_argument("text", nargs="*")
    answer.add_argument("--file", default="", help="a file with the answer")
    following = actions.add_parser("next", help="when a round is over: the next request")
    following.add_argument("session")
    following.add_argument("text", nargs="*")
    following.add_argument("--execute", action="store_true", help="after a plan: do it")
    for name in DECISIONS:
        decide = actions.add_parser(name, help=f"{name} the card that waits")
        decide.add_argument("session")
        decide.add_argument("why", nargs="*")
    solo.add_argument("--json", action="store_true", help="what the desk says, as JSON")


def describeTools(tools):
    return "Your tools, as commands (solo call SESSION name key=value, or one JSON object; solo tools SESSION gives their arguments):\n" + \
        describeCommands(tools).replace("\n/", "\n").removeprefix("/")


# What to do next, as commands, or as tools for a model that works through MCP (solo_mcp_server.py).
HINTS = {"commands": {"card": "Decide with: solo approve {session}, solo always {session}, or solo deny {session} why", "work": "Next: solo call {session} TOOL key=value, or solo answer {session} \"your answer\"",
                      "person": "Then: solo wait {session}", "over": "Go on with: solo next {session} \"request\", or end with: solo close {session}", "busy": "See the next turn with: solo wait {session}"},
         "tools": {"card": "Decide with solo_decide.", "work": "Next: call one of your tools, or give your answer with solo_answer.", "person": "Then: solo_status.",
                   "over": "Go on with solo_next, or end with solo_close.", "busy": "See the next turn with solo_status."}}


# What the model reads after each of its commands: everything it needs for its next step.
def renderView(view, session="", hints="commands"):
    hint = lambda key: HINTS[hints][key].format(session=session)
    parts = [f"Not done: {view['message']}"] if view.get("type") == "error" else []
    parts += [f"SwarmUP: {note}" for note in view.get("notes") or []]
    turn = view.get("turn")
    if turn:
        if turn["agent"] == ARCHITECT_NAME and turn.get("opens"):
            parts.append("=== The work is done. You are now the architect of this project: decide what to automate, then answer with your report. "
                         "An answer ends the pass, and \"nothing worth automating\" is a good one. ===")
        parts.append(turn["text"])
        if turn.get("fresh"):
            parts.append(describeTools(turn["fresh"]))
        parts.append(f"(Turn {turn['id']}. {hint('card' if turn['kind'] == 'card' else 'work')})")
    elif view.get("person"):
        parts.append(f"A person must answer in another terminal (swarmup_cli.py attach {session}): {view['person']}. {hint('person')}")
    elif view.get("state") == "over":
        parts.append(f"The round is over. {view.get('result') or ''}\n{hint('over')}")
    elif view.get("state") == "ended":
        parts.append(f"The session has ended. {view.get('result') or ''}")
    elif view.get("type") != "error":
        parts.append(f"The agent is still at work. {hint('busy')}")
    return "\n\n".join(parts)


def checkStart(options):
    approvals = options.approvals
    if not approvals and options.open:
        return ""
    if not approvals and sys.stdin.isatty():
        print("\nHow must the actions of the agent be approved?\n" + "\n".join(f"  {key}: {text}" for key, text in APPROVALS.items()))
        approvals = input("Your choice: ").strip().lower()
    if approvals not in APPROVALS:
        raise ValueError("How the actions of the agent are approved is for the user to choose, so ask them, then give --approvals:\n" +
                         "\n".join(f"  {key}: {text}" for key, text in APPROVALS.items()))
    return approvals


def startMission(mission="", folder="", approvals="", plan=False, rules=(), opened=""):
    if opened:
        spec = {"missionId": opened}
    else:
        if not str(mission).strip():
            raise ValueError("Write the mission: what must be done.")
        place = Path(folder or os.getcwd()).expanduser().resolve()
        if not place.is_dir():
            raise ValueError(f"There is no folder {place}.")
        spec = {"mission": str(mission).strip(), "folder": str(place), "approvals": approvals, "plan": bool(plan), "rules": list(rules or [])}
    info = startDaemon(newSessionId(), solo=spec)
    return info, ask(info, "wait")


def ask(info, operation, **details):
    return request(info, {"type": "solo", "op": operation, **details}, timeout=WAIT_SECONDS + REPLY_MARGIN)


def readCall(options):
    if options.args_file:
        arguments = json.loads(Path(options.args_file).read_text(encoding="utf-8"))
        if not isinstance(arguments, dict):
            raise ValueError("The arguments of a tool are one JSON object.")
        return arguments
    if not options.arguments:
        return {}
    words = options.arguments
    call = readCommand(f"/{options.tool} " + (" ".join(words) if words[0].lstrip().startswith("{") else shlex.join(words)))[0]
    if call.get("problem"):
        raise ValueError(call["problem"])
    return call["arguments"]


def readText(options):
    return Path(options.file).read_text(encoding="utf-8") if getattr(options, "file", "") else " ".join(options.text)


def runCommandOf(options):
    if options.action == "start":
        mission = " ".join(options.mission)
        approvals = checkStart(options)
        info, view = startMission(mission, options.folder, approvals, options.plan, options.rules, options.open)
        print(f"Session {info['id']}. Close it with: swarmup_cli.py solo close {info['id']}")
        return info, view
    info = findSession(options.session, listSessions())
    if options.action == "close":
        killSession(info)
        print(f"Session {info['id']} is closed. The mission stays in the history.")
        return info, None
    if options.action == "tools":
        return info, ask(info, "tools")
    if options.action in ("status", "wait"):
        return info, ask(info, "view" if options.action == "status" else "wait")
    if options.action == "call":
        return info, ask(info, "call", name=options.tool, arguments=readCall(options))
    if options.action == "answer":
        return info, ask(info, "answer", text=readText(options))
    if options.action == "next":
        return info, ask(info, "next", text=readText(options), execute=options.execute)
    return info, ask(info, "decide", decision=options.action, why=" ".join(options.why))


def runSoloCommand(options):
    info, view = runCommandOf(options)
    if view is None:
        return
    if options.json:
        print(json.dumps(view, ensure_ascii=False, indent=1))
    elif view.get("type") == "tools":
        print("\n".join(f"{tool['name']}: {tool.get('description', '')}\n  arguments: {json.dumps(tool.get('parameters') or {}, ensure_ascii=False)}" for tool in view["tools"]) or
              "This turn has no tools.")
    else:
        print(renderView(view, info["id"]))
    if view.get("type") == "error":
        sys.exit(1)
