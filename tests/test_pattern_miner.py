import argparse
import json
import sys
import tempfile
import time
import unittest
from patching import everywhere
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import harness_utils
import miner_service
import pattern_store
from agent_journal import Journal
from cli_patterns import addPatternArguments, runPatterns, runWheels
from miner_service import LOCK_FILE, MINERS, MinerService, ProjectLock
from pattern_digest import renderBrief, renderPattern, renderTable, searchJournal
from pattern_errors import classifyError
from pattern_miner import Miner, forgetMission
from pattern_registry import findNotes, saveNote
from pattern_sequences import AgentTrail
from pattern_shapes import fingerprint, maskText, shapeCall, shapeCommand, shapePath, shapeUrl
from pattern_tail import readNew
from pattern_text import intentsOf, locationsOf, similarity, sketchOf
from project_identity import projectDir, projectFor, registerMission
from saved_swarms import saveSwarmState
from test_saved_swarms import waitUntil
from wheel_store import saveWheel


class ShapeTests(unittest.TestCase):
    def testWrappersAreTakenAwayAndTheCommandKeepsItsProgramSubcommandAndFlags(self):
        cases = {
            "sudo -E env FOO=1 timeout 30 npm run build -- --prod": ("npm run build", "npm run build -- --prod"),
            "conda run -n cu130 python -m unittest discover -s tests": ("python -m unittest discover", "python -m unittest discover -s <arg>"),
            "bash -lc \"cd /p/app && poetry run pytest tests/test_api.py -q\"": ("pytest", "pytest -q tests/test_<name>.py"),
            "npx tsc --noEmit": ("tsc", "tsc --noEmit"),
            "git commit -m \"fix 12 bugs\"": ("git commit", "git commit -m <arg>"),
            "docker compose up -d web": ("docker compose up", "docker compose up -d <arg>"),
            "python -m pip install requests==2.31": ("python -m pip install", "python -m pip install <arg>"),
        }
        for command, (family, template) in cases.items():
            exact, l1, l2, programs = shapeCommand(command, "/p/app")
            self.assertEqual((l2, l1), (family, template), command)

    def testRedirectionsAndPipesKeepTheShapeOfEachCommand(self):
        exact, l1, l2, programs = shapeCommand("swift build -c release 2>&1 | tail -20 > build.log", "/p/app")
        self.assertEqual(programs, ["swift build", "tail"])
        self.assertEqual(l2, "swift build | tail")
        self.assertEqual(shapeCommand("python -c 'print(1)'")[1], "python -c <code>")
        self.assertNotEqual(shapeCommand("python -c 'print(1)'")[0], shapeCommand("python -c 'print(2)'")[0])

    def testPathsAndAddressesAreMadeRelativeAndTheirChangingPartsMasked(self):
        self.assertEqual(shapePath("/p/app/src/mod/a1.py", "/p/app"), ("src/mod/a1.py", "src/mod/<name>.py", "source.py"))
        self.assertEqual(shapePath("tests/test_api.py")[1], "tests/test_<name>.py")
        self.assertEqual(shapeUrl("https://github.com/user/repo/issues/1234?x=1&a=2")[1:], ("github.com/user/repo/issues/<id>?a&x", "github.com"))
        self.assertEqual(maskText("at 0xdeadbeef1 on 2026-10-01 12:00 value \"abc\" 42"), "at <hex> on <date> value <str> #")

    def testTwoCallsOfTheSameStepHaveTheSameTemplateAndNotTheSameExactShape(self):
        first = shapeCall("files", "read_file", {"path": "/p/app/src/a.py", "start_line": 10}, "/p/app")
        second = shapeCall("files", "read_file", {"path": "/p/app/src/b.py", "start_line": 80}, "/p/app")
        self.assertEqual(first[1], second[1])
        self.assertNotEqual(first[0], second[0])
        click = shapeCall("computer", "click", {"x": 1210, "y": 84})
        self.assertEqual(click[1], "computer__click(x=#, y=#)")
        self.assertEqual(len(fingerprint("a", 1)), 20)


class ErrorTests(unittest.TestCase):
    CORPUS = [
        ("Traceback (most recent call last):\n  File \"/p/run.py\", line 3, in <module>\n    import requests\nModuleNotFoundError: No module named 'requests.adapters'",
         "python", "py:ModuleNotFoundError"),
        ("src/a.ts(3,5): error TS2345: Argument of type \"string\" is not assignable", "compile", "tsc:TS2345"),
        ("error[E0308]: mismatched types", "compile", "rustc:E0308"),
        ("/p/App.swift:12:5: error: cannot find 'x' in scope", "compile", "swiftc"),
        ("main.c:4:10: fatal error: stdio2.h: No such file or directory", "compile", "cc"),
        ("FAILED tests/test_a.py::test_b - AssertionError: assert 1 == 2", "test", "pytest"),
        ("FAIL: test_x (test_mod.Tests.test_x)", "test", "unittest"),
        ("--- FAIL: TestParse (0.00s)", "test", "go-test"),
        ("npm ERR! code ERESOLVE", "package", "npm:ERESOLVE"),
        ("ERROR: No matching distribution found for torchx", "package", "pip:not-found"),
        ("bash: foo: command not found", "missing", "command"),
        ("Error: Cannot find module 'express'", "missing", "node:module"),
        ("Error: listen EADDRINUSE: address already in use :::3000", "port", "in-use"),
        ("curl: (6) Could not resolve host: example.invalid", "network", "dns"),
        ("urllib.error.HTTPError: HTTP Error 404: Not Found", "http", "http:404"),
        ("CONFLICT (content): Merge conflict in a.py", "git", "conflict"),
        ("fatal: not a git repository (or any of the parent directories): .git", "git", "not-repository"),
        ("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", "memory", "gpu"),
        ("src/a.py:3:1: F401 'os' imported but unused", "lint", "ruff:F401"),
        ("Not done: The user refused to let you run a command. Go on without it", "swarmup", "swarmup:denied"),
        ("Not done: there is no tool files__write for you now.", "swarmup", "swarmup:unknown-tool"),
        ("Not done: old_text is not in a.py. Read the file again", "swarmup", "swarmup:old-text"),
        ("Not done: processes__run_command took more than 120 seconds and was stopped.", "swarmup", "swarmup:time-limit"),
        ("The command failed. Exit code 2.\nsomething odd happened", "exit", "exit:2"),
    ]

    def testEachFamilyOfErrorsIsRecognised(self):
        for text, family, code in self.CORPUS:
            found = classifyError(text)
            self.assertEqual((found["family"], found["code"]), (family, code), text)

    def testTheTemplateOfAnErrorMasksWhatChanges(self):
        first = classifyError("FAILED tests/test_a.py::test_b - AssertionError: assert 1 == 2")
        second = classifyError("FAILED tests/test_c.py::test_d - AssertionError: assert 7 == 9")
        self.assertEqual(first["template"], second["template"])
        self.assertIsNone(classifyError("Everything went well.", failed=False))
        self.assertEqual(classifyError("Traceback (most recent call last):\n  File \"/p/x.py\", line 1, in main\nValueError: bad 3")["where"], "x.py:main")


class TextTests(unittest.TestCase):
    LOGIN = ("Looking at the screenshot of the login page, the username field is at the top of the form and the password field is below it. "
             "The login button is blue and sits under the password field, so I will click the login button at (1210, 84) to sign in.")

    def testNearTextsAreNearAndOthersAreNot(self):
        other = ("Looking at the screenshot of the login page, the user name field is at the top of the form and the password field is below it. "
                 "The login button is blue and sits under the password field, so I click the login button at (1212, 86) to sign in now.")
        unrelated = "The test suite failed because the configuration file of the database is missing a port number and the host name of the server."
        self.assertGreaterEqual(similarity(sketchOf(self.LOGIN), sketchOf(other)), 0.6)
        self.assertLess(similarity(sketchOf(self.LOGIN), sketchOf(unrelated)), 0.2)
        self.assertEqual(sketchOf("Too short to count."), [])

    def testIntentsAndPlacesOnAScreenAreFound(self):
        self.assertTrue(any("find the test command" in intent for intent in intentsOf("First, I need to find the test command of this project. Then run it.")))
        self.assertEqual(locationsOf(self.LOGIN), [("button", 1210, 84)])


class SequenceTests(unittest.TestCase):
    def testStepsInOrderGiveSequencesAndALoopIsToldOnce(self):
        trail = AgentTrail()
        trail.add("read a", "read", True)
        found = trail.add("run tests", "run", True)
        self.assertEqual([gram for gram, fp in found["grams"]], [["read a", "run tests"]])
        trail.add("edit", "edit", False, error="e1")
        trail.add("edit", "edit", False, error="e1")
        self.assertIsNotNone(trail.add("edit", "edit", False, error="e1")["loop"])
        self.assertIsNone(trail.add("edit", "edit", False, error="e1")["loop"])

    def testTheFixOfAnErrorIsWhatLedToTheSameStepWorking(self):
        trail = AgentTrail()
        trail.add("python run.py", "python", False, error="missing")
        trail.add("pip install <arg>", "pip install", True)
        found = trail.add("python run.py", "python", True)
        self.assertEqual(found["fix"], ("missing", ["pip install <arg>", "python run.py"]))
        restored = AgentTrail(json.loads(json.dumps(trail.toData())))
        self.assertEqual(restored.shapes, trail.shapes)


class MinerTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        (self.files / "agent-files").mkdir()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.files / "agent-files")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = self.files / "app"
        self.work.mkdir()
        self.project, self.root = projectFor(self.work)
        self.journals = {}
        self.miners = []

    def journalOf(self, mission, project=None, root=None):
        if mission not in self.journals:
            journal = Journal(self.files / "agent-files" / "mission-specific-memory" / mission / ".swarmup", mission, project or self.project, "run")
            registerMission(project or self.project, root or self.root, mission, journal.path)
            self.journals[mission] = journal
        return self.journals[mission]

    def miner(self, project=None):
        miner = Miner(project or self.project)
        self.miners.append(miner)
        self.addCleanup(miner.close)
        return miner

    def call(self, mission, agent, tool, args, outcome="success", result="ok", server=None, seconds=1.0, size=None, number=1):
        server = server or {"run_command": "processes", "read_file": "files", "fetch_page": "web", "click": "computer"}.get(tool, "files")
        self.journalOf(mission).add("call", agent, id=f"c{number}", origin="conversation", server=server, tool=tool, args=args, outcome=outcome, seconds=seconds,
                                     size=size if size is not None else len(result), result=result)

    # One request of an agent that meets a missing module, installs it, and runs again: the habit of a project.
    def habit(self, mission, agent="Coder"):
        journal = self.journalOf(mission)
        journal.add("request", agent, prompt="Fix the failing tests of the project, please")
        text = ("I need to find which test command this project uses. Let me look at the folder and the configuration files first, then run the unit "
                "tests with python -m unittest to see what fails before I change anything in the code.")
        journal.add("step", agent, n=1, text=text, length=len(text), tokens={"in": 100, "out": 300}, calls=[{"id": "c1", "name": "processes__run_command",
                                                                                                     "args": {"command": "python -m unittest discover -s tests"}}])
        self.call(mission, agent, "run_command", {"command": f"cd {self.work} && python -m unittest discover -s tests"}, "error",
                  "The command failed. Exit code 1.\nTraceback (most recent call last):\n  File \"x.py\", line 1, in <module>\nModuleNotFoundError: No module named 'requests'")
        self.call(mission, agent, "run_command", {"command": "pip install requests"})
        self.call(mission, agent, "run_command", {"command": f"cd {self.work} && python -m unittest discover -s tests"}, result="OK")
        self.call(mission, agent, "read_file", {"path": str(self.work / "src/app.py")}, size=5000)


class MinerTests(MinerTestCase):
    def rowOf(self, miner, kind, contains=""):
        return next(row for row in miner.store.patterns((kind,), limit=100) if contains in row["label"])

    def testAPatternIsNewThenEmergingThenRepeatingAndIsCountedByAgentAndMission(self):
        miner = self.miner()
        self.habit("m1")
        miner.poll()
        self.assertEqual(self.rowOf(miner, "fix")["status"], "new")
        self.habit("m1", "Tester")
        miner.poll()
        self.assertEqual(self.rowOf(miner, "fix")["status"], "emerging")
        self.habit("m2")
        miner.poll()
        fix = self.rowOf(miner, "fix")
        self.assertEqual((fix["status"], fix["count"], fix["agents"], fix["missions"], fix["requests"]), ("repeating", 3, 2, 2, 3))
        self.assertEqual({item["value"]: item["count"] for item in miner.store.spreadOf(fix["id"], "agent")}, {"Coder": 2, "Tester": 1})
        self.assertIn("fixRecipe", fix["data"]["hints"])
        command = self.rowOf(miner, "command", "unittest")
        self.assertEqual(command["count"], 6)
        self.assertIn("deterministic", command["data"]["hints"])
        error = self.rowOf(miner, "error", "ModuleNotFoundError")
        self.assertEqual(error["data"]["where"], "x.py:<module>")
        self.assertTrue(any(link["kind"] == "fixedBy" for link in miner.store.linksOf(error["id"])))
        self.assertEqual(self.rowOf(miner, "reread", "src/app.py")["count"], 3)
        self.assertEqual(self.rowOf(miner, "reasoning")["count"], 3)

    def testEachEntryIsCountedOnceAcrossPolls(self):
        miner = self.miner()
        self.habit("m1")
        first = miner.poll()
        self.assertGreater(first, 0)
        self.assertEqual(miner.poll(), 0)
        count = self.rowOf(miner, "command", "pip")["count"]
        again = Miner(self.project)
        self.addCleanup(again.close)
        self.assertEqual(again.poll(), 0)
        self.habit("m1")
        miner.poll()
        self.assertEqual(self.rowOf(miner, "command", "pip")["count"], count + 1)

    def testAHalfWrittenLineWaitsBadLinesAreSkippedAndAShorterJournalIsReadAgain(self):
        miner = self.miner()
        journal = self.journalOf("m1")
        self.call("m1", "A", "read_file", {"path": "a.py"})
        with open(journal.path, "a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write('{"kind": "call", "agent": "A", "server": "files", "tool": "read_fi')
        self.assertEqual(miner.poll(), 2)
        self.assertEqual(miner.store.sources()[str(journal.path)]["bad"], 1)
        with open(journal.path, "a", encoding="utf-8") as handle:
            handle.write('le", "args": {"path": "b.py"}, "outcome": "success", "mission": "m1"}\n')
        self.assertEqual(miner.poll(), 1)
        self.assertEqual(self.rowOf(miner, "action", "read_file")["count"], 2)
        journal.path.write_text("", encoding="utf-8")
        self.call("m1", "A", "read_file", {"path": "c.py"})
        self.assertEqual(miner.poll(), 1)
        self.assertEqual(lines := len(journal.path.read_text().splitlines()), 1, lines)
        lines, offset, inode, size, restarted = readNew(journal.path, 10 ** 6)
        self.assertTrue(restarted)

    # The login page again and again: the same reasoning, then a click at nearly the same place. A steps wheel would do it.
    def testTheSameReasoningThenTheSameActionIsADecisionWithStableNumbers(self):
        miner = self.miner()
        for number, (x, y) in enumerate([(1210, 84), (1212, 85), (1209, 83)]):
            journal = self.journalOf("m1")
            journal.add("request", "Tester", prompt="Check that the login works")
            text = ("Looking at the screenshot of the login page, the username field is at the top of the form and the password field is below it. "
                    f"The login button is blue and sits under the password field, so I will click the login button at ({x}, {y}) to sign in.")
            journal.add("step", "Tester", n=1, text=text, length=len(text), tokens={"in": 2000, "out": 400}, calls=[{"id": "k", "name": "computer__click", "args": {"x": x, "y": y}}])
            self.call("m1", "Tester", "click", {"x": x, "y": y}, server="computer")
        miner.poll()
        decision = self.rowOf(miner, "decision")
        self.assertEqual((decision["status"], decision["count"]), ("repeating", 3))
        self.assertIn("stableArgs", decision["data"]["hints"])
        self.assertIn("toolOnly", decision["data"]["hints"])
        self.assertEqual(decision["data"]["numbers"]["x"][:2], [1209, 1212])

    def testTheOtherKindsOfPatterns(self):
        miner = self.miner()
        journal = self.journalOf("m1")
        for number in range(3):
            journal.add("request", "W", prompt="Write the summary")
            journal.add("bad", "W", id=f"b{number}", name="files__write_file", why="cut", problem="your answer was cut")
            journal.add("permission", "W", key="run:make test", action="run a command", detail="make test", decision="run", how="asked")
            journal.add("draft", "W", attempt=1, problem="The draft is longer than 150 words: shorten it.", length=900)
            journal.add("user", "W", to="W", text="Please always use British spelling in every document you write for me.", whileReady=True)
            journal.add("route", "Leader", comment="Use British spelling everywhere, the user insists on it for every document.", route={"W": "fix it"})
            journal.add("message", "B", **{"from": "A", "to": "B", "text": "Which file do you own? I need to change the configuration of the build first.",
                                           "direct": True})
            journal.add("status", "W", status="failed", error="The command failed. Exit code 1.\nnpm ERR! code ERESOLVE")
            journal.add("wheel", "W", name="run_tests", pin="run_tests@abcd1234", action="run", ok=number != 1,
                        error={"type": "TimeoutExpired", "message": "too slow"} if number == 1 else None, seconds=2)
        miner.poll()
        kinds = {row["kind"] for row in miner.store.patterns(limit=200)} | {row["kind"] for row in miner.store.patterns(("wheel",), limit=10)}
        self.assertTrue({"badcall", "permission", "correction", "message", "error", "wheel", "wheelfail"} <= kinds, kinds)
        sources = {row["data"]["source"] for row in miner.store.patterns(("correction",), limit=10)}
        self.assertEqual(sources, {"check", "user", "leader"})
        wheel = miner.store.patterns(("wheel",), limit=1)[0]
        self.assertEqual((wheel["count"], wheel["errors"]), (3, 1))

    def testTheCommandsACodingAgentRanItselfAreMinedLikeTheOthers(self):
        miner = self.miner()
        journal = self.journalOf("m1")
        for number in range(3):
            journal.add("request", "Codex", prompt="Build it")
            journal.add("cmd", "Codex", command="make test", exit=2, output="make: *** [test] Error 2", seconds=1.5)
        miner.poll()
        command = self.rowOf(miner, "command", "make test")
        self.assertEqual((command["count"], command["errors"], command["status"]), (3, 3, "repeating"))

    def testTheWorkOfAWheelDoneByHandIsARedo(self):
        miner = self.miner()
        self.habit("m1")
        miner.poll()
        pip = self.rowOf(miner, "command", "pip install")
        saveWheel("install_missing", "script", "WHEEL = {}\n", "Installs the module that is missing, then runs the tests again.", [pip["fp"]], "project", self.project, {})
        journal = self.journalOf("m2")
        journal.add("run", agents=[{"name": "Coder", "wheels": ["install_missing"]}, {"name": "Other", "wheels": []}])
        self.call("m2", "Coder", "run_command", {"command": "pip install requests"})
        self.call("m2", "Other", "run_command", {"command": "pip install requests"})
        miner.poll()
        redos = {row["data"]["how"]: row["count"] for row in miner.store.patterns(("redo",), limit=10)}
        self.assertEqual(redos, {"ignored": 1, "missed": 1})
        # A wheel worked around by an agent that had it is a wheel to fix at once; a wheel nobody gave waits for a second time.
        signalled = {row["data"].get("how") for signal in miner.store.pendingSignals() for row in [miner.store.pattern(signal["pattern"])] if row["kind"] == "redo"}
        self.assertEqual(signalled, {"ignored"})
        self.assertEqual(self.rowOf(miner, "command", "pip install")["status"], "covered")

    def testOnlyUsefulPatternsSignalTheArchitectOnceWhenTheyStartToRepeat(self):
        miner = self.miner()
        for mission in ("m1", "m1", "m2"):
            self.habit(mission)
        miner.poll()
        signals = miner.store.pendingSignals()
        kinds = {signal["patternKind"] for signal in signals}
        self.assertIn("fix", kinds)
        self.assertNotIn("intent", kinds)
        self.assertNotIn("action", kinds)
        self.assertEqual(len(signals), len({signal["pattern"] for signal in signals}))
        miner.store.consumeSignals(signals[-1]["id"])
        self.habit("m2")
        miner.poll()
        self.assertEqual(miner.store.pendingSignals(), [])

    def testTwoArchitectsNeverClaimTheSamePattern(self):
        miner = self.miner()
        self.habit("m1")
        miner.poll()
        ids = [row["id"] for row in miner.store.patterns(limit=5)]
        self.assertEqual(miner.store.claim(ids, "one"), ids)
        self.assertEqual(miner.store.claim(ids, "two"), [])
        miner.store.release("one")
        self.assertEqual(miner.store.claim(ids[:2], "two"), ids[:2])

    # A step met a little in two projects repeats in fact: its patterns are cross, and a note of an architect of the other project is found.
    def testWhatRepeatsAcrossProjectsIsCrossAndTheNotesOfOtherProjectsMatch(self):
        other = self.files / "other"
        other.mkdir()
        otherProject, otherRoot = projectFor(other)
        first = self.miner(otherProject)
        for mission in ("o1", "o2"):
            journal = self.journalOf(mission, otherProject, otherRoot)
            journal.add("request", "A", prompt="Run the linter")
            self.call(mission, "A", "run_command", {"command": "ruff check --fix ."}, number=1)
        first.poll()
        row = next(row for row in first.store.patterns(("command",), limit=5) if "ruff" in row["label"])
        saveNote(otherProject, "other", "o2", [row], "Agents run ruff check --fix before every commit; worth a wheel when it shows up in more projects.")
        second = self.miner()
        self.journalOf("m1").add("request", "B", prompt="Run the linter")
        self.call("m1", "B", "run_command", {"command": "ruff check --fix ."})
        second.poll()
        mine = next(row for row in second.store.patterns(("command",), limit=5) if "ruff" in row["label"])
        self.assertEqual(mine["status"], "cross")
        self.assertEqual(mine["data"]["note"], "N1")
        self.assertEqual(mine["data"]["cross"]["projects"], [otherProject])
        self.assertEqual(findNotes("ruff linter")[0]["id"], "N1")

    def testAMissionWithoutAJournalIsReadFromItsOldFilesWhenTheMinerIsRebuilt(self):
        state = {"id": "old1", "mission": "Old", "members": {"Coder": {"role": "coder", "status": "failed", "error": "The command failed. Exit code 1.\nbash: make: command not found",
                                                                       "recipe": {"task": "agent", "answers": {"folder": str(self.work)}}}}}
        saveSwarmState("old1", state)
        folder = self.files / "agent-files" / "mission-specific-memory" / "old1" / ".swarmup"
        (folder / "conversations").mkdir(parents=True)
        (folder / "calls.jsonl").write_text("\n".join(json.dumps({"time": "2026-10-01 10:00:00", "round": 1, "agent": "Coder", "server": "processes", "tool": "run_command",
                                                                   "arguments": {"command": "make test"}, "outcome": "error", "seconds": 1, "size": 10,
                                                                   "note": "bash: make: command not found"}) for _ in range(3)) + "\n", encoding="utf-8")
        (folder / "conversations" / "Coder.json").write_text(json.dumps({"system": "", "delivered": [0, 0], "messages": [
            {"role": "user", "content": "Build it"}, {"role": "assistant", "content": "I will run make test now to build and test the project as usual.", "calls": []}]}), encoding="utf-8")
        miner = self.miner()
        self.assertGreater(miner.rebuild(), 0)
        error = self.rowOf(miner, "error", "after processes__run_command: make test")
        self.assertGreaterEqual(error["count"], 3)

    def testADeletedMissionLeavesNoExampleBehind(self):
        miner = self.miner()
        self.habit("m1")
        miner.poll()
        self.assertTrue(miner.store.query("SELECT * FROM examples WHERE mission='m1'"))
        forgetMission("m1")
        self.assertFalse(miner.store.query("SELECT * FROM examples WHERE mission='m1'"))

    def testTwentyThousandEntriesAreReadQuickly(self):
        miner = self.miner()
        journal = self.journalOf("big")
        for number in range(2500):
            agent = f"A{number % 5}"
            journal.add("request", agent, prompt=f"Task {number % 7}")
            journal.add("step", agent, n=1, text=f"I will read the file number {number % 40} and then run the tests of module {number % 9} to check the change I made.",
                        length=120, calls=[{"id": "x", "name": "files__read_file", "args": {"path": f"src/m{number % 40}.py"}}])
            for tool, args in (("read_file", {"path": f"src/m{number % 40}.py"}), ("run_command", {"command": f"pytest tests/test_m{number % 9}.py -q"}),
                               ("read_file", {"path": "README.md"})):
                journal.add("call", agent, id="x", origin="conversation", server="files" if tool == "read_file" else "processes", tool=tool, args=args,
                            outcome="success" if number % 4 else "error", seconds=0.5, size=3000, result="ok" if number % 4 else "FAILED tests/test_m.py::test_a - AssertionError")
            journal.add("permission", agent, key="change", decision="once", how="asked", action="change a file", detail="src/m.py")
        started = time.time()
        total = sum(iter(miner.poll, 0))
        self.assertEqual(total, 15000)
        self.assertLess(time.time() - started, 90)
        self.assertLess(len(renderBrief(miner.store, self.project)), 12001)


class DigestTests(MinerTestCase):
    def testTheBriefTellsWhatRepeatsTheBestFirstWithinItsBudget(self):
        miner = self.miner()
        for mission in ("m1", "m1", "m2"):
            self.habit(mission)
        miner.poll()
        brief = renderBrief(miner.store, self.project)
        self.assertIn("REPEATING, NOT AUTOMATED YET", brief)
        self.assertIn("ModuleNotFoundError", brief)
        self.assertIn("by Coder", brief)
        self.assertIn("THE WHEELS OF THIS PROJECT:\n- none yet", brief)
        self.assertLessEqual(len(renderBrief(miner.store, self.project, chars=800)), 800)
        table = renderTable(miner.store)
        self.assertIn("#", table)

    def testAPatternIsShownWithExamplesFromTheJournalsMarkedAsData(self):
        miner = self.miner()
        self.habit("m1")
        self.habit("m2")
        miner.poll()
        error = next(row for row in miner.store.patterns(("error",), limit=5))
        shown = renderPattern(miner.store, error["id"])
        self.assertIn("<data from=\"journal\">", shown)
        self.assertIn("[Coder called processes__run_command]", shown)
        self.assertIn("There is no pattern", renderPattern(miner.store, 9999))
        found = searchJournal(self.project, "pip install")
        self.assertIn("pip install requests", found)
        self.assertIn("Nothing in the journals", searchJournal(self.project, "zebra crossing"))


class ServiceTests(MinerTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("POLL_SECONDS", 0.05), ("IDLE_SECONDS", 0.1)):
            patcher = everywhere(miner_service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(MINERS.stopAll)

    def testTheMinerRunsWhileItHasUsersAndTellsItsSubscribers(self):
        received = []
        service = MINERS.acquire(self.project, "swarm1")
        self.assertIs(MINERS.acquire(self.project, "swarm2"), service)
        service.subscribe(received.extend)
        for mission in ("m1", "m1", "m2"):
            self.habit(mission)
        waitUntil(lambda: received, "the signals of the miner")
        self.assertTrue(service.leading)
        self.assertTrue(service.drain(5))
        MINERS.release(self.project, "swarm1")
        self.assertTrue(service.thread.is_alive())
        MINERS.release(self.project, "swarm2")
        self.assertFalse(service.thread.is_alive())
        self.assertIsNone(MINERS.get(self.project))

    # Another program mines the project: this one only reads the store, and mines once the lock is free.
    def testASecondProgramOnlyReadsUntilTheLockIsFree(self):
        self.habit("m1")
        Miner(self.project).close()
        other = ProjectLock(projectDir(self.project) / LOCK_FILE)
        self.assertTrue(other.take())
        service = MinerService(self.project)
        self.assertEqual(service.pollOnce(), 0)
        self.assertFalse(service.leading)
        other.free()
        self.assertGreater(service.pollOnce(), 0)
        self.assertTrue(service.leading)
        service.stopping.set()
        service.miner.close()
        service.projectLock.free()


class CommandTests(MinerTestCase):
    def options(self, *words):
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command")
        addPatternArguments(commands)
        return parser.parse_args(list(words))

    def testTheCommandsShowTheProjectsThePatternsAndTheWheels(self):
        self.assertIn("No project yet", runPatterns(self.options("patterns", "projects")))
        for mission in ("m1", "m2", "m2"):
            self.habit(mission)
        self.assertIn("Read", runPatterns(self.options("patterns", "mine", str(self.work))))
        self.assertIn(self.project, runPatterns(self.options("patterns", "projects")))
        table = runPatterns(self.options("patterns", "list", str(self.work), "--kind", "fix"))
        self.assertIn("fixed by", table)
        self.assertNotIn("command x", table)
        self.assertIn("REPEATING", runPatterns(self.options("patterns", "brief", str(self.work))))
        self.assertIn("pip install", runPatterns(self.options("patterns", "search", "pip", "--project", str(self.work))))
        self.assertIn("Its shape", runPatterns(self.options("patterns", "show", "#1", "--project", str(self.work))))
        self.assertIn("No wheel yet", runWheels(self.options("wheels", "list")))
        self.assertIn("No note", runWheels(self.options("wheels", "notes")))
        with self.assertRaises(ValueError):
            runPatterns(self.options("patterns", "list", "nowhere-project"))


if __name__ == "__main__":
    unittest.main()
