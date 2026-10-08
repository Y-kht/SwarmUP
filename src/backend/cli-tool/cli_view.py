# What the command line shows of a swarm (its tree, its events, an agent) and the commands of the user while it runs.
import os
import threading
import time

from cli_console import paint, shorten
from harness_utils import isYes
from mission_costs import formatDollars
from models_library import DEFAULT_CLI_MODEL, MODELS_CLI


COMMANDS = """Commands (type them at any time while the swarm runs):
  tree                    show the tree of the swarm again
  log                     show the latest messages between the agents
  <agent>                 look at an agent: its task, state, plan, draft or result
  approve <agent>         approve what an agent waits to do (its plan or its result)
  reject <agent>          reject it, the agent stops
  correct <agent> <text>  ask an agent to change its plan or its result
  msg <agent> <text>      send a message to an agent, it reads it with its next prompt
  start <agent>           start an agent that waits for a time of the day now
  add                     add an agent to the swarm while it runs (every agent is told)
  remove <agent> <why>    remove an agent from the swarm (it stops, its model frees its memory, every agent is told)
  cost                    what the mission spent so far, agent by agent, and what is left of its budget
  stop                    stop the swarm for good, after the leader lists what it already did (what is not finished is put back)
  quit                    leave the program: the swarm is saved, and the next start offers to continue it
When the leader asks a question, type yes, no, the name of an agent to look at it alone, or what you want changed."""


# ==============
# The tree of the swarm, drawn in text, and kept up to date live.
# ==============
def describeModel(info):
    if not info:
        return "no model"
    if info.get("cli"):
        return f"{MODELS_CLI[info['cli']]['label']}, {'its default model' if info['name'] == DEFAULT_CLI_MODEL else info['name']}"
    return f"{info['name']}, local, {info['vram']} GB" if info["local"] else f"{info['name']}, API"


def describeState(node, mode):
    waiting = f"waiting for {', '.join(node['waitingOn'])}" if node["waitingOn"] else "waiting"
    if node["status"] == "failed":
        return "FAILED", "red"
    if node["status"] == "paused":
        return "PAUSED: the connection was lost", "red"
    if node["startAt"]:
        return f"scheduled for {node['startAt']}", "yellow"
    if node["review"] == "ready":
        return ("READY: its plan waits for you" if mode == "plan" else "READY: its result waits for your approval"), "green"
    if node["status"] == "done":
        return ("done, plan approved" if mode == "plan" else "done"), "dim"
    if node["status"] == "working":
        return ("approved, working" if node["review"] == "approved" else "writing its plan" if mode == "plan" else "working"), "cyan"
    return waiting, "yellow"


def renderNode(swarm, node, prefix, last, enabled):
    state, color = describeState(node, swarm.getInfo(node["name"])["mode"])
    statuses = swarm.getStatuses()
    waits = f"  waits for: {', '.join(f'{other} ({statuses[other]})' for other in node['waitsFor'])}" if node["waitsFor"] else ""
    branch = "" if prefix is None else prefix + ("`-- " if last else "|-- ")
    lines = [f"{branch}{paint(node['name'], 'bold', enabled)} [{node['role']}] {describeModel(node['model'])} -> {paint(state, color, enabled)}{waits}"]
    children = node["children"]
    for number, child in enumerate(children):
        lines += renderNode(swarm, child, "" if prefix is None else prefix + ("    " if last else "|   "), number == len(children) - 1, enabled)
    return lines


def describeSpent(costs):
    unknown = f" + {costs['unpriced']:,} tokens at an unknown price" if costs["unpriced"] else ""
    return f"{formatDollars(costs['spent'])}{unknown}" + (f" of a budget of {formatDollars(costs['budget'])}" if costs["budget"] else "")


def renderTree(swarm, enabled=False):
    status = swarm.getVramStatus()
    vram = f" | VRAM: {status['needed']} of {status['total']} GB expected, {status['free']} GB free now" if status["needed"] else ""
    costs = swarm.getCosts()
    spent = f" | cost: {describeSpent(costs)}" if costs["spent"] or costs["budget"] or costs["unpriced"] else ""
    header = f"Swarm: {shorten(swarm.mission, 70)} | mode: {swarm.getMode()}{vram}{spent}"
    return "\n".join([header, *renderNode(swarm, swarm.getTree(), None, True, enabled)])


def formatEvent(event):
    kind, agent, when = event["kind"], event["agent"], event["time"]
    if kind == "run":
        return f"== The swarm starts in {event['mode']} mode =="
    if kind == "finished":
        return f"== The swarm finished {'successfully' if event['ok'] else 'without a result'} =="
    if kind == "status":
        return f"[{when}] {agent} is now {event['status']}"
    if kind == "review":
        wording = {"ready": "is ready: it waits for you", "approved": "was approved", "rejected": "was rejected", "": "was asked to correct it"}
        return f"[{when}] {agent} {wording[event['review']]}"
    if kind == "message":
        return f"[{when}] {event['sender']} -> {event['receiver']}: {shorten(event['message'], 90)}"
    if kind == "scheduled":
        return f"[{when}] {agent} is scheduled for {event['startAt']} (type: start {agent})"
    if kind == "connectionLost":
        return f"[{when}] {agent} lost its connection and is paused"
    if kind == "resumed":
        return f"[{when}] The swarm goes on"
    if kind == "stopped":
        return "== The swarm was stopped by you =="
    if kind == "joined":
        return f"[{when}] {agent} joined the swarm (every agent was told)"
    if kind == "removed":
        return f"[{when}] {agent} left the swarm. Why: {(event.get('reason') or 'not given').rstrip('.')}. Every agent was told."
    if kind == "retired":
        return f"[{when}] {agent} is gone: what it left half done was put back, and its model freed its memory"
    if kind == "model":
        return f"[{when}] {agent} now uses {event['model']}"
    if kind == "activity":
        return f"[{when}] {agent}: {shorten(event['text'], 110)}"
    return None


# Follows the swarm: it prints what happens, and the tree each time it changes. The events that come together are printed together.
class TreeView:
    def __init__(self, swarm, console):
        self.swarm = swarm
        self.console = console
        self.events = []
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.stopped = False
        self.last = ""
        self.thread = threading.Thread(target=self.follow, daemon=True)

    def onEvent(self, event):
        with self.lock:
            self.events.append(event)
        self.changed.set()

    # In a session (cli_hub.py) the tree is the panel of window 0 and every agent has its window, instead of a stream of text.
    def flush(self):
        with self.lock:
            events, self.events = self.events, []
        hub = getattr(self.console, "hub", None)
        if hub is not None:
            hub.follow(self.swarm, events)
            return
        lines = [line for line in (formatEvent(event) for event in events) if line]
        tree = renderTree(self.swarm, self.console.color)
        if tree != self.last and (lines or not self.last):
            lines += ["", tree, ""]
            self.last = tree
        if lines:
            self.console.say("\n".join(lines))

    def follow(self):
        while not self.stopped:
            self.changed.wait()
            time.sleep(self.console.treeDelay)
            self.changed.clear()
            self.flush()

    def start(self):
        self.swarm.addListener(self.onEvent)
        self.thread.start()

    def stop(self):
        self.stopped = True
        self.changed.set()
        self.thread.join(timeout=5)
        self.flush()


# ==============
# The commands of the user while the swarm runs.
# ==============
def stopSwarm(console, swarm):
    console.say("The leader is listing what the swarm already did...")
    console.say(swarm.summarizeChanges())
    if isYes(console.ask("Stop the swarm now? What is not finished is put back. (yes/no)")):
        swarm.stopWork()
        console.say("The swarm is stopped.")
    else:
        console.say("The swarm goes on.")


def findAgent(swarm, text):
    return next((name for name in swarm.getAgents() if name.lower() == text.strip().lower()), None)


def openAgent(swarm, name):
    info, lines = swarm.getInfo(name), []
    lines.append(f"{name} [{info['role']}] {'(the leader) ' if info['isLeader'] else ''}model: {describeModel(info['model'])}")
    lines.append(f"  task: {info['task']}")
    lines.append(f"  state: {info['status']}, review: {info['review'] or 'nothing to review'}" + (f", scheduled for {info['startAt']}" if info["startAt"] else ""))
    if info["waitsFor"] and not info["isLeader"]:
        lines.append(f"  waits for: {', '.join(info['waitsFor'])}" + (f" (still waiting for {', '.join(info['waitingOn'])})" if info["waitingOn"] else ""))
    if info["plan"]:
        lines.append(f"  approved plan: {info['plan']}")
    if info["draft"]:
        lines.append(f"  latest draft:\n{info['draft']}" + (f"\n  automatic check: {info['problem']}" if info["problem"] else ""))
    if info["result"]:
        lines.append(f"  result: {shorten(info['result'], 400)}")
    if info["error"]:
        lines.append(f"  problem: {info['error']}")
    if info["review"] == "ready":
        lines.append(f"  You can: approve {name} | reject {name} | correct {name} <what to change> | msg {name} <message>")
    elif info["startAt"]:
        lines.append(f"  You can: start {name} (to collect now) | msg {name} <message>")
    elif info["status"] in ("waiting", "working", "paused"):
        lines.append(f"  You can: msg {name} <message>")
    return "\n".join(lines)


def runCommand(console, swarm, line):
    words = line.strip().split(maxsplit=2)
    if not words:
        return
    word = words[0].lower()
    actions = {"approve": lambda name, text: swarm.approveDraft(name), "reject": lambda name, text: swarm.rejectDraft(name),
               "correct": lambda name, text: swarm.correctDraft(name, text), "msg": lambda name, text: swarm.sendUserMessage(name, text),
               "start": lambda name, text: swarm.startNow(name), "remove": lambda name, text: swarm.removeAgent(name, text or "The user removed it.")}
    if word in ("help", "?"):
        console.say(COMMANDS)
    elif word == "add":
        # The questions of a new agent are asked while the swarm runs, so they run in their own thread (this one reads what the user types).
        if console.newAgent:
            threading.Thread(target=console.newAgent, args=(swarm,), daemon=True).start()
        else:
            console.say("Agents cannot be added here.")
    elif word == "tree":
        console.say(renderTree(swarm, console.color))
    elif word == "cost":
        console.say(describeCosts(swarm))
    elif word == "log":
        console.say("\n".join(f"[{message['time']}] {message['sender']} -> {message['receiver']}: {shorten(message['message'], 120)}"
                              for message in swarm.getMessages()[-15:]) or "No messages yet.")
    elif word == "stop":
        # The leader takes time to list the changes, and the answer is typed while the swarm runs: it all happens in its own thread.
        threading.Thread(target=stopSwarm, args=(console, swarm), daemon=True).start()
    elif word == "quit":
        console.say("Leaving. The agents that work are stopped. Your swarm is saved: start the program again to continue it.")
        swarm.saveForExit()
        os._exit(0)
    elif word in actions:
        name = findAgent(swarm, words[1]) if len(words) > 1 else None
        if not name:
            console.say(f"Write the name of an agent after {word}. The agents are: {', '.join(swarm.getAgents())}.")
        elif word in ("correct", "msg") and len(words) < 3:
            console.say(f"Write what you want to say after {word} {name}.")
        else:
            try:
                actions[word](name, words[2] if len(words) > 2 else "")
                console.say(f"Done: {word} {name}.")
            except ValueError as error:
                console.say(str(error))
    elif findAgent(swarm, line):
        console.say(openAgent(swarm, findAgent(swarm, line)))
    elif ":" in line and findAgent(swarm, line.split(":", 1)[0]):
        name, text = line.split(":", 1)
        runCommand(console, swarm, f"msg {name.strip()} {text.strip()}")
    else:
        console.say("I do not know this command. Type help.")


def describeCosts(swarm):
    costs = swarm.getCosts()
    lines = [f"The mission spent {describeSpent(costs)}."]
    if costs["budget"]:
        lines.append(f"The agents that did not finish set aside {formatDollars(costs['setAside'])}, so {formatDollars(costs['left'])} is left for new models.")
    lines += [f"  {row['agent']}: {formatDollars(row['cost'])} ({', '.join(row['models'])}; {row['calls']} calls, {row['input']:,} tokens read, {row['output']:,} written)"
              + (f" + {row['unpriced']:,} tokens at an unknown price" if row["unpriced"] else "") for row in costs["agents"]]
    return "\n".join(lines)


# After every call of a model the user is told when the mission reaches 80% and 100% of its budget.
def watchCosts(console, swarm, loop):
    loop.onUsage = lambda: [console.say(f"Warning: {warning}") for warning in swarm.costs.warnings()]
