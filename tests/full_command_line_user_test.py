# A real-user test of SwarmUP in the command line. It asks guiding questions to build a swarm, and then runs it.
# Run it with: python tests/full_command_line_user_test.py
#
# 1. Who builds the swarm: you, agent by agent, or the leader (it proposes the agents, their tasks and their models, and you approve).
# 2. How many agents, and the mission of the swarm.
# 3. For every agent: its task (email, writing, coding, math checking, literature review...) and what it needs to work (credentials, files, outlets...).
# 4. For every agent: the folder it works inside (optional).
# 5. For every agent: its model, local on the GPUs (with the VRAM it needs, and a check of the GPUs) or paid through an API (with the prices).
# 6. The tree of the swarm, which is updated live while the swarm plans or executes: who waits for whom, who talks to whom, what needs your approval.
# When the leader builds the swarm, steps 2 to 5 are the mission, the folder of the mission and the model of the leader: the leader does the rest,
# and while the swarm works it can propose to add or remove agents, which you approve or reject like its first proposal.
# While the swarm runs you can type commands (type help): look at an agent, send it a message, approve, reject or correct it, or start it earlier.
# The state of the swarm is saved all the time. If the connection is lost the swarm pauses and asks you to continue or cancel, and if the program
# or the computer stops, the next start offers to continue the swarm where it was (or to cancel it, after a summary of what it did).
# The graphical interface will do the same with clicks, from the same functions of src/backend (tasks_library.py, models_library.py and the modules of swarm-utils).
# Local models are downloaded to the Hugging Face cache (the HF_HOME environment variable), and API keys come from the environment variables
# of their provider (see API_KEYS in models_library.py) or are typed here. Keys and passwords are only kept in memory.
import getpass
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
from base_loop import Loop
from codex_agent import CodexLogin, checkCodex, listCodexModels, readCodexAccount
from gpu_check import checkVram, readGpus
from harness_utils import ConnectionLost, FETCH_ERRORS, describeError
from internet_cache import describeCached, getModelCost, keepFresh
from leader_catalog import LeaderCatalog
from leader_manager import LeaderManager
from leader_utils import designSwarm
from message_loops import checkEmailLogin, nextOccurrence
from messengers import MessagingError, checkMessenger, findTelegramChats
from mission_costs import MissionCosts, checkBudget, formatDollars
from model_clients import LocalModel, createModel
from model_support import ModelError, findMissingPackages, getApiKey, getHubFolder, isDownloaded, lookupHuggingFace
from models_library import (API_KEYS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, getProvider, isGated)
from saved_swarms import findUnfinishedSwarms
from sources_library import ALL_NEWS_OUTLETS, MESSAGING_APPS, NEWS_OUTLETS, PAPER_PUBLISHERS
from swarm_harness import Swarm
from tasks_library import (ADVANCED_FIELDS, DEFAULT_LOOPS, LEADER_TASK, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getHelp, getTask, isAsked,
                           messengerSettings, parseAnswer, parseChoices, publicAnswers, restoreAnswers, secretFields, suggestFolder, suggestName)
from writing_loops import findPublishers

LINE = "=" * 72
SHOW_ALL, MANUAL, BACK = "Show all the models of the library", "Type the name of another model myself", "Go back"
CHANGE_KIND = "back"
META_WORDS = ("tree", "help", "?", "log")
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
  quit                    leave the program (the agents are stopped)
When the leader asks a question, type yes, no, the name of an agent to look at it alone, or what you want changed."""


# ==============
# The console: everything the program says to the user and everything the user types goes through here.
# While the swarm runs, one thread reads what the user types and gives it to the question that waits, or runs it as a command.
# ==============
class Console:
    def __init__(self, read=input, readSecret=getpass.getpass, write=None, interactive=None, treeDelay=0.3):
        self.read = read
        self.readSecret = readSecret
        self.write = write or (lambda text: print(text, flush=True))
        self.interactive = (sys.stdin.isatty() and sys.stdout.isatty()) if interactive is None else interactive
        self.color = self.interactive and not os.environ.get("NO_COLOR") and (os.name != "nt" or bool(os.environ.get("WT_SESSION")))
        self.treeDelay = treeDelay
        self.output = threading.RLock()
        self.questions = threading.Lock()
        self.pending = None
        self.pumping = False
        self.closed = False
        self.commands = None
        # Adds an agent to the swarm while it runs (the command add), set by the program that knows the keys and the models.
        self.newAgent = None

    def say(self, text=""):
        with self.output:
            self.write(text)
            if self.pending and not self.pending["event"].is_set():
                self.write(f"  (the question that waits for you) {self.pending['text']}")

    def ask(self, question):
        if not (self.pumping and self.interactive):
            with self.questions:
                return self.read(f"{question} ")
        with self.questions:
            if self.closed:
                return ""
            pending = {"text": question, "event": threading.Event(), "answer": ""}
            with self.output:
                self.write(question)
                self.pending = pending
            pending["event"].wait()
            self.pending = None
            return pending["answer"]

    def askSecret(self, question):
        if self.pumping:
            self.say("(A secret cannot be typed while the swarm runs in this test. Give it in the setup. This question is skipped.)")
            return ""
        return self.readSecret(f"{question} ")

    def close(self):
        self.closed = True
        if self.pending:
            self.pending["event"].set()

    def route(self, line):
        pending = self.pending
        if line.strip().lower() in META_WORDS or not (pending and not pending["event"].is_set()):
            if self.commands:
                self.commands(line)
        else:
            pending["answer"] = line
            pending["event"].set()

    # Runs in its own thread while the swarm runs. It ends with the first line typed after pumping is set to False.
    def pump(self):
        while True:
            try:
                line = self.read("")
            except EOFError:
                self.close()
                return
            if not self.pumping:
                return
            self.route(line)


def paint(text, color, enabled):
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36", "dim": "2", "bold": "1"}
    return f"\033[{codes[color]}m{text}\033[0m" if enabled else text


def shorten(text, width=100):
    line = " ".join(str(text).split())
    return line if len(line) <= width else line[:width - 3] + "..."


def askUntilValid(console, question, parse, secret=False):
    while True:
        value, error = parse(console.askSecret(question) if secret else console.ask(question))
        if not error:
            return value
        console.say(error)


def askYesNo(console, question, default=True):
    while True:
        text = console.ask(f"{question} ({'Y/n' if default else 'y/N'})").strip().lower()
        if not text:
            return default
        if text in ("y", "yes"):
            return True
        if text in ("n", "no"):
            return False
        console.say("Please answer yes or no.")


# Shows numbered choices and asks until the answer is valid. It returns the label of the choice (a list of labels with one=False).
# extra(text) can handle an answer of its own (like a request for information) and returns True if it did. An empty answer gives the default.
# words maps words that can be typed instead of a number to what they give (like back).
def chooseFrom(console, title, labels, one=True, extra=None, default=None, hint="", none=False, words=None):
    console.say(title)
    for number, label in enumerate(labels, 1):
        console.say(f"  {number}. {label}")
    while True:
        text = console.ask(hint or ("Your choice (a number):" if one else "Your choices (numbers like 1,3-5):")).strip()
        if words and text.lower() in words:
            return words[text.lower()]
        if extra and extra(text):
            continue
        if not text and default is not None:
            return default
        if none and text.lower() == "none":
            return []
        chosen, error = parseChoices(labels, text, one)
        if not error:
            return chosen[0] if one else chosen
        console.say(error)


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

    def flush(self):
        with self.lock:
            events, self.events = self.events, []
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


# ==============
# The questions about the swarm and its agents.
# ==============
def heading(console, text):
    console.say(f"\n{LINE}\n{text}\n{LINE}")


def askAgentCount(console):
    console.say("A swarm is a team of agents. Each agent has one task (write an email, check a proof...) and a model, the AI that does the thinking. "
                "The first agent is the leader: it summarises the work of the others for you, and passes your corrections on to the agents they concern. "
                "One agent is enough to start.")
    return askUntilValid(console, "How many agents do you want in your swarm?", lambda text: parseAnswer({"key": "count", "ask": "", "kind": "number", "required": True}, text))


# The budget of the whole mission, for its API models: each one chosen sets aside the price of 1 million of its tokens (see MissionCosts).
def askBudget(console, costs):
    console.say("You can give the mission a budget, in US dollars, for its API models (the leader included). Each API model you choose sets aside the price of "
                "1 million of its tokens, and the lists only show the models that fit in what is left. Models on your GPUs and Codex cost nothing from it. "
                "What the mission spends is counted, and shown while the swarm runs (type cost).")
    def parse(text):
        try:
            return checkBudget(text), ""
        except ValueError as error:
            return None, str(error)
    costs.setBudget(askUntilValid(console, "Budget of the mission in US dollars (Enter for no budget):", parse))


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


def askMission(console):
    console.say("The agents read the mission to understand what they work for.")
    return askUntilValid(console, "What is the mission of the swarm? (one or two sentences)", lambda text: parseAnswer({"key": "mission", "ask": "", "kind": "text", "required": True}, text))


def chooseTask(console, number, total):
    keys = list(TASKS)
    labels = [TASKS[key]["label"] for key in keys]
    def showInfo(text):
        if not text.lower().startswith(("info", "?")):
            return False
        chosen, error = parseChoices(labels, text.lstrip("?").strip() if text.startswith("?") else text[4:], one=True)
        console.say(TASKS[keys[labels.index(chosen[0])]]["info"] if not error else "Write info and the number of a task, like: info 3")
        return True
    label = chooseFrom(console, f"\nAgent {number} of {total}: what must this agent do? (type info 2 to read about task 2)", labels, extra=showInfo)
    return keys[labels.index(label)]


def askName(console, key, taken):
    suggestion = suggestName(key, taken)
    def parse(text):
        name = text.strip() or suggestion
        error = checkAgentName(name, taken)
        return (None, error) if error else (name, "")
    return askUntilValid(console, f"Name of this agent [{suggestion}]:", parse)


def chooseOutlets(console):
    console.say("Pick outlets from the lists, group by group. You can also give the address of any news feed (RSS or Atom), or of a news website that has one.")
    chosen = []
    while True:
        groups = list(NEWS_OUTLETS)
        labels = [f"{group} ({len(NEWS_OUTLETS[group])} outlets)" for group in groups] + ["Search an outlet by name", "Add the address of a feed myself", f"Done ({len(chosen)} chosen)"]
        index = labels.index(chooseFrom(console, f"\nOutlets chosen so far: {', '.join(chosen) or 'none'}", labels))
        if index == len(labels) - 1:
            if chosen:
                return chosen
            console.say("Choose at least one outlet.")
        elif index == len(labels) - 2:
            address = askUntilValid(console, "Address of the feed (starting with http:// or https://):",
                                    lambda text: (text.strip(), "") if text.strip().startswith(("http://", "https://")) else (None, "The address must start with http:// or https://."))
            chosen.append(address)
        elif index == len(labels) - 3:
            text = console.ask("Part of the name of the outlet:").strip().lower()
            found = [name for name in ALL_NEWS_OUTLETS if text and text in name.lower()]
            if not found:
                console.say("No outlet has this in its name.")
            chosen += [name for name in (chooseFrom(console, "Which ones?", found, one=False, hint="Your choices (numbers like 1,3-5; empty = none):", default=[]) if found else []) if name not in chosen]
        else:
            names = list(NEWS_OUTLETS[groups[index]])
            picked = chooseFrom(console, f"\n{groups[index]}:", names, one=False, hint="Your choices (numbers like 1,3-5; empty = none):", default=[])
            chosen += [name for name in picked if name not in chosen]


def choosePublishers(console):
    console.say("A survey can be restricted to some publishers: then only their papers are searched (with the number Crossref gives to each publisher). "
                "Choose none to search only in the search engines.")
    names = list(PAPER_PUBLISHERS)
    picked = chooseFrom(console, "Publishers:", names, one=False, hint="Your choices (numbers like 1,3-5; empty = none):", default=[])
    publishers = {name: PAPER_PUBLISHERS[name] for name in picked}
    while askYesNo(console, "Add a publisher that is not in the list?", False):
        text = askUntilValid(console, "Name of the publisher:", lambda value: parseAnswer({"key": "publisher", "ask": "", "kind": "text", "required": True}, value))
        try:
            found = findPublishers(text)
        except FETCH_ERRORS as error:
            console.say(f"Crossref could not be asked: {describeError(error)}.")
            continue
        labels = [f"{publisher['name']} ({publisher['papers']:,} works)" for publisher in found]
        if not found:
            console.say("Crossref does not know a publisher with this name.")
            continue
        label = chooseFrom(console, "Which one?", labels + ["None of them"])
        if label != "None of them":
            publishers[found[labels.index(label)]["name"]] = found[labels.index(label)]["id"]
    return publishers


def askAccounts(console):
    accounts = {}
    while askYesNo(console, "Add an account for a website?", False):
        host = askUntilValid(console, "The website (like ieeexplore.ieee.org):", lambda text: (text.strip().lower(), "") if text.strip() and "/" not in text else (None, "Write only the name of the website, without https:// or a path."))
        user = askUntilValid(console, f"Username for {host}:", lambda text: parseAnswer({"key": "user", "ask": "", "kind": "text", "required": True}, text))
        accounts[host] = (user, askUntilValid(console, f"Password for {host} (hidden):", lambda text: parseAnswer({"key": "password", "ask": "", "kind": "secret", "required": True}, text), secret=True))
    return accounts


def askField(console, field, answers):
    kind, help = field["kind"], getHelp(field, answers)
    if help:
        console.say(f"  ({help})")
    if kind == "choice":
        default = getDefault(field, answers)
        return chooseFrom(console, field["ask"], field["options"], default=default, hint=f"Your choice (a number, Enter = {default}):" if default else "")
    if kind == "choices":
        picked = chooseFrom(console, field["ask"], field["options"], one=False, default=field["default"], none=True,
                            hint="Your choices (numbers like 1,3-4; Enter = all; none = none of them):")
        return picked
    if kind == "outlets":
        return chooseOutlets(console)
    if kind == "publishers":
        return choosePublishers(console)
    if kind == "accounts":
        console.say(field["ask"])
        return askAccounts(console)
    default = getDefault(field, answers)
    shown = f" [{' '.join(default) if isinstance(default, list) else default}]" if default not in (None, "") and kind != "secret" else ""
    return askUntilValid(console, f"{field['ask']}{shown}:", lambda text: parseAnswer(field, text, answers), secret=kind == "secret")


def explainSchedule(console, clock):
    if not clock:
        console.say("The feeds are collected as soon as the agent starts.")
        return
    moment = nextOccurrence(clock)
    minutes = int((moment - datetime.now()).total_seconds() // 60)
    console.say(f"The feeds will be collected on {moment:%A %Y-%m-%d at %H:%M}, in {minutes // 60} h {minutes % 60} min. This agent waits until then, the other agents "
                "do not. While the swarm runs you can start it earlier with: start <agent>")


def verifyEmail(console, answers):
    password = next(field for field in TASKS["email"]["fields"] if field["key"] == "password")
    while askYesNo(console, "Test the login now? Nothing is sent: the program only logs in to check your password.", True):
        console.say("Testing the login...")
        problem = checkEmailLogin(answers["sender"], answers["password"], answers["smtp"], answers["imap"])
        if not problem:
            console.say("The login works.")
            return
        console.say(problem)
        if not askYesNo(console, "Type the password again?", True):
            return
        answers["password"] = askField(console, password, answers)


# What the agent will do, in the words of the loop itself, built without a model.
def describeAgent(spec):
    return describeLoop(buildLoop(spec["task"], None, spec["answers"]))


# The chat of the user in Telegram is found from the messages they sent to their bot.
def lookUpTelegramChat(console, token):
    console.say("Open your bot in Telegram, press Start and send it any message. Then come back here.")
    while True:
        if console.ask("Press Enter when you sent a message to your bot (or type skip to write the chat number yourself):").strip().lower() == "skip":
            return ""
        try:
            chats = findTelegramChats(token)
        except (MessagingError, ConnectionLost) as error:
            console.say(str(error))
            chats = []
        if chats:
            labels = [f"{chat['name']} (chat number {chat['id']})" for chat in chats]
            return str(chats[labels.index(chooseFrom(console, "Which chat is yours?", labels))]["id"])
        console.say("I did not see any message yet.")
        if not askYesNo(console, "Try again?", True):
            return ""


def verifyMessenger(console, answers):
    app = answers.get("messenger")
    if app not in MESSAGING_APPS:
        return
    chat = answerKey(app, "chat")
    while app == "Telegram" and not answers[chat]:
        answers[chat] = lookUpTelegramChat(console, answers[answerKey(app, "token")]) or askField(console, {"ask": "Your chat number", "kind": "text", "required": True}, answers)
    while askYesNo(console, f"Test the connection to {app} now? Nothing is sent: only the information is checked.", True):
        console.say(f"Testing {app}...")
        problem = checkMessenger(app, messengerSettings(answers))
        if not problem:
            console.say(f"{app} accepted the information.")
            return
        console.say(problem)
        if not askYesNo(console, "Enter the information again?", True):
            return
        for field in MESSAGING_APPS[app]["fields"]:
            answers[answerKey(app, field["key"])] = askField(console, field, answers)


def fillTask(console, key, taken):
    task, answers = TASKS[key], {}
    console.say(f"\n--- {task['label']} ---\n{task['info']}")
    for field in task["fields"]:
        if not isAsked(field, answers):
            continue
        answers[field["key"]] = askField(console, field, answers)
        if field["key"] == "collectAt":
            explainSchedule(console, answers["collectAt"])
        if field["key"] == "messenger" and answers["messenger"] in MESSAGING_APPS:
            console.say(MESSAGING_APPS[answers["messenger"]]["info"])
    while key == "literature" and not (answers["searches"] or answers["publishers"]):
        console.say("The survey needs a place to search: choose at least a search engine or a publisher.")
        answers["searches"], answers["publishers"] = (askField(console, field, answers) for field in task["fields"] if field["key"] in ("searches", "publishers"))
    if key == "email":
        verifyEmail(console, answers)
    if key == "news":
        verifyMessenger(console, answers)
    if askYesNo(console, "Change the advanced settings of this agent?", False):
        for field in ADVANCED_FIELDS:
            answers[field["key"]] = askField(console, field, answers)
    spec = {"task": key, "answers": answers, "name": askName(console, key, taken)}
    console.say(f"{spec['name']} will do this: {describeAgent(spec)}")
    return spec


# ==============
# The folder of each agent: where it saves what it makes, and where the files it works on must be.
# ==============
def askFolder(console, spec, number, total):
    task = getTask(spec["task"])
    suggestion = suggestFolder(spec["answers"])
    console.say(f"\n--- Folder of agent {number} of {total}: {spec['name']} ({task['role']}) ---\n{describeAgent(spec)}\n{task['folder']}")
    def parse(text):
        if text.strip().lower() == "none" or not (text.strip() or suggestion):
            return None, ""
        value, error = parseAnswer({"key": "folder", "ask": "", "kind": "folder"}, text.strip() or suggestion)
        if error:
            return None, error
        try:
            buildLoop(spec["task"], None, {**spec["answers"], "folder": value})
        except ValueError as problem:
            return None, str(problem)
        return value, ""
    shown = f" [{suggestion}]" if suggestion else ""
    return askUntilValid(console, f"Folder of {spec['name']}{shown} (a path, Enter = {'the one in brackets' if suggestion else 'no folder'}, none = no folder):", parse)


def chooseFolders(console, specs):
    console.say("An agent can work inside a folder of your computer, where it saves what it makes. This is optional, and every agent can have its own folder.")
    for number, spec in enumerate(specs, 1):
        spec["answers"]["folder"] = askFolder(console, spec, number, len(specs))


# ==============
# The model of each agent: local on the GPUs, or paid through an API.
# ==============
def describeGpus(status):
    lines = ["GPU check:"] + [f"  {gpu['name']}: {gpu['total']} GB in total, {gpu['free']} GB free now" for gpu in status["gpus"]]
    lines.append(f"  All the GPUs: {status['total']} GB in total, {status['free']} GB free now ({status['used']} GB are used by other jobs)")
    lines.append(f"  VRAM the swarm is expected to need: {status['needed']} GB. Left in the GPUs: {status['left']} GB.")
    return "\n".join(lines)


def describeCost(cost):
    if "error" in cost:
        return f"  {cost['model']}: {cost['error']} Official prices: {cost['page']}"
    cached = f", cached input ${cost['cachedInput']}" if cost.get("cachedInput") is not None else ""
    context = f". Context window: {cost['context']:,} tokens" if cost.get("context") else ""
    note = f" {cost['note']}" if cost.get("note") else ""
    return f"  {cost['model']}: input ${cost['input']} and output ${cost['output']} per 1 million tokens{cached}{context}.{note} Official prices: {cost['page']}"


def labelLocal(swarm, name, replacing=None):
    info = getModelInfo(name)
    check = swarm.checkModel(info, replacing)
    mark = "" if not check["message"] else "  [too big for the GPUs]" if not check["allowed"] else "  [not free now]"
    return f"{name} ({info['vram']} GB{', gated' if isGated(name) else ''}){mark}"


def browseAll(console, groups, label):
    names = list(groups)
    labels = [f"{name} ({len(groups[name])} models)" for name in names]
    picked = chooseFrom(console, f"\n{label}:", labels + [BACK])
    return None if picked == BACK else names[labels.index(picked)]


def askManualLocal(console):
    name = askUntilValid(console, "Name of the model on Hugging Face (owner/name, like Qwen/Qwen3-8B):",
                         lambda text: (text.strip(), "") if text.strip().count("/") == 1 and " " not in text.strip() else (None, "It is written owner/name, like Qwen/Qwen3-8B."))
    billions = next((family[name] for family in MODELS_LOCAL.values() if name in family), None)
    if billions is None:
        try:
            found = lookupHuggingFace(name)
        except ModelError as error:
            console.say(str(error))
            return None
        billions = found["billions"]
        console.say(f"Found on Hugging Face. " + (f"It has {billions} billion parameters." if billions else "Its size is not published."))
        if not billions:
            billions = askUntilValid(console, "How many billion parameters does it have? (a number, like 7 or 8.2)",
                                     lambda text: (float(text), "") if text.replace(".", "", 1).isdigit() and float(text) > 0 else (None, "Write a number above 0, like 8.2."))
    return getModelInfo(name, billions=billions)


# The first list only has the models that fit in the VRAM the other agents leave. All of them are in the list of all the models.
def chooseLocalModel(console, swarm, task, replacing=None):
    listed = RECOMMENDED_LOCAL[getTask(task)["recommend"]]
    recommended = [name for name in listed if swarm.checkModel(getModelInfo(name), replacing)["allowed"]]
    while True:
        labels = [labelLocal(swarm, name, replacing) for name in recommended] + [SHOW_ALL, MANUAL]
        console.say("\nThe number in parentheses is the VRAM, in GB, that the model is expected to need.")
        if len(recommended) < len(listed):
            console.say(f"{len(listed) - len(recommended)} recommended models need more VRAM than the other agents leave, so they are not shown here. They are in the list of all the models.")
        picked = chooseFrom(console, "Models recommended for this task, from the smallest to the largest (type back to choose again where the model runs):", labels,
                            words={"back": CHANGE_KIND})
        if picked == CHANGE_KIND:
            return None
        if picked == SHOW_ALL:
            families = {family: list(models) for family, models in MODELS_LOCAL.items()}
            family = browseAll(console, families, "Families of local models")
            if not family:
                continue
            shown = [labelLocal(swarm, name, replacing) for name in families[family]]
            back = chooseFrom(console, f"\n{family}:", shown + [BACK])
            if back == BACK:
                continue
            info = getModelInfo(families[family][shown.index(back)])
        elif picked == MANUAL:
            info = askManualLocal(console)
            if info is None:
                continue
        else:
            info = getModelInfo(recommended[labels.index(picked)])
        check = swarm.checkModel(info, replacing)
        if not check["allowed"]:
            console.say(check["message"])
            continue
        return info


# From when the prices are: without internet, they are those of the last connection.
def describePriceDate():
    state = describeCached("model-prices")
    if not state["fetchedAt"]:
        return ""
    return f"No internet: the prices are those of the last connection, {state['fetchedAt']}." if state["offline"] else f"Prices of {state['fetchedAt']}."


def describePrice(costs, info):
    price = costs.priceOf(info)["reference"]
    return "price unknown" if price is None else f"{formatDollars(price)} per 1M tokens"


# What is left of the budget of the mission for the model of an agent (replacing is the agent whose model changes), or None without a budget.
def moneyLeft(swarm, replacing=None):
    return swarm.costs.left([member for member in swarm.describeTeam() if member["agent"] != replacing], wait=True)["left"]


def overBudget(swarm, info, money):
    price = swarm.costs.priceOf(info)["reference"]
    return money is not None and price is not None and price > money


# The first list only has the models whose price of 1 million tokens fits in what is left of the budget. All of them are in the list of all the models.
def chooseApiModel(console, swarm, task, replacing=None):
    money = moneyLeft(swarm, replacing)
    listed = RECOMMENDED_API[getTask(task)["recommend"]]
    recommended = [name for name in listed if not overBudget(swarm, getModelInfo(name), money)]
    if money is not None:
        console.say(f"\n{formatDollars(max(money, 0))} of the budget of the mission is left for this model.")
    if describePriceDate():
        console.say(describePriceDate())
    if len(recommended) < len(listed):
        console.say(f"{len(listed) - len(recommended)} recommended models cost more than that per 1 million tokens, so they are not shown here. They are in the list of all the models.")
    while True:
        labels = [f"{name} ({API_KEYS[getProvider(name)]['company']}, {describePrice(swarm.costs, getModelInfo(name))})" for name in recommended] + [SHOW_ALL, MANUAL]
        def showPrices(text):
            word, _, rest = text.partition(" ")
            if word.lower() not in ("p", "price", "prices"):
                return False
            chosen, error = parseChoices(labels[:len(recommended)], rest) if rest.strip() and rest.strip().lower() != "all" else (labels[:len(recommended)], "")
            console.say(error or "\n".join(describeCost(getModelCost(getProvider(recommended[labels.index(label)]), recommended[labels.index(label)])) for label in chosen))
            return True
        picked = chooseFrom(console, "\nModels recommended for this task. Type p 2 for the price of model 2, or p all for the prices of all of them (type back to choose again where the model runs):",
                            labels, extra=showPrices, words={"back": CHANGE_KIND})
        if picked == CHANGE_KIND:
            return None
        if picked == SHOW_ALL:
            family = browseAll(console, MODELS_API, "Providers of API models")
            if not family:
                continue
            def showFamilyPrices(text):
                word, _, rest = text.partition(" ")
                if word.lower() not in ("p", "price", "prices"):
                    return False
                chosen, error = parseChoices(MODELS_API[family], rest) if rest.strip() and rest.strip().lower() != "all" else (MODELS_API[family], "")
                console.say(error or "\n".join(describeCost(getModelCost(family, name)) for name in chosen))
                return True
            shown = [f"{name} ({describePrice(swarm.costs, getModelInfo(name))}){'  [over your budget]' if overBudget(swarm, getModelInfo(name), money) else ''}"
                     for name in MODELS_API[family]]
            name = chooseFrom(console, f"\n{family} (type p 2 for the price of model 2, or p all):", shown + [BACK], extra=showFamilyPrices)
            if name == BACK:
                continue
            info = getModelInfo(MODELS_API[family][shown.index(name)])
            if overBudget(swarm, info, money):
                console.say(f"Warning: {info['name']} costs more per 1 million tokens than the {formatDollars(max(money, 0))} left of the budget.")
            return info
        if picked == MANUAL:
            provider = chooseFrom(console, "\nWhich company provides the model?", [f"{API_KEYS[key]['company']} ({key}-...)" for key in MODELS_API] + [BACK])
            if provider == BACK:
                continue
            key = list(MODELS_API)[[f"{API_KEYS[key]['company']} ({key}-...)" for key in MODELS_API].index(provider)]
            name = askUntilValid(console, "Exact name of the model, as the company writes it in its API:", lambda text: (text.strip(), "") if text.strip() else (None, "This answer is needed."))
            return getModelInfo(name, provider=key)
        return getModelInfo(recommended[labels.index(picked)])


def chooseKind(console, swarm, number, replacing=None):
    status = checkVram(swarm.getNeededVram(replacing), readGpus())
    local = f"Local: runs on your GPUs ({status['total']} GB of VRAM, {status['free']} GB free now). Free to use and private, but the model must fit in the VRAM." if status["gpus"] \
        else "Local: runs on your GPUs. No supported GPU was found on this computer, so it is not possible."
    api = "API: runs on the servers of a company (OpenAI, Anthropic, Google, DeepSeek). You pay for every use, and you need an API key."
    cli = ("Coding agent: Claude Code (with your Anthropic API key) or Codex (with your ChatGPT plan), on this computer. It can also read files and run "
           "commands, each time with your approval.")
    while True:
        console.say(f"\nWhere must the model of agent {number} run?\n  {local}\n  {api}\n  {cli}")
        kind = chooseFrom(console, "", ["Local (on my GPUs)", "API (paid)", "Coding agent (Claude Code or Codex)"], hint="Your choice (1, 2 or 3):")
        if not kind.startswith("Local") or status["gpus"]:
            return "local" if kind.startswith("Local") else "api" if kind.startswith("API") else "cli"
        console.say(status["message"] or "No supported GPU was found, so local models cannot run. Choose API models.")


# Codex uses the ChatGPT plan of the user: if it is not signed in, the user signs in on the page of OpenAI (in the browser, or with a code).
# It returns True when Codex is ready.
def signInCodex(console):
    status = checkCodex()
    if status["problem"]:
        console.say(status["problem"])
        return False
    account = readCodexAccount()
    while not account["signedIn"]:
        options = ["Sign in with ChatGPT in the browser", "Sign in with a code (when the browser cannot come back to this computer)", BACK]
        picked = chooseFrom(console, "\nCodex is not signed in. It uses your ChatGPT plan: you sign in on the page of OpenAI, and Codex keeps the sign-in.", options)
        if picked == BACK:
            return False
        login = CodexLogin()
        target = login.start("code" if picked == options[1] else "browser")
        if target["code"]:
            console.say(f"Open {target['url']}, sign in with your ChatGPT account, and type this code: {target['code']}")
        else:
            console.say(f"Open this page in your browser and sign in with your ChatGPT account:\n{target['url']}")
        console.say("Waiting for the sign-in (up to 15 minutes)...")
        problem = login.wait(timeout=900)
        if problem:
            console.say(f"The sign-in did not work: {problem}")
        account = readCodexAccount()
    console.say(f"Codex is signed in{' as ' + account['email'] if account.get('email') else ''}.")
    return True


def chooseCodingAgent(console, swarm, replacing=None):
    while True:
        labels = [f"{agent['label']} ({'your Anthropic API key' if agent['provider'] else 'your ChatGPT plan'})" for agent in MODELS_CLI.values()] + [BACK]
        picked = chooseFrom(console, "\nWhich coding agent? (Anthropic does not allow other programs to use a Claude subscription, so Claude Code is used with an API key.)", labels)
        if picked == BACK:
            return None
        cli = list(MODELS_CLI)[labels.index(picked)]
        if cli == "codex":
            if not signInCodex(console):
                continue
            models = [DEFAULT_CLI_MODEL] + [model["id"] for model in listCodexModels()]
        else:
            money = moneyLeft(swarm, replacing)
            models = [name for name in MODELS_CLI[cli]["models"] if name == DEFAULT_CLI_MODEL or not overBudget(swarm, getModelInfo(name, cli=cli), money)]
            if len(models) < len(MODELS_CLI[cli]["models"]):
                console.say(f"{len(MODELS_CLI[cli]['models']) - len(models)} models cost more per 1 million tokens than the {formatDollars(max(money, 0))} left of the budget, so they are not shown.")
        shown = [f"Let {MODELS_CLI[cli]['label']} choose" if name == DEFAULT_CLI_MODEL else name if cli == "codex" else
                 f"{name} ({describePrice(swarm.costs, getModelInfo(name, cli=cli))})" for name in models]
        name = chooseFrom(console, f"\nWhich model must {MODELS_CLI[cli]['label']} use?", shown + [BACK])
        if name == BACK:
            continue
        return getModelInfo(models[shown.index(name)], cli=cli)


def prepareLocal(console, info, tokens):
    name = info["name"]
    if isDownloaded(name):
        console.say(f"{name} is already downloaded in {getHubFolder()}.")
    else:
        folder = getHubFolder()
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        console.say(f"{name} will be downloaded to {getHubFolder()}: about {round(info['billions'] * 2, 1)} GB, and {shutil.disk_usage(folder).free / 1e9:.0f} GB are free there. "
                    "Change the folder with the HF_HOME environment variable.")
    if isGated(name) and not os.environ.get("HF_TOKEN"):
        console.say(f"{name} is a gated model: accept its license at https://huggingface.co/{name} first. Then give a Hugging Face token (created at "
                    "https://huggingface.co/settings/tokens), or press Enter if you already logged in with huggingface-cli.")
        token = askUntilValid(console, "Hugging Face token (hidden, Enter to skip):", lambda text: (text.strip(), ""), secret=True)
        tokens[name] = token or None
    waitForPackages(console, info)


def waitForPackages(console, info):
    missing = findMissingPackages(info)
    while missing:
        console.say(f"This model needs the packages {', '.join(missing)}. Install them in another terminal with: pip install -U {' '.join(missing)}")
        if console.ask("Press Enter when they are installed, or type skip to go on without them:").strip().lower() == "skip":
            return
        missing = findMissingPackages(info)


def prepareApi(console, info, keys, price=True):
    provider, details = info["provider"], API_KEYS[info["provider"]]
    if provider not in keys:
        if getApiKey(provider):
            console.say(f"Using the API key found in {details['variable']}.")
        else:
            console.say(f"{details['company']} needs an API key. Create one at {details['page']}. It is only kept in memory while this program runs. "
                        f"(Next time you can set {details['variable']} instead of typing it.)")
            keys[provider] = askUntilValid(console, f"Your {details['company']} API key (hidden):", lambda text: parseAnswer({"key": "key", "ask": "", "kind": "secret", "required": True}, text), secret=True)
    waitForPackages(console, info)
    if price and askYesNo(console, f"Do you want the price of {info['name']}?", True):
        console.say(describeCost(getModelCost(provider, info["name"])))


def testConnection(console, model):
    if not askYesNo(console, "Test the connection now with a tiny request? (it costs a fraction of a cent)", True):
        return
    try:
        console.say(f"The model answered: {shorten(model.input('Reply with the single word OK.'), 60)}")
    except ModelError as error:
        console.say(f"It did not work: {error}")


def chooseModel(console, swarm, spec, number, total, keys, tokens, replacing=None):
    task = spec["task"]
    status = checkVram(swarm.getNeededVram(replacing), readGpus())
    console.say(f"\n--- Model of agent {number} of {total}: {spec['name']} ({getTask(task)['role']}) ---\n{describeAgent(spec)}")
    if task == "leader":
        console.say("This leader builds the swarm, follows it, and proposes changes to you: it must plan well and follow a strict format, so a capable model is worth it.")
    elif number == 1:
        console.say("This agent is the leader: besides its own task, its model writes the summaries you approve and decides which agents your corrections concern. "
                    "A capable model is worth it here.")
    if status["needed"]:
        console.say(f"VRAM of the swarm so far: {status['needed']} GB of {status['total']} GB ({status['free']} GB free now).")
    while True:
        kind = chooseKind(console, swarm, number, replacing)
        info = chooseLocalModel(console, swarm, task, replacing) if kind == "local" else chooseApiModel(console, swarm, task, replacing) if kind == "api" else \
            chooseCodingAgent(console, swarm, replacing)
        if info is None:
            continue
        if info.get("cli"):
            console.say(f"\nYou chose {describeModel(info)}. It reads the files of its folder freely, and asks you before anything else (a command, a change of a "
                        "file, a web page, a file outside its folder).")
            if info["cli"] == "claude-code":
                prepareApi(console, info, keys, price=False)
            else:
                waitForPackages(console, info)
            return info, createModel(info, keys, report=console.say)
        check = swarm.checkModel(info, replacing)
        if info["local"]:
            console.say(f"\nYou chose {info['name']}: it is expected to need {info['vram']} GB of VRAM.\n" + describeGpus(checkVram(check["needed"], readGpus())))
            if check["message"]:
                console.say(f"Warning: {check['message']}")
                if not askYesNo(console, "You can choose it, but the swarm cannot run until the memory is free. Keep this model?", False):
                    continue
            prepareLocal(console, info, tokens)
            model = createModel(info, token=tokens.get(info["name"]), report=console.say)
        else:
            console.say(f"\nYou chose {info['name']} ({API_KEYS[info['provider']]['company']}).")
            if check["message"]:
                console.say(check["message"])
            prepareApi(console, info, keys)
            model = createModel(info, keys)
            testConnection(console, model)
        return info, model


# ==============
# The swarm: its agents, its order, and its run.
# ==============
# The leader tells the user to click on an agent, as a graphical interface does. In the command line the name is typed.
def forTerminal(message):
    return message.replace("by clicking on its name in the swarm", "by typing its name (type help to see how)")


def connect(loop, console):
    loop.notifyUser, loop.askUser, loop.askSecret = (lambda message: console.say(forTerminal(message))), console.ask, console.askSecret
    loop.askLogin = lambda host: console.say(f"{host} asks for an account. Add it in the setup of the literature reviewer next time. Skipping it.")


def buildAgent(console, swarm, spec, info, model, waitsFor=()):
    loop = buildLoop(spec["task"], model, spec["answers"])
    connect(loop, console)
    watchCosts(console, swarm, loop)
    recipe = {"task": spec["task"], "answers": publicAnswers(spec["task"], spec["answers"])}
    swarm.addAgent(spec["name"], loop, getTask(spec["task"])["role"], describeLoop(loop), waitsFor=waitsFor, model=info, recipe=recipe)
    return loop


# An agent that joins the swarm while it runs (the command add): its task, its folder, its model, and the agents it waits for (in execute mode).
# Every agent of the swarm is told. If it cannot join (the leader already started its final work), its model is let go.
def addLive(console, swarm, specs, keys, tokens, models):
    number = len(swarm.getAgents()) + 1
    spec = fillTask(console, chooseTask(console, number, number), swarm.getAgents())
    spec["answers"]["folder"] = askFolder(console, spec, number, number)
    info, model = chooseModel(console, swarm, spec, number, number, keys, tokens)
    others = [name for name in swarm.getAgents() if name != swarm.getLeader()]
    waits = []
    if swarm.getMode() == "execute" and others:
        waits = chooseFrom(console, f"\nWhich agents must {spec['name']} wait for? It receives their results.", others, one=False, none=True, default=[],
                           hint="Your choices (numbers like 1,3; Enter or none = nobody):")
    try:
        buildAgent(console, swarm, spec, info, model, waits)
    except ValueError as error:
        console.say(f"{spec['name']} cannot join: {error}")
        if hasattr(model, "unload"):
            model.unload()
        return
    specs.append(spec)
    models[spec["name"]] = model
    console.say(f"{spec['name']} joined the swarm. Every agent was told.")


# ==============
# The leader builds the swarm (leader_utils.py): the user gives the mission, the folder of the mission and the model of the leader, and approves
# the swarm the leader proposes. While the swarm runs, the leader can propose to add or remove agents, with a reason: the user approves each change.
# ==============
def chooseBuilder(console):
    console.say("You can build the swarm yourself, agent by agent. Or a leader agent builds it: you choose its model and the folder of the mission, it proposes "
                "the agents, their tasks and their models, and you approve. While the swarm works, it can also propose to add or remove agents, always with a "
                "reason, and nothing changes before you approve.")
    options = ["I build the swarm myself", "The leader builds the swarm (I approve its proposal)"]
    return "leader" if chooseFrom(console, "Who builds the swarm?", options) == options[1] else "manual"


# What the manager of the leader needs to make an agent here (see LeaderManager): the loops of the agents it adds, or of an agent with another model.
class ConsoleMaker:
    def __init__(self, console, swarm, specs, keys, tokens, models):
        self.console, self.swarm, self.specs, self.keys, self.tokens, self.models = console, swarm, specs, keys, tokens, models

    def makeLoop(self, agent):
        model = createModel(agent["model"], self.keys, token=self.tokens.get(agent["model"]["name"]), report=self.console.say)
        loop = buildLoop(agent["task"], model, agent["answers"])
        connect(loop, self.console)
        watchCosts(self.console, self.swarm, loop)
        return loop

    def joined(self, agent, loop):
        self.specs.append({"task": agent["task"], "name": agent["name"], "answers": agent["answers"]})
        self.models[agent["name"]] = loop.agent

    def remakeLoop(self, name, model):
        spec = next((spec for spec in self.specs if spec["name"] == name), None)
        if spec is None:
            raise ValueError(f"{name} cannot be made again here.")
        return self.makeLoop({"name": name, "task": spec["task"], "answers": spec["answers"], "model": model})

    def remade(self, name, model, loop):
        self.models[name] = loop.agent


# It returns the swarm the leader built and the user approved (the leader is its first agent), or None.
def buildWithLeader(console, mission, specs, keys, tokens, models, costs):
    heading(console, "Step 1: the leader and the folder of the mission")
    console.say(LEADER_TASK["info"])
    folder = askUntilValid(console, "Folder of the mission (the agents work in it, and the final report is saved in it):",
                           lambda text: parseAnswer({"key": "folder", "ask": "", "kind": "folder", "required": True}, text))
    leader = {"task": "leader", "name": LEADER_TASK["name"], "answers": {"mission": mission, "folder": folder, "numberOfLoops": DEFAULT_LOOPS}}
    swarm = Swarm(mission)
    swarm.costs = costs
    info, model = chooseModel(console, swarm, leader, 1, 1, keys, tokens)
    models[leader["name"]] = model
    costs.track(leader["name"], info, model)
    loop = buildLoop("leader", model, leader["answers"])
    connect(loop, console)
    watchCosts(console, swarm, loop)
    loop.name = leader["name"]
    catalog = LeaderCatalog(mission, folder, leader["name"], info, keys, tokens, costs)
    heading(console, "Step 2: the leader builds the swarm")
    console.say(f"{leader['name']} is building the swarm for your mission. It shows you its proposal, and nothing is made before you approve it.")
    try:
        agents = designSwarm(loop, catalog)
    except (ValueError, ModelError) as error:
        console.say(str(error))
        return None
    if agents is None:
        console.say("You rejected the swarm of the leader.")
        return None
    buildAgent(console, swarm, leader, info, model)
    specs.append(leader)
    for agent in agents:
        spec = {"task": agent["task"], "name": agent["name"], "answers": agent["answers"]}
        models[spec["name"]] = createModel(agent["model"], keys, token=tokens.get(agent["model"]["name"]), report=console.say)
        buildAgent(console, swarm, spec, agent["model"], models[spec["name"]])
        specs.append(spec)
    for agent in agents:
        swarm.setWaitsFor(agent["name"], agent["waitsFor"])
    LeaderManager(swarm, catalog, ConsoleMaker(console, swarm, specs, keys, tokens, models))
    return swarm


def chooseOrder(console, swarm):
    workers = [name for name in swarm.getAgents() if name != swarm.getLeader()]
    if not workers:
        console.say("There is only one agent, so nobody waits for anybody.")
        return
    options = ["All the agents work at the same time", "The leader decides who waits for whom (you approve its plan)", "I choose who waits for whom"]
    picked = chooseFrom(console, "\nWho waits for whom? An agent that waits starts when the agents it waits for are done, and receives their results.", options)
    if picked == options[0]:
        for name in workers:
            swarm.setWaitsFor(name, [])
    elif picked == options[1]:
        console.say("The leader is working out the order...")
        try:
            console.say("The order is approved." if swarm.planWithLeader() else "No order was approved, so nothing changed.")
        except ModelError as error:
            console.say(f"The leader could not decide: {error}")
    else:
        for name in workers:
            others = [other for other in workers if other != name]
            if others:
                chosen = chooseFrom(console, f"\nWhich agents must {name} wait for?", others, one=False, none=True, default=[], hint="Your choices (numbers like 1,3; Enter or none = nobody):")
                swarm.setWaitsFor(name, chosen)
    try:
        swarm.getStages()
    except ValueError as error:
        console.say(f"{error} Everybody works at the same time instead.")
        for name in workers:
            swarm.setWaitsFor(name, [])
    console.say("\n" + renderTree(swarm, console.color))


def chooseMode(console):
    options = ["Plan first: every agent writes its plan, the leader summarises them, and you approve (recommended)", "Execute right away: every agent drafts its work, and you approve before it acts"]
    console.say("\nNothing is ever done without your approval: emails are only sent, events booked and files written after you approve the exact result.")
    return "plan" if chooseFrom(console, "How must the swarm start?", options) == options[0] else "execute"


def showUsage(console, models):
    lines = [f"  {name}: {model.usage['calls']} calls, {model.usage['input']:,} tokens read, {model.usage['output']:,} tokens written" for name, model in models.items() if hasattr(model, "usage")]
    if lines:
        console.say("\nTokens used (API models are billed for them):\n" + "\n".join(lines))


def report(console, swarm, outcome, models):
    heading(console, "Result")
    if "error" in outcome:
        console.say(f"The swarm could not run: {outcome['error']}")
        return False
    for name in swarm.getAgents():
        info = swarm.getInfo(name)
        detail = f"approved plan: {shorten(info['plan'], 300)}" if swarm.getMode() == "plan" and info["plan"] else f"result: {shorten(info['result'], 300)}" if info["result"] else f"problem: {info['error']}"
        console.say(f"- {name}: {info['status']}. {detail}")
    if swarm.getMode() == "plan" and len(swarm.getAgents()) > 1 and swarm.getSummary():
        console.say(f"\nThe summary plan of the leader:\n{swarm.getSummary()}")
    showUsage(console, models)
    console.say("\n" + describeCosts(swarm))
    return outcome.get("result") is not None


def runSwarm(console, swarm, resume=False):
    view = TreeView(swarm, console)
    console.commands = lambda line: runCommand(console, swarm, line)
    console.pumping, console.closed = console.interactive, False
    pump = threading.Thread(target=console.pump, daemon=True)
    console.say(f"\nThe swarm {'goes on' if resume else 'starts'}. {'Type help to see what you can do while it runs.' if console.interactive else ''}\n")
    view.start()
    if console.interactive:
        pump.start()
    swarm.startInBackground(resume)
    try:
        swarm.wait()
    except KeyboardInterrupt:
        swarm.saveForExit()
        raise
    view.stop()
    if console.interactive:
        console.say("Press Enter to continue.")
        console.pumping = False
        pump.join()
    console.pumping = False
    return swarm.outcome


# Runs the swarm in its mode. A plan that was approved can be executed right away. It returns the outcome of the last run.
# With resume the first run goes on where the swarm was interrupted.
def startSwarm(console, swarm, mode, models, resume=False):
    while True:
        swarm.setMode(mode)
        outcome = runSwarm(console, swarm, resume)
        resume = False
        succeeded = report(console, swarm, outcome, models)
        if mode == "plan" and succeeded and askYesNo(console, "\nThe plans are approved. Execute them now?", True):
            mode = "execute"
            continue
        return outcome


def changeModel(console, swarm, specs, models, keys, tokens):
    name = chooseFrom(console, "\nThe model of which agent do you want to change?", swarm.getAgents())
    spec, number = next(spec for spec in specs if spec["name"] == name), swarm.getAgents().index(name) + 1
    taken = swarm.getNeededVram(replacing=name)
    console.say(f"Without {name}, the swarm needs {taken} GB of VRAM.")
    info, model = chooseModel(console, swarm, spec, number, len(specs), keys, tokens, replacing=name)
    loop = buildLoop(spec["task"], model, spec["answers"])
    connect(loop, console)
    try:
        swarm.setModel(name, info, agent=loop)
    except ValueError as error:
        console.say(str(error))
        return
    old = models.get(name)
    if hasattr(old, "unload"):
        old.unload()
    models[name] = model


# ==============
# A swarm that was interrupted (the connection was lost, or the program or the computer stopped) is found again from its saved state.
# The user continues it where it stopped, or cancels it after reading what the leader says it did, or leaves it for later.
# ==============
def unloadModels(models):
    for model in models.values():
        if hasattr(model, "unload"):
            model.unload()


def describeInterrupted(saved):
    reason = "The connection was lost" if saved["state"] == "paused" else "The program or the computer stopped"
    agents = [f"  - {name} ({member['role']}): {member['status']}" for name, member in saved["members"].items()]
    return "\n".join([f"The swarm \"{shorten(saved['mission'], 70)}\" was interrupted at {saved['savedAt']}. {reason}, and your work is saved.", *agents])


# The model of an agent of a swarm that is brought back. The API keys and the Hugging Face tokens are never saved, so they are asked again.
def rebuildModel(console, info, keys, tokens):
    if info.get("cli") == "codex":
        if not signInCodex(console):
            raise ModelError("Codex is not signed in, so this agent cannot continue.")
        return createModel(info, keys, report=console.say)
    if info["local"]:
        prepareLocal(console, info, tokens)
        return createModel(info, token=tokens.get(info["name"]), report=console.say)
    prepareApi(console, info, keys, price=False)
    return createModel(info, keys)


def rebuildAgent(console, name, data, keys, tokens, models, specs=None):
    recipe = data["recipe"]
    task, secrets = recipe["task"], {}
    console.say(f"\n--- {name} ({getTask(task)['role']}) ---")
    for field in secretFields(task, recipe["answers"]):
        secrets[field["key"]] = askField(console, field, recipe["answers"])
    if task == "literature":
        secrets["accounts"] = askAccounts(console)
    models[name] = rebuildModel(console, data["model"], keys, tokens) if data["model"] else None
    answers = restoreAnswers(task, recipe["answers"], secrets)
    loop = buildLoop(task, models[name], answers)
    connect(loop, console)
    if specs is not None:
        specs.append({"task": task, "name": name, "answers": answers})
    return loop


def resumeSaved(console, saved):
    keys, tokens, models, specs = {}, {}, {}, []
    if not all(member.get("recipe") for member in saved["members"].values()):
        console.say("This swarm was made by another program, so it cannot be continued here.")
        return False
    heading(console, "Continuing your swarm")
    try:
        swarm = Swarm.restore(saved, lambda name, data: rebuildAgent(console, name, data, keys, tokens, models, specs))
    except (ValueError, ModelError) as error:
        console.say(f"The swarm cannot be continued yet: {error} It stays saved, so you can try again.")
        unloadModels(models)
        return False
    console.say("\n" + renderTree(swarm, console.color))
    for name in swarm.getAgents():
        watchCosts(console, swarm, swarm.getMember(name)["agent"])
    leader = saved["members"][saved["leader"]]
    if saved.get("managed") and leader["recipe"]["task"] == "leader":
        catalog = LeaderCatalog(saved["mission"], leader["recipe"]["answers"].get("folder"), saved["leader"], leader["model"], keys, tokens, swarm.costs)
        LeaderManager(swarm, catalog, ConsoleMaker(console, swarm, specs, keys, tokens, models))
    console.newAgent = lambda swarm: addLive(console, swarm, specs, keys, tokens, models)
    startSwarm(console, swarm, swarm.getMode(), models, resume=True)
    unloadModels(models)
    return True


# The leader writes the summary with its model if that can be made again (an API key is asked for it, and can be skipped).
def makeLeaderModel(console, info, keys, tokens):
    if not info:
        return None
    if info.get("cli") == "codex":
        try:
            if not readCodexAccount()["signedIn"]:
                return None
        except ModelError:
            return None
    if not info["local"] and info["provider"] and not (keys.get(info["provider"]) or getApiKey(info["provider"])):
        console.say(f"The leader writes the summary with {info['name']}, which needs its API key again. Press Enter to skip it and get a plain list instead.")
        key = askUntilValid(console, f"Your {API_KEYS[info['provider']]['company']} API key (hidden, Enter to skip):", lambda text: (text.strip(), ""), secret=True)
        if not key:
            return None
        keys[info["provider"]] = key
    try:
        return rebuildModel(console, info, keys, tokens)
    except ModelError as error:
        console.say(str(error))
        return None


# An agent of a swarm that is cancelled: it only needs to remember what it did and to undo what it left half done, so it needs no model.
def rebuildForCancel(console, name, data):
    recipe = data.get("recipe") or {}
    try:
        loop = buildLoop(recipe["task"], None, {**restoreAnswers(recipe["task"], recipe["answers"]), "folder": None})
    except (KeyError, ValueError, OSError):
        loop = Loop(None)
    connect(loop, console)
    return loop


# It returns True if the swarm was continued after all.
def cancelSaved(console, saved):
    keys, tokens = {}, {}
    swarm = Swarm.restore(saved, lambda name, data: rebuildForCancel(console, name, data))
    leader = swarm.getMember(swarm.getLeader())["agent"]
    leader.agent = makeLeaderModel(console, saved["members"][saved["leader"]]["model"], keys, tokens)
    console.say(f"\n[{swarm.getLeader()}] Summary of what the swarm did so far:\n{swarm.summarizeChanges()}")
    console.say("If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task.")
    if chooseFrom(console, "\nWhat now?", ["Stop here", "Continue it until the end"]) == "Stop here":
        swarm.abandon()
        console.say("The swarm is stopped. What the agents that did not finish had changed in your files was put back.")
        return False
    return resumeSaved(console, saved)


# It returns True if a swarm was continued, which is all the program has to do then.
def offerUnfinished(console):
    for saved in findUnfinishedSwarms():
        if saved["running"]:
            console.say(f"\nThe swarm \"{shorten(saved['mission'], 70)}\" seems to be working in another window of the program, so it is left alone. "
                        "If that window was closed a few seconds ago, start this program again in half a minute.")
            continue
        heading(console, "A swarm was interrupted")
        console.say(describeInterrupted(saved))
        options = ["Continue it where it stopped", "Cancel it (you will see what it did, and confirm)", "Leave it for later and go on"]
        picked = chooseFrom(console, "\nWhat do you want to do?", options)
        if (picked == options[0] and resumeSaved(console, saved)) or (picked == options[1] and cancelSaved(console, saved)):
            return True
    return False


def runProgram(console):
    heading(console, "SwarmUP: build and run a swarm of agents")
    if offerUnfinished(console):
        console.say("\nBye.")
        return
    specs, keys, tokens, models = [], {}, {}, {}
    if chooseBuilder(console) == "leader":
        mission, costs = askMission(console), MissionCosts()
        askBudget(console, costs)
        swarm = buildWithLeader(console, mission, specs, keys, tokens, models, costs)
        if swarm is None:
            unloadModels(models)
            console.say("\nBye.")
            return
    else:
        count = askAgentCount(console)
        swarm = Swarm(askMission(console))
        askBudget(console, swarm.costs)
        heading(console, "Step 1: the task of each agent")
        for number in range(1, count + 1):
            specs.append(fillTask(console, chooseTask(console, number, count), [spec["name"] for spec in specs]))
        heading(console, "Step 2: the folder of each agent")
        chooseFolders(console, specs)
        heading(console, "Step 3: the model of each agent")
        for number, spec in enumerate(specs, 1):
            info, model = chooseModel(console, swarm, spec, number, count, keys, tokens)
            buildAgent(console, swarm, spec, info, model)
            models[spec["name"]] = model
            status = swarm.getVramStatus()
            if status["needed"]:
                console.say(f"\nVRAM the swarm is expected to need so far: {status['needed']} GB of {status['total']} GB ({status['free']} GB free now)." + (f"\n{status['message']}" if status["message"] else ""))
    console.newAgent = lambda swarm: addLive(console, swarm, specs, keys, tokens, models)
    heading(console, "Your swarm")
    console.say(f"The first agent, {swarm.getLeader()}, is the leader: it works last, and it speaks to you for the swarm.\n\n" + renderTree(swarm, console.color))
    mode = chooseMode(console)
    while True:
        options = ["Start the swarm", "Change the model of an agent (for example to a smaller one)", "Change who waits for whom", "Show the tree again", "Quit"]
        choice = chooseFrom(console, "\nWhat now?", options)
        if choice == options[1]:
            changeModel(console, swarm, specs, models, keys, tokens)
        elif choice == options[2]:
            chooseOrder(console, swarm)
        elif choice == options[3]:
            console.say(renderTree(swarm, console.color))
        elif choice == options[4]:
            break
        else:
            outcome = startSwarm(console, swarm, mode, models)
            if "error" in outcome:
                console.say("Change a model or free some GPU memory, then start again.")
                continue
            break
    unloadModels(models)
    console.say("\nBye.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    console = Console()
    keepFresh()
    try:
        runProgram(console)
    except (KeyboardInterrupt, EOFError):
        console.say("\nStopped by the user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
