import json
import sys
import tempfile
import threading
import time
import types
import unittest
from patching import everywhere
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import architect_manager
import harness_utils
import miner_service
from agent_journal import Journal
from architect_loop import ArchitectLoop, pickArchitect
from architect_manager import ArchitectManager, attachArchitect
from cli_models import USER_LEADER
from cli_program import chooseArchitect
from harness_utils import ARCHITECT_NAME
from leader_manager import LeaderManager
from mcp_gateway import Gateway
from miner_service import MINERS
from models_library import getModelInfo
from pattern_registry import readNotes
from pattern_store import DB_FILE, PatternStore
from project_identity import projectDir, projectFor, registerMission
from swarm_harness import Swarm
from tasks_library import checkAgentName
from test_agent_conversation import ChatLoop, say
from test_leader_utils import usePrices
from test_saved_swarms import waitUntil
from test_user_interface import SessionTestCase
from wheel_store import catalogFor, readCatalog


WHEEL = '''WHEEL = {"name": "install_and_test", "summary": "Installs the module the tests miss, then runs the unit tests of the project and gives the failing ones.",
         "kind": "script", "inputs": {}, "required": [], "changes": True, "network": True, "timeout": 300}


def run(args, folder):
    return "ok"
'''
API = getModelInfo("claude-sonnet-5-5")


def call(tool, number, **arguments):
    return {"text": "", "calls": [{"id": f"a{number}", "name": tool, "arguments": arguments}]}


# The model of the architect of a test: each reply is a dictionary, or a function of the loop that gives one; then it has nothing more to do.
class ArchitectChat:
    def __init__(self, replies=(), delay=0.0):
        self.replies, self.delay, self.prompts, self.tools = list(replies), delay, [], []

    def converse(self, system, messages, tools):
        self.prompts.append(messages[-1]["content"])
        self.tools.append([tool["name"] for tool in tools])
        time.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else say("Nothing more to automate.")
        return reply(self.loop) if callable(reply) else reply


def patternId(loop, kind, contains=""):
    return "#" + str(next(row["id"] for row in loop.desk.store().patterns((kind,), limit=50) if contains in row["label"]))


class ArchitectTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        (self.files / "agent-files").mkdir()
        for target, name, value in ((harness_utils, "AGENT_FILES", self.files / "agent-files"), (miner_service, "POLL_SECONDS", 0.05),
                                    (miner_service, "IDLE_SECONDS", 0.1), (architect_manager, "DEBOUNCE_SECONDS", 0.05),
                                    (architect_manager, "PASS_GAP_SECONDS", 0), (architect_manager, "DRAIN_SECONDS", 3), (architect_manager, "MIN_ACTIVITY", 1)):
            patcher = everywhere(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        usePrices(self)
        self.addCleanup(MINERS.stopAll)
        self.work = self.files / "app"
        self.work.mkdir()
        self.project, self.root = projectFor(self.work)

    # An earlier mission of the project, in which an agent met a missing module three times and fixed it the same way.
    def earlierMission(self, times=3, project=None, root=None):
        journal = Journal(self.files / "agent-files" / "mission-specific-memory" / "old" / ".swarmup", "old", project or self.project, "run")
        registerMission(project or self.project, root or self.root, "old", journal.path)
        for number in range(times):
            journal.add("request", "Coder", prompt="Fix the tests")
            journal.add("call", "Coder", id="1", origin="conversation", server="processes", tool="run_command", args={"command": "python -m unittest"}, outcome="error",
                        seconds=2, size=200, result="Traceback (most recent call last):\n  File \"t.py\", line 1, in <module>\nModuleNotFoundError: No module named 'requests'")
            journal.add("call", "Coder", id="2", origin="conversation", server="processes", tool="run_command", args={"command": "pip install requests"}, outcome="success",
                        seconds=4, size=20, result="ok")
            journal.add("call", "Coder", id="3", origin="conversation", server="processes", tool="run_command", args={"command": "python -m unittest"}, outcome="success",
                        seconds=2, size=20, result="OK")

    def swarm(self, chat, choice=None):
        swarm = Swarm("Fix the tests of the app")
        for name, text in (("Leader", "The report."), ("Coder", "The tests pass.")):
            loop = ChatLoop(types.SimpleNamespace(converse=lambda system, messages, tools, text=text: say(text)))
            loop.setFolder(self.work)
            loop.notifyUser = lambda message: None
            swarm.addAgent(name, loop, "leader" if name == "Leader" else "coder", "work", model=API)
        said = []
        chat.loop = None

        def makeClient(info):
            return chat
        architect = attachArchitect(swarm, choice or {"same": "Leader"}, makeClient, said.append)
        architect.said = said
        self.addCleanup(architect.close)
        return swarm, architect

    def runSwarm(self, swarm, seconds=30):
        swarm.setMode("execute")
        swarm.startInBackground()
        self.assertIn("result", swarm.wait(seconds))


class LifecycleTests(ArchitectTestCase):
    # The architect reads the brief, saves a wheel for what repeats, and the leader and the user are told.
    def testAPassSavesAWheelForWhatRepeats(self):
        self.earlierMission()
        chat = ArchitectChat([call("architect__read_patterns", 1),
                              lambda loop: call("architect__save_wheel", 2, name="install_and_test", kind="script", code=WHEEL,
                                                covers=[patternId(loop, "fix")], why="Agents install the same module and run the tests again."),
                              say("I saved install_and_test.")])
        swarm, architect = self.swarm(chat)
        events = []
        swarm.addListener(events.append)
        chat.loop = None
        original = architect.ensureLoop

        def ensure():
            loop = original()
            chat.loop = loop
            return loop
        architect.ensureLoop = ensure
        self.runSwarm(swarm)
        waitUntil(lambda: catalogFor(self.project), "the wheel of the architect", 20)
        entry = catalogFor(self.project)["install_and_test"]
        self.assertEqual((entry["version"], entry["scope"], entry["project"]), (1, "project", self.project))
        self.assertTrue(entry["covers"])
        self.assertIn("REPEATING, NOT AUTOMATED YET", chat.prompts[0])
        waitUntil(lambda: any(event["kind"] == "wheel" for event in events), "the event of the wheel")
        self.assertEqual(next(event for event in events if event["kind"] == "wheel")["name"], "install_and_test")
        waitUntil(lambda: architect.state == "idle" and architect.passCount >= 1, "the end of the pass")
        self.assertEqual(architect.describe()["wheels"][0]["name"], "install_and_test")
        self.assertTrue(any("Pass 1" in text for text in architect.said))
        store = PatternStore(projectDir(self.project) / DB_FILE, readOnly=True)
        self.addCleanup(store.close)
        covered = [row for row in store.patterns(limit=50) if row["status"] == "covered"]
        self.assertTrue(covered)
        self.assertEqual({row["wheel"] for row in covered}, {"install_and_test"})
        self.assertFalse(any(json.loads(line)["agent"] == ARCHITECT_NAME and json.loads(line)["kind"] in ("step", "call")
                             for line in swarm.journal.path.read_text().splitlines()))

    # What does not repeat yet is refused as a wheel: the architect writes a note instead, which the architects of every project read.
    def testWhatDoesNotRepeatYetBecomesANote(self):
        self.earlierMission(times=2)
        chat = ArchitectChat([lambda loop: call("architect__save_wheel", 1, name="install_and_test", kind="script", code=WHEEL, covers=[patternId(loop, "fix")], why="x"),
                              lambda loop: call("architect__save_note", 2, patterns=[patternId(loop, "fix")],
                                                text="Agents install requests by hand before the tests; it may come back in other Python projects."),
                              say("I kept a note.")])
        swarm, architect = self.swarm(chat)
        original = architect.ensureLoop
        architect.ensureLoop = lambda: setattr(chat, "loop", original()) or chat.loop
        self.runSwarm(swarm)
        architect.runPass("end")
        self.assertEqual(readCatalog()["wheels"], {})
        self.assertIn("None of these patterns repeats yet", chat.prompts[1])
        notes = readNotes()
        self.assertEqual(len(notes), 1)
        self.assertIn("requests by hand", notes[0]["text"])

    def testTheArchitectOnlyReachesItsToolsAndTheReadingToolsAndNeverAsks(self):
        loop = ArchitectLoop(None)
        loop.setFolder(self.work)
        gateway = Gateway("m1")
        self.addCleanup(gateway.close)
        names = {tool["name"] for tool in gateway.link(loop).tools()}
        self.assertEqual({name for name in names if not name.startswith("architect__")},
                         {"files__list_files", "files__find_files", "files__search_files", "files__read_file"})
        self.assertIn("architect__save_wheel", names)
        self.assertEqual(loop.askPermission({"action": "x"})["decision"], "deny")
        self.assertEqual((loop.askUser("?"), loop.askQuestions([{"id": "q"}])), ("", {}))
        self.assertIn("checkAgentName", checkAgentName.__name__)
        self.assertIn("name of the architect", checkAgentName("architect"))

    # A stop of the swarm ends the pass at its next step, and closing the architect ends its thread at once.
    def testAStopEndsThePassQuicklyAndCloseEndsTheThread(self):
        self.earlierMission()
        chat = ArchitectChat([call("architect__read_patterns", number) for number in range(12)], delay=0.3)
        swarm, architect = self.swarm(chat)
        architect.ensureLoop = (lambda original: lambda: setattr(chat, "loop", original()) or chat.loop)(architect.ensureLoop)
        self.runSwarm(swarm)
        waitUntil(lambda: architect.state == "working", "a pass", 15)
        started = time.monotonic()
        architect.close()
        self.assertLess(time.monotonic() - started, 5)
        waitUntil(lambda: architect.state == "idle", "the end of the pass", 5)
        self.assertIsNone(architect.thread)
        self.assertIsNone(MINERS.get(self.project))

    def testAShareOfTheBudgetIsTheMostTheArchitectSpends(self):
        swarm, architect = self.swarm(ArchitectChat())
        architect.how = "own"
        with everywhere(ArchitectManager, "spent", lambda self: 0.5):
            self.assertFalse(architect.overBudget())
            swarm.costs.setBudget(2.0)
            self.assertTrue(architect.overBudget())
            self.assertTrue(architect.desk.shouldStop(ARCHITECT_NAME))
            self.assertFalse(architect.mayRunNow("end"))


    # A wheel saved while agents still work reaches the leader at once, so it can give it to one of them.
    def testANewWheelIsToldToTheLeaderWhileAgentsWork(self):
        swarm, architect = self.swarm(ArchitectChat())
        manager = LeaderManager(swarm, None, None, watchFinishes=False)
        manager.onEvent({"kind": "wheel", "agent": ARCHITECT_NAME, "time": "", "action": "saved", "name": "install_and_test", "summary": "Installs and tests."})
        self.assertIn("install_and_test", manager.queue.get_nowait()[1])
        swarm.members["Coder"]["status"] = "done"
        manager.onEvent({"kind": "wheel", "agent": ARCHITECT_NAME, "time": "", "action": "saved", "name": "other_wheel", "summary": "x"})
        self.assertTrue(manager.queue.empty())


class ChoiceTests(ArchitectTestCase):
    def testTheModelOfTheArchitect(self):
        swarm = Swarm("Mission")
        local = getModelInfo("Qwen/Qwen3-8B")
        for name, info in (("Leader", API), ("Local", local), ("Me", dict(USER_LEADER)), ("Codex", getModelInfo("", cli="codex")),
                           ("Claude", getModelInfo("", cli="claude-code"))):
            swarm.addAgent(name, ChatLoop(None), "worker", "work", model=info)
        self.assertEqual(pickArchitect(swarm, {"same": "Leader"}), (API, "own"))
        self.assertEqual(pickArchitect(swarm, {"same": "Local"}), (local, "share"))
        self.assertEqual(pickArchitect(swarm, {"same": "Me"}), (None, ""))
        self.assertEqual(pickArchitect(swarm, {"same": "Codex"}), (None, ""))
        claude, how = pickArchitect(swarm, {"same": "Claude"})
        self.assertEqual((claude["provider"], claude["cli"], how), ("claude", None, "own"))
        self.assertEqual(pickArchitect(swarm, API), (API, "own"))
        self.assertEqual(pickArchitect(swarm, None), (None, ""))

    # A local model shared with the leader is only used once the run is over, so the agents never wait for it.
    def testASharedLocalModelWaitsForTheEndOfTheRun(self):
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", ChatLoop(None), "leader", "lead", model=getModelInfo("Qwen/Qwen3-8B"))
        architect = ArchitectManager(swarm, {"same": "Leader"})
        self.assertFalse(architect.mayRunNow("signal"))
        self.assertTrue(architect.mayRunNow("end"))

    def testTheStateIsSavedAndTheArchitectIsAttachedOnce(self):
        swarm = Swarm("Mission")
        architect = attachArchitect(swarm, {"same": "Leader"})
        architect.wheels, architect.passCount = [{"name": "w"}], 2
        self.assertIs(attachArchitect(swarm, None), architect)
        state = swarm.describeState()["architect"]
        self.assertEqual(state["choice"], {"same": "Leader"})
        again = ArchitectManager(Swarm("Mission"), state["choice"])
        again.restoreState(state)
        self.assertEqual((again.passCount, again.wheels), (2, [{"name": "w"}]))

    def testTheCommandLineOffersTheModelOfTheLeaderFirst(self):
        answers, said = ["y"], []
        console = types.SimpleNamespace(ask=lambda question, **options: said.append(question) or answers.pop(0), say=lambda text, **options: said.append(text))
        self.assertEqual(chooseArchitect(console, Swarm("x"), [("Leader", API)], {}, {}), ({"same": "Leader"}, None))
        self.assertTrue(any(text.startswith("The architect: use the same model as Leader") for text in said))
        answers[:] = ["n"]
        self.assertEqual(chooseArchitect(console, Swarm("x"), [("Me", {"user": True})], {}, {}), (None, None))


class WindowTests(SessionTestCase):
    def testTheWindowGivesTheSwarmItsArchitectOnTheChosenModel(self):
        self.act("setMission", mission="Write two texts about bees.")
        first = self.act("saveAgent", task="author", values={"subject": "bees", "length": "200"})["agentId"]
        self.act("chooseModel", agentId=first, name="claude-sonnet-5-5", local=False, apiKey="sk-test")
        architect = self.session.describe()["architect"]
        self.assertEqual((architect["same"], architect["options"]), (self.session.specs[0]["name"], [self.session.specs[0]["name"]]))
        self.act("chooseModel", agentId="architect", name="claude-haiku-4-5", local=False)
        self.assertEqual(self.session.describe()["architect"]["model"]["name"], "claude-haiku-4-5")
        swarm = self.session.currentSwarm()
        self.assertEqual(swarm.architect.choice["name"], "claude-haiku-4-5")
        self.act("setArchitect", same=self.session.specs[0]["name"])
        self.assertIsNone(self.session.describe()["architect"]["model"])
        with self.assertRaises(ValueError):
            self.act("setArchitect", same="Nobody")
        self.assertEqual(self.act("wheels")["wheels"], [])


if __name__ == "__main__":
    unittest.main()
