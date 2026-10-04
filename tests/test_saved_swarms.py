import json
import os
import queue
import sys
import threading
import time
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import harness_utils
from harness_utils import (STOPPED_MESSAGE, AuthorLoop, CoderLoop, ConnectionLost, EmailLoop, Loop, NewsLoop, Swarm, findUnfinishedSwarms, saveSwarmState,
                           swarmStatePath)
from test_harness_utils import DraftLoop, FakeAgent, LoopTestCase, TimedDraftLoop


def waitUntil(condition, what, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"Timeout while waiting for {what}")


def failing(error):
    raise error


# A pretend model. Each step is a text, an error to raise, or a function of the prompt.
class Model:
    def __init__(self, *steps):
        self.steps, self.prompts = list(steps), []

    def input(self, prompt):
        self.prompts.append(prompt)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(prompt) if callable(step) else step


# The user at the keyboard. The questions that start like a key of auto are answered at once, the other ones wait in questions
# until the test puts the answer in replies.
class Human:
    def __init__(self, **auto):
        self.auto = auto
        self.questions, self.replies, self.said = queue.Queue(), queue.Queue(), []

    def attach(self, loop):
        loop.notifyUser = self.said.append
        loop.askUser = self.ask
        return loop

    def ask(self, question):
        for start, reply in self.auto.items():
            if question.startswith(start.replace("_", " ")):
                return reply
        self.questions.put(question)
        return self.replies.get(timeout=10)

    def next(self):
        return self.questions.get(timeout=10)

    def text(self):
        return "\n".join(self.said)


class SavedSwarmCase(LoopTestCase):
    def readState(self, swarm):
        path = swarmStatePath(swarm.id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def savedMember(self, swarm, name):
        state = self.readState(swarm)
        return state["members"][name] if state else {}

    def statusesInFile(self, swarm):
        state = self.readState(swarm)
        return {name: member["status"] for name, member in state["members"].items()} if state else None

    # What a computer that stops leaves behind: the state as it was at that moment, with a heartbeat that nobody renews anymore.
    def crashed(self, swarm):
        saved = self.readState(swarm)
        saved["heartbeat"] -= 1000
        return saved

    def leaveBehind(self, saved):
        saveSwarmState(saved["id"], saved)
        return findUnfinishedSwarms()

    def plainLoop(self, result="done", gate=None, ran=None, name=""):
        loop = Loop(FakeAgent())
        def run():
            if ran is not None:
                ran.append(name)
            if gate is not None:
                gate.wait(10)
            return result
        loop.run = run
        return loop

    def swarmOf(self, mission="Test mission", **loops):
        swarm = Swarm(mission)
        for name, loop in loops.items():
            swarm.addAgent(name, loop, f"role {name}", f"task {name}", recipe={"name": name})
        return swarm


class SavedStateTests(SavedSwarmCase):
    def testTheStateIsSavedWhileTheSwarmRunsAndDeletedWhenItEnds(self):
        gate = threading.Event()
        swarm = self.swarmOf("Write the report", Leader=self.plainLoop("final"), Slow=self.plainLoop(gate=gate))
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) == {"Leader": "waiting", "Slow": "working"}, "the first state")
        saved = self.readState(swarm)
        self.assertEqual((saved["version"], saved["id"], saved["mission"], saved["leader"], saved["mode"], saved["state"]),
                         (1, swarm.id, "Write the report", "Leader", "execute", "running"))
        self.assertEqual(saved["pid"], os.getpid())
        self.assertLess(datetime.now().timestamp() - saved["heartbeat"], 5)
        self.assertEqual(saved["members"]["Slow"]["recipe"], {"name": "Slow"})
        self.assertEqual(list(saved["members"]), ["Leader", "Slow"])
        self.assertTrue(swarm.isRunning())
        gate.set()
        self.assertEqual(swarm.wait(10), {"result": "final"})
        self.assertFalse(swarmStatePath(swarm.id).exists())
        self.assertFalse(swarm.isRunning())

    def testNothingIsWrittenBeforeTheSwarmRuns(self):
        swarm = self.swarmOf(Leader=self.plainLoop())
        swarm.sendUserMessage("Leader", "hello")
        self.assertFalse((self.folder / harness_utils.RUNS_FOLDER).exists())

    def testNoPasswordTokenOrAccountIsEverWritten(self):
        gate = threading.Event()
        email = EmailLoop(FakeAgent(), "me@example.com", "sara@example.com", "Meeting", "Ask")
        email.settings.update(EMAIL_PASSWORD="hunter2-secret", EMAIL_SMTP_SERVER="smtp.test")
        news = NewsLoop(FakeAgent(), messenger="Telegram", messengerSettings={"token": "123:BOT-SECRET", "chat": "42"})
        survey = harness_utils.LiteratureSurveyLoop(FakeAgent(), "graphs", 100)
        survey.logins["publisher.test"] = ("me", "account-secret")
        for loop in (email, news, survey):
            loop.run = lambda: gate.wait(10) and "done"
        swarm = self.swarmOf(Leader=self.plainLoop("final"), Emailer=email, Newsman=news, Reviewer=survey)
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) and self.statusesInFile(swarm)["Reviewer"] == "working", "all the agents at work")
        content = swarmStatePath(swarm.id).read_text(encoding="utf-8")
        for secret in ("hunter2-secret", "BOT-SECRET", "account-secret"):
            self.assertNotIn(secret, content)
        gate.set()
        swarm.wait(10)

    def testAWriteThatIsCutShortNeverBreaksTheSavedState(self):
        saveSwarmState("abc", {"version": 1, "number": 1})
        path = swarmStatePath("abc")
        with mock.patch.object(harness_utils.os, "replace", side_effect=OSError("power cut")), self.assertRaises(OSError):
            saveSwarmState("abc", {"version": 1, "number": 2})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["number"], 1)
        saveSwarmState("abc", {"version": 1, "number": 3})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["number"], 3)
        self.assertEqual(sorted(file.name for file in path.parent.iterdir()), [path.name])

    def testOnlySwarmsThatDidNotEndAreFoundNewestFirst(self):
        now = datetime.now().timestamp()
        def leave(swarmId, **fields):
            base = {"version": 1, "id": swarmId, "mission": swarmId, "mode": "execute", "leader": "A", "state": "interrupted", "heartbeat": now - 1000,
                    "pid": os.getpid() + 1, "savedAt": "2026-10-04 07:30:00", "summary": "", "messages": [], "members": {"A": {"status": "waiting"}}}
            saveSwarmState(swarmId, {**base, **fields})
        leave("old", heartbeat=now - 500)
        leave("newer", heartbeat=now - 100, state="paused")
        leave("elsewhere", state="running", heartbeat=now - 2)
        leave("justStopped", state="interrupted", heartbeat=now - 3)
        leave("mine", state="running", heartbeat=now - 2, pid=os.getpid())
        leave("stale", state="running", heartbeat=now - 200)
        leave("wrongVersion", version=99, heartbeat=now - 4)
        leave("noMembers", members={}, heartbeat=now - 5)
        runs = self.folder / harness_utils.RUNS_FOLDER
        (runs / "swarm_broken.json").write_text("{not json", encoding="utf-8")
        (runs / "swarm_half.json.tmp").write_text("{}", encoding="utf-8")
        (runs / "notes.txt").write_text("hello", encoding="utf-8")
        found = findUnfinishedSwarms()
        self.assertEqual([(state["id"], state["running"]) for state in found],
                         [("elsewhere", True), ("justStopped", False), ("newer", False), ("stale", False), ("old", False)])

    def testNoSavedSwarmsAtAllIsNotAnError(self):
        self.assertEqual(findUnfinishedSwarms(), [])

    def testTheStateIsRenewedEveryFewSeconds(self):
        gate = threading.Event()
        with mock.patch.object(harness_utils, "HEARTBEAT_SECONDS", 0.02):
            swarm = self.swarmOf(Leader=self.plainLoop("final"), Slow=self.plainLoop(gate=gate))
            swarm.startInBackground()
            waitUntil(lambda: self.readState(swarm), "the first state")
            first = self.readState(swarm)["heartbeat"]
            waitUntil(lambda: self.readState(swarm)["heartbeat"] > first, "a renewed state")
            gate.set()
            swarm.wait(10)

    def testASwarmThatCannotSaveItsStateGoesOnAndWarnsOnlyOnce(self):
        leader = self.plainLoop("final")
        said = []
        leader.notifyUser = said.append
        swarm = self.swarmOf(Leader=leader, Worker=self.plainLoop())
        with mock.patch.object(harness_utils, "saveSwarmState", side_effect=OSError("disk full")):
            self.assertEqual(swarm.run(), "final")
        waitUntil(lambda: any("could not be saved (disk full)" in line for line in said), "the warning")
        time.sleep(0.05)
        self.assertEqual(sum("could not be saved" in line for line in said), 1)

    def testAnythingThatGoesWrongWhileSavingNeverStopsTheSwarm(self):
        leader = self.plainLoop("final")
        said = []
        leader.notifyUser = said.append
        swarm = self.swarmOf(Leader=leader, Worker=self.plainLoop())
        with mock.patch.object(swarm, "describeState", side_effect=ValueError("Circular reference detected")):
            self.assertEqual(swarm.run(), "final")
        waitUntil(lambda: any("could not be saved (Circular reference detected)" in line for line in said), "the warning")

    def testAProgramCutShortByAnErrorLeavesTheStateAsInterrupted(self):
        swarm = self.swarmOf(Leader=self.plainLoop("final"))
        with mock.patch.object(swarm, "runExecution", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            swarm.run()
        [found] = findUnfinishedSwarms()
        self.assertEqual((found["id"], found["state"], found["running"]), (swarm.id, "interrupted", False))
        self.assertFalse(swarm.isRunning())

    def testAProgramThatIsAboutToStopSaysSo(self):
        gate = threading.Event()
        swarm = self.swarmOf(Leader=self.plainLoop("final"), Slow=self.plainLoop(gate=gate))
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) and self.statusesInFile(swarm)["Slow"] == "working", "the worker")
        swarm.saveForExit()
        self.assertEqual(self.readState(swarm)["state"], "interrupted")
        gate.set()
        swarm.wait(10)
        swarm.saveForExit()

    def testTheSwarmCanBeFollowedInTheBackground(self):
        gate = threading.Event()
        swarm = self.swarmOf(Leader=self.plainLoop("final"), Slow=self.plainLoop(gate=gate))
        swarm.startInBackground()
        self.assertTrue(swarm.isRunning())
        with self.assertRaisesRegex(ValueError, "already running"):
            swarm.startInBackground()
        self.assertEqual(swarm.wait(0.05), {})
        gate.set()
        self.assertEqual(swarm.wait(10), {"result": "final"})
        broken = self.swarmOf(Leader=self.plainLoop())
        broken.setLeader("Leader")
        broken.members["Leader"]["agent"].run = lambda: failing(KeyError("x"))
        broken.startInBackground()
        self.assertEqual(broken.wait(10), {"result": None})
        empty = Swarm("empty")
        empty.startInBackground()
        self.assertIsInstance(empty.wait(10)["error"], ValueError)


class ResumeTests(SavedSwarmCase):
    def pipeline(self, ran, gate):
        return self.swarmOf("Pipeline", Leader=self.plainLoop("final", ran=ran, name="Leader"), Fast=self.plainLoop("fast result", ran=ran, name="Fast"),
                            Slow=self.plainLoop("slow result", gate=gate, ran=ran, name="Slow"), After=self.plainLoop("after result", ran=ran, name="After"))

    def testASwarmThatWasCutShortGoesOnWhereItStoppedAndNothingRunsTwice(self):
        ran, gate = [], threading.Event()
        swarm = self.pipeline(ran, gate)
        swarm.setWaitsFor("After", ["Fast"])
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) == {"Leader": "waiting", "Fast": "done", "Slow": "working", "After": "done"}, "the state in the middle of the work")
        saved = self.crashed(swarm)
        gate.set()
        swarm.wait(10)
        ran.clear()
        [found] = self.leaveBehind(saved)
        self.assertEqual((found["mission"], found["running"], found["state"]), ("Pipeline", False, "running"))
        restored = Swarm.restore(found, lambda name, data: self.plainLoop(f"{name} again", ran=ran, name=name))
        self.assertEqual(restored.getStatuses(), {"Leader": "waiting", "Fast": "done", "Slow": "waiting", "After": "done"})
        self.assertEqual((restored.id, restored.mission, restored.getLeader(), restored.members["After"]["waitsFor"]), (swarm.id, "Pipeline", "Leader", ["Fast"]))
        self.assertEqual(restored.getInfo("Fast")["result"], "fast result")
        self.assertEqual(restored.resume(), "Leader again")
        self.assertEqual(ran, ["Slow", "Leader"])
        self.assertFalse(any(member["agent"].resumed for member in restored.members.values()))
        self.assertEqual(set(restored.getStatuses().values()), {"done"})
        counts = Counter((message["sender"], message["receiver"], message["message"]) for message in restored.messages)
        self.assertEqual(max(counts.values()), 1)
        self.assertEqual(restored.getInfo("Fast")["result"], "fast result")
        self.assertFalse(swarmStatePath(swarm.id).exists())

    def testWhatIsDoneCanBeUsedByTheAgentsThatWereWaitingForIt(self):
        ran, gate = [], threading.Event()
        swarm = self.swarmOf("Chain", Leader=self.plainLoop("final"), First=self.plainLoop("first result", gate=gate), Second=self.plainLoop("second result"))
        swarm.setWaitsFor("Second", ["First"])
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) == {"Leader": "waiting", "First": "working", "Second": "waiting"}, "the first agent at work")
        saved = self.crashed(swarm)
        gate.set()
        swarm.wait(10)
        [found] = self.leaveBehind(saved)
        restored = Swarm.restore(found, lambda name, data: self.plainLoop(f"{name} again"))
        self.assertEqual(restored.resume(), "Leader again")
        self.assertTrue(any("First: First again" in line for line in restored.members["Second"]["agent"].inbox))

    def testWhatTheUserTypedToAnAgentIsNotLost(self):
        gate = threading.Event()
        writer = self.plainLoop("text", gate=gate)
        swarm = self.swarmOf(Leader=self.plainLoop("final"), Writer=writer)
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) and self.statusesInFile(swarm)["Writer"] == "working", "the writer")
        swarm.sendUserMessage("Writer", "Mention the budget")
        waitUntil(lambda: "Mention the budget" in json.dumps(self.readState(swarm)), "the message in the state")
        saved = self.crashed(swarm)
        gate.set()
        swarm.wait(10)
        [found] = self.leaveBehind(saved)
        restored = Swarm.restore(found, lambda name, data: self.plainLoop())
        self.assertEqual(restored.members["Writer"]["agent"].userMessages, ["Mention the budget"])
        self.assertEqual([message["message"] for message in restored.getMessages("User", "Writer")], ["Mention the budget"])

    def testAResultIsNotDeliveredAgainToAnAgentThatAlreadyHasIt(self):
        gate = threading.Event()
        swarm = self.swarmOf("Chain", Leader=self.plainLoop("final"), First=self.plainLoop("first result"), Second=self.plainLoop("second result", gate=gate))
        swarm.setWaitsFor("Second", ["First"])
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) == {"Leader": "waiting", "First": "done", "Second": "working"}, "the second agent at work")
        saved = self.crashed(swarm)
        gate.set()
        swarm.wait(10)
        [found] = self.leaveBehind(saved)
        restored = Swarm.restore(found, lambda name, data: self.plainLoop(f"{name} again"))
        self.assertEqual(len(restored.getMessages("First", "Second")), 1)
        self.assertEqual(len(restored.members["Second"]["agent"].inbox), 2)
        self.assertEqual(restored.resume(), "Leader again")
        self.assertEqual(len(restored.getMessages("First", "Second")), 1)
        self.assertEqual(len([line for line in restored.members["Second"]["agent"].inbox if line.startswith("First:")]), 1)

    def testTheSavedStateIsUsedByAnotherProgramToShowTheTree(self):
        gate = threading.Event()
        swarm = self.swarmOf(Leader=self.plainLoop("final"), Slow=self.plainLoop(gate=gate))
        swarm.startInBackground()
        waitUntil(lambda: self.statusesInFile(swarm) and self.statusesInFile(swarm)["Slow"] == "working", "the worker")
        saved = self.crashed(swarm)
        gate.set()
        swarm.wait(10)
        restored = Swarm.restore(self.leaveBehind(saved)[0], lambda name, data: self.plainLoop())
        self.assertEqual([child["name"] for child in restored.getTree()["children"]], ["Slow"])
        self.assertEqual(restored.getInfo("Slow")["status"], "waiting")
        json.dumps(restored.getInfo("Slow"))

    # A swarm cut short while an agent waits for its time of the day. It returns the state as it was saved, and the human that approves.
    def cutWhileAgentSleeps(self):
        human = Human(Do_you_approve="yes")
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(["Summary of Sleeper"])
        swarm = self.swarmOf(Leader=leader, Sleeper=human.attach(TimedDraftLoop(FakeAgent(["draft"]), 3600)))
        swarm.startInBackground()
        waitUntil(lambda: self.savedMember(swarm, "Sleeper").get("startAt"), "the schedule in the state")
        saved = self.crashed(swarm)
        swarm.startNow("Sleeper")
        swarm.wait(10)
        return saved, human

    def restoreWithSleeper(self, saved, human):
        def build(name, data):
            if name == "Leader":
                leader = human.attach(self.plainLoop("final"))
                leader.agent = FakeAgent(["Summary of Sleeper"])
                return leader
            return human.attach(TimedDraftLoop(FakeAgent(["draft"]), 3600))
        return Swarm.restore(self.leaveBehind(saved)[0], build)

    def testAnAgentKeepsItsTimeAfterARestart(self):
        saved, human = self.cutWhileAgentSleeps()
        restored = self.restoreWithSleeper(saved, human)
        restored.startInBackground(resume=True)
        waitUntil(lambda: restored.getStartAt("Sleeper"), "the agent to wait for its time")
        self.assertEqual(restored.getStartAt("Sleeper"), saved["members"]["Sleeper"]["startAt"])
        self.assertEqual(restored.getStatus("Sleeper"), "waiting")
        restored.startNow("Sleeper")
        self.assertEqual(restored.wait(10), {"result": "final"})
        self.assertEqual(restored.getInfo("Sleeper")["result"], "draft")

    def testAnAgentWhoseTimePassedWhileTheProgramWasStoppedStartsAtOnce(self):
        saved, human = self.cutWhileAgentSleeps()
        saved["members"]["Sleeper"]["startAt"] = "2000-01-01 07:30"
        restored = self.restoreWithSleeper(saved, human)
        restored.startInBackground(resume=True)
        self.assertEqual(restored.wait(10), {"result": "final"})
        self.assertEqual(restored.getInfo("Sleeper")["result"], "draft")

    def testAnAgentThatAlreadyStartedDoesNotWaitForItsTimeAgain(self):
        saved, human = self.cutWhileAgentSleeps()
        saved["members"]["Sleeper"].update(startAt=None, started=True)
        restored = self.restoreWithSleeper(saved, human)
        restored.startInBackground(resume=True)
        self.assertEqual(restored.wait(10), {"result": "final"})
        self.assertEqual(restored.getInfo("Sleeper")["result"], "draft")

    def testAnApprovedPlanIsKeptAndTheOtherPlansAreWrittenAfterARestart(self):
        human = Human(Do_you_approve="yes")
        swarm = self.swarmOf("Plans", Leader=human.attach(DraftLoop(FakeAgent(["Plan of Leader", "Summary: Leader and Writer"]))),
                             Writer=human.attach(DraftLoop(FakeAgent(["Plan of Writer"]))))
        swarm.setMode("plan")
        for name in swarm.getAgents():
            swarm.members[name]["agent"].makePlan = lambda task, name=name: "A plan" if name == "Writer" else failing(RuntimeError("never"))
        self.assertIsNone(None)
        restoredFrom = {"version": 1, "id": "plans", "mission": "Plans", "mode": "plan", "leader": "Leader", "state": "running", "heartbeat": 0, "pid": 1,
                        "savedAt": "now", "summary": "", "messages": [],
                        "members": {"Leader": {"role": "boss", "task": "t", "boss": None, "waitsFor": [], "model": None, "recipe": {}, "status": "working", "result": None,
                                               "error": "", "mode": "plan", "review": "", "draft": "", "problem": "", "revision": 1, "started": True,
                                               "state": {"approvedPlan": ""}},
                                    "Writer": {"role": "writer", "task": "t", "boss": None, "waitsFor": [], "model": None, "recipe": {}, "status": "done",
                                               "result": "The plan of the writer", "error": "", "mode": "plan", "review": "approved", "draft": "", "problem": "",
                                               "revision": 2, "started": True, "state": {"approvedPlan": "The plan of the writer"}}}}
        planned = []
        def makeAgent(name, data):
            loop = human.attach(Loop(FakeAgent(["Summary: Leader and Writer"] if name == "Leader" else [])))
            loop.makePlan = lambda task: planned.append(name) or f"New plan of {name}"
            return loop
        restored = Swarm.restore(restoredFrom, makeAgent)
        self.assertEqual(restored.getInfo("Writer")["plan"], "The plan of the writer")
        restored.resume()
        self.assertEqual(planned, ["Leader"])
        self.assertEqual(restored.getInfo("Writer")["result"], "The plan of the writer")
        self.assertEqual(restored.getInfo("Leader")["result"], "New plan of Leader")


class RememberedWorkTests(SavedSwarmCase):
    def testAnApprovedEmailIsSentExactlyOnceWhateverHappensToTheProgram(self):
        smtp = mock.patch.object(harness_utils.smtplib, "SMTP")
        started = smtp.start()
        self.addCleanup(smtp.stop)
        server = started.return_value.__enter__.return_value
        sending, release = threading.Event(), threading.Event()
        def slowSend(message):
            sending.set()
            release.wait(10)
        server.send_message.side_effect = slowSend
        def makeEmailer(model):
            loop = EmailLoop(model, "me@example.com", "sara@example.com", "Meeting", "Ask to move it")
            loop.settings.update(EMAIL_PASSWORD="pw", EMAIL_SMTP_SERVER="smtp.test")
            return loop
        human = Human(Do_you_approve="yes")
        emailer = human.attach(makeEmailer(FakeAgent(["Hello Sara, can we move it?", "OK"])))
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(["Summary of Emailer"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMAIL_IMAP_SERVER", None)
            swarm = self.swarmOf(Leader=leader, Emailer=emailer)
            swarm.startInBackground()
            self.assertTrue(sending.wait(10))
            saved = self.crashed(swarm)
            self.assertEqual(saved["members"]["Emailer"]["state"]["progress"], {"mail": "Hello Sara, can we move it?", "sent": "sending"})
            release.set()
            swarm.wait(10)
            self.assertEqual(server.send_message.call_count, 1)
            self.assertEqual(swarm.getInfo("Emailer")["actions"][0]["text"], "Sent the email \"Meeting\" to sara@example.com.")
            for answer, again in (("no", 0), ("yes", 1)):
                server.send_message.reset_mock()
                server.send_message.side_effect = None
                human = Human(Do_you_approve="yes", The_program_stopped=answer)
                restored = Swarm.restore(self.leaveBehind(json.loads(json.dumps(saved)))[0],
                                         lambda name, data: human.attach(makeEmailer(FakeAgent()) if name == "Emailer" else self.plainLoop("final")))
                restored.members["Leader"]["agent"].agent = FakeAgent(["Summary of Emailer"])
                self.assertEqual(restored.resume(), "final")
                self.assertEqual(server.send_message.call_count, again)
                self.assertEqual(restored.members["Emailer"]["agent"].agent.prompts, [])
                self.assertEqual(restored.getInfo("Emailer")["result"], "Hello Sara, can we move it?")

    def testTheOriginalCodeIsNeverLostWhenTheProgramStopsInTheMiddleOfTheWork(self):
        target = self.folder / "tool.py"
        target.write_text("ORIGINAL\n", encoding="utf-8")
        human = Human(Type_yes="yes")
        coder = human.attach(CoderLoop(FakeAgent(["print('draft')"]), "make a tool", target))
        coder.askUser = lambda question: "yes"
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(["Summary of Coder"])
        swarm = self.swarmOf(Leader=leader, Coder=coder)
        swarm.startInBackground()
        question = human.next()
        self.assertTrue(question.startswith("Do you approve?"))
        self.assertEqual(target.read_text(encoding="utf-8"), "print('draft')")
        saved = self.crashed(swarm)
        progress = saved["members"]["Coder"]["state"]["progress"]
        self.assertEqual((progress["original"], progress["touched"], progress["kept"]), ("ORIGINAL\n", True, False))
        human.replies.put("no")
        swarm.wait(10)
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL\n")
        target.write_text("print('draft')", encoding="utf-8")
        found = self.leaveBehind(json.loads(json.dumps(saved)))[0]
        build = lambda name, data: human.attach(CoderLoop(FakeAgent(["print('final')"]), "make a tool", target)) if name == "Coder" else self.plainLoop("final")
        abandoned = Swarm.restore(found, build)
        self.assertEqual(abandoned.members["Coder"]["agent"].describePending(), f"{target} holds a draft of the code that the user did not approve yet. Stopping puts the original back.")
        abandoned.abandon()
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL\n")
        self.assertFalse(swarmStatePath(swarm.id).exists())
        target.write_text("print('draft')", encoding="utf-8")
        human = Human(Do_you_approve="yes")
        resumed = Swarm.restore(self.leaveBehind(json.loads(json.dumps(saved)))[0],
                                lambda name, data: human.attach(CoderLoop(FakeAgent(["print('final')"]), "make a tool", target)) if name == "Coder" else human.attach(self.plainLoop("final")))
        resumed.members["Leader"]["agent"].agent = FakeAgent(["Summary of Coder"])
        resumed.members["Coder"]["agent"].askUser = lambda question: "yes"
        self.assertEqual(resumed.resume(), "final")
        prompt = resumed.members["Coder"]["agent"].agent.prompts[0]
        self.assertIn("ORIGINAL", prompt)
        self.assertNotIn("print('draft')", prompt)
        self.assertEqual(target.read_text(encoding="utf-8"), "print('final')")

    def testTheOriginalCodeIsInTheSavedStateBeforeTheFileIsFirstOverwritten(self):
        target = self.folder / "tool.py"
        target.write_text("ORIGINAL\n", encoding="utf-8")
        loop = CoderLoop(FakeAgent(["print('draft')"]), "make a tool", target)
        loop.notifyUser = lambda message: None
        loop.askUser = lambda question: "yes"
        seen = []
        loop.onProgress = lambda: seen.append((dict(loop.progress), target.read_text(encoding="utf-8")))
        loop.run()
        first = next(entry for entry in seen if "original" in entry[0])
        self.assertEqual((first[0]["original"], first[1]), ("ORIGINAL\n", "ORIGINAL\n"))
        touched = next(entry for entry in seen if entry[0].get("touched"))
        self.assertEqual((touched[0]["original"], touched[1]), ("ORIGINAL\n", "ORIGINAL\n"))
        self.assertEqual(target.read_text(encoding="utf-8"), "print('draft')")

    def testAbandoningASwarmThatRunsIsRefused(self):
        gate = threading.Event()
        swarm = self.swarmOf(Leader=self.plainLoop("final"), Slow=self.plainLoop(gate=gate))
        swarm.startInBackground()
        with self.assertRaisesRegex(ValueError, "Stop it first"):
            swarm.abandon()
        gate.set()
        swarm.wait(10)


class LostConnectionTests(SavedSwarmCase):
    def writerSwarm(self, model, human, leaderReplies=("Summary of Writer",), **others):
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(list(leaderReplies))
        writer = human.attach(AuthorLoop(model, "birds", 50))
        swarm = self.swarmOf(Leader=leader, Writer=writer, **others)
        swarm.events = []
        swarm.addListener(lambda event: swarm.events.append(event["kind"]))
        return swarm

    def testALostConnectionPausesTheAgentAndContinueTriesTheSameCallAgain(self):
        model = Model(ConnectionLost("The internet connection was lost."), "A text about birds")
        human = Human(Do_you_approve="yes")
        swarm = self.writerSwarm(model, human)
        swarm.startInBackground()
        question = human.next()
        self.assertEqual(question, "Type continue to try again, or cancel to stop here:")
        self.assertEqual(swarm.getStatus("Writer"), "paused")
        self.assertEqual(swarm.getInterruption(), {"Writer": "The internet connection was lost."})
        self.assertEqual(self.readState(swarm)["state"], "paused")
        self.assertIn("The swarm lost its connection and is paused. Everything done so far is saved, so nothing is lost.", human.text())
        with mock.patch.object(harness_utils, "isOnline", lambda: False):
            human.replies.put("continue")
            self.assertEqual(human.next(), question)
        self.assertIn("There is still no internet connection", human.text())
        self.assertEqual(swarm.getStatus("Writer"), "paused")
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            human.replies.put("continue")
            self.assertEqual(swarm.wait(10), {"result": "final"})
        self.assertEqual(len(model.prompts), 2)
        self.assertEqual(model.prompts[0], model.prompts[1])
        self.assertEqual(swarm.getInfo("Writer")["result"], "A text about birds")
        self.assertEqual([kind for kind in swarm.events if kind in ("connectionLost", "resumed")], ["connectionLost", "resumed"])
        self.assertIsNone(swarm.getInterruption())
        self.assertFalse(swarmStatePath(swarm.id).exists())

    def testAgentsThatLoseTheConnectionTogetherShareOneQuestion(self):
        barrier = threading.Barrier(2)
        def lose(prompt):
            barrier.wait(10)
            raise ConnectionLost("gone")
        human = Human(Do_you_approve="yes")
        second = human.attach(AuthorLoop(Model(lose, "second text"), "fish", 50))
        swarm = self.writerSwarm(Model(lose, "first text"), human, ("Summary of Writer and Other",), Other=second)
        swarm.startInBackground()
        question = human.next()
        waitUntil(lambda: sorted(swarm.getInterruption() or []) == ["Other", "Writer"], "both agents in the interruption")
        self.assertEqual(question, "Type continue to try again, or cancel to stop here:")
        self.assertTrue(human.questions.empty())
        self.assertEqual({swarm.getStatus("Writer"), swarm.getStatus("Other")}, {"paused"})
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            human.replies.put("continue")
            self.assertEqual(swarm.wait(10), {"result": "final"})
        self.assertEqual(sorted(name for name, status in swarm.getStatuses().items() if status == "done"), ["Leader", "Other", "Writer"])

    def testAConnectionLostAfterTheApprovalDoesNotLoseTheApprovedEmail(self):
        smtp = mock.patch.object(harness_utils.smtplib, "SMTP")
        started = smtp.start()
        self.addCleanup(smtp.stop)
        server = started.return_value.__enter__.return_value
        server.send_message.side_effect = [OSError("network is unreachable"), None]
        human = Human(Do_you_approve="yes")
        emailer = EmailLoop(FakeAgent(["Hello Sara", "OK"]), "me@example.com", "sara@example.com", "Meeting", "Ask")
        emailer.settings.update(EMAIL_PASSWORD="pw", EMAIL_SMTP_SERVER="smtp.test")
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(["Summary of Emailer"])
        swarm = self.swarmOf(Leader=leader, Emailer=human.attach(emailer))
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(harness_utils, "isOnline", lambda: False):
            os.environ.pop("EMAIL_IMAP_SERVER", None)
            swarm.startInBackground()
            self.assertTrue(human.next().startswith("Type continue"))
            self.assertEqual(self.readState(swarm)["members"]["Emailer"]["state"]["progress"], {"mail": "Hello Sara", "sent": "sending"})
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            human.replies.put("continue")
            self.assertEqual(swarm.wait(10), {"result": "final"})
        self.assertEqual(server.send_message.call_count, 2)
        self.assertEqual(swarm.getInfo("Emailer")["result"], "Hello Sara")
        self.assertEqual(emailer.agent.prompts.count(emailer.agent.prompts[0]), 1)

    # A swarm whose writer loses the connection while the mailer has already sent its email, and a human to answer.
    def cancelling(self, summaryReply):
        mailer = Loop(FakeAgent())
        def mail():
            mailer.logAction("Sent the email \"Meeting\" to sara@example.com.")
            return "mailed"
        mailer.run = mail
        human = Human(Do_you_approve="yes")
        model = Model(ConnectionLost("The internet connection was lost."), "never used")
        return self.writerSwarm(model, human, (summaryReply,), Mailer=human.attach(mailer)), human

    def testCancelShowsTheSummaryOfTheLeaderAndStopsOnlyAfterTheConfirmation(self):
        swarm, human = self.cancelling("Summary: the Mailer sent the email to sara@example.com. The Writer did not finish. Leader waits.")
        swarm.startInBackground()
        self.assertTrue(human.next().startswith("Type continue"))
        human.replies.put("cancel")
        self.assertEqual(human.next(), "Type stop to stop here, or continue to go on until the end:")
        shown = human.text()
        self.assertIn("Summary of what the swarm did so far:\nSummary: the Mailer sent the email to sara@example.com.", shown)
        self.assertIn("the agents that did not finish are cut in the middle of their task", shown)
        prompt = swarm.members["Leader"]["agent"].agent.prompts[0]
        self.assertIn("Sent the email \"Meeting\" to sara@example.com.", prompt)
        self.assertIn("Writer (role Writer): paused because the connection was lost", prompt)
        self.assertEqual(swarm.getStatus("Writer"), "paused")
        human.replies.put("stop")
        self.assertEqual(swarm.wait(10), {"result": None})
        self.assertEqual(swarm.getInfo("Writer")["error"], STOPPED_MESSAGE)
        self.assertEqual(swarm.getStatus("Writer"), "failed")
        self.assertEqual(swarm.getInfo("Mailer")["status"], "done")
        self.assertFalse(swarmStatePath(swarm.id).exists())
        self.assertIn("stopped", swarm.events)

    def testCancelThenContinueGoesOnUntilTheEnd(self):
        swarm, human = self.cancelling("Summary of Leader, Writer and Mailer.")
        swarm.members["Writer"]["agent"].agent = Model(ConnectionLost("gone"), "A text")
        swarm.startInBackground()
        human.next()
        human.replies.put("cancel")
        human.next()
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            human.replies.put("continue")
            self.assertEqual(swarm.wait(10), {"result": "final"})
        self.assertEqual(swarm.getInfo("Writer")["result"], "A text")

    def testNothingIsStoppedByAnAnswerThatIsNotUnderstood(self):
        swarm, human = self.cancelling("Summary of Leader, Writer and Mailer.")
        swarm.members["Writer"]["agent"].agent = Model(ConnectionLost("gone"), "A text")
        swarm.startInBackground()
        first = human.next()
        human.replies.put("maybe")
        self.assertEqual(human.next(), first)
        self.assertIn("Please type continue or cancel.", human.text())
        human.replies.put("cancel")
        second = human.next()
        human.replies.put("hmm")
        self.assertEqual(human.next(), first)
        self.assertIn("Nothing was changed, the swarm is still paused.", human.text())
        self.assertEqual(swarm.getStatus("Writer"), "paused")
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            human.replies.put("continue")
            self.assertEqual(swarm.wait(10), {"result": "final"})
        self.assertTrue(second.startswith("Type stop"))

    def testWhenTheLeaderCannotWriteTheSummaryTheFactsAreGiven(self):
        swarm, human = self.cancelling("never used")
        swarm.members["Leader"]["agent"].agent = Model(ConnectionLost("The internet connection was lost."))
        swarm.startInBackground()
        human.next()
        human.replies.put("cancel")
        human.next()
        human.replies.put("stop")
        swarm.wait(10)
        shown = human.text()
        self.assertIn("The leader could not write the summary, so these are the facts:", shown)
        self.assertIn("- Writer (role Writer): paused because the connection was lost", shown)
        self.assertIn("Sent the email \"Meeting\" to sara@example.com.", shown)
        self.assertIn("- Mailer (role Mailer): ", shown)

    def testAUserInterfaceDecidesWithoutTheConsole(self):
        swarm, human = self.cancelling("Summary of Leader, Writer and Mailer.")
        swarm.startInBackground()
        question = human.next()
        with mock.patch.object(harness_utils, "isOnline", lambda: False):
            with self.assertRaisesRegex(ValueError, "still no internet connection"):
                swarm.continueWork()
        self.assertEqual(swarm.summarizeChanges(), "Summary of Leader, Writer and Mailer.")
        swarm.stopWork()
        human.replies.put("continue")
        self.assertEqual(swarm.wait(10), {"result": None})
        self.assertEqual(swarm.getStatus("Writer"), "failed")
        self.assertTrue(question.startswith("Type continue"))
        with self.assertRaisesRegex(ValueError, "not waiting for a decision"):
            swarm.continueWork()

    def testStoppingReleasesEveryAgentThatWaits(self):
        human = Human()
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(["Summary of Drafter"])
        drafter = human.attach(DraftLoop(FakeAgent(["a draft"])))
        sleeper = human.attach(TimedDraftLoop(FakeAgent(["another draft"]), 3600))
        swarm = self.swarmOf(Leader=leader, Drafter=drafter, Sleeper=sleeper, Follower=self.plainLoop("never"))
        swarm.setWaitsFor("Follower", ["Sleeper"])
        swarm.startInBackground()
        self.assertTrue(human.next().startswith("Do you approve?"))
        waitUntil(lambda: swarm.getInfo("Sleeper")["startAt"], "the sleeper to wait for its time")
        swarm.stopWork()
        human.replies.put("yes")
        outcome = swarm.wait(20)
        self.assertEqual(outcome, {"result": None})
        self.assertEqual({swarm.getStatus(name) for name in ("Drafter", "Sleeper", "Follower")}, {"failed"})
        self.assertFalse(swarmStatePath(swarm.id).exists())

    def testTheCoderIsPutBackWhenTheUserStopsAfterALostConnection(self):
        target = self.folder / "tool.py"
        target.write_text("ORIGINAL\n", encoding="utf-8")
        human = Human()
        coder = human.attach(CoderLoop(Model("print('draft')", ConnectionLost("gone")), "make a tool", target))
        coder.askUser = lambda question: "yes"
        leader = human.attach(self.plainLoop("final"))
        leader.agent = FakeAgent(["Summary of Coder", '{"Coder": "change it"}', "Summary: the coder was cut."])
        swarm = self.swarmOf(Leader=leader, Coder=coder)
        swarm.startInBackground()
        self.assertTrue(human.next().startswith("Do you approve?"))
        self.assertEqual(target.read_text(encoding="utf-8"), "print('draft')")
        human.replies.put("change it")
        self.assertTrue(human.next().startswith("Type continue"))
        human.replies.put("cancel")
        self.assertTrue(human.next().startswith("Type stop"))
        self.assertIn("holds a draft of the code that the user did not approve yet", leader.agent.prompts[-1])
        human.replies.put("stop")
        self.assertEqual(swarm.wait(10), {"result": None})
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL\n")
        self.assertEqual(swarm.getInfo("Coder")["error"], STOPPED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
