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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import harness_utils
import model_clients
import user_interface as gui
from model_clients import ApiModel
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
        self.assertEqual(statuses["meta-llama/Llama-3.3-70B-Instruct"], "tooBig")
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
