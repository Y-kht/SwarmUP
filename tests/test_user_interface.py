import http.client
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import harness_utils
import leader_utils
import model_clients
import user_interface as gui
from model_clients import ApiModel
from test_leader_utils import usePrices
from test_saved_swarms import waitUntil

GPUS = [{"name": "RTX A5000", "total": 25.8, "free": 25.3}]


# A swarm that still runs when a test ends is stopped, and its questions are answered until its thread ends: otherwise it would keep
# the lock of the user (USER_LOCK) and the next tests would wait for it.
def settle(session):
    swarm = session.swarm
    if swarm and swarm.isRunning():
        swarm.stopWork()
    end = time.monotonic() + 10
    while True:
        with session.lock:
            for question in list(session.questions.values()):
                session.questions.pop(question["id"])
                session.release(question, "", shown=False)
        if not (swarm and swarm.isRunning()) or time.monotonic() > end:
            return
        time.sleep(0.01)


# A pretend model: plans, drafts and summaries that pass the checks of the loops, and the names of the agents in every summary.
class Model:
    def __init__(self, info=None):
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.prompts = []

    def input(self, prompt):
        self.prompts.append(prompt)
        self.usage["calls"] += 1
        names = re.findall(r"^- ([A-Za-z][\w.-]*) \(", prompt, re.M)
        if "summary" in prompt.lower() and names:
            return "Where the team stands: " + ", ".join(names) + "."
        if "write a plan" in prompt.lower():
            return "1. Read the mission. 2. Write the text. 3. Show it to the user."
        return "Bees keep our gardens alive."


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for target, name, value in ((harness_utils, "AGENT_FILES", self.folder), (harness_utils, "readGpus", lambda: GPUS), (gui, "readGpus", lambda: GPUS),
                                    (gui, "createModel", lambda info, keys=None, token=None, report=None: Model(info))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        usePrices(self)
        self.session = gui.Session()
        self.addCleanup(lambda: settle(self.session))

    def act(self, action, **payload):
        return self.session.act(action, payload)

    def addWriter(self, subject="bees", name=""):
        return self.act("saveAgent", task="author", values={"subject": subject, "length": "200"}, name=name)["agentId"]

    def chooseApi(self, agentId, name="claude-sonnet-5-5"):
        return self.act("chooseModel", agentId=agentId, name=name, local=False, apiKey="sk-test")

    def buildTwoWriters(self):
        self.act("setMission", mission="Write two texts about bees.")
        first, second = self.addWriter("bees"), self.addWriter("honey")
        self.chooseApi(first)
        self.chooseApi(second)
        return first, second

    def waitForQuestion(self, kind=None):
        waitUntil(lambda: any(kind in (None, question["kind"]) for question in self.session.questions.values()), "a question")
        return next(question for question in self.session.questions.values() if kind in (None, question["kind"]))

    def waitForEnd(self):
        waitUntil(lambda: self.session.swarm and not self.session.swarm.isRunning() and self.session.runInfo.get("finishedAt"), "the end of the run")
        return self.session.describe()["run"]


class QuestionTests(unittest.TestCase):
    def testTheQuestionsOfTheSwarmGetTheirButtons(self):
        cases = {
            "Do you approve? Type yes to approve all of them, no to reject all of them, the name of an agent to look at it alone, or write what you want changed:": ("review", ["yes", "no"]),
            "Is this good to go? Type yes, no, or what you want changed:": ("approval", ["yes", "no"]),
            "Type continue to try again, or cancel to stop here:": ("connection", ["continue", "cancel"]),
            "Type stop to stop here, or continue to go on until the end:": ("stop", ["continue", "stop"]),
            "I will write code into a.py and run: python a.py\nThis changes files on your computer. Continue? (yes/no)": ("yesno", ["yes", "no"]),
            "Username for ieeexplore.ieee.org:": ("text", [""]),
            "Please enter EMAIL_SMTP_SERVER:": ("text", []),
        }
        for text, (kind, values) in cases.items():
            found, replies = gui.describeQuestion(text)
            self.assertEqual((found, [reply["value"] for reply in replies]), (kind, values), text)
        self.assertEqual(gui.describeQuestion("Password for x:", secret=True), ("secret", []))

    def testTheCommandLineHintIsKeptForEditing(self):
        field = {"key": "testCommand", "kind": "command"}
        self.assertEqual(gui.describeRawValue(field, ["python", "-m", "pytest", "my tests"]), "python -m pytest 'my tests'" if gui.os.name != "nt" else 'python -m pytest "my tests"')
        self.assertEqual(gui.describeRawValue({"key": "password", "kind": "secret"}, "hunter2"), "")


class BuilderTests(SessionTestCase):
    def testTheMissionIsNeeded(self):
        with self.assertRaises(gui.FormError) as caught:
            self.act("setMission", mission="   ")
        self.assertIn("mission", caught.exception.errors)
        self.assertEqual(self.act("setMission", mission="Write about bees.")["state"]["mission"], "Write about bees.")

    def testAnAgentIsCheckedFieldByFieldAndDescribed(self):
        with self.assertRaises(gui.FormError) as caught:
            self.act("saveAgent", task="author", values={"subject": "", "length": "zero"})
        self.assertEqual(set(caught.exception.errors), {"subject", "length"})
        agentId = self.addWriter("bees")
        agent = self.session.describe()["agents"][0]
        self.assertEqual((agent["id"], agent["name"], agent["isLeader"]), (agentId, "Writer", True))
        self.assertEqual(agent["description"], "Write a text about bees in at most 200 words.")
        self.assertEqual(self.addWriter("honey") and self.session.describe()["agents"][1]["name"], "Writer2")
        with self.assertRaises(gui.FormError) as caught:
            self.addWriter("wax", name="writer")
        self.assertIn("already an agent", caught.exception.errors["name"])

    def testTheFormFollowsTheAnswers(self):
        form = self.act("taskForm", task="email", values={"provider": "Gmail"})["form"]
        fields = {field["key"]: field for field in form["fields"]}
        self.assertEqual((fields["smtp"]["default"], fields["imap"]["default"]), ("smtp.gmail.com", "imap.gmail.com"))
        self.assertIn("app password", fields["password"]["help"])
        news = {field["key"]: field["visible"] for field in self.act("taskForm", task="news", values={"messenger": "WhatsApp"})["form"]["fields"]}
        self.assertTrue(news["whatsappToken"] and news["whatsappTo"])
        self.assertFalse(news["telegramToken"])

    def testAPasswordIsNeverSentBackAndIsKeptWhenLeftEmpty(self):
        values = {"provider": "Gmail", "sender": "me@example.com", "password": "hunter2", "receiver": "you@example.com", "subject": "Hi", "request": "Say hello"}
        agentId = self.act("saveAgent", task="email", values=values)["agentId"]
        form = self.act("taskForm", task="email", agentId=agentId)["form"]
        self.assertEqual(form["values"]["password"], "")
        self.assertTrue(next(field for field in form["fields"] if field["key"] == "password")["saved"])
        self.act("saveAgent", task="email", agentId=agentId, values={**values, "password": ""})
        self.assertEqual(self.session.findSpec(agentId)["answers"]["password"], "hunter2")
        self.assertNotIn("hunter2", json.dumps(self.session.describe()))

    def testTheTelegramChatIsFoundWhenTheUserDidNotGiveIt(self):
        values = {"outlets": ["BBC World"], "messenger": "Telegram", "telegramToken": "123:abc", "telegramChat": ""}
        with mock.patch.object(gui, "findTelegramChats", return_value=[{"id": 42, "name": "Me"}]):
            agentId = self.act("saveAgent", task="news", values=values)["agentId"]
        self.assertEqual(self.session.findSpec(agentId)["answers"]["telegramChat"], "42")
        with mock.patch.object(gui, "findTelegramChats", return_value=[]), self.assertRaises(gui.FormError) as caught:
            self.act("saveAgent", task="news", values=values)
        self.assertIn("send it any message", caught.exception.errors["telegramChat"])

    def testTheLiteratureReviewerNeedsAPlaceToSearch(self):
        with self.assertRaises(gui.FormError) as caught:
            self.act("saveAgent", task="literature", values={"subject": "bees", "searches": [], "publishers": {}})
        self.assertIn("place to search", caught.exception.errors["searches"])

    def testARemovedAgentComesBackWithUndoAndTheLeaderWaitsForNobody(self):
        self.act("setMission", mission="Bees.")
        first, second, third = self.addWriter("a"), self.addWriter("b"), self.addWriter("c")
        self.act("setOrder", order="custom", waits={"Writer3": ["Writer2"]})
        self.act("removeAgent", agentId=second)
        self.assertEqual([agent["name"] for agent in self.session.describe()["agents"]], ["Writer", "Writer3"])
        self.assertEqual(self.session.findSpec(third)["waitsFor"], [])
        self.act("undoRemove")
        self.assertEqual([agent["name"] for agent in self.session.describe()["agents"]], ["Writer", "Writer2", "Writer3"])
        self.act("setOrder", order="custom", waits={"Writer3": ["Writer2"]})
        self.act("moveAgent", agentId=second, position=0)
        self.assertEqual([agent["name"] for agent in self.session.describe()["agents"]], ["Writer2", "Writer", "Writer3"])
        self.assertEqual((self.session.findSpec(second)["waitsFor"], self.session.findSpec(third)["waitsFor"]), ([], []))

    def testAgentsCannotWaitForEachOtherInACircle(self):
        self.act("setMission", mission="Bees.")
        self.addWriter("a"), self.addWriter("b"), self.addWriter("c")
        with self.assertRaises(ValueError) as caught:
            self.act("setOrder", order="custom", waits={"Writer2": ["Writer3"], "Writer3": ["Writer2"]})
        self.assertIn("circle", str(caught.exception))
        self.assertEqual(self.act("setOrder", order="custom", waits={"Writer3": ["Writer2"]})["stages"], [["Writer2"], ["Writer3"]])

    def testAFolderMustExistAndHoldTheFilesOfTheAgent(self):
        document = self.folder / "inside" / "paper.md"
        document.parent.mkdir()
        document.write_text("Theorem 1.", encoding="utf-8")
        agentId = self.act("saveAgent", task="math", values={"filePath": str(document)})["agentId"]
        with self.assertRaises(gui.FormError):
            self.act("setFolder", agentId=agentId, folder=str(self.folder / "missing"))
        other = self.folder / "other"
        other.mkdir()
        with self.assertRaises(gui.FormError) as caught:
            self.act("setFolder", agentId=agentId, folder=str(other))
        self.assertIn("not inside", caught.exception.errors["folder"])
        state = self.act("setFolder", agentId=agentId, folder=str(document.parent))["state"]
        self.assertEqual(state["agents"][0]["folder"], str(document.parent.resolve()))
        self.assertEqual(state["agents"][0]["suggestion"], str(document.parent.resolve()))

    def testTheBrowserOfFoldersListsFoldersFirst(self):
        (self.folder / "b-folder").mkdir()
        (self.folder / "a-file.txt").write_text("x", encoding="utf-8")
        (self.folder / ".hidden").mkdir()
        listing = self.act("browse", path=str(self.folder), files=True)["browse"]
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["b-folder", "a-file.txt"])
        self.assertEqual([entry["name"] for entry in self.act("browse", path=str(self.folder))["browse"]["entries"]], ["b-folder"])


class ModelTests(SessionTestCase):
    def testALocalModelThatDoesNotFitIsRefusedAndOneThatFitsIsChosen(self):
        self.act("setMission", mission="Bees.")
        agentId = self.addWriter()
        catalog = self.act("modelCatalog", agentId=agentId)["catalog"]
        statuses = {entry["name"]: entry["status"] for entry in catalog["local"]}
        self.assertEqual(statuses["google/gemma-4-E4B-it"], "fits")
        self.assertNotIn("meta-llama/Llama-3.3-70B-Instruct", statuses)
        self.assertEqual(catalog["hiddenLocal"], 4)
        family = {entry["name"]: entry["status"] for entry in catalog["families"]["llama"]}
        self.assertEqual(family["meta-llama/Llama-3.3-70B-Instruct"], "tooBig")
        with self.assertRaises(ValueError) as caught:
            self.act("chooseModel", agentId=agentId, name="meta-llama/Llama-3.3-70B-Instruct", local=True)
        self.assertIn("only have 25.8 GB", str(caught.exception))
        self.assertEqual(self.act("modelCatalog", agentId=agentId, bits=4)["catalog"]["local"][0]["vram"], 4.8)
        result = self.act("chooseModel", agentId=agentId, name="google/gemma-4-E4B-it", local=True)
        self.assertEqual(result["download"]["size"], 16.0)
        self.assertEqual(self.session.describe()["gpu"]["needed"], 19.2)

    def testAnApiModelNeedsAKeyThatIsNeverShown(self):
        self.act("setMission", mission="Bees.")
        agentId = self.addWriter()
        with mock.patch.dict(gui.os.environ, {"ANTHROPIC_API_KEY": ""}), self.assertRaises(gui.FormError) as caught:
            self.act("chooseModel", agentId=agentId, name="claude-sonnet-5-5", local=False)
        self.assertIn("apiKey", caught.exception.errors)
        with mock.patch.object(gui, "createModel", model_clients.createModel):
            state = self.act("chooseModel", agentId=agentId, name="claude-sonnet-5-5", local=False, apiKey="sk-secret-123")["state"]
        self.assertIsInstance(self.session.findSpec(agentId)["client"], ApiModel)
        self.assertEqual(state["keys"]["claude"]["source"], "typed")
        self.assertNotIn("sk-secret-123", json.dumps(state))

    def testTheMissingPackagesOfAModelAreShown(self):
        self.act("setMission", mission="Bees.")
        agentId = self.addWriter()
        with mock.patch.object(gui, "findMissingPackages", return_value=["anthropic"]):
            self.chooseApi(agentId)
        self.assertEqual(self.session.describe()["agents"][0]["missing"], ["anthropic"])
        with mock.patch.object(gui, "findMissingPackages", return_value=[]):
            self.act("checkPackages")
        self.assertEqual(self.session.describe()["agents"][0]["missing"], [])

    def testTheSwarmNeedsAModelForEveryAgent(self):
        self.act("setMission", mission="Bees.")
        self.addWriter()
        with self.assertRaises(ValueError) as caught:
            self.act("start", mode="plan")
        self.assertIn("Choose a model for Writer", str(caught.exception))


class RunTests(SessionTestCase):
    def testOneAgentPlansAndTheUserApprovesFromTheInterface(self):
        self.act("setMission", mission="Write about bees.")
        self.chooseApi(self.addWriter())
        self.act("start", mode="plan")
        question = self.waitForQuestion("review")
        self.assertEqual([reply["label"] for reply in question["replies"]], ["Approve", "Reject"])
        self.assertTrue(self.session.describe()["run"]["running"])
        self.act("answer", id=question["id"], answer="yes")
        run = self.waitForEnd()
        self.assertEqual((run["state"], run["mode"]), ("succeeded", "plan"))
        self.assertEqual(run["agents"][0]["status"], "done")
        self.assertEqual(run["agents"][0]["usage"]["calls"], 1)
        feed = [item["text"] for item in self.session.feed]
        self.assertIn("The swarm starts in plan mode.", feed)
        self.assertIn("yes", feed)

    def testTheApprovedPlansAreExecutedBySameSwarm(self):
        self.act("setMission", mission="Write about bees.")
        self.chooseApi(self.addWriter())
        self.act("start", mode="plan")
        self.act("answer", id=self.waitForQuestion("review")["id"], answer="yes")
        self.waitForEnd()
        swarm = self.session.swarm
        self.act("execute")
        self.assertIs(self.session.swarm, swarm)
        self.act("answer", id=self.waitForQuestion("review")["id"], answer="yes")
        run = self.waitForEnd()
        self.assertEqual((run["state"], run["mode"]), ("succeeded", "execute"))
        self.assertEqual(run["agents"][0]["result"], "Bees keep our gardens alive.")
        self.assertIn("Your plan, approved by the user", self.session.specs[0]["client"].prompts[-1])

    def testASummaryWithNothingLeftToDecideAnswersItself(self):
        self.buildTwoWriters()
        self.act("start", mode="plan")
        question = self.waitForQuestion("review")
        run = self.session.describe()["run"]
        self.assertEqual(sorted(run["ready"]), ["Writer", "Writer2"])
        for agent in run["agents"]:
            self.act("approve", agent=agent["name"], revision=agent["revision"])
        waitUntil(lambda: question["id"] not in self.session.questions, "the summary to answer itself")
        run = self.waitForEnd()
        self.assertEqual(run["state"], "succeeded")
        self.assertNotIn("yes", [item["text"] for item in self.session.feed if item["kind"] == "answer"])

    def testAMessageToAnAgentRewritesItsDraft(self):
        self.act("setMission", mission="Write about bees.")
        self.chooseApi(self.addWriter())
        self.act("start", mode="execute")
        question = self.waitForQuestion("review")
        self.act("correct", agent="Writer", text="Mention honey.")
        waitUntil(lambda: question["id"] not in self.session.questions or self.session.swarm.getInfo("Writer")["revision"] > 1, "the correction")
        self.assertIn("Mention honey.", [item["text"] for item in self.session.feed])

    def testAnAgentJoinsTheRunningSwarmAndAnotherLeavesIt(self):
        self.buildTwoWriters()
        self.act("start", mode="plan")
        self.waitForQuestion("review")
        agentId = self.act("saveAgent", task="author", values={"subject": "wax", "length": "100"}, name="Waxer", live=True)["agentId"]
        state = self.session.describe()
        self.assertEqual((state["agents"][-1]["name"], state["agents"][-1]["pending"]), ("Waxer", True))
        self.assertNotIn("Waxer", self.session.swarm.getAgents())
        with self.assertRaises(gui.FormError):
            self.act("joinLive", agentId=agentId, waitsFor=[])
        self.chooseApi(agentId)
        self.assertTrue(self.session.describe()["run"]["canJoin"])
        self.act("joinLive", agentId=agentId, waitsFor=[])
        self.assertIn("Waxer", self.session.swarm.getAgents())
        self.assertFalse(self.session.describe()["agents"][-1]["pending"])
        self.assertIn("Waxer joined the swarm (writer). Every agent was told.", [item["text"] for item in self.session.feed])
        self.act("removeLive", agent="Writer2", reason="Two texts are enough.")
        state = self.session.describe()
        self.assertEqual([agent["name"] for agent in state["agents"]], ["Writer", "Waxer"])
        self.assertEqual(state["run"]["removed"][0]["reason"], "Two texts are enough.")
        with self.assertRaises(ValueError):
            self.act("removeLive", agent="Writer")
        while self.session.swarm.isRunning():
            question = self.waitForQuestion()
            self.act("answer", id=question["id"], answer="yes")
            time.sleep(0.05)
        run = self.waitForEnd()
        self.assertEqual({agent["name"]: agent["status"] for agent in run["agents"]}, {"Writer": "done", "Waxer": "done"})

    def testAPendingAgentCanBeDroppedAndTheOtherAgentsStayLocked(self):
        self.buildTwoWriters()
        self.act("start", mode="plan")
        self.waitForQuestion("review")
        with self.assertRaises(ValueError):
            self.addWriter("not now")
        agentId = self.act("saveAgent", task="author", values={"subject": "wax", "length": "100"}, live=True)["agentId"]
        with self.assertRaises(ValueError):
            self.act("removeAgent", agentId=self.session.specs[1]["id"])
        self.act("removeAgent", agentId=agentId)
        self.assertEqual(len(self.session.describe()["agents"]), 2)

    def testStoppingReleasesTheQuestionsAndEndsTheRun(self):
        self.act("setMission", mission="Write about bees.")
        self.chooseApi(self.addWriter())
        self.act("start", mode="execute")
        self.waitForQuestion("review")
        self.act("stop")
        run = self.waitForEnd()
        self.assertEqual(run["state"], "stopped")
        self.assertEqual(self.session.questions, {})

    def testAnInterruptedSwarmIsContinuedWithItsSecretsGivenAgain(self):
        values = {"provider": "Gmail", "sender": "me@example.com", "password": "hunter2", "receiver": "you@example.com", "subject": "Hi", "request": "Say hello"}
        self.act("setMission", mission="Say hello.")
        self.chooseApi(self.act("saveAgent", task="email", values=values)["agentId"])
        self.act("start", mode="plan")
        self.waitForQuestion("review")
        self.session.swarm.saveForExit()
        saved = json.loads(next((self.folder / harness_utils.RUNS_FOLDER).glob("swarm_*.json")).read_text(encoding="utf-8"))
        self.assertNotIn("hunter2", json.dumps(saved))
        settle(self.session)
        harness_utils.saveSwarmState(saved["id"], saved)
        later = gui.Session()
        self.addCleanup(lambda: settle(later))
        self.assertEqual([swarm["mission"] for swarm in later.describe()["unfinished"]], ["Say hello."])
        form = later.act("resumeForm", {"id": saved["id"]})["resume"]
        self.assertEqual([field["key"] for field in form["agents"][0]["fields"]], ["password"])
        self.assertEqual(list(form["providers"]), ["claude"])
        with self.assertRaises(gui.FormError) as caught:
            later.act("resume", {"id": saved["id"], "secrets": {}})
        self.assertEqual(set(caught.exception.errors), {"Emailer.password", "key.claude"})
        later.act("resume", {"id": saved["id"], "secrets": {"Emailer": {"password": "hunter2"}}, "keys": {"claude": "sk-test"}})
        self.assertEqual(later.specs[0]["answers"]["password"], "hunter2")
        waitUntil(lambda: later.questions, "the continued swarm to ask")
        question = next(iter(later.questions.values()))
        later.act("answer", {"id": question["id"], "answer": "yes"})
        waitUntil(lambda: not later.swarm.isRunning() and later.runInfo.get("finishedAt"), "the end of the continued run")
        self.assertEqual(later.describe()["run"]["state"], "succeeded")
        self.assertEqual(list((self.folder / harness_utils.RUNS_FOLDER).glob("swarm_*.json")), [])


# A pretend coding agent: before its plan it asks for a permission and asks a question, through the same helpers as Claude Code and Codex.
class CodingAgent(Model):
    def __init__(self, info=None):
        super().__init__(info)
        self.loop, self.decisions, self.answers, self.runs = None, [], [], 0

    def attach(self, loop):
        self.loop = loop

    def newRun(self):
        self.runs += 1

    def input(self, prompt):
        if "write a plan" in prompt.lower():
            self.decisions.append(model_clients.askPermission(self.loop, {"action": "run a command", "detail": "npm test", "folder": "/work", "reason": "Check the code"}))
            self.answers.append(model_clients.askQuestions(self.loop, [{"id": "tone", "header": "Tone", "question": "Which tone?",
                                                                         "options": [{"label": "Warm", "description": ""}], "multiple": False, "secret": False}]))
        return super().input(prompt)


class CodingAgentTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        self.agent = CodingAgent()
        self.account = {"signedIn": True, "type": "chatgpt", "email": "me@example.com", "plan": "plus"}
        for name, value in (("createModel", lambda info, keys=None, token=None, report=None: self.agent if info.get("cli") else Model(info)),
                            ("checkCodex", lambda: {"path": "codex", "version": "0.160.0", "problem": ""}), ("readCodexAccount", lambda: dict(self.account)),
                            ("listCodexModels", lambda: [{"id": "gpt-6-luna", "name": "GPT-6 Luna", "description": "", "isDefault": False}])):
            patcher = mock.patch.object(gui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def chooseCodex(self):
        self.act("setMission", mission="Write about bees.")
        agentId = self.addWriter()
        self.act("chooseModel", agentId=agentId, name=gui.DEFAULT_CLI_MODEL, cli="codex")
        return agentId

    def testTheRequestsOfACodingAgentAreAnsweredInTheConversation(self):
        self.chooseCodex()
        model = self.session.describe()["agents"][0]["model"]
        self.assertEqual((model["cli"], model["label"]), ("codex", "Codex · its default model"))
        self.act("start", mode="plan")
        question = self.waitForQuestion("permission")
        self.assertEqual((question["payload"]["detail"], [reply["value"] for reply in question["replies"]]), ("npm test", ["once", "run", "deny"]))
        with self.assertRaises(ValueError):
            self.act("answer", id=question["id"], answer="yes")
        self.act("answer", id=question["id"], answer={"decision": "run", "message": ""})
        form = self.waitForQuestion("form")
        self.assertEqual(form["payload"][0]["question"], "Which tone?")
        self.act("answer", id=form["id"], answer={"tone": ["Warm"]})
        self.act("answer", id=self.waitForQuestion("review")["id"], answer="yes")
        self.assertEqual(self.waitForEnd()["state"], "succeeded")
        self.assertEqual((self.agent.decisions[0]["decision"], self.agent.answers, self.agent.runs), ("run", [{"tone": ["Warm"]}], 1))
        self.assertIn("Allowed until the next run: npm test", [item["text"] for item in self.session.feed])

    def testStoppingTheSwarmDeniesAPermissionThatWaits(self):
        self.chooseCodex()
        self.act("start", mode="plan")
        self.waitForQuestion("permission")
        self.act("stop")
        self.waitForEnd()
        self.assertEqual(self.agent.decisions[-1]["decision"], "deny")

    def testCodexMustBeInstalledAndSignedIn(self):
        self.act("setMission", mission="Write about bees.")
        agentId = self.addWriter()
        self.account = {"signedIn": False, "type": None, "email": None, "plan": None}
        with self.assertRaises(gui.FormError) as caught:
            self.act("chooseModel", agentId=agentId, name=gui.DEFAULT_CLI_MODEL, cli="codex")
        self.assertIn("codex", caught.exception.errors)
        self.assertEqual(self.act("codexAccount")["codex"]["account"]["signedIn"], False)
        with mock.patch.object(gui, "checkCodex", lambda: {"path": None, "version": None, "problem": "Codex is not installed."}):
            with self.assertRaises(ValueError) as caught:
                self.act("chooseModel", agentId=agentId, name=gui.DEFAULT_CLI_MODEL, cli="codex")
            self.assertIn("not installed", str(caught.exception))
            self.assertEqual(self.act("codexAccount")["codex"]["problem"], "Codex is not installed.")
        with self.assertRaises(ValueError):
            self.act("price", name="x", cli="codex")

    def testClaudeCodeIsUsedWithTheAnthropicKeyOnly(self):
        self.act("setMission", mission="Write about bees.")
        agentId = self.addWriter()
        with mock.patch.dict(gui.os.environ, {"ANTHROPIC_API_KEY": ""}), self.assertRaises(gui.FormError) as caught:
            self.act("chooseModel", agentId=agentId, name=gui.DEFAULT_CLI_MODEL, cli="claude-code")
        self.assertIn("apiKey", caught.exception.errors)
        state = self.act("chooseModel", agentId=agentId, name=gui.DEFAULT_CLI_MODEL, cli="claude-code", apiKey="sk-ant-secret")["state"]
        self.assertEqual((state["agents"][0]["model"]["label"], state["keys"]["claude"]["source"]), ("Claude Code · its default model", "typed"))
        self.assertNotIn("sk-ant-secret", json.dumps(state))
        self.assertTrue(gui.hasCredentials(gui.getModelInfo("", cli="codex"), {}))
        with mock.patch.dict(gui.os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(gui.hasCredentials(gui.getModelInfo("", cli="claude-code"), {}))

    def testTheSignInOfCodexIsShownUntilItEnds(self):
        finished = threading.Event()
        class Login:
            def start(self, kind):
                return {"url": "https://auth.openai.com/codex/device", "code": "ABCD-1234" if kind == "code" else None}
            def wait(self, timeout=None):
                finished.wait(10)
                return ""
            def cancel(self):
                finished.set()
        self.account = {"signedIn": False, "type": None, "email": None, "plan": None}
        with mock.patch.object(gui, "CodexLogin", Login), mock.patch.object(gui.webbrowser, "open") as opened:
            login = self.act("codexSignIn", method="code")["state"]["codexLogin"]
            self.assertEqual((login["state"], login["code"], login["url"]), ("waiting", "ABCD-1234", "https://auth.openai.com/codex/device"))
            self.assertNotIn("login", login)
            opened.assert_not_called()
            self.account = {"signedIn": True, "type": "chatgpt", "email": "me@example.com", "plan": "plus"}
            finished.set()
            waitUntil(lambda: self.session.describe()["codexLogin"]["state"] == "done", "the end of the sign-in")
            self.assertEqual(self.session.describe()["codexLogin"]["account"]["email"], "me@example.com")
            finished.clear()
            self.act("codexSignIn", method="browser")
            opened.assert_called_once_with("https://auth.openai.com/codex/device")
            self.act("codexCancel")
            self.assertIsNone(self.session.describe()["codexLogin"])


# The leader builds the swarm (leader_utils.py). Its model answers the prompts of the leader with the blocks a test gives it.
class LeaderModeTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        from test_leader_utils import LeaderModel, block
        self.block = block
        self.leaderModel = LeaderModel()
        for target, name, value in ((leader_utils, "checkCodex", lambda: {"problem": "Codex is not installed."}), (leader_utils, "findMissingPackages", lambda info: []),
                                    (leader_utils, "readGpus", lambda: GPUS), (leader_utils, "DEBOUNCE_SECONDS", 0.02),
                                    (gui, "createModel", lambda info, keys=None, token=None, report=None: self.leaderModel if info["name"] == "claude-opus-5-5" else Model(info))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.folder / "notes.md").write_text("Bees pollinate.", encoding="utf-8")

    def prepareLeader(self):
        self.act("setMission", mission="Write a text about bees and a shorter version of it.")
        self.act("setBuildMode", mode="leader")
        leader = self.session.describe()["agents"][0]
        self.act("setFolder", agentId=leader["id"], folder=str(self.folder))
        self.chooseApi(leader["id"], "claude-opus-5-5")
        return leader

    def answerProposal(self, decision="approve", message=""):
        question = self.waitForQuestion("proposal")
        self.act("answer", id=question["id"], answer={"decision": decision, "message": message})
        return question

    def testTheLeaderBuildsTheSwarmOnceTheUserApprovesItsProposal(self):
        agents = [{"name": "Writer", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "bees", "length": 300}, "why": "It writes the text."},
                  {"name": "Shortener", "task": "author", "model": "claude-haiku-4-5", "waits_for": ["Writer"], "settings": {"subject": "the text of Writer in 50 words",
                   "length": 50}, "why": "It writes the short version."}]
        self.leaderModel.replies = {"build": [self.block("build", agents=agents[:1])], "revise": ["Here it is.\n" + self.block("build", agents=agents)]}
        leader = self.prepareLeader()
        self.assertEqual((leader["name"], leader["task"], leader["builder"], leader["isLeader"]), ("Leader", "leader", True, True))
        with self.assertRaises(ValueError):
            self.act("removeAgent", agentId=leader["id"])
        with self.assertRaises(ValueError):
            self.act("start", mode="execute")
        self.act("buildWithLeader")
        question = self.answerProposal("reject", "Add a short version too.")
        self.assertEqual((question["kind"], question["payload"]["action"], [agent["name"] for agent in question["payload"]["agents"]]), ("proposal", "build", ["Writer"]))
        question = self.answerProposal()
        self.assertEqual(question["payload"]["why"], "Here it is.")
        waitUntil(lambda: self.session.jobs["leader"]["state"] == "done", "the leader to build the swarm")
        state = self.session.describe()
        self.assertEqual([(agent["name"], agent["task"], agent["model"]["name"], agent["waitsFor"]) for agent in state["agents"]],
                         [("Leader", "leader", "claude-opus-5-5", []), ("Writer", "author", "claude-sonnet-5-5", []), ("Shortener", "author", "claude-haiku-4-5", ["Writer"])])
        self.assertEqual((state["agents"][2]["folder"], state["agents"][2]["why"], state["order"], state["buildMode"]), (str(self.folder.resolve()), "It writes the short version.", "custom", "leader"))
        self.assertIn("Approved: the swarm of the leader", [item["text"] for item in self.session.feed])
        self.assertIn("The user read the swarm you proposed and wants changes: Add a short version too.", self.leaderModel.prompts[1])
        self.assertIn("notes.md", self.leaderModel.prompts[0])
        self.act("start", mode="execute")
        self.assertIsInstance(self.session.swarm.manager, leader_utils.LeaderManager)
        while self.session.swarm.isRunning():
            question = self.waitForQuestion()
            self.act("answer", id=question["id"], answer={"decision": "approve", "message": ""} if question["kind"] == "proposal" else "yes")
            time.sleep(0.05)
        run = self.waitForEnd()
        self.assertEqual((run["state"], run["agents"][0]["result"]), ("succeeded", "Report: every agent did its work."))
        self.assertEqual(len(list(self.folder.glob("report_*.md"))), 1)

    def testTheLeaderAddsAnAgentWhileTheSwarmRunsOnceTheUserApproves(self):
        agents = [{"name": "Writer", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "bees"}, "why": "It writes."},
                  {"name": "Second", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "honey"}, "why": "It writes too."}]
        add = self.block("add", name="Translator", task="author", model="claude-haiku-4-5", waits_for=["Writer"], settings={"subject": "the text of Writer in French"},
                         why="A French version helps the user.")
        self.leaderModel.replies = {"build": [self.block("build", agents=agents)], "supervise": [add]}
        self.prepareLeader()
        self.act("buildWithLeader")
        self.answerProposal()
        waitUntil(lambda: self.session.jobs["leader"]["state"] == "done", "the leader to build the swarm")
        self.act("start", mode="execute")
        waitUntil(lambda: "Writer" in self.session.swarm.getReadyAgents(), "the draft of Writer")
        self.act("approve", agent="Writer", revision=self.session.swarm.getInfo("Writer")["revision"])
        question = self.answerProposal()
        self.assertEqual((question["payload"]["action"], question["payload"]["agent"]), ("add", "Translator"))
        self.assertEqual(question["text"], "Leader proposes: Add Translator (Writer (texts, essays, articles)) with claude-haiku-4-5 (Anthropic API), waiting for Writer.")
        waitUntil(lambda: "Translator" in self.session.swarm.getAgents(), "the translator to join")
        state = self.session.describe()
        self.assertEqual(state["agents"][-1]["name"], "Translator")
        self.assertIn("Translator joined the swarm (writer). Every agent was told.", [item["text"] for item in self.session.feed])
        settle(self.session)

    def testBackToBuildingByHandTheLeaderLeavesAndItsAgentsStay(self):
        self.leaderModel.replies = {"build": [self.block("build", agents=[{"name": "Writer", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "bees"}}])]}
        self.prepareLeader()
        self.act("buildWithLeader")
        self.answerProposal()
        waitUntil(lambda: self.session.jobs["leader"]["state"] == "done", "the leader to build the swarm")
        self.act("setBuildMode", mode="manual")
        state = self.session.describe()
        self.assertEqual(([agent["name"] for agent in state["agents"]], state["buildMode"]), (["Writer"], "manual"))

    def testTheLeaderNeedsItsFolderAndItsModelBeforeItBuilds(self):
        self.act("setMission", mission="Bees.")
        self.act("setBuildMode", mode="leader")
        with self.assertRaises(gui.FormError) as caught:
            self.act("buildWithLeader")
        self.assertIn("folder", caught.exception.errors)
        leader = self.session.describe()["agents"][0]
        self.act("setFolder", agentId=leader["id"], folder=str(self.folder))
        with self.assertRaises(gui.FormError) as caught:
            self.act("buildWithLeader")
        self.assertIn("model", caught.exception.errors)
        with self.assertRaises(ValueError):
            self.act("taskForm", task="leader", agentId=leader["id"])
        self.assertEqual(self.act("modelCatalog", agentId=leader["id"])["catalog"]["api"][0]["name"], "claude-opus-5-5")


# The budget of the mission, and what it spent: the first lists only show the models that fit in what is left, and the cost grows with every call.
class CostTests(SessionTestCase):
    def testTheFirstListsOnlyShowTheModelsThatFitTheVramAndTheBudgetLeft(self):
        self.act("setMission", mission="Bees.")
        first, second = self.addWriter("bees"), self.addWriter("honey")
        api = lambda agentId: [entry["name"] for entry in self.act("modelCatalog", agentId=agentId)["catalog"]["api"]]
        self.assertEqual(api(first), ["claude-sonnet-5-5", "gpt-6-luna", "gemini-3.8-flash", "deepseek-v4-pro"])
        self.act("setBudget", budget="20")
        catalog = self.act("modelCatalog", agentId=first)["catalog"]
        self.assertEqual(([entry["name"] for entry in catalog["api"]], catalog["hiddenApi"]), (["claude-sonnet-5-5", "gpt-6-luna", "gemini-3.8-flash", "deepseek-v4-pro"], 0))
        self.assertEqual({entry["name"]: entry["price"] for entry in catalog["prices"]["claude"] if entry["price"]}["claude-opus-5-5"], 25.0)
        self.chooseApi(first, "claude-sonnet-5-5")
        catalog = self.act("modelCatalog", agentId=second)["catalog"]
        self.assertEqual(catalog["budget"]["left"], 5.0)
        self.assertEqual(([entry["name"] for entry in catalog["api"]], catalog["hiddenApi"]), (["gemini-3.8-flash", "deepseek-v4-pro"], 2))
        self.assertEqual({entry["name"]: entry["status"] for entry in catalog["prices"]["claude"]}["claude-sonnet-5-5"], "overBudget")
        self.assertEqual({entry["name"]: entry["status"] for entry in catalog["prices"]["gemini"]}["gemini-3.8-flash"], "unknown")
        result = self.chooseApi(second, "claude-haiku-4-5")
        self.assertEqual(result["warning"], "")
        result = self.chooseApi(second, "claude-sonnet-5-5")
        self.assertEqual(result["warning"], "claude-sonnet-5-5 costs $15.00 per 1 million tokens, more than the $5.00 left of the budget of the mission.")
        costs = self.session.describe()["costs"]
        self.assertEqual((costs["budget"], costs["spent"], costs["setAside"], costs["left"]), (20.0, 0.0, 30.0, -10.0))
        catalog = self.act("modelCatalog", agentId=first, bits=16)["catalog"]
        self.assertEqual(catalog["budget"]["left"], 5.0)
        with self.assertRaises(gui.FormError):
            self.act("setBudget", budget="-3")
        self.act("setBudget", budget="")
        self.assertIsNone(self.session.describe()["costs"]["budget"])

    def testEveryCallIsCountedAndTheUserIsWarnedWhenTheBudgetRunsOut(self):
        class Billed(Model):
            def input(self, prompt):
                answer = super().input(prompt)
                self.usage["calls"] -= 1
                model_clients.recordCall(self.usage, input=400000, cachedInput=100000, output=200000)
                return answer
        with mock.patch.object(gui, "createModel", lambda info, keys=None, token=None, report=None: Billed(info)):
            self.act("setMission", mission="Write a text about bees.")
            writer = self.addWriter()
            self.chooseApi(writer, "claude-haiku-4-5")
            self.act("setBudget", budget="1.5")
            self.act("start", mode="execute")
            while self.session.swarm.isRunning():
                question = self.waitForQuestion()
                self.act("answer", id=question["id"], answer="yes")
                time.sleep(0.05)
        costs = self.session.describe()["costs"]
        [row] = costs["agents"]
        self.assertEqual((row["agent"], row["calls"], row["input"], row["output"]), ("Writer", 1, 500000, 200000))
        self.assertAlmostEqual(costs["spent"], 0.4 * 1.0 + 0.1 * 0.1 + 0.2 * 5.0)
        self.assertEqual((costs["setAside"], costs["unpriced"]), (0.0, 0))
        self.assertAlmostEqual(costs["left"], 1.5 - 1.41)
        warnings = [item["text"] for item in self.session.feed if "of its budget" in item["text"]]
        self.assertEqual(warnings, ["The mission spent $1.41, 80% of its budget of $1.50. The leader and you can remove agents that are not needed anymore."])
        state = harness_utils.findUnfinishedSwarms() or [self.session.swarm.describeState()]
        self.assertEqual(state[0]["costs"]["clients"][0]["usage"]["records"][0]["cachedInput"], 100000)

    def testTheMostAgentsOfTheLeaderIsASettingOfTheUser(self):
        self.assertEqual(self.session.describe()["settings"]["maxAgents"], 10)
        self.assertEqual(self.act("saveSettings", maxAgents=4)["settings"]["maxAgents"], 4)
        self.assertEqual(gui.Session().describe()["settings"]["maxAgents"], 4)
        leader = {"answers": {"folder": None}, "name": "Leader", "model": None}
        self.assertEqual(self.session.leaderCatalog(leader).maxAgents, 4)
        for wrong in (0, "many", 1000):
            with self.assertRaises(gui.FormError):
                self.act("saveSettings", maxAgents=wrong)


class ServerTests(SessionTestCase):
    def setUp(self):
        super().setUp()
        self.server = gui.makeServer(self.session)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(gui.HOST, self.server.server_port, timeout=10)
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def testThePageCarriesTheTokenThatEveryActionNeeds(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.session.token.encode(), page)
        self.assertEqual(self.request("POST", "/api/setMission", {"mission": "Bees."})[0], 403)
        status, data = self.request("POST", "/api/setMission", {"mission": "Bees."}, {gui.TOKEN_HEADER: self.session.token})
        self.assertEqual((status, json.loads(data)["state"]["mission"]), (200, "Bees."))
        status, data = self.request("POST", "/api/setMission", {"mission": ""}, {gui.TOKEN_HEADER: self.session.token})
        self.assertEqual((status, json.loads(data)["errors"]), (400, {"mission": "Write the mission of the swarm in a sentence or two."}))

    def testOnlyThisComputerAndTheFilesOfTheInterfaceAreServed(self):
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/assets/../user_interface.py")[0], 404)
        self.assertEqual(self.request("GET", "/assets/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/api/catalog")[0], 403)
        status, data = self.request("GET", "/api/catalog", headers={gui.TOKEN_HEADER: self.session.token})
        self.assertEqual(len(json.loads(data)["tasks"]), len(gui.TASKS))

    def testThePollAnswersAtOnceWhenTheBrowserIsBehind(self):
        status, data = self.request("GET", "/api/poll?version=-1&feed=0", headers={gui.TOKEN_HEADER: self.session.token})
        self.assertEqual((status, json.loads(data)["state"]["version"]), (200, self.session.version))


class WindowTests(unittest.TestCase):
    def testTheDialogsOfTheSystemGiveOnePath(self):
        webview = mock.Mock(spec=["FileDialog"])
        webview.FileDialog = mock.Mock(FOLDER=20, OPEN=10, SAVE=30)
        window = mock.Mock()
        choose = gui.makeDialog(webview, window)
        window.create_file_dialog.return_value = ("/home/me/folder",)
        self.assertEqual(choose("folder", ""), "/home/me/folder")
        self.assertEqual(window.create_file_dialog.call_args.args[0], 20)
        window.create_file_dialog.return_value = None
        self.assertEqual(choose("file", "/home/me/notes.md"), "")
        window.create_file_dialog.return_value = "/home/me/new.py"
        self.assertEqual(choose("path", "/home/me/new.py"), "/home/me/new.py")
        self.assertEqual(window.create_file_dialog.call_args.kwargs["save_filename"], "new.py")

    def testWithoutPywebviewTheWindowOfABrowserIsLookedFor(self):
        with mock.patch.object(gui.shutil, "which", side_effect=lambda name: "/usr/bin/chromium" if name == "chromium" else None):
            self.assertEqual(gui.findAppBrowser(), "/usr/bin/chromium")


if __name__ == "__main__":
    unittest.main()
