import json
import subprocess
import sys
import tempfile
import threading
import unittest
from patching import everywhere
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import agent_journal
import harness_utils
from agent_journal import ENTRY_CHARS, Journal, shapeArguments
from base_loop import Loop
from mcp_gateway import Gateway
from mcp_policy import decidePermission
from mission_history import deleteMission
from mission_memory import WorkSession, missionFolder
from project_identity import GENERAL, findProject, projectFor, projectJournals, readProject, registerMission
from swarm_harness import Swarm
from test_agent_conversation import ChatLoop, FakeChat, act, say, use
from test_harness_utils import DraftLoop
from test_saved_swarms import waitUntil


def entries(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]


class JournalTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        (self.files / "agent-files").mkdir()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.files / "agent-files")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = self.files / "project"
        self.work.mkdir()
        (self.work / "a.txt").write_text("hello\n", encoding="utf-8")

    def journal(self, project="p"):
        return Journal(self.files / "m1" / ".swarmup", "m1", project=project, run="run1")


class WritingTests(JournalTestCase):
    def testEveryEntryHasItsMissionRunRoundProjectAgentAndKind(self):
        journal = self.journal()
        journal.add("user", "Writer", to="Writer", text="Shorter, please")
        entry = entries(journal)[0]
        self.assertEqual({key: entry[key] for key in ("v", "mission", "run", "round", "project", "agent", "kind", "seq", "text")},
                         {"v": 1, "mission": "m1", "run": "run1", "round": 1, "project": "p", "agent": "Writer", "kind": "user", "seq": 1, "text": "Shorter, please"})
        self.assertGreater(entry["t"], 1_600_000_000_000)

    # SEC-09: no secret reaches the journal, neither the ones the agent was given nor the keys a model writes by mistake.
    def testSecretsAreMasked(self):
        journal = self.journal()
        loop = Loop(None)
        loop.settings = {"EMAIL_PASSWORD": "hunter22secret"}
        journal.add("step", "Writer", loop=loop, text="I log in with hunter22secret and sk-proj-abcdefghijklmnopqrstuvwx, password=letmein99 and token: ghp_abcdefghijklmnopqrstuvwxyz0123")
        text = entries(journal)[0]["text"]
        for secret in ("hunter22secret", "sk-proj-abcdefghijklmnopqrstuvwx", "letmein99", "ghp_abcdefghijklmnopqrstuvwxyz0123"):
            self.assertNotIn(secret, text)
        self.assertIn("***", text)

    def testALongEntryIsCutToFitAndTheContentOfAFileIsShortened(self):
        journal = self.journal()
        journal.add("step", "Writer", text="x" * 50000, other="y" * 30000)
        line = journal.path.read_text(encoding="utf-8").splitlines()[0]
        self.assertLessEqual(len(line), ENTRY_CHARS)
        self.assertIn("characters ...", json.loads(line)["text"])
        shaped = shapeArguments({"path": "a.py", "content": "z" * 5000, "count": 3})
        self.assertEqual((shaped["path"], shaped["count"], shaped["content"]["len"], len(shaped["content"]["head"])), ("a.py", 3, 5000, 300))

    # Several agents write at the same time: every line stays whole.
    def testWritersAtTheSameTimeNeverTearALine(self):
        journal = self.journal()

        def write(number):
            for step in range(150):
                journal.add("step", f"A{number}", text=f"{number}-{step} " + "w" * 3000)
        threads = [threading.Thread(target=write, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        found = entries(journal)
        self.assertEqual(len(found), 1200)
        self.assertEqual(len({entry["seq"] for entry in found}), 1200)

    def testAProblemOfTheDiskNeverStopsTheAgentAndIsToldOnce(self):
        told = []
        journal = Journal(self.files / "m1" / ".swarmup", "m1", onProblem=told.append)
        with everywhere(agent_journal.os, "write", side_effect=OSError("disk full")):
            journal.add("step", "A", text="a")
            journal.add("step", "A", text="b")
        self.assertEqual(journal.problems, 2)
        self.assertEqual(len(told), 1)
        self.assertIn("cannot be written", told[0])


class HookTests(JournalTestCase):
    def loopWith(self, replies, journal):
        loop = ChatLoop(None)
        loop.agent = FakeChat(replies, loop)
        loop.notifyUser = lambda message: None
        loop.askPermission = lambda request: {"decision": "once", "message": ""}
        loop.setFolder(self.work)
        loop.session = WorkSession("m1", "run1", leader="Leader")
        loop.journal = journal
        loop.gateway = Gateway("m1")
        loop.gateway.journal = journal
        self.addCleanup(loop.gateway.close)
        return loop

    # The step of the model, the call it made (with its id), the call that could not run, and the request are all in the journal.
    def testAConversationWritesItsRequestStepsCallsAndBadCalls(self):
        journal = self.journal()
        bad = {"id": "c9", "name": "files__read_file", "arguments": {}, "problem": "the arguments are not JSON"}
        loop = self.loopWith([use(act("read_file", 1, path="a.txt"), text="I need to read the file first."), use(bad), say("Done: it says hello.")], journal)
        self.assertEqual(loop.run(), "Done: it says hello.")
        found = entries(journal)
        kinds = [entry["kind"] for entry in found]
        self.assertEqual(kinds[0], "request")
        self.assertIn("bad", kinds)
        steps = [entry for entry in found if entry["kind"] == "step"]
        self.assertEqual([step["n"] for step in steps], [1, 2, 3])
        self.assertEqual(steps[0]["text"], "I need to read the file first.")
        self.assertEqual(steps[0]["calls"][0], {"id": "call1", "name": "files__read_file", "args": {"path": "a.txt"}})
        call = next(entry for entry in found if entry["kind"] == "call")
        self.assertEqual((call["id"], call["tool"], call["outcome"]), ("call1", "read_file", "success"))
        self.assertIn("hello", call["result"])
        badEntry = next(entry for entry in found if entry["kind"] == "bad")
        self.assertEqual((badEntry["id"], badEntry["why"]), ("c9", "parse"))

    # The source of a correction is known: the checks of the draft, or the user through its approval.
    def testTheDraftsAndTheReviewsTellWhereACorrectionCameFrom(self):
        journal = self.journal()
        loop = DraftLoop(FakeChat([say("draft one"), say("draft two"), say("draft three")]))
        loop.agent.loop = loop
        loop.journal = journal
        checks = iter(["Too long.", "", ""])
        replies = iter(["make it blue", "yes"])
        loop.askApproval = lambda draft, problem: next(replies)
        self.assertEqual(loop.reviewLoop("Write it", lambda draft: next(checks)), "draft three")
        found = [entry for entry in entries(journal) if entry["kind"] in ("draft", "review")]
        self.assertEqual([(entry["kind"], entry.get("problem", entry.get("reply"))) for entry in found],
                         [("draft", "Too long."), ("draft", ""), ("review", "correction"), ("draft", ""), ("review", "approved")])
        self.assertEqual(found[2]["text"], "make it blue")

    def testEveryPermissionIsWrittenWithHowItWasDecided(self):
        journal = self.journal()
        loop = Loop(None)
        loop.journal = journal
        loop.setFolder(self.work)
        loop.askPermission = lambda request: {"decision": "run", "message": ""}
        request = {"action": "run a command", "detail": "make", "key": "run:make", "place": ""}
        decidePermission(loop, request)
        decidePermission(loop, request)
        decidePermission(loop, {**request, "free": True})
        found = [(entry["decision"], entry["how"]) for entry in entries(journal) if entry["kind"] == "permission"]
        self.assertEqual(found, [("run", "asked"), ("once", "remembered"), ("once", "free")])

    # A whole run of a swarm: the run, the statuses with the errors, the messages, the results and the end are written, and the mission is
    # given to its project.
    def testARunOfASwarmIsWrittenFromItsStartToItsEnd(self):
        swarm = Swarm("Mission")
        leader, worker = ChatLoop(FakeChat([say("The report.")])), ChatLoop(FakeChat([say("The facts.")]))
        for loop in (leader, worker):
            loop.agent.loop = loop
            loop.setFolder(self.work)
        swarm.addAgent("Leader", leader, "leader", "lead")
        swarm.addAgent("Worker", worker, "worker", "find facts")
        swarm.setMode("execute")
        swarm.startInBackground()
        self.assertIn("result", swarm.wait(20))
        journal = swarm.journal
        found = entries(journal)
        kinds = [entry["kind"] for entry in found]
        self.assertEqual(kinds[0], "run")
        self.assertEqual(kinds[-1], "end")
        self.assertEqual(found[-1]["state"], "finished")
        self.assertEqual({agent["name"] for agent in found[0]["agents"]}, {"Leader", "Worker"})
        self.assertIn("status", kinds)
        self.assertIn("message", kinds)
        self.assertEqual({entry["agent"] for entry in found if entry["kind"] == "result"}, {"Leader", "Worker"})
        project, root = projectFor(self.work)
        self.assertEqual(swarm.project, project)
        self.assertEqual(projectJournals(project), {swarm.id: journal.path})
        self.assertEqual(json.loads((missionFolder(swarm.id) / ".swarmup" / "mission.json").read_text())["project"], project)


class ProjectTests(JournalTestCase):
    def testAProjectIsTheRootOfItsGitRepositoryOrItsFolder(self):
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        (self.work / "src").mkdir()
        repository, root = projectFor(self.work / "src")
        self.assertEqual(root, self.work)
        self.assertEqual(repository, projectFor(self.work)[0])
        self.assertTrue(repository.startswith("project-"))
        other = self.files / "other"
        other.mkdir()
        self.assertNotEqual(projectFor(other)[0], repository)
        self.assertEqual(projectFor(None), (GENERAL, None))

    def testTheProjectOfASwarmIsTheFolderMostOfItsAgentsWorkIn(self):
        other = self.files / "other"
        other.mkdir()
        self.assertEqual(findProject([other, self.work, self.work, None])[0], projectFor(self.work)[0])
        self.assertEqual(findProject([None, None])[0], GENERAL)

    def testAMissionDeletedIsForgottenByItsProject(self):
        swarm = Swarm("Mission")
        loop = ChatLoop(FakeChat([say("Alone.")]))
        loop.agent.loop = loop
        loop.setFolder(self.work)
        swarm.addAgent("Leader", loop, "leader", "lead")
        swarm.setMode("execute")
        swarm.startInBackground()
        swarm.wait(20)
        project = swarm.project
        waitUntil(lambda: not swarm.isRunning(), "the end of the run")
        self.assertIn(swarm.id, readProject(project)["missions"])
        deleteMission(swarm.id)
        self.assertNotIn(swarm.id, readProject(project)["missions"])
        self.assertFalse((missionFolder(swarm.id)).exists())

    def testRegisteringAMissionTwiceKeepsOneEntry(self):
        project, root = projectFor(self.work)
        registerMission(project, root, "m1", self.files / "j1")
        registerMission(project, root, "m1", self.files / "j1")
        self.assertEqual(list(readProject(project)["missions"]), ["m1"])
        self.assertEqual(readProject(project)["folder"], str(self.work))


if __name__ == "__main__":
    unittest.main()
