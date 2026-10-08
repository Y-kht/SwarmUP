# The console of the command line: what the program says and asks, and the questions that guide the user. The program runs with this
# console in the foreground of a terminal (swarmup_cli.py run), and with HubConsole (cli_hub.py) in a session that terminals attach to.
import getpass
import os
import sys
import threading

from tasks_library import parseChoices


LINE = "=" * 72


META_WORDS = ("tree", "help", "?", "log")


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
        self.pumpThread = None
        # Adds an agent to the swarm while it runs (the command add), set by the program that knows the keys and the models.
        self.newAgent = None

    # window is the window of a session the text belongs to (an agent, or the swarm when it is None). This console has only one window.
    def say(self, text="", window=None):
        with self.output:
            self.write(text)
            if self.pending and not self.pending["event"].is_set():
                self.write(f"  (the question that waits for you) {self.pending['text']}")

    def ask(self, question, window=None):
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

    def askSecret(self, question, window=None):
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

    # While the swarm runs, a thread reads what the user types (pump). When the run is over, the user presses Enter to give the terminal back.
    def startPump(self):
        self.pumping, self.closed = self.interactive, False
        self.pumpThread = threading.Thread(target=self.pump, daemon=True) if self.interactive else None
        if self.pumpThread:
            self.pumpThread.start()

    def stopPump(self):
        if self.pumpThread:
            self.say("Press Enter to continue.")
            self.pumping = False
            self.pumpThread.join()
        self.pumping = False

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
# The questions about the swarm and its agents.
# ==============
def heading(console, text):
    console.say(f"\n{LINE}\n{text}\n{LINE}")
