import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from patching import everywhere
from pathlib import Path
from types import SimpleNamespace

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import architect_manager
import harness_utils
from architect_loop import pickArchitect
from architect_manager import ArchitectManager
from cli_solo import SoloDesk, renderView
from general_loop import GeneralLoop
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp_policy import decidePermission
from model_adapters import USER_LEADER, Driver, UserLeader, describeCommands, readCommand, typedArguments
from project_identity import projectFor
from swarm_harness import Swarm
from test_saved_swarms import waitUntil
from wheel_store import saveWheel

ENTRY = Path(__file__).resolve().parent.parent / "src" / "backend" / "cli-tool" / "swarmup_cli.py"
WHEEL = {"name": "run_unit_tests", "kind": "script", "covers": ["#1"], "code": '''WHEEL = {"name": "run_unit_tests", "summary": "Runs the unittest suite of the project and gives back if it passed, with its failing tests.",
         "kind": "script", "inputs": {}, "required": [], "changes": False, "network": False, "timeout": 60}


import subprocess
import sys


def run(args, folder):
    done = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=folder, capture_output=True, text=True, timeout=50)
    return {"passed": done.returncode == 0, "failures": [line for line in done.stderr.splitlines() if line.startswith(("FAIL:", "ERROR:"))]}
'''}
TEST_FILE = """import unittest
from calc import add


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
"""
RUN_TESTS = ["command=python -m unittest -q", "reason=run the tests"]


# A model that gives its calls in order: it answers when none is left, and keeps the results it read.
class ScriptedModel:
    approvesItself = True

    def __init__(self, calls, answer="Done."):
        self.calls, self.answer, self.results, self.tools, self.usage = list(calls), answer, [], [], {"calls": 0, "input": 0, "output": 0}

    def converse(self, system, messages, tools, onText=None):
        self.results.append(messages[-1]["content"] if messages[-1]["role"] == "tool" else "")
        self.tools.append([tool["name"] for tool in tools])
        if self.calls:
            name, arguments = self.calls.pop(0)
            return {"text": "", "calls": [{"id": f"c{len(self.calls)}", "name": name, "arguments": arguments}], "raw": None, "cut": False}
        return {"text": self.answer, "calls": [], "raw": None, "cut": False}


class ScriptedConsole:
    def __init__(self, reply):
        self.reply, self.said = reply, []

    def say(self, text, window=None):
        self.said.append(text)

    def ask(self, question, window=None):
        return self.reply


class CommandTests(unittest.TestCase):
    # The whole text of a wheel is one JSON string with \n: writing them as new lines before reading the JSON made it invalid, and a wheel could not be written.
    def testJsonKeepsItsOwnNewLinesAndKeyValueGetsThem(self):
        self.assertEqual(readCommand('/files__edit_file {"path": "a.py", "old_text": "a\\nb"}')[0]["arguments"], {"path": "a.py", "old_text": "a\nb"})
        self.assertEqual(readCommand('/files__write_file path=a.py content="a\\nb"')[0]["arguments"], {"path": "a.py", "content": "a\nb"})
        self.assertIn("problem", readCommand("/files__read_file {nothing")[0])

    def testAnAnswerOfTheUserStillGetsItsNewLines(self):
        reply = UserLeader(ScriptedConsole("One\\nTwo"), "Boss").converse("", [{"role": "user", "content": "Go"}], [])
        self.assertEqual(reply["text"], "One\nTwo")

    def testNewToolsAreToldEachOnce(self):
        console = ScriptedConsole("Done")
        said = console.said
        leader = UserLeader(console, "Boss")
        tools = [{"name": "files__read_file", "description": "Read.", "parameters": {"properties": {}}}]
        leader.converse("", [{"role": "user", "content": "Go"}], tools)
        leader.converse("", [{"role": "user", "content": "Go"}], tools)
        leader.converse("", [{"role": "user", "content": "Go"}], tools + [{"name": "wheels__run_tests", "description": "Run.", "parameters": {"properties": {}}}])
        self.assertIn("files__read_file", said[0])
        self.assertNotIn("Your tools", said[1])
        self.assertIn("wheels__run_tests", said[2])
        self.assertNotIn("files__read_file", said[2])

    def testTheListOfTheToolsEndsEachLineWithOneFullStop(self):
        tools = [{"name": "transfer__list_clips", "description": "List your clipboards.", "parameters": {"properties": {}}},
                 {"name": "files__read_file", "description": "Read a file. It changes nothing.", "parameters": {"properties": {"path": {}}}}]
        self.assertEqual(describeCommands(tools), "/transfer__list_clips   List your clipboards.\n/files__read_file path=...  Read a file.")

    # The card of a wheel can be allowed for good (forever): the model is told so, only for a wheel.
    def testTheCardOfAWheelTellsThatItsVersionCanBeAllowedForGood(self):
        card = {"type": "solo", "state": "working", "notes": [], "person": "", "turn": {"id": 3, "kind": "card", "agent": "Agent", "text": "Agent wants to run the wheel x", "opens": False, "pin": True}}
        self.assertIn("solo forever s1 (this version of the wheel, in every swarm)", renderView(card, "s1"))
        self.assertIn("forever allows this version", renderView(card, "s1", hints="tools"))
        self.assertNotIn("forever", renderView({**card, "turn": {**card["turn"], "pin": False}}, "s1"))

    # Who types id=12 cannot say that it is a text; only the tool can. The gateway still refuses a wrong type from a model.
    def testANumberForATextBecomesTheTextTheToolAsksFor(self):
        tools = [{"name": "architect__read_pattern", "parameters": {"properties": {"id": {"type": "string"}, "examples": {"type": "integer"}}}}]
        self.assertEqual(typedArguments("architect__read_pattern", {"id": 12, "examples": 2}, tools), {"id": "12", "examples": 2})
        self.assertEqual(typedArguments("nothing", {"id": 12}, tools), {"id": 12})


class LoneAgentTests(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        (self.files / "home").mkdir()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.files / "home")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.files / "project"
        self.folder.mkdir()
        (self.folder / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    def swarm(self, model, recipe=None, approvals="auto"):
        swarm = Swarm("Fix the code")
        loop = GeneralLoop(model, "Fix the code")
        loop.setFolder(self.folder)
        loop.approvals = approvals
        loop.notifyUser = lambda message: None
        swarm.addAgent("Agent", loop, "agent", "Fix the code", model=dict(USER_LEADER), recipe=recipe or {"task": "agent", "answers": {}})
        return swarm, loop

    def runSwarm(self, swarm):
        swarm.startInBackground()
        swarm.wait(60)
        self.addCleanup(lambda: swarm.gateway.close() if swarm.gateway else None)
        self.assertEqual(swarm.outcome.get("error"), None)

    # In a real run an agent that fixed a bug was refused the third run of its tests: the guard did not know anything had changed.
    def testAnAgentRunsItsTestsAgainAfterEachFix(self):
        run = ("processes__run_command", {"command": "echo tests", "reason": "tests"})
        edit = lambda old, new: ("files__edit_file", {"path": "calc.py", "old_text": old, "new_text": new, "reason": "fix"})
        read = ("files__read_file", {"path": "calc.py"})
        model = ScriptedModel([read, run, edit("a - b", "a * b"), read, run, edit("a * b", "a + b"), read, run, edit("a + b", "a - b"), read, run])
        swarm, loop = self.swarm(model)
        self.runSwarm(swarm)
        self.assertFalse([result for result in model.results if "you already made this same call" in result], model.results)
        self.assertIn("a - b", (self.folder / "calc.py").read_text(encoding="utf-8"))

    def testAModelThatChangesNothingIsStillToldToUseWhatItHas(self):
        read = ("files__read_file", {"path": "calc.py"})
        model = ScriptedModel([read, read, read])
        swarm, loop = self.swarm(model)
        self.runSwarm(swarm)
        self.assertIn("you already made this same call 2 times", model.results[3])

    def testACommandThatGivesNothingNewEndsToo(self):
        run = ("processes__run_command", {"command": "echo tests", "reason": "tests"})
        model = ScriptedModel([run] * 6)
        swarm, loop = self.swarm(model)
        self.runSwarm(swarm)
        self.assertIn("you already made this same call 5 times", model.results[6])

    def testAnAgentAloneHasNoToolsOfATeamUntilAnotherOneJoins(self):
        model = ScriptedModel([])
        swarm, loop = self.swarm(model)
        self.runSwarm(swarm)
        self.assertFalse([name for name in model.tools[0] if name.startswith("team__")], model.tools[0])
        self.assertNotIn("leader__give_wheels", model.tools[0])
        self.assertIn("files__read_file", model.tools[0])
        self.assertIn("you work alone", swarm.describeTeamFor("Agent"))
        self.assertIn("You work alone", swarm.describe("Agent"))
        other = GeneralLoop(ScriptedModel([]), "Help")
        swarm.addAgent("Other", other, "agent", "Help", model=dict(USER_LEADER))
        self.assertIn("Other", swarm.describe("Agent"))
        self.assertNotIn("work alone", swarm.describe("Agent"))

    def saveWheel(self, name="run_unit_tests"):
        code = WHEEL["code"].replace("run_unit_tests", name)
        return saveWheel(name, "script", code, "Runs the unittest suite of the project.", ["f1"], "project", projectFor(self.folder)[0], {"mission": "m1"})

    def testAnAgentAloneGetsTheWheelsOfItsProjectAtTheStart(self):
        self.saveWheel()
        model = ScriptedModel([])
        swarm, loop = self.swarm(model)
        self.runSwarm(swarm)
        self.assertEqual(loop.wheels, ["run_unit_tests"])
        self.assertIn("wheels__run_unit_tests", model.tools[0])
        self.assertNotIn("wheels", swarm.getMember("Agent")["recipe"]["answers"])

    def testAListTheUserChoseStays(self):
        self.saveWheel()
        model = ScriptedModel([])
        swarm, loop = self.swarm(model, recipe={"task": "agent", "answers": {"wheels": []}})
        loop.wheels = []
        self.runSwarm(swarm)
        self.assertEqual(loop.wheels, [])

    def testAWheelSavedWhileAnAgentWorksAloneReachesItsNextStep(self):
        model = ScriptedModel([("files__read_file", {"path": "calc.py"})])
        swarm, loop = self.swarm(model)
        converse = model.converse
        def saving(system, messages, tools, onText=None):
            if not model.tools:
                entry = self.saveWheel()[0]
                swarm.emit("wheel", "Architect", action="saved", name=entry["name"], version=entry["version"], summary="", why="")
            return converse(system, messages, tools, onText)
        model.converse = saving
        self.runSwarm(swarm)
        self.assertNotIn("wheels__run_unit_tests", model.tools[0])
        self.assertIn("wheels__run_unit_tests", model.tools[1])

    # The leader, and an agent alone, work on the whole follow-up: only an agent whose leader gave it a part reads "your part".
    def testAnAgentAloneWorksOnTheWholeFollowUp(self):
        loop = GeneralLoop(None, "Fix the tests")
        loop.team, loop.roundRequest = SimpleNamespace(currentRequest=lambda: "Add a test"), "Add a test"
        self.assertNotIn("from your leader", loop.followUpTask())
        self.assertIn("new request:\nAdd a test\n\nYour standing instructions", loop.followUpTask())
        loop.roundRequest = "Write the test of add"
        self.assertIn("Add a test\n\nYour part in it, from your leader:\nWrite the test of add\n\nYour standing instructions", loop.followUpTask())

    def testTwoAgentsAreNotGivenWheelsByThemselves(self):
        self.saveWheel()
        swarm, loop = self.swarm(ScriptedModel([]))
        other = GeneralLoop(ScriptedModel([]), "Help")
        other.notifyUser = lambda message: None
        swarm.addAgent("Other", other, "agent", "Help", model=dict(USER_LEADER))
        self.runSwarm(swarm)
        self.assertEqual((loop.wheels, other.wheels), ([], []))

    # A person or a model that chose auto is not asked. Plan mode still only looks, and nothing else of the policy changes.
    def testAutoApprovesByTheCallAndIsRecordedButPlanModeStillRefuses(self):
        notes = []
        loop = SimpleNamespace(planning=False, folder=str(self.folder), allowed=set(), approvals="auto", note=lambda kind, **fields: notes.append(fields),
                               askPermission=lambda card: self.fail("auto must not ask"))
        self.assertEqual(decidePermission(loop, {"action": "run", "key": "command:ls"})["decision"], "once")
        self.assertEqual(notes[-1]["how"], "auto")
        loop.planning = True
        self.assertEqual(decidePermission(loop, {"action": "run", "key": "command:ls"})["decision"], "deny")

    def testWithoutAutoTheCardIsStillAsked(self):
        asked = []
        loop = SimpleNamespace(planning=False, folder=str(self.folder), allowed=set(), approvals="", note=lambda kind, **fields: None,
                               askPermission=lambda card: asked.append(card) or {"decision": "deny", "message": "no"})
        self.assertEqual(decidePermission(loop, {"action": "run", "key": "command:ls"})["decision"], "deny")
        self.assertEqual(len(asked), 1)


# Many short missions repeat too: in a real run of 27 entries the last pass never came, however much repeated.
class ShortRunTests(unittest.TestCase):
    # repeating: what the architect can decide about (the focus of a pass); looked: when its last pass ended.
    def manager(self, signals, written, repeating=(), looked=0):
        store = SimpleNamespace(pendingSignals=lambda: signals, lastPass=lambda: {"ended": looked} if looked else None)
        patcher = everywhere(architect_manager, "focusPatterns", lambda store, limit, project=None: list(repeating))
        patcher.start()
        self.addCleanup(patcher.stop)
        manager = ArchitectManager.__new__(ArchitectManager)
        manager.swarm, manager.store = SimpleNamespace(journal=SimpleNamespace(written=written), project="p1"), lambda: store
        return manager

    def testAPatternThatJustRepeatsGetsItsPassAfterAShortRun(self):
        self.assertTrue(self.manager([{"id": 1}], 5).worthEnd())

    def testAShortRunWithNothingNewGetsNoPass(self):
        self.assertFalse(self.manager([], 5, repeating=[{"id": 1, "kind": "command"}]).worthEnd())

    def testALongRunStillLearnsFromWhatRepeats(self):
        self.assertTrue(self.manager([], 40, repeating=[{"id": 1, "kind": "command"}]).worthEnd())
        self.assertFalse(self.manager([], 40).worthEnd())

    # A wheel that went wrong again since the architect last looked gets its pass, however short the run; one it already looked at waits for the next failure.
    def testAWheelThatFailedAgainGetsItsPass(self):
        failed = {"id": 2, "kind": "wheelfail", "lastSeen": 200.0, "data": {"wheel": "run_tests"}}
        self.assertTrue(self.manager([], 5, repeating=[failed], looked=100.0).worthEnd())
        self.assertFalse(self.manager([], 40, repeating=[failed], looked=300.0).worthEnd())


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.desk = SoloDesk()
        self.replies = []

    def turn(self, kind="work", tools=("files__read_file",), agent="Agent"):
        thread = threading.Thread(target=lambda: self.replies.append(self.desk.take({"kind": kind, "agent": agent, "text": "Go", "tools": [{"name": name} for name in tools]})), daemon=True)
        thread.start()
        waitUntil(lambda: self.desk.turn is not None, "the turn")
        return thread

    def testACallOfAToolThatIsNotThereKeepsTheTurn(self):
        thread = self.turn()
        reply = self.desk.handle({"op": "call", "name": "fly", "seconds": 0.05})
        self.assertEqual(reply["type"], "error")
        self.assertIn("You have no tool fly now. Your tools: files__read_file.", reply["message"])
        self.assertIsNotNone(self.desk.turn)
        self.desk.handle({"op": "call", "name": "files__read_file", "arguments": {"path": "a"}, "seconds": 0.05})
        thread.join(2)
        self.assertEqual(self.replies, [{"call": {"name": "files__read_file", "arguments": {"path": "a"}}}])

    def testAnEmptyAnswerIsRefusedSoNoEmptyResultIsSaved(self):
        thread = self.turn()
        self.assertIn("Write the answer", self.desk.handle({"op": "answer", "text": "  "})["message"])
        self.desk.handle({"op": "answer", "text": "Done", "seconds": 0.05})
        thread.join(2)
        self.assertEqual(self.replies, [{"answer": "Done"}])

    def testACardTakesADecisionAndNotACall(self):
        thread = self.turn(kind="card", tools=())
        self.assertIn("A card waits", self.desk.handle({"op": "call", "name": "x"})["message"])
        self.desk.handle({"op": "decide", "decision": "deny", "why": "not now", "seconds": 0.05})
        thread.join(2)
        self.assertEqual(self.replies, [{"decision": "deny", "message": "not now"}])

    def testEachToolIsToldOnceAndAWheelThatComesIsToldToo(self):
        first = self.turn()
        self.assertEqual([tool["name"] for tool in self.desk.view()["turn"]["fresh"]], ["files__read_file"])
        self.desk.handle({"op": "answer", "text": "x", "seconds": 0.05})
        first.join(2)
        second = self.turn(tools=("files__read_file", "wheels__run_tests"))
        self.assertEqual([tool["name"] for tool in self.desk.view()["turn"]["fresh"]], ["wheels__run_tests"])
        self.desk.handle({"op": "answer", "text": "x", "seconds": 0.05})
        second.join(2)

    def testAStopEndsTheTurnThatWaits(self):
        thread = self.turn()
        Driver(self.desk, "Agent").cancel()
        thread.join(2)
        self.assertEqual(self.replies, [{}])

    def testTheDriverIsItsOwnArchitectOnlyWithItsDesk(self):
        swarm = Swarm("A mission")
        self.assertEqual(pickArchitect(swarm, {"driver": True}), (None, ""))
        swarm.driver = self.desk
        info, how = pickArchitect(swarm, {"driver": True})
        self.assertEqual((how, info["user"]), ("driver", True))


# The real program: a daemon, its commands and its files.
class SoloProgramTests(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        self.folder = self.files / "project"
        self.folder.mkdir()
        (self.folder / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (self.folder / "test_calc.py").write_text(TEST_FILE, encoding="utf-8")
        self.environment = {**os.environ, "SWARMUP_HOME": str(self.files / "home"), "NO_COLOR": "1"}
        self.addCleanup(lambda: self.command("kill", "--all"))

    def command(self, *arguments, check=True):
        done = subprocess.run([sys.executable, str(ENTRY), *arguments], capture_output=True, text=True, env=self.environment, stdin=subprocess.DEVNULL, timeout=120, cwd=self.folder)
        if check and done.returncode:
            self.fail(f"{arguments} failed: {done.stdout}\n{done.stderr}")
        return done.stdout + done.stderr

    def solo(self, *arguments, check=True):
        return self.command("solo", *arguments, check=check)

    def start(self, approvals="auto", mission="Fix calc.py so the tests pass"):
        return self.solo("start", mission, "--folder", str(self.folder), "--approvals", approvals)

    def testTheApprovalsAreForTheUserToChoose(self):
        text = self.solo("start", "Fix it", "--folder", str(self.folder), check=False)
        self.assertIn("is for the user to choose, so ask them", text)
        for mode in ("auto:", "model:", "manual:"):
            self.assertIn(mode, text)
        self.assertIn("No session runs", self.command("list"))

    def testAMissionNeedsAFolderThatExists(self):
        self.assertIn("There is no folder", self.solo("start", "Fix it", "--folder", str(self.files / "nothing"), "--approvals", "auto", check=False))

    def testTheModelWorksFailsFixesAndItsResultIsSaved(self):
        first = self.start()
        self.assertIn("You work alone", first)
        self.assertIn("files__read_file", first)
        self.assertNotIn("team__send_message", first)
        self.assertNotIn("user__ask_user", first)
        self.assertIn("FAILED", self.solo("call", "s1", "processes__run_command", *RUN_TESTS))
        self.assertIn("Done: change calc.py", self.solo("call", "s1", "files__edit_file", '{"path": "calc.py", "old_text": "def add(a, b):\\n    return a - b", "new_text": "def add(a, b):\\n    return a * b", "reason": "try"}'))
        self.assertIn("FAILED", self.solo("call", "s1", "processes__run_command", *RUN_TESTS))
        self.solo("call", "s1", "files__edit_file", "path=calc.py", "old_text=a * b", "new_text=a + b", "reason=fix")
        self.assertIn("OK", self.solo("call", "s1", "processes__run_command", *RUN_TESTS))
        self.assertIn("You have no tool fly now", self.solo("call", "s1", "fly", check=False))
        self.assertIn("Write the answer", self.solo("answer", "s1", " ", check=False))
        over = self.solo("answer", "s1", "Fixed add.")
        self.assertIn("The round is over. Fixed add.", over)
        self.assertEqual(next(self.folder.glob("swarmup-results/*/Agent_result.md")).read_text(encoding="utf-8").strip(), "Fixed add.")
        calls = self.command("calls", "1")
        self.assertIn("processes__run_command", calls)
        self.assertIn("files__edit_file", calls)

    def testACardComesBackToTheModelWhenTheUserChoseIt(self):
        self.start("model")
        card = self.solo("call", "s1", "processes__run_command", "command=echo hello", "reason=say it")
        self.assertIn("wants to run a command", card)
        self.assertIn("Decide with: solo approve s1", card)
        self.assertIn("A card waits", self.solo("call", "s1", "files__read_file", "path=calc.py", check=False))
        self.assertIn("hello", self.solo("approve", "s1"))
        refused = self.solo("call", "s1", "files__write_file", "path=new.txt", "content=x", "reason=try")
        self.assertIn("wants to", refused)
        self.assertIn("not now", self.solo("deny", "s1", "not", "now"))
        self.assertFalse((self.folder / "new.txt").exists())
        self.solo("call", "s1", "processes__run_command", "command=echo again", "reason=say it")
        self.assertIn("again", self.solo("always", "s1"))
        self.assertIn("again", self.solo("call", "s1", "processes__run_command", "command=echo again", "reason=say it"))

    def testACardWaitsForAPersonWhenTheUserChoseIt(self):
        self.start("manual")
        waiting = self.solo("call", "s1", "processes__run_command", "command=echo hello", "reason=say it")
        self.assertIn("A person must answer in another terminal", waiting)
        self.assertIn("A person must answer first", self.solo("call", "s1", "files__read_file", "path=calc.py", check=False))
        status = json.loads(self.command("status", "s1", "--json"))
        self.assertIn("Type yes to allow it", status["question"]["text"])
        self.command("send", "s1", "yes", "--question", str(status["question"]["id"]))
        self.assertIn("hello", self.solo("wait", "s1"))

    def testPlanModeOnlyLooksThenTheNextRoundDoesIt(self):
        self.solo("start", "Fix calc.py", "--folder", str(self.folder), "--approvals", "auto", "--plan")
        self.assertNotIn("files__write_file", self.solo("status", "s1"))
        self.assertIn("You have no tool processes__run_command", self.solo("call", "s1", "processes__run_command", *RUN_TESTS, check=False))
        self.assertIn("round is over", self.solo("answer", "s1", "Plan: change a - b into a + b."))
        self.assertIn("a - b", (self.folder / "calc.py").read_text(encoding="utf-8"))
        doing = self.solo("next", "s1", "--execute")
        self.assertIn("files__edit_file", doing + self.solo("tools", "s1"))
        self.solo("call", "s1", "files__edit_file", "path=calc.py", "old_text=a - b", "new_text=a + b", "reason=the plan")
        self.assertIn("a + b", (self.folder / "calc.py").read_text(encoding="utf-8"))

    # What the plan promises: the model repeats its work, is asked as its own architect, writes a wheel, and has it in the next round and the next mission.
    def testTheModelRepeatsBecomesItsArchitectAndGetsItsWheel(self):
        self.start()
        reply = ""
        for number in range(1, 7):
            for repeat in range(2):
                self.solo("call", "s1", "files__read_file", "path=calc.py")
                self.solo("call", "s1", "files__read_file", "path=test_calc.py")
                self.solo("call", "s1", "processes__run_command", *RUN_TESTS)
            reply = self.solo("answer", "s1", f"Round {number}: done.")
            if "You are now the architect" in reply:
                break
            self.assertIn("The round is over", reply)
            self.solo("next", "s1", f"Round {number + 1}: run the tests again.")
        self.assertIn("You are now the architect", reply)
        self.assertIn("REPEATING", reply)
        self.assertIn("architect__save_wheel", reply)
        self.assertNotIn("files__write_file", reply)
        command = next(line.split()[0] for line in reply.splitlines() if line.startswith("#") and "python -m unittest" in line and "command" in line)
        path = self.files / "wheel.json"
        path.write_text(json.dumps({**WHEEL, "covers": [command]}), encoding="utf-8")
        self.assertIn("It passes the checks", self.solo("call", "s1", "architect__check_wheel", "--args-file", str(path)))
        saved = self.solo("call", "s1", "architect__save_wheel", "--args-file", str(path))
        self.assertIn("Saved run_unit_tests v1", saved)
        self.assertEqual(saved.count("You are now the architect"), 0)
        self.assertIn("Its shape", self.solo("call", "s1", "architect__read_pattern", f"id={command.lstrip('#')}"))
        over = self.solo("answer", "s1", "Saved run_unit_tests.")
        self.assertIn("Pass 1 (done): Saved run_unit_tests", over)
        self.assertIn("The round is over", over)
        following = self.solo("next", "s1", "Check the tests again.")
        self.assertIn("wheels__run_unit_tests", following)
        self.assertIn("You have the wheels of your project", following)
        self.assertIn('"passed": false', self.solo("call", "s1", "wheels__run_unit_tests"))
        self.solo("close", "s1")
        self.assertIn("wheels__run_unit_tests", self.start(mission="A new mission in the same project"))
        self.assertIn('"passed": false', self.solo("call", "s1", "wheels__run_unit_tests"))
        self.assertIn("run_unit_tests", self.command("wheels"))
        self.assertIn("python -m unittest", self.command("patterns", str(self.folder)))

    def testAnInterruptedMissionGoesOnWithItsDesk(self):
        self.start("model")
        self.solo("call", "s1", "files__read_file", "path=calc.py")
        self.solo("close", "s1")
        history = json.loads(self.command("history", "--json"))
        self.assertEqual(len(history), 1)
        opened = self.solo("start", "--open", history[0]["id"])
        self.assertIn("Session s1", opened)
        self.assertIn("files__read_file", opened + self.solo("tools", "s1"))
        self.assertIn("wants to run a command", self.solo("call", "s1", "processes__run_command", "command=echo back", "reason=test"))
        self.assertIn("back", self.solo("approve", "s1"))
        self.assertIn("round is over", self.solo("answer", "s1", "Finished after the stop."))

    # The MCP surface: real tools with their schemas, and the client is told when they change.
    def testTheModelWorksThroughMcpWithRealTools(self):
        asyncio.run(self.driveMcp())

    async def driveMcp(self):
        changes = []
        async def onMessage(message):
            if isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
                changes.append(True)
        parameters = StdioServerParameters(command=sys.executable, args=[str(ENTRY), "solo-mcp"], env=self.environment, cwd=str(self.folder))
        async with stdio_client(parameters) as (reading, writing):
            async with ClientSession(reading, writing, message_handler=onMessage) as session:
                await session.initialize()
                names = [tool.name for tool in (await session.list_tools()).tools]
                self.assertIn("solo_start", names)
                self.assertNotIn("files__read_file", names)
                refused = await session.call_tool("solo_start", {"mission": "Fix calc.py"})
                self.assertTrue(refused.isError)
                self.assertIn("Ask your user", refused.content[0].text)
                started = await session.call_tool("solo_start", {"mission": "Fix calc.py", "folder": str(self.folder), "approvals": "auto"})
                self.assertFalse(started.isError, started.content[0].text)
                self.assertIn("You work alone", started.content[0].text)
                self.assertNotIn("solo call", started.content[0].text)
                self.assertIn("give your answer with solo_answer", started.content[0].text)
                self.assertTrue(changes, "the client must hear that the tools changed")
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertIn("path", tools["files__read_file"].inputSchema["properties"])
                self.assertNotIn("team__send_message", tools)
                read = await session.call_tool("files__read_file", {"path": "calc.py"})
                self.assertIn("return a - b", read.content[0].text)
                edit = await session.call_tool("files__edit_file", {"path": "calc.py", "old_text": "a - b", "new_text": "a + b", "reason": "fix"})
                self.assertIn("Done: change calc.py", edit.content[0].text)
                ran = await session.call_tool("processes__run_command", {"command": "python -m unittest -q", "reason": "tests"})
                self.assertIn("OK", ran.content[0].text)
                wrong = await session.call_tool("fly", {})
                self.assertTrue(wrong.isError)
                over = await session.call_tool("solo_answer", {"text": "Fixed add."})
                self.assertIn("The round is over. Fixed add.", over.content[0].text)
                self.assertIn("a + b", (self.folder / "calc.py").read_text(encoding="utf-8"))
                closed = await session.call_tool("solo_close", {})
                self.assertIn("is closed", closed.content[0].text)


if __name__ == "__main__":
    unittest.main()
