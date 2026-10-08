# The daemon side of a command line session (swarmup_cli.py). A session runs the program of the command line (runProgram) away from any terminal,
# so it goes on when the terminal is closed, and terminals attach to it and leave it as they do with tmux. The session has windows: the swarm
# (window 0: the tree, the events, the summaries of the leader and every question), and one window for each agent (what it does with its tools,
# what it says, its messages, its drafts and its result). The hub keeps the windows, routes what the user types, and serves the terminals
# (cli_screen.py) on a socket of this computer only (127.0.0.1), which needs the token of the session.
# Messages are JSON, one per line. A terminal first sends {"token", "type"}: attach (then input, kill or detach), status, capture, input (one line
# typed in a window, without attaching, for scripts) or kill.
# The hub sends hello (everything a terminal shows), lines, panel, windows, question, state and bye.
import hmac
import json
import os
import queue
import socketserver
import threading
import traceback
from collections import deque

from cli_console import META_WORDS, Console
from cli_view import formatEvent, openAgent, renderTree
from mission_costs import formatDollars


SWARM_WINDOW = "swarm"
SCROLLBACK = 5000
HELLO_LINES = 2000
CAPTURE_LINES = 200
CLIENT_BACKLOG = 20000
REQUEST_BYTES = 1000000
FLUSH_SECONDS = 0.5
# What the user types in the window of an agent is about that agent: "approve" approves it, "correct shorter" corrects it, and any other text is a message to it.
AGENT_WORDS = ("approve", "reject", "start")
AGENT_TEXT_WORDS = ("correct", "remove", "msg")
SWARM_WORDS = ("help", "?", "tree", "log", "cost", "add", "stop", "kill", "quit")
AGENT_EVENTS = ("activity", "status", "review", "scheduled", "model", "connectionLost", "joined", "removed")


class Window:
    def __init__(self, name):
        self.name, self.lines, self.panel, self.left = name, deque(maxlen=SCROLLBACK), "", False

    def describe(self, lines=HELLO_LINES):
        return {"name": self.name, "panel": self.panel, "left": self.left, "lines": list(self.lines)[-lines:]}


# An attached terminal. What it must receive goes through a queue and a thread of its own, so a slow terminal never holds up the swarm.
class Client:
    def __init__(self, stream):
        self.stream, self.outbox, self.alive = stream, queue.Queue(), True
        self.thread = threading.Thread(target=self.write, daemon=True)
        self.thread.start()

    def send(self, message):
        if not self.alive:
            return
        if self.outbox.qsize() > CLIENT_BACKLOG:
            self.close()
            return
        self.outbox.put(message)

    def write(self):
        while True:
            message = self.outbox.get()
            if message is None:
                return
            try:
                self.stream.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                self.stream.flush()
            except (OSError, ValueError):
                self.alive = False
                return

    def close(self):
        self.alive = False
        self.outbox.put(None)


class Hub:
    def __init__(self, sessionId, token):
        self.id, self.token = sessionId, token
        self.windows = {SWARM_WINDOW: Window(SWARM_WINDOW)}
        self.lock = threading.RLock()
        self.clients = []
        self.question, self.questionNumber = None, 0
        # While the swarm runs, the commands of the user (runCommand) run one after the other in a thread of their own.
        self.commands, self.work = None, queue.Queue()
        self.state, self.swarm, self.ending = "building", None, False
        # onChange writes the file of the session, onEnd stops the server and removes the file. exit ends the process.
        self.onChange, self.onEnd, self.exit = None, None, os._exit
        self.lastSummary = None
        threading.Thread(target=self.runCommands, daemon=True).start()

    # ---------- The windows. Must be called with self.lock held. ----------
    def window(self, name):
        name = name or SWARM_WINDOW
        if name not in self.windows:
            self.windows[name] = Window(name)
            self.broadcast(self.describeWindows())
        return self.windows[name]

    def describeWindows(self):
        return {"type": "windows", "windows": [{"name": window.name, "left": window.left} for window in self.windows.values()]}

    def append(self, name, lines):
        window = self.window(name)
        window.lines.extend(lines)
        self.broadcast({"type": "lines", "window": window.name, "lines": lines})

    def setPanel(self, name, text):
        window = self.window(name)
        if window.panel != text:
            window.panel = text
            self.broadcast({"type": "panel", "window": window.name, "text": text})

    def leader(self):
        return self.swarm.getLeader() if self.swarm else None

    # ---------- What the program says and asks. ----------
    # The leader speaks for the swarm, so what it says is also shown in window 0.
    def write(self, name, text):
        lines = str(text).split("\n")
        with self.lock:
            self.append(name, lines)
            if name and name != SWARM_WINDOW and name == self.leader():
                self.append(SWARM_WINDOW, lines if lines[0].startswith(f"[{name}]") else [f"[{name}] {lines[0]}", *lines[1:]])

    # Only one question waits at a time (the console asks them one after the other). It is shown in its window and in window 0, and it is
    # answered from either of them.
    def waitForAnswer(self, name, text, secret=False):
        with self.lock:
            window = self.window(name)
            self.questionNumber += 1
            question = {"id": self.questionNumber, "window": window.name, "text": text, "secret": secret, "event": threading.Event(), "answer": ""}
            self.question = question
            self.append(window.name, [f"? {text}"])
            if window.name != SWARM_WINDOW:
                self.append(SWARM_WINDOW, [f"? [{window.name}] {text}"])
            self.broadcast(self.describeQuestion())
        self.changed()
        question["event"].wait()
        with self.lock:
            if self.question is question:
                self.question = None
            self.append(window.name, [f"> {'*' * len(question['answer']) if secret else question['answer']}"])
            self.broadcast(self.describeQuestion())
        self.changed()
        return question["answer"]

    def describeQuestion(self):
        question = self.question
        return {"type": "question", "question": {key: question[key] for key in ("id", "window", "text", "secret")} if question else None}

    # ---------- What the user types. ----------
    # The text answers the question that waits when it is typed in window 0 or in the window of the question. In the window of an agent it is
    # about that agent, and it never answers the question of another window. It gives the command to run, or None for an answer.
    def route(self, name, text):
        words = text.strip().split(maxsplit=1)
        word, rest = (words[0].lower() if words else ""), (words[1] if len(words) > 1 else "")
        if text.strip().lower() in META_WORDS:
            return text.strip()
        if name == SWARM_WINDOW or name not in self.windows or (self.question and self.question["window"] == name):
            return None if self.question else text
        if word in AGENT_WORDS:
            return f"{word} {name}"
        if word in ("show", ""):
            return name
        if word in AGENT_TEXT_WORDS:
            return f"{word} {name} {rest}".strip()
        if word in SWARM_WORDS:
            return text.strip()
        return f"msg {name} {text.strip()}"

    def input(self, name, text):
        text = str(text)
        with self.lock:
            name = name if name in self.windows else SWARM_WINDOW
            command, question = self.route(name, text), self.question
            if command is None and question is not None:
                question["answer"] = text
                question["event"].set()
                return
        if command is not None and command.strip():
            self.work.put((name, command))

    def runCommands(self):
        while True:
            name, command = self.work.get()
            if command.strip().lower() in ("kill", "quit"):
                self.kill()
                continue
            handler = self.commands
            if handler is None:
                self.write(name, "Nothing waits for an answer here, and the swarm is not running yet. Answer the question in window 0, or type :help.")
                continue
            try:
                handler(name, command)
            except Exception as error:
                traceback.print_exc()
                self.write(name, f"This command failed: {error}")

    # ---------- The swarm. ----------
    def startRun(self, handler):
        self.commands, self.state = handler, "running"
        self.changed()

    def stopRun(self):
        self.commands, self.state = None, "ready"
        self.changed()

    def finish(self):
        self.commands, self.state = None, "finished"
        self.write(SWARM_WINDOW, "\nThe program has ended. Read what you need, then close the session with Ctrl-b x (or :kill). The work of the agents is saved.")
        self.changed()

    # The events of the swarm (TreeView in cli_view.py): window 0 gets them all, and each agent the events about itself. What is read of the
    # swarm is read before the lock of the hub is taken, so an agent that speaks while the swarm is busy never waits for this.
    def follow(self, swarm, events):
        names = swarm.getAgents()
        lines = [line for line in (formatEvent(event) for event in events) if line]
        perAgent = [item for event in events for item in self.readEvent(swarm, event, names)]
        panels = [(SWARM_WINDOW, f"{renderTree(swarm)}\n{self.describeCost(swarm)}")] + [(name, self.describeAgent(swarm, name)) for name in names]
        removed = {item["name"] for item in swarm.removed} - set(names)
        with self.lock:
            self.swarm = swarm
            for name in names:
                self.window(name)
            for name in removed & set(self.windows):
                if not self.windows[name].left:
                    self.windows[name].left = True
                    self.broadcast(self.describeWindows())
            if lines:
                self.append(SWARM_WINDOW, lines)
            for name, added in perAgent:
                self.append(name, added)
            for name, panel in panels:
                self.setPanel(name, panel)
        self.changed()

    # The lines an event adds to the windows of the agents: [(window, lines)].
    def readEvent(self, swarm, event, names):
        kind, agent, line = event["kind"], event.get("agent"), formatEvent(event)
        if kind == "message":
            return [(name, [line]) for name in (event["sender"], event["receiver"]) if name in names]
        if kind not in AGENT_EVENTS or agent not in names:
            return []
        added = [line] if line else []
        info = swarm.getInfo(agent)
        if kind == "review" and event.get("review") == "ready":
            added += openAgent(swarm, agent).split("\n")
        elif kind == "status" and event.get("status") == "done" and info["result"] is not None:
            added += ["Its result:", *str(info["result"]).split("\n")]
        elif kind == "status" and event.get("status") == "failed" and info["error"]:
            added.append(f"It did not finish: {info['error']}")
        return [(agent, added)] if added else []

    def describeAgent(self, swarm, name):
        info = swarm.getInfo(name)
        spent = next((row for row in swarm.getCosts()["agents"] if row["agent"] == name), None)
        state = f"{info['status']}" + (f", its draft waits for you" if info["review"] == "ready" else "") + (f", starts at {info['startAt']}" if info["startAt"] else "")
        lines = [f"{name}{' (the leader)' if info['isLeader'] else ''}: the {info['role']}, {state}",
                 f"Task: {info['task']}"]
        if info["waitingOn"]:
            lines.append(f"Waits for: {', '.join(info['waitingOn'])}")
        if spent:
            lines.append(f"Spent: {formatDollars(spent['cost'])} in {spent['calls']} calls")
        return "\n".join(lines)

    def describeCost(self, swarm):
        report = swarm.getCosts()
        budget = f" of {formatDollars(report['budget'])}" if report.get("budget") is not None else ""
        return f"Spent: {formatDollars(report['spent'])}{budget}"

    # ---------- The terminals. ----------
    def broadcast(self, message):
        for client in list(self.clients):
            client.send(message)
            if not client.alive:
                self.clients.remove(client)

    def attach(self, client):
        with self.lock:
            self.clients.append(client)
            client.send({"type": "hello", "session": self.id, "windows": [window.describe() for window in self.windows.values()],
                         "question": self.describeQuestion()["question"], "state": self.describeState()})

    def detach(self, client):
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
        client.close()

    def describeState(self):
        swarm = self.swarm
        return {"state": self.state, "mission": swarm.mission if swarm else "", "missionId": swarm.id if swarm else None, "cost": self.describeCost(swarm) if swarm else "",
                "agents": swarm.getAgents() if swarm else [], "waiting": self.question["window"] if self.question else None}

    # The session file and the terminals learn the new state, only when it changed.
    def changed(self):
        with self.lock:
            summary = self.describeState()
            if summary == self.lastSummary:
                return
            self.lastSummary = summary
            self.broadcast({"type": "state", **summary})
        if self.onChange:
            try:
                self.onChange(summary)
            except OSError:
                traceback.print_exc()

    def capture(self, name, count=CAPTURE_LINES):
        with self.lock:
            window = self.windows.get(name or SWARM_WINDOW) or next((window for window in self.windows.values() if window.name.lower() == str(name).lower()), None)
            if window is None:
                return None
            return "\n".join(([window.panel, ""] if window.panel else []) + list(window.lines)[-count:])

    def describeStatus(self):
        with self.lock:
            state = self.describeState()
            question = self.describeQuestion()["question"]
            return {"type": "status", "id": self.id, "session": self.id, **state, "question": question, "tree": self.windows[SWARM_WINDOW].panel,
                    "windows": [window.name for window in self.windows.values()]}

    # Ends the session: a swarm that runs is saved so that it can be continued, the terminals are told, and the process ends.
    def end(self, message):
        with self.lock:
            if self.ending:
                return
            self.ending = True
        try:
            if self.swarm is not None and self.swarm.isRunning():
                self.swarm.saveForExit()
        except Exception:
            traceback.print_exc()
        with self.lock:
            question = self.question
            if question:
                question["event"].set()
            self.broadcast({"type": "bye", "message": message})
            clients, self.clients = list(self.clients), []
        for client in clients:
            client.close()
            client.thread.join(timeout=FLUSH_SECONDS)
        if self.onEnd:
            self.onEnd()
        self.exit(0)

    def kill(self):
        saved = self.swarm is not None and self.swarm.isRunning()
        self.end("The session was closed." + (" Its swarm is saved: start SwarmUP again to continue it." if saved else ""))


# The console of a session: it speaks and asks through the hub instead of a terminal. What a command says goes to the window it was typed in.
class HubConsole(Console):
    def __init__(self, hub):
        super().__init__(read=lambda prompt: self.ask(prompt.strip()), readSecret=lambda prompt: self.askSecret(prompt.strip()),
                         write=lambda text: hub.write(SWARM_WINDOW, text), interactive=True, treeDelay=0.3)
        self.hub, self.color, self.local = hub, False, threading.local()

    def target(self, window):
        return window or getattr(self.local, "window", None) or SWARM_WINDOW

    def say(self, text="", window=None):
        self.hub.write(self.target(window), text)

    def ask(self, question, window=None):
        return self.waitFor(question, window, False)

    # A secret can be typed at any time: the terminal hides what is typed.
    def askSecret(self, question, window=None):
        return self.waitFor(question, window, True)

    def waitFor(self, question, window, secret):
        target = self.target(window)
        with self.questions:
            if self.closed:
                return ""
            return self.hub.waitForAnswer(target, question.strip(), secret)

    def startPump(self):
        self.pumping, self.closed = True, False
        self.hub.startRun(self.runCommand)

    def stopPump(self):
        self.pumping = False
        self.hub.stopRun()

    def runCommand(self, window, line):
        self.local.window = window
        try:
            if self.commands:
                self.commands(line)
        finally:
            self.local.window = None


class SessionServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class SessionHandler(socketserver.StreamRequestHandler):
    def reply(self, message):
        self.wfile.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def handle(self):
        hub = self.server.hub
        try:
            request = json.loads(self.rfile.readline(REQUEST_BYTES) or b"{}")
        except ValueError:
            return
        if not isinstance(request, dict) or not hmac.compare_digest(str(request.get("token", "")), hub.token):
            return
        kind = request.get("type")
        if kind == "status":
            self.reply(hub.describeStatus())
        elif kind == "capture":
            self.reply({"type": "capture", "text": hub.capture(request.get("window"), int(request.get("lines") or CAPTURE_LINES))})
        elif kind == "input":
            hub.input(request.get("window"), request.get("text", ""))
            self.reply({"type": "ok"})
        elif kind == "kill":
            self.reply({"type": "ok"})
            hub.kill()
        elif kind == "attach":
            self.attach(hub)

    def attach(self, hub):
        client = Client(self.wfile)
        hub.attach(client)
        try:
            for line in self.rfile:
                message = json.loads(line)
                if message.get("type") == "input":
                    hub.input(message.get("window"), message.get("text", ""))
                elif message.get("type") == "kill":
                    hub.kill()
                elif message.get("type") == "detach":
                    break
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            hub.detach(client)


def startServer(hub):
    server = SessionServer(("127.0.0.1", 0), SessionHandler)
    server.hub = hub
    threading.Thread(target=server.serve_forever, daemon=True, name="session-server").start()
    return server
