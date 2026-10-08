import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from patching import everywhere
from pathlib import Path
from unittest import mock

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import cli_hub
import cli_sessions
import harness_utils
from base_loop import Loop
from cli_hub import SWARM_WINDOW, Hub, HubConsole, startServer
from cli_screen import Connection, ScreenState, handleKey, layoutScreen, parsePrefix, wrap
from cli_sessions import findSession, listSessions, newSessionId, request, sessionPath, writeSession
from swarm_harness import Swarm
from test_saved_swarms import waitUntil

ENTRY = Path(__file__).resolve().parent.parent / "src" / "backend" / "cli-tool" / "swarmup_cli.py"


class FakeAgent:
    def input(self, prompt):
        return "A draft"


# A hub whose process does not really end, and which keeps the commands it was asked to run.
def makeHub():
    hub = Hub("s1", "secret-token")
    hub.exit = lambda code: None
    hub.ran = []
    return hub


def makeSwarm():
    swarm = Swarm("Write about bees")
    for name in ("Leader", "Writer"):
        swarm.addAgent(name, Loop(FakeAgent()), "boss" if name == "Leader" else "writer", f"task of {name}")
    return swarm


def answerLater(hub, ask):
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("answer", ask()), daemon=True)
    thread.start()
    waitUntil(lambda: hub.question is not None, "the question")
    return result, thread


class HubTests(unittest.TestCase):
    def testWhatAnAgentSaysGoesToItsWindowAndTheLeaderAlsoToWindowZero(self):
        hub = makeHub()
        hub.swarm = makeSwarm()
        hub.write("Writer", "I read the notes.")
        hub.write("Leader", "Summary:\nall good")
        self.assertEqual(list(hub.windows), [SWARM_WINDOW, "Writer", "Leader"])
        self.assertEqual(list(hub.windows["Writer"].lines), ["I read the notes."])
        self.assertEqual(list(hub.windows[SWARM_WINDOW].lines), ["[Leader] Summary:", "all good"])

    def testAQuestionIsAnsweredFromItsWindowOrFromWindowZeroButNotFromAnother(self):
        hub = makeHub()
        hub.commands = lambda window, line: hub.ran.append((window, line))
        hub.window("Writer")
        hub.window("Checker")
        result, thread = answerLater(hub, lambda: hub.waitForAnswer("Writer", "Is this good to go?"))
        self.assertEqual(list(hub.windows[SWARM_WINDOW].lines), ["? [Writer] Is this good to go?"])
        hub.input("Checker", "approve")
        hub.input("Checker", "use a formal tone")
        waitUntil(lambda: len(hub.ran) == 2, "the commands")
        self.assertEqual(hub.ran, [("Checker", "approve Checker"), ("Checker", "msg Checker use a formal tone")])
        self.assertTrue(thread.is_alive())
        hub.input("Writer", "yes")
        thread.join(5)
        self.assertEqual(result["answer"], "yes")
        self.assertEqual(list(hub.windows["Writer"].lines)[-1], "> yes")
        result, thread = answerLater(hub, lambda: hub.waitForAnswer("Writer", "Password?", secret=True))
        hub.input(SWARM_WINDOW, "hunter2")
        thread.join(5)
        self.assertEqual((result["answer"], list(hub.windows["Writer"].lines)[-1]), ("hunter2", "> *******"))

    def testWhatIsTypedInTheWindowOfAnAgentIsAboutThatAgent(self):
        hub = makeHub()
        hub.window("Writer")
        for typed, command in (("approve", "approve Writer"), ("reject", "reject Writer"), ("start", "start Writer"), ("show", "Writer"),
                               ("correct make it shorter", "correct Writer make it shorter"), ("remove not needed", "remove Writer not needed"),
                               ("cost", "cost"), ("stop", "stop"), ("tree", "tree"), ("hello there", "msg Writer hello there")):
            self.assertEqual(hub.route("Writer", typed), command, typed)
        self.assertEqual(hub.route(SWARM_WINDOW, "approve Writer"), "approve Writer")

    def testCommandsWaitForTheRunAndKillEndsTheSession(self):
        hub = makeHub()
        hub.input(SWARM_WINDOW, "cost")
        waitUntil(lambda: hub.windows[SWARM_WINDOW].lines, "the answer")
        self.assertIn("the swarm is not running yet", hub.windows[SWARM_WINDOW].lines[-1])
        ended = []
        hub.onEnd, hub.exit = lambda: ended.append("file removed"), lambda code: ended.append(code)
        hub.swarm = mock.Mock(isRunning=lambda: True)
        hub.input(SWARM_WINDOW, "kill")
        waitUntil(lambda: len(ended) == 2, "the end of the session")
        hub.swarm.saveForExit.assert_called_once_with()
        self.assertEqual(ended, ["file removed", 0])

    def testTheSwarmGivesEveryAgentItsWindowItsPanelAndItsEvents(self):
        hub, swarm = makeHub(), makeSwarm()
        swarm.members["Writer"].update(status="working", review="ready", draft="Bees are vital.")
        hub.follow(swarm, [{"kind": "review", "agent": "Writer", "review": "ready", "time": "10:00:00"},
                           {"kind": "message", "agent": "Writer", "sender": "Leader", "receiver": "Writer", "message": "Go", "time": "10:00:01"},
                           {"kind": "activity", "agent": "Writer", "text": "read_file(\"path\": \"notes.md\")", "time": "10:00:02"}])
        writer = list(hub.windows["Writer"].lines)
        self.assertIn("[10:00:00] Writer is ready: it waits for you", writer)
        self.assertIn("Bees are vital.", writer)
        self.assertIn("[10:00:01] Leader -> Writer: Go", writer)
        self.assertIn("[10:00:01] Leader -> Writer: Go", hub.windows["Leader"].lines)
        self.assertIn('[10:00:02] Writer: read_file("path": "notes.md")', writer)
        self.assertIn("Writer: the writer, working, its draft waits for you", hub.windows["Writer"].panel)
        self.assertIn("Swarm: Write about bees", hub.windows[SWARM_WINDOW].panel)
        swarm.removeAgent("Writer", "not needed")
        hub.follow(swarm, [])
        self.assertTrue(hub.windows["Writer"].left)

    def testTheScrollbackIsBounded(self):
        with mock.patch.object(cli_hub, "SCROLLBACK", 3):
            hub = makeHub()
            hub.write(SWARM_WINDOW, "1\n2\n3\n4\n5")
        self.assertEqual(list(hub.windows[SWARM_WINDOW].lines), ["3", "4", "5"])

    def testTheConsoleOfASessionAnswersInTheWindowOfTheCommand(self):
        hub = makeHub()
        console = HubConsole(hub)
        console.commands = lambda line: console.say(f"ran {line}")
        console.startPump()
        self.assertEqual(hub.state, "running")
        hub.window("Writer")
        hub.input("Writer", "approve")
        waitUntil(lambda: hub.windows["Writer"].lines, "the answer of the command")
        self.assertEqual(list(hub.windows["Writer"].lines), ["ran approve Writer"])
        result, thread = answerLater(hub, lambda: console.askSecret("API key?", window="Writer"))
        self.assertTrue(hub.question["secret"])
        hub.input("Writer", "sk-1")
        thread.join(5)
        self.assertEqual(result["answer"], "sk-1")
        console.stopPump()
        self.assertEqual(hub.state, "ready")


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.hub = makeHub()
        self.server = startServer(self.hub)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.info = {"port": self.server.server_address[1], "token": self.hub.token}

    def testATerminalAttachesSeesEverythingAndAnswers(self):
        self.hub.write(SWARM_WINDOW, "Welcome")
        connection = Connection(self.info)
        self.addCleanup(connection.close)
        hello = connection.messages.get(timeout=5)
        self.assertEqual((hello["type"], hello["windows"][0]["lines"]), ("hello", ["Welcome"]))
        result, thread = answerLater(self.hub, lambda: self.hub.waitForAnswer(SWARM_WINDOW, "Your choice?"))
        state = ScreenState()
        state.apply(hello)
        waitUntil(lambda: not connection.messages.empty() and state.apply(connection.messages.get()) is None and state.question, "the question")
        connection.send({"type": "input", "window": SWARM_WINDOW, "text": "2"})
        thread.join(5)
        self.assertEqual(result["answer"], "2")
        self.assertEqual(request(self.info, {"type": "capture", "window": SWARM_WINDOW})["text"], "Welcome\n? Your choice?\n> 2")
        self.assertEqual(request(self.info, {"type": "status"})["state"], "building")

    def testATerminalWithoutTheTokenIsRefusedAndKillEndsTheSession(self):
        self.assertIsNone(request({**self.info, "token": "wrong"}, {"type": "status"}))
        ended = threading.Event()
        self.hub.exit = lambda code: ended.set()
        self.assertEqual(request(self.info, {"type": "kill"}), {"type": "ok"})
        self.assertTrue(ended.wait(5))


class ScreenTests(unittest.TestCase):
    def makeState(self):
        state = ScreenState()
        state.apply({"type": "hello", "session": "s1", "state": {"state": "running", "cost": "Spent: $0.10"}, "question": None,
                     "windows": [{"name": "swarm", "panel": "Swarm: bees\nLeader -> working", "left": False, "lines": [f"line {number}" for number in range(30)]},
                                 {"name": "Leader", "panel": "", "left": False, "lines": []}, {"name": "Writer", "panel": "Writer: working", "left": False, "lines": ["Hi"]},
                                 {"name": "Old", "panel": "", "left": True, "lines": []}]})
        return state

    def testTheScreenHasThePanelTheFeedTheInputAndTheStatusBar(self):
        state = self.makeState()
        state.apply({"type": "lines", "window": "Leader", "lines": ["news"]})
        state.apply({"type": "question", "question": {"id": 1, "window": "Writer", "text": "Approve?", "secret": False}})
        rows, cursor = layoutScreen(state, 80, 12)
        texts = [text for text, style in rows]
        self.assertEqual(texts[:3], ["Swarm: bees", "Leader -> working", "─" * 80])
        self.assertEqual(texts[-3:-2], ["? [Writer] Approve?"])
        self.assertEqual(texts[-2], "> ")
        self.assertTrue(texts[-1].startswith("[SwarmUP s1] 0:swarm* 1:Leader+ 2:Writer! 3:Old-"))
        self.assertTrue(texts[-1].endswith("Spent: $0.10 | waiting for you"))
        self.assertEqual(texts[3:-3][-1], "line 29")
        self.assertEqual(cursor, (10, 2))
        state.scroll["swarm"] = 10
        self.assertEqual([text for text, style in layoutScreen(state, 80, 12)[0]][3:-3][-1], "line 19")

    def testASecretIsHiddenAndTheQuestionOfTheWindowIsNotShownTwice(self):
        state = self.makeState()
        state.switch("Writer")
        state.apply({"type": "lines", "window": "Writer", "lines": ["? Your API key?"]})
        state.apply({"type": "question", "question": {"id": 2, "window": "Writer", "text": "Your API key?", "secret": True}})
        for key in "sk-12":
            handleKey(state, key)
        texts = [text for text, style in layoutScreen(state, 60, 10)[0]]
        self.assertEqual(texts[-2], "> *****")
        self.assertEqual(texts.count("? Your API key?"), 1)
        self.assertEqual(handleKey(state, "ENTER"), ("send", "Writer", "sk-12"))
        self.assertEqual(state.history, [])

    def testThePrefixKeysMoveBetweenWindowsLikeTmux(self):
        state = self.makeState()
        for keys, active in ((["\x02", "2"], "Writer"), (["\x02", "n"], "Old"), (["\x02", "n"], "swarm"), (["\x02", "p"], "Old")):
            for key in keys:
                handleKey(state, key)
            self.assertEqual(state.active, active)
        handleKey(state, "\x02")
        handleKey(state, "w")
        self.assertEqual(state.mode, "choose")
        for key in ("UP", "UP", "ENTER"):
            handleKey(state, key)
        self.assertEqual(state.active, "Leader")
        for key in ["\x02", "'", *"wri", "ENTER"]:
            handleKey(state, key)
        self.assertEqual(state.active, "Writer")
        self.assertEqual([handleKey(state, key) for key in ("\x02", "d")], [None, ("detach",)])
        self.assertEqual([handleKey(state, key) for key in ("\x02", "x", "n")], [None, None, None])
        self.assertEqual([handleKey(state, key) for key in ("\x02", "x", "y")], [None, None, ("kill",)])
        handleKey(state, "\x02")
        handleKey(state, "?")
        self.assertIn("SwarmUP sessions work like tmux", layoutScreen(state, 100, 30)[0][0][0])

    def testTypedCommandsAndTheHistory(self):
        state = self.makeState()
        for text in (":w 2", ":next"):
            for key in [*text, "ENTER"]:
                self.assertIsNone(handleKey(state, key))
        self.assertEqual(state.active, "Old")
        for key in [*":w nobody", "ENTER"]:
            handleKey(state, key)
        self.assertEqual(state.notice, "There is no window nobody.")
        for key in [*"approve", "ENTER"]:
            outcome = handleKey(state, key)
        self.assertEqual(outcome, ("send", "Old", "approve"))
        handleKey(state, "UP")
        self.assertEqual(state.text, "approve")
        handleKey(state, "DOWN")
        self.assertEqual(state.text, "")
        self.assertEqual([handleKey(state, key) for key in [*":detach", "ENTER"]][-1], ("detach",))
        self.assertEqual(parsePrefix("C-a"), "\x01")
        with self.assertRaises(ValueError):
            parsePrefix("F1")
        self.assertEqual(wrap("one two three", 7), ["one two", "three"])
        self.assertEqual(wrap("", 7), [""])


class SessionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = everywhere(harness_utils, "AGENT_FILES", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def testSessionsAreNamedFoundAndForgottenWhenTheirDaemonIsGone(self):
        self.assertEqual(newSessionId(), "s1")
        writeSession({"id": "s1", "pid": 999999999, "port": 9, "token": "t", "started": "1"})
        writeSession({"id": "s3", "pid": 999999999, "port": 9, "token": "t", "started": "2"})
        self.assertEqual(newSessionId(), "s2")
        if os.name != "nt":
            self.assertEqual(sessionPath("s1").stat().st_mode & 0o777, 0o600)
        sessions = [{"id": "s1", "started": "1"}, {"id": "s12", "started": "2"}, {"id": "s2", "started": "3"}]
        self.assertEqual([findSession(text, sessions)["id"] for text in (None, "1", "s12", "S2")], ["s2", "s1", "s12", "s2"])
        with self.assertRaisesRegex(ValueError, "There is no session s4"):
            findSession("4", sessions)
        with self.assertRaisesRegex(ValueError, "No session runs"):
            findSession(None, [])
        self.assertEqual(listSessions(), [])
        self.assertFalse(sessionPath("s1").exists())


# A real session: a daemon started from this test, which goes on after the command that started it ended, answers a terminal, and is killed.
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.environment = {**os.environ, "SWARMUP_HOME": folder.name, "NO_COLOR": "1"}
        self.home = Path(folder.name)

    def command(self, *arguments):
        return subprocess.run([sys.executable, str(ENTRY), *arguments], capture_output=True, text=True, env=self.environment, stdin=subprocess.DEVNULL, timeout=60)

    def testASessionGoesOnWithoutItsTerminalAndEndsWhenItIsKilled(self):
        started = self.command("new", "--detached")
        self.assertIn("Session s1 started.", started.stdout, started.stderr)
        self.addCleanup(lambda: self.command("kill", "--all"))
        info = json.loads((self.home / "cli-sessions" / "s1.json").read_text(encoding="utf-8"))
        self.assertIn("s1    building, waiting for you in swarm", self.command("list").stdout)
        status = json.loads(self.command("status", "s1", "--json").stdout)
        self.assertEqual((status["state"], status["question"]["text"]), ("building", "Your choice (a number):"))
        connection = Connection(info)
        state = ScreenState()
        connection.send({"type": "input", "window": "swarm", "text": "1"})
        def asked():
            while not connection.messages.empty():
                state.apply(connection.messages.get())
            return state.question and "How many agents" in state.question["text"]
        waitUntil(asked, "the next question", seconds=20)
        connection.send({"type": "detach"})
        connection.close()
        self.assertIn("> 1", self.command("show", "s1").stdout)
        self.assertIn("Session s1 is closed.", self.command("kill", "s1").stdout)
        self.assertFalse((self.home / "cli-sessions" / "s1.json").exists())
        waitUntil(lambda: not cli_sessions.pidAlive(info["pid"]) or os.name == "nt" or self.reap(info["pid"]), "the end of the daemon")
        self.assertIn("No session runs.", self.command("list").stdout)

    def reap(self, pid):
        try:
            return os.waitpid(pid, os.WNOHANG)[0] == pid
        except ChildProcessError:
            return False


if __name__ == "__main__":
    unittest.main()
