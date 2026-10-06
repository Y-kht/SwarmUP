import json
import os
import queue
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from patching import everywhere
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import full_command_line_user_test as cli
import harness_utils
from base_loop import Loop
from harness_utils import ConnectionLost
from models_library import getModelInfo
from saved_swarms import findUnfinishedSwarms, saveSwarmState, swarmStatePath
from swarm_harness import Swarm
from tasks_library import NO_MESSENGER
from test_harness_utils import DraftLoop, FakeAgent
from test_leader_utils import usePrices
from test_saved_swarms import waitUntil

GPUS = [{"name": "RTX A5000", "total": 25.8, "free": 25.3}, {"name": "RTX A5000", "total": 25.8, "free": 25.8}]


# Plays the user: the answers are given in the order the program asks its questions, and what the program says is kept.
class Script:
    def __init__(self, answers=(), secrets=()):
        self.answers, self.secrets, self.said, self.log = list(answers), list(secrets), [], []

    def read(self, prompt):
        if not self.answers:
            raise AssertionError(f"The program asked something that the test did not expect: {prompt!r}\nSaid so far:\n{self.text()[-1500:]}")
        answer = self.answers.pop(0)
        answer = answer() if callable(answer) else answer
        self.log.append((prompt, answer))
        return answer

    def readSecret(self, prompt):
        if not self.secrets:
            raise AssertionError(f"The program asked a secret that the test did not expect: {prompt!r}")
        self.log.append((prompt, "<hidden>"))
        return self.secrets.pop(0)

    def console(self):
        return cli.Console(self.read, self.readSecret, self.said.append, interactive=False, treeDelay=0)

    def text(self):
        return "\n".join(self.said)

    def prompts(self):
        return [prompt for prompt, answer in self.log]


class GpuTestCase(unittest.TestCase):
    gpus = GPUS

    def setUp(self):
        usePrices(self)
        self.currentGpus = [dict(gpu) for gpu in self.gpus]
        for target in (harness_utils, cli):
            patcher = everywhere(target, "readGpus", lambda: self.currentGpus)
            patcher.start()
            self.addCleanup(patcher.stop)


class ConsoleTests(unittest.TestCase):
    def testQuestionsAreReadFromTheUserAndSecretsAreHidden(self):
        script = Script(["blue"], ["s3cret"])
        console = script.console()
        self.assertEqual(console.ask("Your colour?"), "blue")
        self.assertEqual(console.askSecret("Your password?"), "s3cret")
        self.assertEqual(script.prompts(), ["Your colour? ", "Your password? "])
        console.say("Hello")
        self.assertEqual(script.said, ["Hello"])

    # The thread that reads the keyboard while the swarm runs: a line answers the question that waits, or it is a command.
    def pumped(self):
        lines, said, commands = queue.Queue(), [], []
        def read(prompt):
            line = lines.get(timeout=5)
            if line is None:
                raise EOFError
            return line
        console = cli.Console(read, write=said.append, interactive=True)
        console.commands, console.pumping = commands.append, True
        pump = threading.Thread(target=console.pump, daemon=True)
        pump.start()
        return console, lines, said, commands, pump

    def waitFor(self, condition):
        for attempt in range(500):
            if condition():
                return
            threading.Event().wait(0.01)
        self.fail("Nothing happened in time.")

    def testALineAnswersTheQuestionThatWaitsAndOtherLinesAreCommands(self):
        console, lines, said, commands, pump = self.pumped()
        lines.put("approve Writer")
        answers = []
        asker = threading.Thread(target=lambda: answers.append(console.ask("Do you approve?")), daemon=True)
        self.waitFor(lambda: commands == ["approve Writer"])
        asker.start()
        self.waitFor(lambda: console.pending is not None)
        lines.put("tree")
        self.waitFor(lambda: commands == ["approve Writer", "tree"])
        self.assertTrue(asker.is_alive())
        console.say("some news")
        self.assertEqual(said[-2:], ["some news", "  (the question that waits for you) Do you approve?"])
        lines.put("yes please")
        asker.join(5)
        self.assertEqual(answers, ["yes please"])
        self.assertIsNone(console.pending)
        console.say("later news")
        self.assertEqual(said[-1], "later news")
        console.pumping = False
        lines.put("the line that ends the reader")
        pump.join(5)
        self.assertFalse(pump.is_alive())

    def testAClosedKeyboardAnswersNothingInsteadOfBlocking(self):
        console, lines, said, commands, pump = self.pumped()
        answers = []
        asker = threading.Thread(target=lambda: answers.append(console.ask("Still there?")), daemon=True)
        asker.start()
        self.waitFor(lambda: console.pending is not None)
        lines.put(None)
        asker.join(5)
        pump.join(5)
        self.assertEqual(answers, [""])
        self.assertEqual(console.ask("And now?"), "")

    def testSecretsCannotBeTypedWhileTheSwarmRuns(self):
        console, lines, said, commands, pump = self.pumped()
        self.assertEqual(console.askSecret("Password?"), "")
        self.assertIn("A secret cannot be typed while the swarm runs", said[-1])
        console.pumping = False
        lines.put("x")

    def testQuestionsOfTwoAgentsNeverMixInTheSameMoment(self):
        script = Script(["one", "two"])
        console, results = script.console(), []
        threads = [threading.Thread(target=lambda number=number: results.append((number, console.ask(f"Question {number}?")))) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sorted(answer for number, answer in results), ["one", "two"])
        self.assertEqual(len(script.log), 2)


class MenuTests(unittest.TestCase):
    def testANumberOrTheLabelChoosesAndMistakesAreExplained(self):
        script = Script(["5", "pear", "2"])
        console = script.console()
        self.assertEqual(cli.chooseFrom(console, "Fruit:", ["apple", "pear", "plum"]), "pear")
        self.assertEqual(script.said[:4], ["Fruit:", "  1. apple", "  2. pear", "  3. plum"])
        self.assertIn("Choose numbers between 1 and 3.", script.said)
        script = Script(["pear"])
        self.assertEqual(cli.chooseFrom(script.console(), "Fruit:", ["apple", "pear"]), "pear")

    def testSeveralChoicesADefaultNoneAndExtraCommands(self):
        script = Script(["1,3", "", "none", "info", "2"])
        console = script.console()
        self.assertEqual(cli.chooseFrom(console, "Fruit:", ["apple", "pear", "plum"], one=False), ["apple", "plum"])
        self.assertEqual(cli.chooseFrom(console, "Fruit:", ["apple", "pear", "plum"], one=False, default=["pear"]), ["pear"])
        self.assertEqual(cli.chooseFrom(console, "Fruit:", ["apple", "pear"], one=False, none=True), [])
        handled = []
        extra = lambda text: handled.append(text) or text == "info"
        self.assertEqual(cli.chooseFrom(console, "Fruit:", ["apple", "pear"], extra=extra), "pear")
        self.assertEqual(handled, ["info", "2"])

    def testYesNoQuestions(self):
        script = Script(["maybe", "YES", "", "n", "", "no"])
        console = script.console()
        self.assertTrue(cli.askYesNo(console, "Sure?"))
        self.assertIn("Please answer yes or no.", script.said)
        self.assertTrue(cli.askYesNo(console, "Sure?", True))
        self.assertFalse(cli.askYesNo(console, "Sure?", True))
        self.assertFalse(cli.askYesNo(console, "Sure?", False))
        self.assertEqual(script.prompts()[-1], "Sure? (y/N) ")
        self.assertFalse(cli.askYesNo(console, "Sure?", True))

    def testAQuestionIsAskedAgainUntilTheAnswerIsValid(self):
        script = Script(["zero", "0", "3"])
        parse = lambda text: (int(text), "") if text.isdigit() and int(text) > 0 else (None, "A number above 0, please.")
        self.assertEqual(cli.askUntilValid(script.console(), "How many?", parse), 3)
        self.assertEqual(script.said.count("A number above 0, please."), 2)
        script = Script([], ["", "x"])
        required = lambda text: (text, "") if text else (None, "This answer is needed.")
        self.assertEqual(cli.askUntilValid(script.console(), "Password?", required, secret=True), "x")


class SwarmTestCase(GpuTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = everywhere(harness_utils, "AGENT_FILES", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def makeSwarm(self, mode="execute", local=False):
        swarm = Swarm("Mail the quarterly report to the whole team")
        models = {"Leader": getModelInfo("claude-opus-5-5"), "Writer": getModelInfo("Qwen/Qwen3.5-9B") if local else getModelInfo("gpt-6-luna"),
                  "Formatter": getModelInfo("Qwen/Qwen3.5-4B") if local else None}
        for name, role in (("Leader", "boss"), ("Writer", "writer"), ("Formatter", "formatter")):
            swarm.addAgent(name, Loop(FakeAgent()), role, f"task of {name}", model=models[name])
        swarm.setWaitsFor("Formatter", ["Writer"])
        swarm.setMode(mode)
        return swarm

    def set(self, swarm, name, **fields):
        swarm.members[name].update(fields)


class TreeTests(SwarmTestCase):
    def testTheTreeShowsEveryAgentItsModelItsStateAndWhoWaitsForWhom(self):
        swarm = self.makeSwarm()
        lines = cli.renderTree(swarm).split("\n")
        self.assertEqual(lines[0], "Swarm: Mail the quarterly report to the whole team | mode: execute")
        self.assertEqual(lines[1], "Leader [boss] claude-opus-5-5, API -> waiting for Writer, Formatter")
        self.assertEqual(lines[2], "|-- Writer [writer] gpt-6-luna, API -> waiting")
        self.assertEqual(lines[3], "`-- Formatter [formatter] no model -> waiting for Writer  waits for: Writer (waiting)")
        self.assertEqual(len(lines), 4)

    def testTheStatesOfThePlansAndOfTheResultsAreTold(self):
        swarm = self.makeSwarm(mode="plan")
        self.set(swarm, "Leader", status="working")
        self.set(swarm, "Writer", status="working", review="ready", mode="plan")
        self.set(swarm, "Formatter", status="done", review="approved", mode="plan")
        text = cli.renderTree(swarm)
        self.assertIn("Leader [boss] claude-opus-5-5, API -> waiting", cli.renderTree(self.makeSwarm()))
        self.assertIn("Writer [writer] gpt-6-luna, API -> READY: its plan waits for you", text)
        self.assertIn("Formatter [formatter] no model -> done, plan approved", text)
        swarm = self.makeSwarm(mode="execute")
        self.set(swarm, "Leader", status="working")
        self.set(swarm, "Writer", status="working", review="ready")
        self.set(swarm, "Formatter", status="working", review="approved")
        text = cli.renderTree(swarm)
        self.assertIn("Leader [boss] claude-opus-5-5, API -> working", text)
        self.assertIn("Writer [writer] gpt-6-luna, API -> READY: its result waits for your approval", text)
        self.assertIn("Formatter [formatter] no model -> approved, working", text)
        self.set(swarm, "Formatter", status="failed")
        self.set(swarm, "Writer", status="done", review="approved")
        text = cli.renderTree(swarm)
        self.assertIn("Formatter [formatter] no model -> FAILED  waits for: Writer (done)", text)
        self.assertIn("Writer [writer] gpt-6-luna, API -> done", text)

    def testAnAgentThatWaitsForATimeIsShownWithIt(self):
        swarm = self.makeSwarm()
        self.set(swarm, "Writer", startAt=cli.datetime(2031, 5, 17, 6, 45))
        self.assertIn("Writer [writer] gpt-6-luna, API -> scheduled for 2031-05-17 06:45", cli.renderTree(swarm))

    def testTheVramOfTheLocalModelsIsInTheHeaderAndInTheAgents(self):
        swarm = self.makeSwarm(local=True)
        lines = cli.renderTree(swarm).split("\n")
        self.assertEqual(lines[0], "Swarm: Mail the quarterly report to the whole team | mode: execute | VRAM: 34.6 of 51.6 GB expected, 51.1 GB free now")
        self.assertIn("Writer [writer] Qwen/Qwen3.5-9B, local, 23.3 GB -> waiting", lines[2])
        self.assertIn("Formatter [formatter] Qwen/Qwen3.5-4B, local, 11.3 GB -> waiting for Writer", lines[3])

    def testTheBossesMakeTheBranchesOfTheTree(self):
        swarm = self.makeSwarm()
        swarm.addAgent("Checker", Loop(FakeAgent()), "checker", "check", boss="Writer")
        swarm.addAgent("Mailer", Loop(FakeAgent()), "mailer", "mail")
        lines = cli.renderTree(swarm).split("\n")[1:]
        self.assertEqual([line.split(" [")[0] for line in lines], ["Leader", "|-- Writer", "|   `-- Checker", "|-- Formatter", "`-- Mailer"])

    def testColoursOnlyAppearWhenAsked(self):
        swarm = self.makeSwarm()
        self.set(swarm, "Writer", status="working", review="ready")
        self.assertNotIn("\033[", cli.renderTree(swarm))
        coloured = cli.renderTree(swarm, True)
        self.assertIn("\033[32mREADY: its result waits for your approval\033[0m", coloured)
        self.assertIn("\033[1mLeader\033[0m", coloured)

    def testEveryKindOfEventIsWordedForTheUser(self):
        event = lambda kind, **details: {"kind": kind, "agent": details.pop("agent", ""), "time": "10:20:30", **details}
        self.assertEqual(cli.formatEvent(event("run", mode="plan")), "== The swarm starts in plan mode ==")
        self.assertEqual(cli.formatEvent(event("finished", mode="plan", ok=True)), "== The swarm finished successfully ==")
        self.assertEqual(cli.formatEvent(event("finished", mode="plan", ok=False)), "== The swarm finished without a result ==")
        self.assertEqual(cli.formatEvent(event("status", agent="Writer", status="done")), "[10:20:30] Writer is now done")
        self.assertEqual(cli.formatEvent(event("review", agent="Writer", review="ready")), "[10:20:30] Writer is ready: it waits for you")
        self.assertEqual(cli.formatEvent(event("review", agent="Writer", review="approved")), "[10:20:30] Writer was approved")
        self.assertEqual(cli.formatEvent(event("review", agent="Writer", review="rejected")), "[10:20:30] Writer was rejected")
        self.assertEqual(cli.formatEvent(event("review", agent="Writer", review="")), "[10:20:30] Writer was asked to correct it")
        self.assertEqual(cli.formatEvent(event("message", agent="Writer", sender="Leader", receiver="Writer", message="Do\nthis " + "x" * 200)),
                         "[10:20:30] Leader -> Writer: Do this " + "x" * 79 + "...")
        self.assertEqual(cli.formatEvent(event("scheduled", agent="News", startAt="2031-05-17 06:45")), "[10:20:30] News is scheduled for 2031-05-17 06:45 (type: start News)")
        self.assertIsNone(cli.formatEvent(event("summary", agent="Leader", text="x")))


    def testTheEventsOfALostConnectionAreWordedForTheUser(self):
        event = lambda kind, **details: {"kind": kind, "agent": details.pop("agent", ""), "time": "10:20:30", **details}
        self.assertEqual(cli.formatEvent(event("connectionLost", agent="Writer", reason="gone")), "[10:20:30] Writer lost its connection and is paused")
        self.assertEqual(cli.formatEvent(event("resumed")), "[10:20:30] The swarm goes on")
        self.assertEqual(cli.formatEvent(event("stopped")), "== The swarm was stopped by you ==")

    def testAPausedAgentIsShownWithItsReason(self):
        swarm = self.makeSwarm()
        self.set(swarm, "Writer", status="paused")
        self.assertIn("Writer [writer] gpt-6-luna, API -> PAUSED: the connection was lost", cli.renderTree(swarm))
        self.assertIn("\033[31mPAUSED: the connection was lost\033[0m", cli.renderTree(swarm, True))
        self.assertIn("You can: msg Writer <message>", cli.openAgent(swarm, "Writer"))


class LiveTreeTests(SwarmTestCase):
    def testTheViewPrintsWhatHappensAndTheTreeOnlyWhenItChanged(self):
        script = Script()
        swarm, console = self.makeSwarm(), script.console()
        view = cli.TreeView(swarm, console)
        swarm.addListener(view.onEvent)
        view.flush()
        self.assertEqual(sum(line.startswith("Swarm:") for block in script.said for line in block.split("\n")), 1)
        view.flush()
        self.assertEqual(len(script.said), 1)
        swarm.emit("message", "Writer", sender="Leader", receiver="Writer", message="Hello Writer")
        view.flush()
        self.assertEqual(len(script.said), 2)
        self.assertIn("Leader -> Writer: Hello Writer", script.said[-1])
        self.assertNotIn("Swarm:", script.said[-1])
        self.set(swarm, "Writer", status="working")
        swarm.emit("status", "Writer", status="working")
        view.flush()
        self.assertIn("Writer is now working", script.said[-1])
        self.assertIn("Writer [writer] gpt-6-luna, API -> working", script.said[-1])

    def testTheViewFollowsARealRunInThreads(self):
        script = Script()
        swarm, console = self.makeSwarm(), script.console()
        for name in ("Leader", "Writer", "Formatter"):
            swarm.members[name]["agent"].run = lambda name=name: f"result of {name}"
        view = cli.TreeView(swarm, console)
        view.start()
        self.assertEqual(swarm.run(), "result of Leader")
        view.stop()
        text = script.text()
        for line in ("== The swarm starts in execute mode ==", "Leader -> Writer: Mission of the swarm", "Writer is now working", "Writer is now done",
                     "Formatter is now done", "Writer -> Leader: writer: result of Writer", "== The swarm finished successfully =="):
            self.assertIn(line, text)
        self.assertTrue(any("Leader [boss] claude-opus-5-5, API -> done" in block for block in script.said))
        self.assertTrue(any("Formatter [formatter] no model -> done  waits for: Writer (done)" in block for block in script.said))
        self.assertFalse(view.thread.is_alive())

class CommandTests(SwarmTestCase):
    def run_(self, swarm, line):
        script = Script()
        cli.runCommand(script.console(), swarm, line)
        return script.text()

    def testHelpTreeAndLog(self):
        swarm = self.makeSwarm()
        self.assertIn("approve <agent>", self.run_(swarm, "help"))
        self.assertIn("approve <agent>", self.run_(swarm, "?"))
        self.assertIn("Writer [writer]", self.run_(swarm, "tree"))
        self.assertEqual(self.run_(swarm, "log"), "No messages yet.")
        swarm.communicate("Leader", "Writer", "Hello Writer")
        self.assertIn("Leader -> Writer: Hello Writer", self.run_(swarm, "log"))
        self.assertEqual(self.run_(swarm, ""), "")
        self.assertIn("I do not know this command. Type help.", self.run_(swarm, "dance"))

    def testTheNameOfAnAgentOpensIt(self):
        swarm = self.makeSwarm()
        text = self.run_(swarm, "writer")
        self.assertIn("Writer [writer] model: gpt-6-luna, API", text)
        self.assertIn("task: task of Writer", text)
        self.assertIn("You can: msg Writer <message>", text)
        self.assertNotIn("approve Writer", text)
        self.set(swarm, "Writer", status="working", review="ready", draft="The draft text", problem="Too long", result=None)
        text = self.run_(swarm, "Writer")
        self.assertIn("latest draft:\nThe draft text", text)
        self.assertIn("automatic check: Too long", text)
        self.assertIn("You can: approve Writer | reject Writer | correct Writer <what to change> | msg Writer <message>", text)
        text = self.run_(swarm, "Formatter")
        self.assertIn("waits for: Writer (still waiting for Writer)", text)
        self.assertIn("state: waiting, review: nothing to review", text)
        self.set(swarm, "Formatter", startAt=cli.datetime(2031, 5, 17, 6, 45))
        self.assertIn("scheduled for 2031-05-17 06:45", self.run_(swarm, "Formatter"))
        self.assertIn("You can: start Formatter", self.run_(swarm, "Formatter"))
        self.assertIn("(the leader)", self.run_(swarm, "Leader"))
        self.set(swarm, "Writer", status="done", review="approved", result="The final text", error="")
        self.assertIn("result: The final text", self.run_(swarm, "Writer"))
        self.set(swarm, "Writer", status="failed", result=None, error="It did not finish")
        self.assertIn("problem: It did not finish", self.run_(swarm, "Writer"))

    def testApproveRejectAndCorrectAnAgentThatWaits(self):
        swarm = self.makeSwarm()
        for command, review, decision in (("approve Writer", "approved", "yes"), ("reject writer", "rejected", "no")):
            self.set(swarm, "Writer", status="working", review="ready", decision=None)
            self.assertEqual(self.run_(swarm, command), f"Done: {command.split()[0]} Writer.")
            self.assertEqual((swarm.members["Writer"]["review"], swarm.members["Writer"]["decision"]), (review, decision))
        self.set(swarm, "Writer", status="working", review="ready", decision=None)
        self.assertEqual(self.run_(swarm, "correct Writer make it shorter, please"), "Done: correct Writer.")
        self.assertEqual(swarm.members["Writer"]["agent"].userMessages, ["make it shorter, please"])
        self.assertEqual((swarm.members["Writer"]["review"], swarm.members["Writer"]["decision"] is not None), ("", True))
        self.assertIn("nothing waiting for the user", self.run_(swarm, "approve Writer"))

    def testMessagesToAnAgentWithOrWithoutAColon(self):
        swarm = self.makeSwarm()
        self.assertEqual(self.run_(swarm, "msg Writer use a formal tone"), "Done: msg Writer.")
        self.assertEqual(self.run_(swarm, "Writer: and keep it short"), "Done: msg Writer.")
        self.assertEqual(swarm.members["Writer"]["agent"].userMessages, ["use a formal tone", "and keep it short"])
        self.assertEqual([message["message"] for message in swarm.getMessages("User", "Writer")], ["use a formal tone", "and keep it short"])
        self.set(swarm, "Writer", status="done")
        self.assertIn("already finished", self.run_(swarm, "msg Writer too late"))

    def testAnAgentThatWaitsForATimeCanBeStartedNow(self):
        swarm = self.makeSwarm()
        self.assertIn("not waiting for a time to start", self.run_(swarm, "start Writer"))
        self.set(swarm, "Writer", startAt=cli.datetime(2031, 5, 17, 6, 45))
        self.assertEqual(self.run_(swarm, "start Writer"), "Done: start Writer.")
        self.assertTrue(swarm.members["Writer"]["wake"].is_set())

    def testMistakesAreExplained(self):
        swarm = self.makeSwarm()
        self.assertIn("Write the name of an agent after approve. The agents are: Leader, Writer, Formatter.", self.run_(swarm, "approve"))
        self.assertIn("Write the name of an agent after msg.", self.run_(swarm, "msg Nobody hello"))
        self.assertIn("Write what you want to say after msg Writer.", self.run_(swarm, "msg Writer"))
        self.assertIn("Write what you want to say after correct Writer.", self.run_(swarm, "correct Writer"))

    def testQuitLeavesTheProgram(self):
        swarm = self.makeSwarm()
        with everywhere(cli.os, "_exit") as leave:
            self.assertIn("Your swarm is saved: start the program again to continue it.", self.run_(swarm, "quit"))
        leave.assert_called_once_with(0)


class TaskQuestionsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.document = self.folder / "paper.tex"
        self.document.write_text("Theorem 1. A statement.")

    def testTheTaskIsChosenFromTheListAndInformationIsOneCommandAway(self):
        script = Script(["info 2", "info 99", "info", "?3", "2"])
        self.assertEqual(cli.chooseTask(script.console(), 1, 3), "calendar")
        self.assertIn("Agent 1 of 3: what must this agent do?", script.text())
        self.assertIn("  1. Email writer and sender", script.said)
        self.assertEqual(sum(label.startswith("  ") for label in script.said), 8)
        self.assertIn(cli.TASKS["calendar"]["info"], script.said)
        self.assertIn(cli.TASKS["news"]["info"], script.said)
        self.assertEqual(script.said.count("Write info and the number of a task, like: info 3"), 2)

    def testACalendarAgentNeedsOnlyItsRequestAndAName(self):
        script = Script(["dentist next Monday at 10", "", "my planner", "Planner"])
        spec = cli.fillTask(script.console(), "calendar", [])
        self.assertEqual(spec, {"task": "calendar", "answers": {"request": "dentist next Monday at 10"}, "name": "Planner"})
        self.assertIn("A name starts with a letter", script.text())
        self.assertIn("Planner will do this: Book this event in the calendar: dentist next Monday at 10", script.text())
        self.assertIn(f"\n--- Calendar planner ---\n{cli.TASKS['calendar']['info']}", script.said)

    def testTheAdvancedSettingsAreOptional(self):
        script = Script(["the sea", "", "y", "zero", "3", ""])
        spec = cli.fillTask(script.console(), "author", ["Writer"])
        self.assertEqual(spec["answers"], {"subject": "the sea", "length": 300, "numberOfLoops": 3})
        self.assertEqual(spec["name"], "Writer2")
        self.assertIn("Write a whole number above 0.", script.said)
        self.assertIn("Name of this agent [Writer2]: ", script.prompts())

    def testTheEmailAgentGetsItsServersFromTheProviderAndItsLoginIsTested(self):
        script = Script(["1", "sara", "me@gmail.com", "", "", "sara@example.com", "Meeting", "move it to Friday", "", "y", "y", "y", "n", ""], ["app-password", "new-password"])
        tests = []
        def login(sender, password, smtp, imap):
            tests.append((sender, password, smtp, imap))
            return "smtp.gmail.com refused the address or the password." if len(tests) == 1 else ""
        with everywhere(cli, "checkEmailLogin", login):
            spec = cli.fillTask(script.console(), "email", [])
        self.assertEqual(spec["answers"], {"provider": "Gmail", "sender": "me@gmail.com", "password": "new-password", "smtp": "smtp.gmail.com", "imap": "imap.gmail.com",
                                           "receiver": "sara@example.com", "subject": "Meeting", "request": "move it to Friday", "language": "English"})
        self.assertEqual(tests, [("me@gmail.com", "app-password", "smtp.gmail.com", "imap.gmail.com"), ("me@gmail.com", "new-password", "smtp.gmail.com", "imap.gmail.com")])
        text = script.text()
        self.assertIn("apppasswords", text)
        self.assertIn("This is not an email address.", text)
        self.assertIn("SMTP server to send the email [smtp.gmail.com]: ", script.prompts())
        self.assertIn("The login works.", text)
        self.assertNotIn("app-password", text)
        self.assertEqual(spec["name"], "Emailer")

    def testAnEmailProviderThatIsNotInTheListNeedsItsServers(self):
        script = Script(["6", "me@mine.test", "", "smtp.mine.test", "", "sara@example.com", "Hi", "say hi", "", "n", "n", ""], ["pw"])
        spec = cli.fillTask(script.console(), "email", [])
        self.assertEqual((spec["answers"]["smtp"], spec["answers"]["imap"]), ("smtp.mine.test", ""))
        self.assertIn("This answer is needed.", script.said)

    def testTheNewsAgentNeedsItsOutletsAndTheTimeOfTheDay(self):
        groups = len(cli.NEWS_OUTLETS)
        script = Script([str(groups + 3), "1", "1,3", str(groups + 1), "nasa", "1", str(groups + 1), "no such outlet", str(groups + 2), "ftp://x", "https://example.com/feed",
                         str(groups + 3), "science", "7h30", "7:05", "", "", "", "", ""])
        spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual(spec["answers"], {"outlets": ["BBC World", "NPR News", "NASA Breaking News", "https://example.com/feed"], "topics": "science", "collectAt": "07:05",
                                           "maxWords": 250, "language": "English", "messenger": NO_MESSENGER})
        text = script.text()
        self.assertIn("Choose at least one outlet.", text)
        self.assertIn("No outlet has this in its name.", text)
        self.assertIn("The address must start with http:// or https://.", text)
        self.assertIn("Outlets chosen so far: BBC World, NPR News, NASA Breaking News", text)
        self.assertIn("is not a time. Write it as HH:MM", text)
        self.assertRegex(text, r"The feeds will be collected on \w+ \d{4}-\d{2}-\d{2} at 07:05, in \d+ h \d+ min\.")
        self.assertIn("start <agent>", text)
        self.assertEqual(spec["name"], "NewsBriefer")

    def testTheNewsAgentCanCollectTheFeedsRightAway(self):
        groups = len(cli.NEWS_OUTLETS)
        script = Script(["1", "2", str(groups + 3), "", "", "", "", "", "", ""])
        spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual((spec["answers"]["outlets"], spec["answers"]["collectAt"]), (["BBC Top Stories"], None))
        self.assertIn("The feeds are collected as soon as the agent starts.", script.said)

    def newsScript(self, *after):
        groups = len(cli.NEWS_OUTLETS)
        return ["1", "2", str(groups + 3), "", "", "", "", *after]

    def testTheNewsAgentCanSendItsBriefingToTelegram(self):
        checks = []
        script = Script(self.newsScript("2", "42", "", "", ""), ["123:SECRET-TOKEN"])
        with everywhere(cli, "checkMessenger", lambda app, settings: checks.append((app, settings)) or ""):
            spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual(spec["answers"], {"outlets": ["BBC Top Stories"], "topics": "", "collectAt": None, "maxWords": 250, "language": "English",
                                           "messenger": "Telegram", "telegramToken": "123:SECRET-TOKEN", "telegramChat": "42"})
        self.assertEqual(checks, [("Telegram", {"token": "123:SECRET-TOKEN", "chat": "42"})])
        text = script.text()
        self.assertIn("Do you also want to receive the briefing in a messaging app?", text)
        self.assertIn("  1. No, I will read it here", script.said)
        self.assertIn("  3. WhatsApp", script.said)
        self.assertIn("The briefing arrives as a message from a Telegram bot that you create yourself.", text)
        self.assertIn("Telegram accepted the information.", text)
        self.assertNotIn("SECRET-TOKEN", text)
        self.assertTrue(any(prompt.startswith("Token of your Telegram bot") and answer == "<hidden>" for prompt, answer in script.log))
        self.assertIn("also sent to Telegram", cli.describeAgent(spec))

    def testTheMessagingQuestionsAreSkippedWhenTheUserKeepsTheBriefingHere(self):
        script = Script(self.newsScript("", "", ""))
        spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual(spec["answers"]["messenger"], NO_MESSENGER)
        self.assertNotIn("telegramToken", spec["answers"])
        self.assertNotIn("whatsappTo", spec["answers"])
        self.assertNotIn("Test the connection", script.text())

    def testTheChatOfTheUserInTelegramIsFoundFromTheMessagesToTheirBot(self):
        looked = []
        def find(token):
            looked.append(token)
            return [] if len(looked) == 1 else [{"id": 42, "name": "Sara"}]
        script = Script(self.newsScript("2", "", "", "", "", "1", "n", "", ""), ["123:SECRET-TOKEN"])
        with everywhere(cli, "findTelegramChats", find):
            spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual(spec["answers"]["telegramChat"], "42")
        self.assertEqual(looked, ["123:SECRET-TOKEN", "123:SECRET-TOKEN"])
        text = script.text()
        self.assertIn("Open your bot in Telegram, press Start and send it any message.", text)
        self.assertIn("I did not see any message yet.", text)
        self.assertIn("  1. Sara (chat number 42)", script.said)

    def testTheChatNumberCanAlwaysBeWrittenInsteadOfLookedUp(self):
        script = Script(self.newsScript("2", "", "skip", "", "7", "n", "", ""), ["123:SECRET-TOKEN"])
        with everywhere(cli, "findTelegramChats", side_effect=AssertionError("nothing to look up")):
            spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual(spec["answers"]["telegramChat"], "7")

    def testAFailedLookupOfTheChatIsToldAndTheUserCanWriteTheNumber(self):
        script = Script(self.newsScript("2", "", "", "n", "7", "n", "", ""), ["123:SECRET-TOKEN"])
        with everywhere(cli, "findTelegramChats", side_effect=cli.MessagingError("Telegram refused the bot token.")):
            spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual(spec["answers"]["telegramChat"], "7")
        self.assertIn("Telegram refused the bot token.", script.said)
        self.assertIn("I did not see any message yet.", script.said)

    def testWhatsappNeedsItsTokenItsPhoneNumberIdAndTheNumberOfTheUserWithItsCountryCode(self):
        script = Script(self.newsScript("3", "555", "0151 12345678", "+49 151 12345678", "n", "", ""), ["WA-SECRET"])
        spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual({key: spec["answers"][key] for key in ("messenger", "whatsappToken", "whatsappPhoneId", "whatsappTo")},
                         {"messenger": "WhatsApp", "whatsappToken": "WA-SECRET", "whatsappPhoneId": "555", "whatsappTo": "+4915112345678"})
        text = script.text()
        self.assertIn("Write the number with its country code, like +4915112345678.", text)
        self.assertIn("only lets such an app write free text to someone who wrote to it in the last 24 hours", text)
        self.assertNotIn("WA-SECRET", text)

    def testInformationThatDoesNotWorkCanBeEnteredAgain(self):
        problems = ["Telegram refused the bot token. Copy it again from @BotFather.", ""]
        checks = []
        script = Script(self.newsScript("2", "42", "", "", "43", "", "", ""), ["wrong-token", "right-token"])
        with everywhere(cli, "checkMessenger", lambda app, settings: checks.append(dict(settings)) or problems.pop(0)):
            spec = cli.fillTask(script.console(), "news", [])
        self.assertEqual((spec["answers"]["telegramToken"], spec["answers"]["telegramChat"]), ("right-token", "43"))
        self.assertEqual(checks, [{"token": "wrong-token", "chat": "42"}, {"token": "right-token", "chat": "43"}])
        self.assertIn("Telegram refused the bot token.", script.text())
        self.assertEqual(script.secrets, [])

    def testOnlyTheNewsAgentAsksAboutAMessagingApp(self):
        script = Script(["the sea", "", "", "", ""])
        cli.fillTask(script.console(), "author", [])
        self.assertNotIn("messaging app", script.text())

    def folderSpec(self, task, **answers):
        return {"task": task, "answers": answers, "name": "Agent"}

    def testAnAgentThatWorksOnAFileSuggestsTheFolderOfThatFile(self):
        script = Script([""])
        folder = cli.askFolder(script.console(), self.folderSpec("math", filePath=str(self.document)), 1, 2)
        self.assertEqual(folder, str(self.folder.resolve()))
        self.assertEqual(script.prompts(), [f"Folder of Agent [{self.folder.resolve()}] (a path, Enter = the one in brackets, none = no folder): "])
        text = script.text()
        self.assertIn("--- Folder of agent 1 of 2: Agent (math checker) ---", text)
        self.assertIn(cli.TASKS["math"]["folder"], text)
        self.assertIn("Referee the mathematical text paper.tex", text)

    def testNoneMeansNoFolderEvenWhenOneIsSuggested(self):
        self.assertIsNone(cli.askFolder(Script(["none"]).console(), self.folderSpec("math", filePath=str(self.document)), 1, 1))
        self.assertIsNone(cli.askFolder(Script([" NONE "]).console(), self.folderSpec("math", filePath=str(self.document)), 1, 1))

    def testAFolderThatCannotHoldTheFileOrDoesNotExistIsRefused(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        script = Script([elsewhere.name, str(self.folder / "missing"), ""])
        folder = cli.askFolder(script.console(), self.folderSpec("math", filePath=str(self.document)), 1, 1)
        self.assertEqual(folder, str(self.folder.resolve()))
        text = script.text()
        self.assertIn("paper.tex is not inside", text)
        self.assertIn("There is no folder at", text)

    def testAnAgentWithoutFilesHasNoFolderUnlessTheUserGivesOne(self):
        script = Script([""])
        self.assertIsNone(cli.askFolder(script.console(), self.folderSpec("author", subject="the sea", length=300), 1, 1))
        self.assertEqual(script.prompts(), ["Folder of Agent (a path, Enter = no folder, none = no folder): "])
        self.assertEqual(cli.askFolder(Script([str(self.folder)]).console(), self.folderSpec("author", subject="the sea", length=300), 1, 1), str(self.folder.resolve()))

    def testEveryAgentIsAskedAboutItsFolder(self):
        specs = [self.folderSpec("author", subject="a", length=3), self.folderSpec("math", filePath=str(self.document)), self.folderSpec("calendar", request="x")]
        script = Script([str(self.folder), "", "none"])
        cli.chooseFolders(script.console(), specs)
        self.assertEqual([spec["answers"]["folder"] for spec in specs], [str(self.folder.resolve()), str(self.folder.resolve()), None])
        self.assertIn("This is optional, and every agent can have its own folder.", script.text())

    def testTheLiteratureAgentGetsItsSearchesItsPublishersAndItsAccounts(self):
        found = [{"name": "Oxford University Press (OUP)", "id": 286, "papers": 2387000}, {"name": "Oxford Academic", "id": 5, "papers": 12}]
        script = Script(["graphs", "", "1,3", "1,4", "y", "oxford", "1", "y", "nobody", "y", "ghost", "n", "y", "ieeexplore.ieee.org", "me", "n", "", ""], ["hunter2-secret"])
        with everywhere(cli, "findPublishers", lambda name: found if name == "oxford" else []):
            spec = cli.fillTask(script.console(), "literature", [])
        self.assertEqual(spec["answers"], {"subject": "graphs", "length": 500, "searches": ["Google Scholar", "Crossref"], "accounts": {"ieeexplore.ieee.org": ("me", "hunter2-secret")},
                                           "publishers": {"Springer Nature": 297, "IEEE": 263, "Oxford University Press (OUP)": 286}})
        text = script.text()
        self.assertIn("  1. Oxford University Press (OUP) (2,387,000 works)", script.said)
        self.assertIn("Crossref does not know a publisher with this name.", text)
        self.assertIn("Literature reviewer".lower(), text.lower())
        self.assertNotIn("hunter2-secret", text)

    def testALiteratureSurveyNeedsAPlaceToSearch(self):
        script = Script(["graphs", "", "none", "", "n", "n", "1", "", "n", "n", ""])
        spec = cli.fillTask(script.console(), "literature", [])
        self.assertIn("The survey needs a place to search", script.text())
        self.assertEqual((spec["answers"]["searches"], spec["answers"]["publishers"]), (["Google Scholar"], {}))

    def testTheDocumentAgentsNeedAFileThatExists(self):
        script = Script([str(self.folder / "nope.tex"), str(self.document), "IEEE", "n", ""])
        spec = cli.fillTask(script.console(), "format", [])
        self.assertEqual(spec["answers"], {"filePath": str(self.document), "style": "IEEE"})
        self.assertIn("There is no file at", script.text())
        script = Script([str(self.document), "n", ""])
        self.assertEqual(cli.fillTask(script.console(), "math", [])["answers"], {"filePath": str(self.document)})
        self.assertIn("MathChecker will do this: Referee the mathematical text paper.tex", script.text())

    def testTheCoderNeedsATaskAFileAndMayHaveACommand(self):
        script = Script(["sort a list", str(self.folder / "sort.py"), 'python -m pytest "my tests"', "n", ""])
        spec = cli.fillTask(script.console(), "coder", [])
        self.assertEqual(spec["answers"], {"task": "sort a list", "filePath": str(self.folder / "sort.py"), "testCommand": ["python", "-m", "pytest", "my tests"]})
        self.assertIn("It writes and RUNS code on your computer", script.text())
        script = Script(["sort a list", str(self.folder / "sort.py"), "", "n", ""])
        self.assertIsNone(cli.fillTask(script.console(), "coder", [])["answers"]["testCommand"])

    def testTheNumberOfAgentsAndTheMissionAreAsked(self):
        script = Script(["many", "0", "-2", "3", "", "Write texts"])
        console = script.console()
        self.assertEqual(cli.askAgentCount(console), 3)
        self.assertEqual(script.said.count("Write a whole number above 0."), 3)
        self.assertEqual(cli.askMission(console), "Write texts")
        self.assertIn("This answer is needed.", script.said)
        self.assertIn("The first agent is the leader", script.text())


class FakeModel:
    def __init__(self, name="fake"):
        self.name, self.prompts = name, []
        self.usage = {"calls": 0, "input": 0, "output": 0}

    def input(self, prompt):
        self.prompts.append(prompt)
        return "OK"


COST = {"model": "x", "input": 3.0, "output": 15.0, "cachedInput": 0.3, "context": 200000, "unit": "US dollars per 1 million tokens", "note": "", "page": "https://prices.test"}


class ModelFlowCase(GpuTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.created, self.missing, self.downloaded = [], [], [False]
        def create(info, apiKeys=None, token=None, report=None):
            model = FakeModel(info["name"])
            self.created.append((info["name"], dict(apiKeys or {}), token, model))
            return model
        patches = {"createModel": create, "findMissingPackages": lambda info: self.missing.pop(0) if self.missing else [],
                   "isDownloaded": lambda name: self.downloaded[0], "getHubFolder": lambda: Path(folder.name) / "hub",
                   "getModelCost": lambda provider, model: {**COST, "model": model}}
        for name, replacement in patches.items():
            patcher = everywhere(cli, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict(cli.os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "HF_TOKEN"):
            cli.os.environ.pop(variable, None)
        self.swarm = Swarm("Write texts")
        self.keys, self.tokens = {}, {}

    def spec(self, name="Writer", task="author"):
        return {"task": task, "name": name, "answers": {"subject": "the sea", "length": 300}}

    def choose(self, script, name="Writer", number=1, total=2, replacing=None):
        return cli.chooseModel(script.console(), self.swarm, self.spec(name), number, total, self.keys, self.tokens, replacing)

    def add(self, script, name="Writer", number=1, total=2):
        info, model = self.choose(script, name, number, total)
        cli.buildAgent(script.console(), self.swarm, self.spec(name), info, model)
        return info


class LocalModelTests(ModelFlowCase):
    def testTheFirstListOnlyShowsTheModelsThatFitAndTheOthersAreRefused(self):
        script = Script(["1", "4", "2", "4", "1"])
        info, model = self.choose(script)
        text = script.text()
        self.assertEqual(info["name"], "google/gemma-4-E4B-it")
        self.assertIn("  1. google/gemma-4-E4B-it (19.2 GB)", script.said)
        self.assertIn("  3. google/gemma-4-12B-it (28.8 GB)", script.said)
        self.assertIn("  4. Show all the models of the library", script.said)
        self.assertIn("3 recommended models need more VRAM than the other agents leave, so they are not shown here. They are in the list of all the models.", text)
        self.assertIn("  4. meta-llama/Llama-3.3-70B-Instruct (169.4 GB, gated)  [too big for the GPUs]", script.said)
        self.assertIn("The number in parentheses is the VRAM, in GB, that the model is expected to need.", text)
        self.assertEqual(script.said.count("Models recommended for this task, from the smallest to the largest (type back to choose again where the model runs):"), 2)
        self.assertIn("meta-llama/Llama-3.3-70B-Instruct needs about 169.4 GB of VRAM, but your GPUs only have 51.6 GB in total. Choose a smaller model or an API model.", text)
        self.assertIs(self.created[0][3], model)

    def testAfterTheChoiceTheGpusAreCheckedAndTheVramOfTheSwarmGrows(self):
        script = Script(["1", "1"])
        self.add(script)
        text = script.text()
        self.assertIn("You chose google/gemma-4-E4B-it: it is expected to need 19.2 GB of VRAM.", text)
        self.assertIn("GPU check:\n  RTX A5000: 25.8 GB in total, 25.3 GB free now\n  RTX A5000: 25.8 GB in total, 25.8 GB free now", text)
        self.assertIn("All the GPUs: 51.6 GB in total, 51.1 GB free now (0.5 GB are used by other jobs)", text)
        self.assertIn("VRAM the swarm is expected to need: 19.2 GB. Left in the GPUs: 32.4 GB.", text)
        self.assertIn("will be downloaded to", text)
        self.assertIn("about 16.0 GB", text)
        script = Script(["1", "2"])
        self.add(script, "Writer2", 2)
        text = script.text()
        self.assertIn("VRAM of the swarm so far: 19.2 GB of 51.6 GB (51.1 GB free now).", text)
        self.assertIn("VRAM the swarm is expected to need: 40.6 GB. Left in the GPUs: 11.0 GB.", text)
        self.assertEqual(self.swarm.getNeededVram(), 40.6)
        self.assertIn("[too big for the GPUs]", cli.labelLocal(self.swarm, "meta-llama/Llama-3.3-70B-Instruct"))
        self.assertNotIn("[", cli.labelLocal(self.swarm, "microsoft/Phi-4-mini-instruct"))
        self.assertIn("[too big for the GPUs]", cli.labelLocal(self.swarm, "google/gemma-4-31B-it"))

    def testWhenTheSwarmFillsTheGpusNoOtherLocalModelCanBeChosenButApiModelsCan(self):
        self.add(Script(["1", "2"]), "Writer", 1, 3)
        self.add(Script(["1", "3"]), "Writer2", 2, 3)
        self.assertEqual((self.swarm.getNeededVram(), self.swarm.getVramStatus()["left"]), (50.2, 1.4))
        script = Script(["1", "back", "2", "1", "n", "n"], ["sk"])
        info, model = self.choose(script, "Writer3", 3, 3)
        text = script.text()
        self.assertIn("6 recommended models need more VRAM than the other agents leave, so they are not shown here.", text)
        self.assertIn("  1. Show all the models of the library", script.said)
        self.assertNotIn("  1. google/gemma-4-E4B-it (19.2 GB)  [too big for the GPUs]", script.said)
        self.assertIn("(type back to choose again where the model runs)", text)
        self.assertEqual((info["name"], info["local"]), ("claude-sonnet-5-5", False))
        cli.buildAgent(script.console(), self.swarm, self.spec("Writer3"), info, model)
        self.assertEqual(self.swarm.getNeededVram(), 50.2)

    def testAModelThatFitsButIsNotFreeNowCanBeKeptOrNot(self):
        self.currentGpus[:] = [{"name": "G", "total": 51.6, "free": 10.0}]
        script = Script(["1", "1", "n", "2", "1", "n", "n"], ["sk-test"])
        info, model = self.choose(script)
        text = script.text()
        self.assertIn("  1. google/gemma-4-E4B-it (19.2 GB)  [not free now]", script.said)
        self.assertIn("Warning: The swarm needs about 19.2 GB of VRAM. Your GPUs have 51.6 GB in total, but only 10.0 GB are free now because other jobs use 41.6 GB.", text)
        self.assertIn("You will not be able to run the swarm until they free enough memory.", text)
        self.assertIn("You can choose it, but the swarm cannot run until the memory is free. Keep this model? (y/N) ", script.prompts())
        self.assertEqual(info["name"], "claude-sonnet-5-5")
        script = Script(["1", "1", "y"])
        self.assertEqual(self.choose(script)[0]["name"], "google/gemma-4-E4B-it")

    def testWithoutAGpuOnlyApiModelsCanBeChosen(self):
        self.currentGpus[:] = []
        script = Script(["1", "2", "1", "n", "n"], ["sk-test"])
        info, model = self.choose(script)
        self.assertIn("No supported GPU was found on this computer, so it is not possible.", script.text())
        self.assertIn("No supported GPU was found", script.said[[number for number, line in enumerate(script.said) if "so local models cannot run" in line][0]])
        self.assertFalse(info["local"])

    def testAllTheModelsOfTheLibraryCanBeBrowsedByFamily(self):
        script = Script(["1", "4", "1", "1"])
        info, model = self.choose(script)
        text = script.text()
        self.assertIn("Families of local models", text)
        self.assertIn(f"  1. qwen ({len(cli.MODELS_LOCAL['qwen'])} models)", script.said)
        self.assertIn("  1. Qwen/Qwen3.5-0.8B (2.2 GB)", "\n".join(script.said).split("\n"))
        self.assertEqual(info["name"], "Qwen/Qwen3.5-0.8B")
        script = Script(["1", "4", "13", "4", "1", str(len(cli.MODELS_LOCAL["qwen"]) + 1), "1"])
        self.assertEqual(self.choose(script)[0]["name"], "google/gemma-4-E4B-it")

    def testALocalModelCanBeTypedAndItsSizeIsFoundOnHuggingFace(self):
        script = Script(["1", "5", "badname", "someone/custom-7b", "1"])
        with everywhere(cli, "lookupHuggingFace", lambda name: {"billions": 7.0, "gated": False}):
            info, model = self.choose(script)
        self.assertEqual((info["name"], info["billions"], info["vram"]), ("someone/custom-7b", 7.0, 16.8))
        self.assertIn("It is written owner/name, like Qwen/Qwen3-8B.", script.said)
        self.assertIn("Found on Hugging Face. It has 7.0 billion parameters.", script.said)

    def testATypedModelThatCannotBeFoundOrHasNoPublishedSizeIsHandled(self):
        script = Script(["1", "5", "someone/ghost", "5", "someone/mystery", "many", "6"])
        def lookup(name):
            if name == "someone/ghost":
                raise cli.ModelError("There is no model called someone/ghost on Hugging Face. Check how it is written.")
            return {"billions": None, "gated": False}
        with everywhere(cli, "lookupHuggingFace", lookup):
            info, model = self.choose(script)
        self.assertIn("There is no model called someone/ghost", script.text())
        self.assertIn("Found on Hugging Face. Its size is not published.", script.said)
        self.assertIn("Write a number above 0, like 8.2.", script.said)
        self.assertEqual((info["name"], info["billions"]), ("someone/mystery", 6.0))

    def testAGatedModelNeedsTheLicenseAndAToken(self):
        script = Script(["1", "4", "2", "1"], ["hf_abc"])
        info, model = self.choose(script)
        self.assertEqual(info["name"], "meta-llama/Llama-3.2-1B-Instruct")
        self.assertIn("is a gated model: accept its license at https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct", script.text())
        self.assertEqual(self.tokens, {"meta-llama/Llama-3.2-1B-Instruct": "hf_abc"})
        self.assertEqual(self.created[0][2], "hf_abc")
        self.tokens.clear()
        cli.os.environ["HF_TOKEN"] = "from-env"
        script = Script(["1", "4", "2", "1"])
        self.choose(script)
        self.assertEqual(self.tokens, {})

    def testTheDownloadIsAnnouncedOrTheModelIsAlreadyThere(self):
        self.downloaded[0] = True
        script = Script(["1", "1"])
        self.choose(script)
        self.assertIn("is already downloaded in", script.text())
        self.assertNotIn("will be downloaded", script.text())

    def testMissingPackagesAreAnnouncedBeforeTheSwarmRuns(self):
        self.missing[:] = [["torch", "accelerate"], []]
        script = Script(["1", "1", ""])
        self.choose(script)
        self.assertIn("This model needs the packages torch, accelerate. Install them in another terminal with: pip install -U torch accelerate", script.text())
        self.missing[:] = [["torch"], ["torch"]]
        script = Script(["1", "1", "", "SKIP"])
        self.choose(script)
        self.assertEqual(script.text().count("This model needs the packages torch."), 2)

    def testChangingTheModelOfAnAgentDoesNotCountItsOldModelAgainstTheNewOne(self):
        self.add(Script(["1", "2"]), "Writer", 1)
        self.add(Script(["1", "1"]), "Writer2", 2)
        self.assertEqual(self.swarm.getNeededVram(), 40.6)
        self.assertIn("[too big for the GPUs]", cli.labelLocal(self.swarm, "google/gemma-4-12B-it"))
        self.assertNotIn("[", cli.labelLocal(self.swarm, "google/gemma-4-12B-it", replacing="Writer"))
        script = Script(["1", "3"])
        info, model = self.choose(script, "Writer", 1, 2, replacing="Writer")
        self.assertEqual(info["name"], "google/gemma-4-12B-it")
        self.assertIn("VRAM the swarm is expected to need: 48.0 GB.", script.text())
        self.assertIn("VRAM of the swarm so far: 19.2 GB of 51.6 GB", script.text())

class ApiModelTests(ModelFlowCase):
    def testPricesCanBeAskedBeforeChoosingAndTheKeyIsAskedOnce(self):
        script = Script(["2", "p 2", "p 1,3", "p all", "p 9", "1", "y", "y"], ["sk-ant"])
        info, model = self.choose(script)
        text = script.text()
        self.assertEqual((info["name"], info["provider"]), ("claude-sonnet-5-5", "claude"))
        self.assertIn("  1. claude-sonnet-5-5 (Anthropic, $15.00 per 1M tokens)", script.said)
        self.assertIn("  2. gpt-6-luna (OpenAI, $10.00 per 1M tokens)", script.said)
        self.assertIn("  3. gemini-3.8-flash (Google, price unknown)", script.said)
        self.assertIn("Type p 2 for the price of model 2, or p all for the prices of all of them (type back to choose again where the model runs):", text)
        self.assertEqual(text.count("input $3.0 and output $15.0 per 1 million tokens, cached input $0.3. Context window: 200,000 tokens."), 1 + 2 + 4 + 1)
        self.assertIn("  gpt-6-luna: input $3.0", text)
        self.assertIn("Choose numbers between 1 and 4.", text)
        self.assertIn("Create one at https://platform.claude.com/settings/keys", text)
        self.assertEqual(self.keys, {"claude": "sk-ant"})
        self.assertIn("Do you want the price of claude-sonnet-5-5? (Y/n) ", script.prompts())
        self.assertIn("The model answered: OK", text)
        self.assertEqual(self.created[0][:2], ("claude-sonnet-5-5", {"claude": "sk-ant"}))
        self.assertEqual(model.prompts, ["Reply with the single word OK."])
        script = Script(["2", "1", "n", "n"])
        self.choose(script, "Writer2", 2)
        self.assertNotIn("Create one at", script.text())

    def testTheBudgetLeftHidesTheModelsThatCostMoreAndAnAgentCountsWhatItSetAside(self):
        self.swarm.costs.setBudget(20)
        cli.os.environ.update(ANTHROPIC_API_KEY="sk-a", OPENAI_API_KEY="sk-o", GEMINI_API_KEY="sk-g")
        script = Script(["2", "1", "n", "n"])
        info, model = self.choose(script)
        self.assertIn("$20.00 of the budget of the mission is left for this model.", script.text())
        self.assertIn("  1. claude-sonnet-5-5 (Anthropic, $15.00 per 1M tokens)", script.said)
        cli.buildAgent(script.console(), self.swarm, self.spec("Writer"), info, model)
        script = Script(["2", "1", "n", "n"])
        info, model = self.choose(script, "Writer2", 2)
        text = script.text()
        self.assertIn("$5.00 of the budget of the mission is left for this model.", text)
        self.assertIn("2 recommended models cost more than that per 1 million tokens, so they are not shown here.", text)
        self.assertIn("  1. gemini-3.8-flash (Google, price unknown)", script.said)
        self.assertIn("  2. deepseek-v4-pro (DeepSeek, $2.00 per 1M tokens)", script.said)
        self.assertNotIn("claude-sonnet-5-5 (Anthropic", "\n".join(script.said))
        self.assertEqual(info["name"], "gemini-3.8-flash")
        script = Script(["2", "3", "2", "3", "n", "n"])
        info, model = self.choose(script, "Writer2", 2)
        self.assertIn("  3. claude-opus-5-5 ($25.00 per 1M tokens)  [over your budget]", script.said)
        self.assertIn("Warning: claude-opus-5-5 costs more per 1 million tokens than the $5.00 left of the budget.", script.text())

    def testTheKeyOfTheEnvironmentIsUsedWithoutAskingAndAPriceCanBeUnavailable(self):
        cli.os.environ["OPENAI_API_KEY"] = "from-env"
        script = Script(["2", "2", "y", "n"])
        with everywhere(cli, "getModelCost", lambda provider, model: {"model": model, "error": "No price is published for this model yet.", "page": "https://prices.test"}):
            info, model = self.choose(script)
        self.assertEqual(info["name"], "gpt-6-luna")
        self.assertIn("Using the API key found in OPENAI_API_KEY.", script.text())
        self.assertIn("gpt-6-luna: No price is published for this model yet. Official prices: https://prices.test", script.text())
        self.assertEqual(self.keys, {})

    def testAConnectionThatFailsIsReportedAndTheAgentStillGetsItsModel(self):
        script = Script(["2", "1", "n", "y"], ["sk-ant"])
        def failing(self, prompt):
            raise cli.ModelError("Anthropic refused the API key. Check that it is correct and still active.")
        with everywhere(FakeModel, "input", failing):
            info, model = self.choose(script)
        self.assertIn("It did not work: Anthropic refused the API key.", script.text())
        self.assertEqual(info["name"], "claude-sonnet-5-5")

    def testAllTheApiModelsCanBeBrowsedByProviderWithTheirPrices(self):
        script = Script(["2", "5", "3", "p 1", "p all", "5", "1", "7", "n", "n"], ["sk"])
        # the sixth entry of the recommended list is "show all": the provider list comes next, the models of Google after it
        info, model = self.choose(script)
        text = script.text()
        self.assertIn("Providers of API models", text)
        self.assertIn(f"  3. gemini ({len(cli.MODELS_API['gemini'])} models)", script.said)
        self.assertEqual(info["provider"], "gemini")
        self.assertIn("gemini (type p 2 for the price of model 2, or p all):", text)

    def testAModelOfTheApiThatIsNotInTheListCanBeTyped(self):
        script = Script(["2", "6", "2", "", "claude-future-9", "n", "n"], ["sk"])
        info, model = self.choose(script)
        self.assertEqual((info["name"], info["provider"], info["local"]), ("claude-future-9", "claude", False))
        self.assertIn("Which company provides the model?", script.text())
        self.assertIn("  2. Anthropic (claude-...)", script.said)
        self.assertIn("This answer is needed.", script.said)

    def testGoingBackReturnsToThePreviousList(self):
        script = Script(["2", "5", "5", "1", "n", "n"], ["sk"])
        info, model = self.choose(script)
        self.assertEqual(info["name"], "claude-sonnet-5-5")
        self.assertEqual(script.said.count("\nModels recommended for this task. Type p 2 for the price of model 2, or p all for the prices of all of them (type back to choose again where the model runs):"), 2)
        script = Script(["2", "BACK", "1", "1", "1"])
        info, model = self.choose(script)
        self.assertEqual(info["name"], "google/gemma-4-E4B-it")
        self.assertEqual(script.said.count("\nWhere must the model of agent 1 run?\n  Local: runs on your GPUs (51.6 GB of VRAM, 51.1 GB free now). Free to use and private, but the model must fit in the VRAM.\n  API: runs on the servers of a company (OpenAI, Anthropic, Google, DeepSeek). You pay for every use, and you need an API key.\n  Coding agent: Claude Code (with your Anthropic API key) or Codex (with your ChatGPT plan), on this computer. It can also read files and run "
                                           "commands, each time with your approval."), 2)

    def testTheLeaderIsToldItsModelMattersMoreAndOtherAgentsAreNot(self):
        script = Script(["2", "1", "n", "n"], ["sk"])
        self.choose(script, number=1)
        self.assertIn("This agent is the leader", script.text())
        script = Script(["2", "1", "n", "n"])
        self.choose(script, "Writer2", number=2)
        self.assertNotIn("This agent is the leader", script.text())

    def testMissingLibrariesAreAnnouncedForApiModelsToo(self):
        self.missing[:] = [["anthropic"], []]
        script = Script(["2", "1", "", "n", "n"], ["sk"])
        self.choose(script)
        self.assertIn("This model needs the packages anthropic. Install them in another terminal with: pip install -U anthropic", script.text())


# A model that answers like the prompts of the swarm ask: plans, summaries, texts and the order of the agents.
class ScriptedModel(FakeModel):
    def input(self, prompt):
        self.prompts.append(prompt)
        self.usage["calls"] += 1
        names = list(dict.fromkeys(re.findall(r"^- (\w+) \(", prompt, re.MULTILINE)))
        if "Decide which agents must wait" in prompt:
            workers = re.findall(r"^- (\w+):", prompt, re.MULTILINE)
            return json.dumps({name: ([workers[number - 1]] if number else []) for number, name in enumerate(workers)})
        if "Reply only with JSON that gives, for every concerned agent" in prompt:
            return "{}"
        if "Write one summary" in prompt or "wants to cancel the swarm" in prompt:
            return "Summary of: " + ", ".join(names)
        if "Write a plan for your task" in prompt:
            return "Plan: write the text carefully."
        if "Write a text about" in prompt:
            return "A short text."
        return "OK"


# The whole program with its models replaced by a pretend one, and a folder of its own for everything it saves.
class ProgramCase(GpuTestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.models, self.busy = [], False
        def create(info, apiKeys=None, token=None, report=None):
            model = ScriptedModel(info["name"])
            self.models.append(model)
            return model
        patches = {harness_utils: {"AGENT_FILES": Path(folder.name)},
                   cli: {"createModel": create, "findMissingPackages": lambda info: [], "isDownloaded": lambda name: True, "getHubFolder": lambda: Path(folder.name),
                         "getModelCost": lambda provider, model: {**COST, "model": model}}}
        for target, replacements in patches.items():
            for name, replacement in replacements.items():
                patcher = everywhere(target, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
        usePrices(self)
        environment = mock.patch.dict(cli.os.environ, {"ANTHROPIC_API_KEY": "sk-from-env"})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(lambda: self.assertFalse([name for name in threading.enumerate() if name.name.startswith("never")]))

    # builder is the first answer: who builds the swarm (1: the user, agent by agent). budget is the answer after the mission (empty: no budget).
    def program(self, answers, secrets=(), builder="1", budget=""):
        answers = list(answers)
        if builder:
            answers.insert(1 if builder == "2" else 2, budget)
        script = Script([builder, *answers] if builder else answers, secrets)
        cli.runProgram(script.console())
        self.assertEqual(script.answers, [], "The program did not ask all the questions of the script.")
        return script


class EndToEndTests(ProgramCase):
    def testTwoWritersPlanTheirWorkAndThenExecuteIt(self):
        script = self.program(["2", "Write two short texts",
                               "4", "the sea", "", "", "",
                               "4", "the sky", "100", "", "",
                               "", "",
                               "2", "1", "n", "n",
                               "2", "1", "n", "n",
                               "1", "1",
                               "yes", "y", "yes", "yes"])
        text = script.text()
        for line in ("Step 1: the task of each agent", "Step 2: the folder of each agent", "Step 3: the model of each agent", "Your swarm", "How many agents do you want in your swarm?"):
            self.assertIn(line, script.text() + "How many agents do you want in your swarm?")
        self.assertIn("Writer will do this: Write a text about the sea in at most 300 words.", text)
        self.assertIn("Writer2 will do this: Write a text about the sky in at most 100 words.", text)
        self.assertIn("The first agent, Writer, is the leader", text)
        self.assertIn("== The swarm starts in plan mode ==", text)
        self.assertIn("== The swarm starts in execute mode ==", text)
        self.assertIn("Summary of the plans:\nSummary of: Writer, Writer2", text)
        self.assertIn("Summary of the results:", text)
        self.assertIn("Writer2 is ready: it waits for you", text)
        self.assertIn("Writer is ready: it waits for you", text)
        self.assertIn("\nThe plans are approved. Execute them now? (Y/n) ", script.prompts())
        self.assertIn("approved plan: Plan: write the text carefully.", text)
        self.assertIn("- Writer: done. result: A short text.", text)
        self.assertIn("- Writer2: done. result: A short text.", text)
        self.assertIn("Tokens used (API models are billed for them):", text)
        self.assertEqual(sorted(harness_utils.loadContext("author_contexts.json")), ["the sea", "the sky"])
        self.assertIn("Bye.", script.said[-1])
        self.assertEqual(len(self.models), 2)
        self.assertTrue(all(any("Your plan, approved by the user" in prompt for prompt in model.prompts) for model in self.models))
        self.assertIn("by typing its name (type help to see how)", text)
        self.assertNotIn("by clicking on its name", text)

    def testASingleLocalAgentExecutesRightAwayAndIsNotSummarised(self):
        script = self.program(["1", "Write a text",
                               "4", "the sea", "", "", "",
                               "",
                               "1", "1",
                               "2", "1", "yes"])
        text = script.text()
        self.assertIn("VRAM the swarm is expected to need: 19.2 GB. Left in the GPUs: 32.4 GB.", text)
        self.assertIn("Swarm: Write a text | mode: execute | VRAM: 19.2 of 51.6 GB expected, 51.1 GB free now", text)
        self.assertIn("Summary of the results:\n- Writer (writer): waiting for the user: A short text.", text)
        self.assertEqual(len(self.models), 1)
        self.assertEqual(len(self.models[0].prompts), 1)
        self.assertIn("- Writer: done. result: A short text.", text)
        self.assertIn("Writer is ready: it waits for you", text)
        self.assertIn("Writer [writer] google/gemma-4-E4B-it, local, 19.2 GB -> done", text)
        self.assertEqual(sorted(harness_utils.loadContext("author_contexts.json")), ["the sea"])

    def testAnAgentWorksInsideTheFolderTheUserChoseAfterTheTasksAndBeforeTheModels(self):
        work = self.folder / "essays"
        work.mkdir()
        script = self.program(["1", "Write a text",
                               "4", "the sea", "", "", "",
                               str(work),
                               "1", "1",
                               "2", "1", "yes"])
        text = script.text()
        steps = [text.index(f"Step {number}:") for number in (1, 2, 3)]
        self.assertEqual(steps, sorted(steps))
        self.assertLess(text.index("--- Folder of agent 1 of 1: Writer (writer) ---"), text.index("--- Model of agent 1 of 1: Writer (writer) ---"))
        self.assertIn(f"It works inside the folder {work.resolve()}.", text.split("--- Model of agent 1 of 1")[1])
        [saved] = list(work.glob("text_*.md"))
        self.assertEqual(saved.read_text(encoding="utf-8"), "A short text.")

    def testTheMenuChangesTheOrderAndTheModelsBeforeTheStart(self):
        cli.os.environ["OPENAI_API_KEY"] = "sk-openai"
        script = self.program(["3", "Three texts",
                               "4", "a", "", "", "", "4", "b", "", "", "", "4", "c", "", "", "",
                               "", "", "",
                               "2", "1", "n", "n", "2", "1", "n", "n", "2", "1", "n", "n",
                               "1",
                               "4",
                               "3", "3", "", "1",
                               "2", "2", "2", "2", "n", "n",
                               "5"])
        text = script.text()
        self.assertIn("Who waits for whom? An agent that waits starts when the agents it waits for are done, and receives their results.", text)
        self.assertIn("Which agents must Writer2 wait for?", text)
        self.assertIn("Which agents must Writer3 wait for?", text)
        self.assertIn("Writer3 [writer] claude-sonnet-5-5, API -> waiting for Writer2  waits for: Writer2 (waiting)", text)
        self.assertIn("The model of which agent do you want to change?", text)
        self.assertIn("You chose gpt-6-luna (OpenAI).", text)
        self.assertEqual(self.models[-1].name, "gpt-6-luna")
        self.assertIn("Using the API key found in OPENAI_API_KEY.", text)
        self.assertEqual(script.said[-1], "\nBye.")
        self.assertNotIn("The swarm starts.", text)

    def testTheLeaderCanDecideWhoWaitsForWhom(self):
        script = self.program(["3", "Three texts",
                               "4", "a", "", "", "", "4", "b", "", "", "", "4", "c", "", "", "",
                               "", "", "",
                               "2", "1", "n", "n", "2", "1", "n", "n", "2", "1", "n", "n",
                               "1",
                               "3", "2", "yes",
                               "5"])
        text = script.text()
        self.assertIn("The leader is working out the order...", text)
        self.assertIn("The order is approved.", text)
        self.assertIn('{"Writer2": [], "Writer3": ["Writer2"]}', text)
        self.assertIn("waits for: Writer2 (waiting)", text)
        self.assertNotIn("Writer2 [writer] claude-sonnet-5-5, API -> waiting for", text.split("The order is approved.")[1])

    def testASwarmThatCannotRunBecauseOfTheGpusCanBeStartedAgainWhenTheyAreFree(self):
        busy = lambda: self.currentGpus.__setitem__(slice(None), [{"name": "G", "total": 51.6, "free": 5.0}]) or "1"
        free = lambda: self.currentGpus.__setitem__(slice(None), [dict(gpu) for gpu in GPUS]) or "1"
        script = self.program(["1", "A text",
                               "4", "the sea", "", "", "",
                               "",
                               "1", "1",
                               "2", busy, free, "yes"])
        text = script.text()
        self.assertIn("The swarm could not run: The swarm needs about 19.2 GB of VRAM. Your GPUs have 51.6 GB in total, but only 5.0 GB are free now because other jobs use 46.6 GB.", text)
        self.assertIn("You will not be able to run the swarm until they free enough memory.", text)
        self.assertIn("Change a model or free some GPU memory, then start again.", text)
        self.assertIn("- Writer: done. result: A short text.", text)

    def testTheLeaderBuildsTheSwarmWithTheFolderAndTheModelTheUserChose(self):
        from test_leader_utils import LeaderModel, block
        import leader_utils
        (self.folder / "draft.md").write_text("Bees.", encoding="utf-8")
        agents = [{"name": "Writer", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "bees", "length": 200}, "why": "It writes the text."},
                  {"name": "Shortener", "task": "author", "model": "claude-haiku-4-5", "waits_for": ["Writer"], "settings": {"subject": "the text in 50 words", "length": 50},
                   "why": "It writes the short version."}]
        leader = LeaderModel(build=["Here is my swarm.\n" + block("build", agents=agents)])
        created = []
        def create(info, apiKeys=None, token=None, report=None):
            created.append(info["name"])
            return leader if info["name"] == "claude-opus-5-5" else ScriptedModel(info["name"])
        for target, name, value in ((cli, "createModel", create), (leader_utils, "checkCodex", lambda: {"problem": "Codex is not installed."}),
                                    (leader_utils, "findMissingPackages", lambda info: []), (leader_utils, "readGpus", lambda: self.currentGpus)):
            patcher = everywhere(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        script = self.program(["Write a text about bees, and a short version of it.", str(self.folder), "2", "1", "n", "n", "yes", "2", "5"], builder="2")
        text = script.text()
        self.assertIn("Who builds the swarm?", text)
        self.assertIn("--- Model of agent 1 of 1: Leader (leader) ---", text)
        self.assertIn("This leader builds the swarm, follows it, and proposes changes to you", text)
        self.assertIn("[Leader] proposes a swarm of 2 agents for your mission:\n1. Writer: Writer (texts, essays, articles), with claude-sonnet-5-5 (Anthropic API), "
                      "$15.00 per 1M tokens. It starts right away.", text)
        self.assertIn("2. Shortener: Writer (texts, essays, articles), with claude-haiku-4-5 (Anthropic API), $5.00 per 1M tokens. It waits for Writer.", text)
        self.assertIn("Why: Here is my swarm.", text)
        self.assertIn("Shortener [writer] claude-haiku-4-5, API -> waiting for Writer  waits for: Writer (waiting)", text)
        self.assertIn("draft.md", leader.prompts[0])
        self.assertEqual(created, ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"])

    def testLeavingBeforeTheStartDoesNotRunAnything(self):
        script = self.program(["1", "A text", "4", "the sea", "", "", "", "", "2", "1", "n", "n", "1", "5"])
        self.assertNotIn("The swarm starts", script.text())
        self.assertEqual(sum(len(model.prompts) for model in self.models), 0)

    def testTheTreeShownBeforeTheStartListsEveryAgentWithItsModel(self):
        script = self.program(["2", "Two texts", "4", "a", "", "", "", "4", "b", "", "", "", "", "", "2", "1", "n", "n", "2", "1", "n", "n", "1", "5"])
        tree = script.text().split("Your swarm")[1]
        self.assertIn("Writer [writer] claude-sonnet-5-5, API -> waiting for Writer2", tree)
        self.assertIn("`-- Writer2 [writer] claude-sonnet-5-5, API -> waiting", tree)
        self.assertIn("Swarm: Two texts | mode: execute", tree)

def savedMember(subject, status, result=None, **fields):
    return {"role": "writer", "task": f"Write a text about {subject} in at most 300 words.", "boss": None, "waitsFor": [], "model": getModelInfo("claude-sonnet-5-5"),
            "recipe": {"task": "author", "answers": {"subject": subject, "length": 300}}, "status": status, "result": result, "error": "", "mode": "execute",
            "review": "approved" if status == "done" else "", "draft": "", "problem": "", "revision": 1, "started": status != "waiting", "startAt": None,
            "state": {"progress": {}, "actions": [], "inbox": [], "userMessages": [], "approvedPlan": ""}, **fields}


# What a program that stopped leaves on the disk: a swarm of two writers, the leader done and the other one at work.
def savedSwarm(swarmId="20261004_073000_abcd", members=None, **fields):
    members = members or {"Writer": savedMember("the sea", "done", "Text about the sea."), "Writer2": savedMember("the sky", "working")}
    return {"version": 1, "id": swarmId, "mission": "Write two texts", "mode": "execute", "leader": "Writer", "state": "running",
            "heartbeat": datetime.now().timestamp() - 1000, "pid": os.getpid() + 1, "savedAt": "2026-10-04 07:31:00", "summary": "", "messages": [],
            "members": members, **fields}


class InterruptedSwarmTests(ProgramCase):
    def leave(self, saved=None):
        saved = saved or savedSwarm()
        saveSwarmState(saved["id"], saved)
        return saved

    def offer(self, answers, secrets=()):
        script = Script(answers, secrets)
        result = cli.offerUnfinished(script.console())
        self.assertEqual(script.answers, [], "The program did not ask all the questions of the script.")
        return result, script

    def promptsOfModels(self):
        return [prompt for model in self.models for prompt in model.prompts]

    def testNothingIsOfferedWhenNothingWasInterrupted(self):
        result, script = self.offer([])
        self.assertFalse(result)
        self.assertEqual(script.said, [])

    def testAnInterruptedSwarmIsContinuedWhereItStopped(self):
        saved = self.leave()
        result, script = self.offer(["1", "yes"])
        self.assertTrue(result)
        text = script.text()
        self.assertIn("A swarm was interrupted", text)
        self.assertIn("The swarm \"Write two texts\" was interrupted at 2026-10-04 07:31:00. The program or the computer stopped, and your work is saved.", text)
        self.assertIn("  - Writer (writer): done\n  - Writer2 (writer): working", text)
        self.assertIn("Continuing your swarm", text)
        self.assertIn("The swarm goes on.", text)
        self.assertIn("Using the API key found in ANTHROPIC_API_KEY.", text)
        self.assertEqual(len(self.models), 2)
        self.assertEqual(sorted(harness_utils.loadContext("author_contexts.json")), ["the sky"])
        self.assertFalse(any("Write a text about the sea" in prompt for prompt in self.promptsOfModels()))
        self.assertTrue(any("Write a text about the sky" in prompt for prompt in self.promptsOfModels()))
        self.assertFalse(swarmStatePath(saved["id"]).exists())
        self.assertIn("- Writer2: done. result: A short text.", text)

    def testAConnectionThatWasLostIsToldAsSuch(self):
        self.leave(savedSwarm(state="paused"))
        result, script = self.offer(["3"])
        self.assertFalse(result)
        self.assertIn("The connection was lost, and your work is saved.", script.text())

    def testCancellingShowsTheSummaryOfTheLeaderAndStopsAfterTheConfirmation(self):
        saved = self.leave()
        result, script = self.offer(["2", "1"])
        self.assertFalse(result)
        text = script.text()
        self.assertIn("[Writer] Summary of what the swarm did so far:\nSummary of: Writer, Writer2", text)
        self.assertIn("If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task.", text)
        self.assertIn("The swarm is stopped.", text)
        self.assertNotIn("The swarm goes on", text)
        self.assertFalse(swarmStatePath(saved["id"]).exists())
        self.assertEqual(findUnfinishedSwarms(), [])
        self.assertTrue(any("wants to cancel the swarm" in prompt and "Writer2 (writer): has not started yet" in prompt for prompt in self.promptsOfModels()))

    def testCancellingAndThenGoingOnContinuesTheSwarm(self):
        saved = self.leave()
        result, script = self.offer(["2", "2", "yes"])
        self.assertTrue(result)
        self.assertIn("Summary of what the swarm did so far", script.text())
        self.assertIn("The swarm goes on.", script.text())
        self.assertFalse(swarmStatePath(saved["id"]).exists())

    def testWithoutTheKeyOfTheLeaderTheUserGetsTheFactsAsTheyAre(self):
        self.leave()
        with mock.patch.dict(cli.os.environ):
            del cli.os.environ["ANTHROPIC_API_KEY"]
            result, script = self.offer(["2", "1"], [""])
        self.assertFalse(result)
        text = script.text()
        self.assertIn("Press Enter to skip it and get a plain list instead.", text)
        self.assertIn("The leader could not write the summary, so these are the facts:\n- Writer (writer): done and approved by the user: Text about the sea.\n- Writer2 (writer): has not started yet", text)
        self.assertEqual(self.models, [])

    def testLeavingItForLaterKeepsEverything(self):
        saved = self.leave()
        result, script = self.offer(["3"])
        self.assertFalse(result)
        self.assertTrue(swarmStatePath(saved["id"]).exists())
        self.assertEqual([found["id"] for found in findUnfinishedSwarms()], [saved["id"]])
        self.assertEqual(self.models, [])

    def testASwarmThatSeemsToWorkInAnotherWindowIsLeftAlone(self):
        saved = self.leave(savedSwarm(heartbeat=datetime.now().timestamp() - 2))
        result, script = self.offer([])
        self.assertFalse(result)
        self.assertIn("seems to be working in another window of the program, so it is left alone.", script.text())
        self.assertTrue(swarmStatePath(saved["id"]).exists())

    def testASwarmOfAnotherProgramCannotBeContinuedHere(self):
        saved = self.leave(savedSwarm(members={"Writer": savedMember("the sea", "waiting", recipe=None)}))
        result, script = self.offer(["1"])
        self.assertFalse(result)
        self.assertIn("This swarm was made by another program, so it cannot be continued here.", script.text())
        self.assertTrue(swarmStatePath(saved["id"]).exists())

    def testAFolderThatDisappearedKeepsTheSwarmSavedForALaterTry(self):
        gone = str(self.folder / "gone")
        saved = self.leave(savedSwarm(members={"Writer": savedMember("the sea", "waiting", recipe={"task": "author", "answers": {"subject": "the sea", "length": 300, "folder": gone}})}))
        result, script = self.offer(["1"])
        self.assertFalse(result)
        self.assertIn(f"The swarm cannot be continued yet: There is no folder at {gone}. It stays saved, so you can try again.", script.text())
        self.assertTrue(swarmStatePath(saved["id"]).exists())

    def testThePasswordsAreAskedAgainAndNeverWritten(self):
        news = {"outlets": ["BBC World"], "topics": "", "collectAt": None, "maxWords": 250, "language": "English", "messenger": "Telegram", "telegramChat": "42"}
        members = {"Writer": savedMember("the sea", "done", "Text about the sea."),
                   "NewsBriefer": savedMember("x", "done", "A briefing", role="news briefer", recipe={"task": "news", "answers": news})}
        saved = self.leave(savedSwarm(members=members))
        result, script = self.offer(["1"], ["123:NEW-TOKEN"])
        self.assertTrue(result)
        text = script.text()
        self.assertIn("--- NewsBriefer (news briefer) ---", text)
        self.assertEqual(script.secrets, [])
        self.assertTrue(any(prompt.startswith("Token of your Telegram bot") for prompt in script.prompts() + [prompt for prompt, answer in script.log]))
        self.assertNotIn("NEW-TOKEN", text)
        for file in self.folder.rglob("*"):
            if file.is_file():
                self.assertNotIn("NEW-TOKEN", file.read_text(encoding="utf-8", errors="replace"), str(file))

    def testAccountsOfTheLiteratureReviewerAreAskedAgain(self):
        literature = {"subject": "graphs", "length": 400, "searches": ["arXiv"], "publishers": {}}
        members = {"Writer": savedMember("the sea", "done", "Text about the sea."), "Reviewer": savedMember("x", "done", "A survey", role="literature reviewer",
                                                                                                         recipe={"task": "literature", "answers": literature})}
        self.leave(savedSwarm(members=members))
        result, script = self.offer(["1", "n"])
        self.assertTrue(result)
        self.assertIn("Add an account for a website? (y/N) ", script.prompts())

    def testAStateWrittenByARealRunIsContinuedByTheProgram(self):
        started, release = threading.Event(), threading.Event()
        class BlockingModel(ScriptedModel):
            def input(self, prompt):
                if "Write a text about" in prompt:
                    started.set()
                    release.wait(10)
                return super().input(prompt)
        script = Script(["yes", "yes"])
        console = script.console()
        swarm, info = Swarm("Two texts"), getModelInfo("claude-sonnet-5-5")
        for name, subject in (("Writer", "the sea"), ("Writer2", "the sky")):
            cli.buildAgent(console, swarm, {"task": "author", "answers": {"subject": subject, "length": 300}, "name": name}, info, BlockingModel(info["name"]))
        swarm.startInBackground()
        self.assertTrue(started.wait(10))
        snapshot = json.loads(swarmStatePath(swarm.id).read_text(encoding="utf-8"))
        snapshot["heartbeat"] -= 1000
        release.set()
        swarm.wait(10)
        self.assertEqual(script.answers, [])
        self.assertFalse(swarmStatePath(swarm.id).exists())
        saveSwarmState(snapshot["id"], snapshot)
        self.assertEqual({name: member["status"] for name, member in snapshot["members"].items()}, {"Writer": "waiting", "Writer2": "working"})
        result, script = self.offer(["1", "yes", "yes"])
        self.assertTrue(result)
        self.assertEqual(len(self.models), 2)
        self.assertTrue(any("Write a text about the sea" in prompt for prompt in self.models[0].prompts))
        self.assertTrue(any("Write a text about the sky" in prompt for prompt in self.models[1].prompts))
        self.assertIn("- Writer: done. result: A short text.", script.text())
        self.assertIn("- Writer2: done. result: A short text.", script.text())

    def testAProgramThatStopsWhileTheSwarmRunsSavesItAsInterrupted(self):
        swarm = Swarm("Mission")
        gate = threading.Event()
        leader = Loop(FakeAgent())
        leader.run = lambda: gate.wait(10) and "done"
        swarm.addAgent("Leader", leader, "boss", "lead", recipe={"task": "author"})
        console = Script().console()
        with everywhere(swarm, "wait", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            cli.runSwarm(console, swarm)
        self.assertEqual(json.loads(swarmStatePath(swarm.id).read_text(encoding="utf-8"))["state"], "interrupted")
        gate.set()


# A model that loses the connection the first time it is asked to write.
class LosingModel(ScriptedModel):
    def input(self, prompt):
        if "Write a text about" in prompt and not getattr(self, "lost", False):
            self.lost = True
            raise ConnectionLost("The internet connection was lost.")
        return super().input(prompt)


# The user at the keyboard while the swarm runs: what is typed goes through the same pump as in the real program.
class KeyboardTests(ProgramCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(cli.os.environ, {"NO_COLOR": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.typed, self.said = queue.Queue(), []
        self.console = cli.Console(read=lambda prompt: self.typed.get(timeout=10), readSecret=lambda prompt: "", write=self.said.append, interactive=True, treeDelay=0)
        self.swarm, info = Swarm("Two texts"), getModelInfo("claude-sonnet-5-5")
        for name, subject, model in (("Writer", "the sea", ScriptedModel(info["name"])), ("Writer2", "the sky", LosingModel(info["name"]))):
            cli.buildAgent(self.console, self.swarm, {"task": "author", "answers": {"subject": subject, "length": 300}, "name": name}, info, model)
        self.run_ = threading.Thread(target=lambda: cli.runSwarm(self.console, self.swarm), daemon=True)
        self.run_.start()

    def question(self, start):
        waitUntil(lambda: self.console.pending and self.console.pending["text"].startswith(start), f"the question {start!r}")

    # Types a line, and waits until the question it answers has been taken, because the next question can be the same one.
    def answer(self, line):
        previous = self.console.pending
        self.typed.put(line)
        waitUntil(lambda: self.console.pending is not previous, "the answer to be taken")

    def finish(self):
        self.question("Do you approve?")
        self.answer("yes")
        self.question("Do you approve?")
        self.answer("yes")
        waitUntil(lambda: "Press Enter to continue." in "\n".join(self.said), "the end of the swarm")
        self.typed.put("")
        self.run_.join(10)
        self.assertFalse(self.run_.is_alive())

    def testTypingContinueResumesThePausedSwarm(self):
        self.question("Type continue to try again, or cancel to stop here:")
        self.assertEqual(self.swarm.getStatus("Writer2"), "paused")
        self.assertTrue(any("Writer2 lost its connection and is paused" in line for line in self.said))
        self.assertTrue(any("PAUSED: the connection was lost" in line for line in self.said))
        with everywhere(harness_utils, "isOnline", lambda: False):
            self.answer("continue")
            self.question("Type continue to try again, or cancel to stop here:")
            self.assertIn("There is still no internet connection", "\n".join(self.said))
            self.assertEqual(self.swarm.getStatus("Writer2"), "paused")
        with everywhere(harness_utils, "isOnline", lambda: True):
            self.answer("continue")
            self.finish()
        self.assertEqual(self.swarm.getStatuses(), {"Writer": "done", "Writer2": "done"})
        self.assertIn("The swarm goes on", "\n".join(self.said))
        self.assertFalse(swarmStatePath(self.swarm.id).exists())

    def testTypingCancelThenStopStopsTheSwarmAfterTheSummary(self):
        self.question("Type continue to try again, or cancel to stop here:")
        self.answer("cancel")
        self.question("Type stop to stop here, or continue to go on until the end:")
        shown = "\n".join(self.said)
        self.assertIn("Summary of what the swarm did so far:\nSummary of: Writer, Writer2", shown)
        self.assertIn("the agents that did not finish are cut in the middle of their task", shown)
        self.assertEqual(self.swarm.getStatus("Writer2"), "paused")
        self.answer("stop")
        waitUntil(lambda: "Press Enter to continue." in "\n".join(self.said), "the end of the swarm")
        self.typed.put("")
        self.run_.join(10)
        self.assertEqual(self.swarm.getStatus("Writer2"), "failed")
        self.assertEqual(self.swarm.getInfo("Writer2")["error"], "The user stopped the swarm before this agent finished.")
        self.assertIn("== The swarm was stopped by you ==", "\n".join(self.said))
        self.assertFalse(swarmStatePath(self.swarm.id).exists())


if __name__ == "__main__":
    unittest.main()


class LiveTeamTests(ProgramCase):
    def testAnAgentIsAddedAndAnotherRemovedFromTheConsole(self):
        swarm = Swarm("Bees")
        swarm.addAgent("Leader", Loop(None), "leader", "lead")
        swarm.addAgent("A", Loop(None), "writer", "write")
        script = Script(["4", "the sea", "", "", "", "", "2", "1", "n", "n", ""])
        console, specs, models = script.console(), [], {}
        cli.addLive(console, swarm, specs, {}, {}, models)
        self.assertEqual(script.answers, [], "The program did not ask all the questions of the script.")
        self.assertEqual((swarm.getAgents(), [spec["name"] for spec in specs], list(models)), (["Leader", "A", "Writer"], ["Writer"], ["Writer"]))
        self.assertIn("Which agents must Writer wait for?", script.text())
        self.assertIn("Writer joined the swarm. Every agent was told.", script.text())
        cli.runCommand(console, swarm, "remove A not needed anymore")
        self.assertEqual(swarm.getAgents(), ["Leader", "Writer"])
        self.assertEqual(swarm.removed[0]["reason"], "not needed anymore")
        self.assertIn("Done: remove A.", script.text())
        cli.runCommand(console, swarm, "remove Leader")
        self.assertIn("The leader cannot be removed", script.text())
        console.newAgent = None
        cli.runCommand(console, swarm, "add")
        self.assertIn("Agents cannot be added here.", script.text())
