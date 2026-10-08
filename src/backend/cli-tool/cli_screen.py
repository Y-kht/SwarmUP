# The terminal side of a command line session: it attaches to the daemon of a session (cli_hub.py) and shows its windows as tmux does. Window 0 is
# the swarm, then one window for each agent. The window shows its panel at the top (the tree, or the state of the agent), what happened below it,
# the question that waits for the user, the line where the user types, and the status bar with every window:
#   [SwarmUP s1] 0:swarm* 1:Leader 2:Writer! 3:Checker+      Spent: $0.42 of $5.00 | running
# * is the window on the screen, ! a window whose question waits for the user, + a window with something new, - an agent that left.
# The keys follow tmux: a prefix (Ctrl-b), then a number, n, p, w, ', d, x or ?. Commands can also be typed with a colon (:w 2, :detach, :kill).
# layoutScreen and handleKey are pure, so the screen can be checked without a terminal. Without curses (or with --plain) the session is shown
# as lines, one window or all of them.
import getpass
import json
import os
import queue
import socket
import sys
import textwrap
import threading

SWARM_WINDOW = "swarm"
SCROLLBACK = 5000
PANEL_SHARE = 0.4
SCROLL_STEP = 10
PLAIN_HELLO_LINES = 40
HELP = """SwarmUP sessions work like tmux. Press the prefix ({prefix}), then:
  0-9   go to a window            n / p   the next or the previous window
  w     choose a window           '       go to a window by its name
  d     detach: the session goes on without this terminal
  x     close the session (a swarm that runs is saved, and can be continued)
  ?     this help
In window 0 you answer the questions and type the commands of the swarm (help lists them). In the window of an agent, what you type is
about that agent: approve, reject, correct <what to change>, start, remove <why>, or any text, which is sent to it as a message.
PgUp / PgDn scroll, Up / Down bring back what you typed, Esc clears the line.
Typed commands: :w <number or name>, :next, :prev, :detach, :kill, :help.
Press any key to go back."""


def parsePrefix(text):
    text = (text or "C-b").strip().lower()
    if text.startswith(("c-", "ctrl-", "^")) and text[-1].isalpha():
        return chr(ord(text[-1]) - ord("a") + 1)
    raise ValueError(f"{text} is not a prefix key. Write it like C-b or C-a.")


def describePrefix(prefix):
    return f"Ctrl-{chr(ord(prefix) + ord('a') - 1)}"


# ==============
# What a terminal knows of the session, kept up to date by the messages of the daemon.
# ==============
class ScreenState:
    def __init__(self, prefix="\x02"):
        self.prefix, self.session, self.windows, self.lines, self.panels = prefix, "", [SWARM_WINDOW], {SWARM_WINDOW: []}, {}
        self.left, self.unread, self.question, self.status = set(), set(), None, {}
        self.active, self.scroll, self.text, self.cursor = SWARM_WINDOW, {}, "", 0
        self.history, self.recalled, self.mode, self.choice, self.notice, self.ended = [], None, "normal", 0, "", None

    def apply(self, message):
        kind = message.get("type")
        if kind == "hello":
            self.session = message["session"]
            self.windows = [window["name"] for window in message["windows"]]
            self.lines = {window["name"]: list(window["lines"]) for window in message["windows"]}
            self.panels = {window["name"]: window["panel"] for window in message["windows"]}
            self.left = {window["name"] for window in message["windows"] if window["left"]}
            self.question, self.status = message.get("question"), message.get("state") or {}
        elif kind == "lines":
            lines = self.lines.setdefault(message["window"], [])
            lines.extend(message["lines"])
            del lines[:-SCROLLBACK]
            if message["window"] != self.active:
                self.unread.add(message["window"])
        elif kind == "panel":
            self.panels[message["window"]] = message["text"]
        elif kind == "windows":
            self.windows = [window["name"] for window in message["windows"]]
            self.left = {window["name"] for window in message["windows"] if window["left"]}
        elif kind == "question":
            self.question = message["question"]
        elif kind == "state":
            self.status = {key: value for key, value in message.items() if key != "type"}
        elif kind in ("bye", "lost"):
            self.ended = message.get("message") or "The connection to the session was lost."

    def switch(self, name):
        if name in self.windows:
            self.active = name
            self.unread.discard(name)

    def find(self, text):
        text = text.strip().lower()
        if not text:
            return None
        if text.isdigit():
            return self.windows[int(text)] if int(text) < len(self.windows) else None
        exact = [name for name in self.windows if name.lower() == text]
        starting = [name for name in self.windows if name.lower().startswith(text)]
        return (exact or starting or [None])[0]

    def move(self, step):
        self.switch(self.windows[(self.windows.index(self.active) + step) % len(self.windows)])

    # The question waits in its window, and window 0 can answer every question.
    def questionHere(self):
        question = self.question
        return question if question and self.active in (question["window"], SWARM_WINDOW) else None


# ==============
# The screen: rows of (text, style). Styles are panel, rule, line, question, input, status and menu.
# ==============
# A line cut at the spaces to fit the width (a word longer than the width is cut where it must be).
def wrap(text, width):
    if width <= 0:
        return []
    return textwrap.wrap(text.expandtabs(4), width, break_on_hyphens=False, drop_whitespace=True) or [""]


def describeStatusBar(state, width):
    names = []
    for number, name in enumerate(state.windows):
        flag = "*" if name == state.active else "!" if state.question and state.question["window"] == name else "-" if name in state.left else "+" if name in state.unread else ""
        names.append(f"{number}:{name}{flag}")
    waiting = "waiting for you" if state.question else state.status.get("state", "")
    right = " | ".join(part for part in (state.status.get("cost"), waiting) if part)
    left = f"[SwarmUP {state.session}] " + " ".join(names)
    if state.notice:
        right = state.notice
    space = width - len(left) - len(right)
    return (left + " " * max(space, 1) + right)[:width] if space > 0 else (left[:max(width - len(right) - 1, 0)] + " " + right)[:width]


# It gives the rows of the screen, and where the cursor goes (row, column).
def layoutScreen(state, width, height):
    rows = []
    if state.mode == "help":
        rows = [(line, "menu") for text in HELP.format(prefix=describePrefix(state.prefix)).split("\n") for line in wrap(text, width)]
        return (rows + [("", "line")] * height)[:height - 1] + [(describeStatusBar(state, width), "status")], (height - 1, 0)
    panel = [line for text in (state.panels.get(state.active) or "").split("\n") for line in wrap(text, width)] if state.panels.get(state.active) else []
    panel = panel[:int(height * PANEL_SHARE)]
    if panel:
        rows += [(line, "panel") for line in panel] + [("─" * width, "rule")]
    question = state.questionHere()
    asked = []
    lastLine = (state.lines.get(state.active) or [""])[-1]
    if question and lastLine not in (f"? {question['text']}", f"? [{question['window']}] {question['text']}"):
        where = f"[{question['window']}] " if question["window"] != state.active else ""
        asked = [(line, "question") for line in wrap(f"? {where}{question['text']}", width)][-3:]
    room = max(height - len(rows) - len(asked) - 2, 0)
    if state.mode == "choose":
        feed = [(f"{'>' if number == state.choice else ' '} {number}: {name}" + (" (it waits for you)" if state.question and state.question["window"] == name else "")
                 + (" (left)" if name in state.left else ""), "menu") for number, name in enumerate(state.windows)]
        feed = [("Choose a window (a number, or the arrows and Enter; Esc goes back):", "menu")] + feed
    else:
        wrapped = [line for text in state.lines.get(state.active, []) for line in wrap(text, width)]
        offset = min(state.scroll.get(state.active, 0), max(len(wrapped) - room, 0))
        state.scroll[state.active] = offset
        feed = [(line, "line") for line in wrapped[max(len(wrapped) - room - offset, 0):len(wrapped) - offset]]
    rows += (feed + [("", "line")] * room)[:room] + asked
    label = {"name": "window: ", "confirm": "Close the session? A swarm that runs is saved. (y/n) "}.get(state.mode, "> ")
    secret = question is not None and question["secret"] and state.mode == "normal" and not state.text.startswith(":")
    typed = "*" * len(state.text) if secret else state.text
    start = max(len(label) + state.cursor - width + 1, 0)
    rows.append(((label + typed)[start:start + width], "input"))
    rows.append((describeStatusBar(state, width), "status"))
    return rows, (len(rows) - 2, len(label) + state.cursor - start)


# ==============
# The keys. A key is a character, or the name of a special key: UP, DOWN, LEFT, RIGHT, PGUP, PGDN, HOME, END, BACKSPACE, DELETE, ENTER, ESC.
# It gives what the terminal must do: None, ("send", window, text), ("detach",) or ("kill",).
# ==============
def handleKey(state, key):
    state.notice = ""
    if state.mode == "prefix":
        state.mode = "normal"
        return afterPrefix(state, key)
    if key == state.prefix:
        state.mode = "prefix"
        return None
    if state.mode == "help":
        state.mode = "normal"
        return None
    if state.mode == "choose":
        return choose(state, key)
    if state.mode == "confirm":
        state.mode = "normal"
        return ("kill",) if key in ("y", "Y") else None
    return edit(state, key)


def afterPrefix(state, key):
    if isinstance(key, str) and key.isdigit():
        if int(key) < len(state.windows):
            state.switch(state.windows[int(key)])
    elif key == "n":
        state.move(1)
    elif key == "p":
        state.move(-1)
    elif key == "w":
        state.mode, state.choice = "choose", state.windows.index(state.active)
    elif key == "'":
        state.mode, state.text, state.cursor = "name", "", 0
    elif key == "d":
        return ("detach",)
    elif key == "x":
        state.mode = "confirm"
    elif key == "?":
        state.mode = "help"
    return None


def choose(state, key):
    if key in ("UP", "k"):
        state.choice = max(state.choice - 1, 0)
    elif key in ("DOWN", "j"):
        state.choice = min(state.choice + 1, len(state.windows) - 1)
    elif key == "ENTER":
        state.mode = "normal"
        state.switch(state.windows[state.choice])
    elif isinstance(key, str) and key.isdigit() and int(key) < len(state.windows):
        state.mode = "normal"
        state.switch(state.windows[int(key)])
    elif key in ("ESC", "q"):
        state.mode = "normal"
    return None


def edit(state, key):
    text, cursor = state.text, state.cursor
    if key == "ENTER":
        return submit(state)
    if key == "ESC":
        state.text, state.cursor, state.mode = "", 0, "normal"
    elif key == "BACKSPACE" and cursor:
        state.text, state.cursor = text[:cursor - 1] + text[cursor:], cursor - 1
    elif key == "DELETE":
        state.text = text[:cursor] + text[cursor + 1:]
    elif key == "LEFT":
        state.cursor = max(cursor - 1, 0)
    elif key == "RIGHT":
        state.cursor = min(cursor + 1, len(text))
    elif key == "HOME":
        state.cursor = 0
    elif key == "END":
        state.cursor = len(text)
    elif key in ("PGUP", "PGDN"):
        state.scroll[state.active] = max(state.scroll.get(state.active, 0) + (SCROLL_STEP if key == "PGUP" else -SCROLL_STEP), 0)
    elif key in ("UP", "DOWN") and state.history:
        last = len(state.history)
        state.recalled = max((state.recalled if state.recalled is not None else last) + (-1 if key == "UP" else 1), 0)
        state.text = state.history[state.recalled] if state.recalled < last else ""
        state.recalled = None if state.recalled >= last else state.recalled
        state.cursor = len(state.text)
    elif isinstance(key, str) and len(key) == 1 and key.isprintable():
        state.text, state.cursor = text[:cursor] + key + text[cursor:], cursor + 1
    return None


def submit(state):
    text, mode = state.text, state.mode
    state.text, state.cursor, state.mode, state.recalled = "", 0, "normal", None
    if mode == "name":
        found = state.find(text)
        if found:
            state.switch(found)
        else:
            state.notice = f"There is no window {text}."
        return None
    secret = state.questionHere() and state.questionHere()["secret"]
    if text.strip() and not secret and (not state.history or state.history[-1] != text):
        state.history.append(text)
    if text.startswith(":"):
        return colonCommand(state, text[1:].strip())
    return ("send", state.active, text)


def colonCommand(state, text):
    word, _, rest = text.partition(" ")
    word = word.lower()
    if word in ("w", "window", "select"):
        found = state.find(rest)
        if found:
            state.switch(found)
        else:
            state.notice = f"There is no window {rest}."
    elif word in ("n", "next"):
        state.move(1)
    elif word in ("p", "prev", "previous"):
        state.move(-1)
    elif word in ("d", "detach", "q"):
        return ("detach",)
    elif word in ("kill", "close"):
        state.mode = "confirm"
    elif word in ("help", "h", "?"):
        state.mode = "help"
    else:
        state.notice = f":{word} is not a command of the screen. Type :help."
    return None


# ==============
# The connection to the daemon, and the two ways to show the session.
# ==============
class Connection:
    def __init__(self, info):
        self.socket = socket.create_connection(("127.0.0.1", info["port"]), timeout=5)
        self.socket.settimeout(None)
        self.lock, self.messages = threading.Lock(), queue.Queue()
        self.send({"token": info["token"], "type": "attach"})
        threading.Thread(target=self.read, daemon=True).start()

    def send(self, message):
        with self.lock:
            try:
                self.socket.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                self.messages.put({"type": "lost"})

    def read(self):
        try:
            with self.socket.makefile("rb") as stream:
                for line in stream:
                    self.messages.put(json.loads(line))
        except (OSError, ValueError):
            pass
        self.messages.put({"type": "lost"})

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass


def act(connection, state, action):
    if action is None:
        return None
    if action[0] == "send":
        connection.send({"type": "input", "window": action[1], "text": action[2]})
        return None
    if action[0] == "kill":
        connection.send({"type": "kill"})
        return None
    connection.send({"type": "detach"})
    return "You left the session. It goes on: attach again with swarmup_cli.py attach " + state.session


CURSES_KEYS = {"KEY_UP": "UP", "KEY_DOWN": "DOWN", "KEY_LEFT": "LEFT", "KEY_RIGHT": "RIGHT", "KEY_PPAGE": "PGUP", "KEY_NPAGE": "PGDN", "KEY_HOME": "HOME",
               "KEY_END": "END", "KEY_BACKSPACE": "BACKSPACE", "KEY_DC": "DELETE", "KEY_ENTER": "ENTER", "KEY_RESIZE": "RESIZE"}
CHARACTER_KEYS = {"\n": "ENTER", "\r": "ENTER", "\x7f": "BACKSPACE", "\b": "BACKSPACE", "\x1b": "ESC"}


def runScreen(connection, state):
    import curses
    os.environ.setdefault("ESCDELAY", "25")
    names = {getattr(curses, name): value for name, value in CURSES_KEYS.items() if hasattr(curses, name)}
    def main(screen):
        curses.raw()
        screen.keypad(True)
        screen.timeout(100)
        styles = {"panel": curses.A_NORMAL, "rule": curses.A_DIM, "line": curses.A_NORMAL, "question": curses.A_BOLD, "input": curses.A_NORMAL,
                  "status": curses.A_REVERSE, "menu": curses.A_BOLD}
        if curses.has_colors():
            curses.use_default_colors()
            for number, (name, color) in enumerate((("panel", curses.COLOR_CYAN), ("question", curses.COLOR_YELLOW), ("status", curses.COLOR_GREEN), ("menu", curses.COLOR_MAGENTA)), 1):
                curses.init_pair(number, color, -1)
                styles[name] = curses.color_pair(number) | (curses.A_BOLD if name == "question" else curses.A_REVERSE if name == "status" else 0)
        changed = True
        while True:
            while not connection.messages.empty():
                state.apply(connection.messages.get())
                changed = True
            if state.ended:
                return state.ended
            if changed:
                height, width = screen.getmaxyx()
                rows, (row, column) = layoutScreen(state, width, height)
                screen.erase()
                for number, (text, style) in enumerate(rows[:height]):
                    try:
                        screen.addnstr(number, 0, text, width - 1 if number == height - 1 else width, styles.get(style, curses.A_NORMAL))
                    except curses.error:
                        pass
                screen.move(min(row, height - 1), min(column, width - 1))
                screen.refresh()
                changed = False
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            key = names.get(key, key) if isinstance(key, int) else CHARACTER_KEYS.get(key, key)
            changed = True
            if key == "\x03":
                state.text, state.cursor = "", 0
                state.notice = f"To leave, detach with {describePrefix(state.prefix)} d. To close the session, {describePrefix(state.prefix)} x."
                continue
            outcome = act(connection, state, handleKey(state, key)) if key != "RESIZE" else None
            if outcome:
                return outcome
    return curses.wrapper(main)


# The plain view: every line of the session, with its window, or only one window after :w <name>. What is typed goes to that window.
def runPlain(connection, state):
    shown = {"window": None}
    def printer():
        while True:
            message = connection.messages.get()
            kind = message.get("type")
            state.apply(message)
            if kind == "hello":
                print(f"Attached to session {state.session}. Windows: {', '.join(state.windows)}. Type :help for the commands of this view.")
                print("\n".join(state.lines.get(SWARM_WINDOW, [])[-PLAIN_HELLO_LINES:]))
            elif kind == "lines" and shown["window"] in (None, message["window"]):
                prefix = "" if message["window"] == SWARM_WINDOW or shown["window"] else f"[{message['window']}] "
                print("\n".join(prefix + line for line in message["lines"]), flush=True)
            elif kind in ("bye", "lost"):
                print(state.ended, flush=True)
                os._exit(0)
    threading.Thread(target=printer, daemon=True).start()
    while True:
        question = state.questionHere()
        try:
            text = getpass.getpass("") if question and question["secret"] else input()
        except EOFError:
            text = ":detach"
        if text.startswith(":"):
            word, _, rest = text[1:].strip().partition(" ")
            if word in ("w", "window"):
                shown["window"] = state.find(rest) if rest and rest != "all" else None
                print(f"Showing {shown['window'] or 'every window'}.")
                continue
            if word in ("kill", "close"):
                if input("Close the session? A swarm that runs is saved. (y/n) ").strip().lower() in ("y", "yes"):
                    connection.send({"type": "kill"})
                continue
            if word in ("d", "detach", "q"):
                return act(connection, state, ("detach",))
            print("The commands of this view: :w <window> (or :w all), :detach, :kill.")
            continue
        connection.send({"type": "input", "window": shown["window"] or SWARM_WINDOW, "text": text})


def canUseScreen():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        import curses
    except ImportError:
        return False
    return bool(curses)


def attach(info, plain=False, prefix="C-b"):
    connection = Connection(info)
    state = ScreenState(parsePrefix(prefix))
    try:
        message = runPlain(connection, state) if plain or not canUseScreen() else runScreen(connection, state)
    finally:
        connection.close()
    return message
