import os
import sys
import tempfile
import time
import types
import unittest
import zipfile
from patching import everywhere
from pathlib import Path
from unittest import mock

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import agent_prompts as prompts
import agent_storehouse
import harness_utils
import mission_memory
from agent_storehouse import agentWorkspace, describeTree, findFiles, findRequests, folderProblem, makeRunName, readFile, resultsFolder, searchFiles
from mission_memory import WorkSession, pruneSessions
from base_loop import Loop
from saved_swarms import saveSwarmState
from swarm_harness import Swarm
from writing_loops import AuthorLoop


class FakeAgent:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def input(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


# A coding agent has tools of its own: the loop gives itself to it.
class FakeCodingAgent(FakeAgent):
    def attach(self, loop):
        self.loop = loop


# A loop that saves what it made, without asking the user.
class SavingLoop(Loop):
    def run(self):
        self.saveResult("note", "saved")
        return "done"


class StorehouseTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.files / "agent-files")
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.files / "agent-files").mkdir()
        self.work = self.files / "project"
        (self.work / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
        (self.work / "a" / "b" / "c" / "d" / "e" / "deep.md").write_text("The deepest note.", encoding="utf-8")
        (self.work / "notes").mkdir()
        (self.work / "notes" / "ideas.md").write_text("Bees\nHoney is sweet\nWax", encoding="utf-8")
        (self.work / "paper.tex").write_text("\\section{Bees}", encoding="utf-8")

    def script(self, loop):
        loop.said = []
        loop.notifyUser = loop.said.append
        loop.askUser = lambda question: "yes"
        return loop

    # os.scandir refuses the folder blocked, as macOS does for a folder that the user did not allow.
    def blocking(self, blocked):
        scandir = os.scandir
        def guarded(path="."):
            if Path(path).resolve() == Path(blocked).resolve():
                raise PermissionError(1, "Operation not permitted", str(path))
            return scandir(path)
        return mock.patch.object(agent_storehouse.os, "scandir", guarded)


class TreeTests(StorehouseTestCase):
    def testEveryFileIsListedAtEveryDepth(self):
        self.assertEqual(describeTree(self.work).splitlines(), ["- paper.tex (14 bytes)", "- notes/ideas.md (23 bytes)", "- a/b/c/d/e/deep.md (17 bytes)"])
        self.assertEqual(describeTree(self.work, "a/b").splitlines(), ["- a/b/c/d/e/deep.md (17 bytes)"])

    def testHiddenAndHeavyFoldersAreNotListedButFilesInICloudAre(self):
        for folder in (".git", "node_modules", "swarmup-results/run"):
            (self.work / folder).mkdir(parents=True)
            (self.work / folder / "x.txt").write_text("x")
        (self.work / ".DS_Store").write_text("x")
        (self.work / ".thesis.pdf.icloud").write_text("stub")
        tree = describeTree(self.work)
        self.assertIn("- thesis.pdf (in iCloud, not downloaded yet)", tree)
        self.assertIn("- node_modules/ (not listed)", tree)
        self.assertIn("- swarmup-results/ (not listed)", tree)
        self.assertNotIn(".git", tree)
        self.assertNotIn("DS_Store", tree)
        self.assertIn("- node_modules/x.txt (1 bytes)", describeTree(self.work, "node_modules"))

    def testABigFolderIsShownByItsFolders(self):
        with mock.patch.object(agent_storehouse, "TREE_LIMIT", 2):
            tree = describeTree(self.work)
        self.assertIn("It holds 3 files, too many to list one by one.", tree)
        self.assertEqual(tree.splitlines()[1:], ["- ./ (1 files)", "- notes/ (1 files)", "- ... and 1 more folders: ask <swarmup_find> to find them by name."])

    def testAnEmptyFolderAndAMissingOneAreSaid(self):
        (self.work / "empty").mkdir()
        self.assertEqual(describeTree(self.work / "empty"), "The folder is empty.")
        with self.assertRaisesRegex(ValueError, "There is no folder nope in the folder"):
            describeTree(self.work, "nope")

    def testMacOsTellsTheUserHowToAllowAFolder(self):
        with self.blocking(self.work / "notes"), mock.patch.object(agent_storehouse, "sys", types.SimpleNamespace(platform="darwin")):
            problem = folderProblem(self.work / "notes")
            tree = describeTree(self.work)
            with self.assertRaisesRegex(ValueError, "macOS does not let SwarmUP read"):
                Loop(FakeAgent()).setFolder(self.work / "notes")
        self.assertIn("System Settings > Privacy & Security > Files and Folders", problem)
        self.assertIn("- notes/ (cannot be read: macOS does not let SwarmUP read", tree)
        with self.blocking(self.work), mock.patch.object(agent_storehouse, "sys", types.SimpleNamespace(platform="linux")):
            self.assertIn("SwarmUP is not allowed to read", folderProblem(self.work))
        self.assertEqual(folderProblem(self.work), "")


class ReadTests(StorehouseTestCase):
    def testALongFileComesInParts(self):
        (self.work / "long.txt").write_text("0123456789abcdefghijXYZ", encoding="utf-8")
        with mock.patch.object(agent_storehouse, "READ_PART_CHARS", 10):
            first, last = readFile(self.work, "long.txt"), readFile(self.work, "long.txt", 3)
        self.assertEqual(first, 'long.txt (part 1 of 3):\n0123456789\n[The file goes on: ask <swarmup_read part="2">long.txt</swarmup_read> for the next part.]')
        self.assertEqual(last, "long.txt (part 3 of 3):\nXYZ")
        self.assertEqual(readFile(self.work, "./notes/ideas.md"), "notes/ideas.md:\nBees\nHoney is sweet\nWax")

    def testWordDocumentsAndUtf16AreReadAndBinaryFilesAreNot(self):
        document = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>bees</w:t></w:r></w:p><w:p><w:r><w:t>Second</w:t></w:r></w:p></w:body></w:document>'
        with zipfile.ZipFile(self.work / "letter.docx", "w") as archive:
            archive.writestr("word/document.xml", document)
        self.assertEqual(readFile(self.work, "letter.docx"), "letter.docx:\nHello bees\nSecond")
        (self.work / "wide.txt").write_text("Wide text", encoding="utf-16")
        self.assertEqual(readFile(self.work, "wide.txt"), "wide.txt:\nWide text")
        (self.work / "image.png").write_bytes(b"\x89PNG\0\0\0")
        with self.assertRaisesRegex(ValueError, "image.png is a binary file"):
            readFile(self.work, "image.png")
        (self.work / "broken.docx").write_text("not a zip")
        with self.assertRaisesRegex(ValueError, "not a Word document"):
            readFile(self.work, "broken.docx")

    def testAPdfNeedsPypdf(self):
        (self.work / "paper.pdf").write_bytes(b"%PDF-1.4")
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            with self.assertRaisesRegex(ValueError, "pip install pypdf"):
                readFile(self.work, "paper.pdf")

    def testNothingOutsideTheFolderCanBeRead(self):
        (self.files / "secret.txt").write_text("secret")
        for path in ("../secret.txt", str(self.files / "secret.txt")):
            with self.assertRaisesRegex(ValueError, "is outside the folder"):
                readFile(self.work, path)
        try:
            (self.work / "link.txt").symlink_to(self.files / "secret.txt")
        except OSError:
            self.skipTest("this computer does not allow links")
        with self.assertRaisesRegex(ValueError, "link.txt is outside the folder"):
            readFile(self.work, "link.txt")

    def testAPathWrittenDifferentlyStillFindsItsFile(self):
        self.assertIn("Honey is sweet", readFile(self.work, "NOTES/Ideas.MD"))
        (self.work / "café.md").write_text("Coffee", encoding="utf-8")
        self.assertEqual(readFile(self.work, "café.md"), "café.md:\nCoffee")
        with self.assertRaisesRegex(ValueError, "There is no file notes/idea.md in the folder. Did you mean: notes/ideas.md"):
            readFile(self.work, "notes/idea.md")
        with self.assertRaisesRegex(ValueError, "Did you mean: a/b/c/d/e/deep.md"):
            readFile(self.work, "deep.md")
        self.assertIn("- notes/ideas.md", readFile(self.work, "notes"))

    def testFilesAreFoundByNameAndByWhatTheySay(self):
        self.assertEqual(findFiles(self.work, "*.md"), "- notes/ideas.md (23 bytes)\n- a/b/c/d/e/deep.md (17 bytes)")
        self.assertEqual(findFiles(self.work, "DEEP"), "- a/b/c/d/e/deep.md (17 bytes)")
        self.assertEqual(findFiles(self.work, "c/d"), "- a/b/c/d/ (folder)\n- a/b/c/d/e/ (folder)\n- a/b/c/d/e/deep.md (17 bytes)")
        self.assertEqual(findFiles(self.work, "*.pdf"), "Nothing in the folder has a name like *.pdf.")
        self.assertEqual(searchFiles(self.work, "honey"), "- notes/ideas.md:2: Honey is sweet")
        self.assertEqual(searchFiles(self.work, "wasps"), "No text file of the folder contains wasps.")


class LoopTests(StorehouseTestCase):
    def testTheRequestsAreReadEvenWhenTheyAreNotWrittenExactly(self):
        answer = ("First I read.\n<SWARMUP_READ part='2'>`notes/ideas.md`</swarmup_read>\n<swarmup_list></swarmup_list>\n"
                  "<swarmup_find>*.tex\n< swarmup_search >honey</ swarmup_search >")
        self.assertEqual(findRequests(answer), [("read", "notes/ideas.md", 2), ("list", "", 1), ("find", "*.tex", 1), ("search", "honey", 1)])
        self.assertEqual(findRequests("A normal answer."), [])

    def testAnyModelReadsTheFilesOfItsFolderBeforeItAnswers(self):
        agent = FakeAgent(["Let me look.\n<swarmup_read>a/b/c/d/e/deep.md</swarmup_read>", "A text built on the deepest note."])
        loop = self.script(AuthorLoop(agent, "bees", 50))
        loop.setFolder(self.work)
        self.assertEqual(loop.run(), "A text built on the deepest note.")
        self.assertIn("- a/b/c/d/e/deep.md (17 bytes)", agent.prompts[0])
        self.assertIn("<swarmup_read>path/of/a/file</swarmup_read>", agent.prompts[0])
        self.assertIn("=== read a/b/c/d/e/deep.md ===\na/b/c/d/e/deep.md:\nThe deepest note.", agent.prompts[1])
        self.assertIn(prompts.FOLDER_NEXT_ROUND, agent.prompts[1])
        self.assertIn("[AuthorLoop] reads a/b/c/d/e/deep.md in its folder.", loop.said)

    def testTheAgentStopsAskingAfterTheLastRound(self):
        agent = FakeAgent(["<swarmup_find>*.md</swarmup_find>"] * 2 + ["<swarmup_read>paper.tex</swarmup_read>The answer"])
        loop = self.script(Loop(agent))
        loop.setFolder(self.work)
        with mock.patch.object(agent_storehouse, "MAX_TOOL_ROUNDS", 2):
            self.assertEqual(loop.askAgent("Task"), "The answer")
        self.assertEqual(len(agent.prompts), 3)
        self.assertIn(prompts.FOLDER_LAST_ROUND, agent.prompts[2])

    def testChecksAgentsWithoutAFolderAndCodingAgentsGetNoBlocks(self):
        agent = FakeAgent(["OK", "Fine", "Fine"])
        loop = self.script(Loop(agent))
        loop.setFolder(self.work)
        loop.askAgent("Check this", tools=False)
        loop.setFolder(None)
        loop.askAgent("Task")
        coding = FakeCodingAgent(["Done"])
        loop = self.script(Loop(coding))
        loop.setFolder(self.work)
        loop.askAgent("Task")
        for prompt in [*agent.prompts, *coding.prompts]:
            self.assertNotIn("<swarmup_read>", prompt)

    def testNotesAreTheMemoryOfTheAgentForTheWholeSession(self):
        agent = FakeAgent(["Draft one <swarmup_note>The user prefers short texts.</swarmup_note>", "Draft two"])
        loop = self.script(Loop(agent))
        loop.name, loop.session = "Writer", WorkSession("swarm1", "run")
        self.assertEqual(loop.askAgent("Task"), "Draft one")
        self.assertNotIn("The user prefers short texts.", agent.prompts[0])
        self.assertEqual(loop.askAgent("Task again"), "Draft two")
        self.assertIn("The user prefers short texts.", agent.prompts[1])
        self.assertIn("The user prefers short texts.", WorkSession("swarm1", "run").readNotes("Writer"))
        self.assertEqual(WorkSession("swarm1", "run").readNotes("Other"), "")


class StoreTests(StorehouseTestCase):
    def testTheResultsOfARunAreSavedTogether(self):
        self.assertRegex(makeRunName("Write an article on bees!"), r"^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_write-an-article-on-bees$")
        self.assertEqual(resultsFolder(self.work, "run"), self.work / "swarmup-results" / "run")
        self.assertEqual(resultsFolder(None, "run"), self.files / "agent-files" / "results" / "run")
        loop = Loop(FakeAgent())
        loop.name, loop.session = "Writer", WorkSession("swarm1", "my-run")
        loop.setFolder(self.work)
        self.assertEqual(loop.saveResult("text", "Hi"), self.work / "swarmup-results" / "my-run" / "Writer_text.md")
        loop.setFolder(None)
        self.assertEqual(loop.saveResult("text", "Hi").read_text(encoding="utf-8"), "Hi")

    # A mission stays as long as its saved state (the history). What is left of one without a saved state goes after SESSION_DAYS.
    def testWhatIsLeftOfAMissionWithoutItsSavedStateGoes(self):
        old = time.time() - (mission_memory.SESSION_DAYS + 1) * 24 * 3600
        for name in ("old", "continued", "recent"):
            WorkSession(name, "run").addNotes("Writer", ["note"])
        saveSwarmState("continued", {"id": "continued"})
        for name in ("old", "continued"):
            for folder in (mission_memory.missionFolder(name), mission_memory.tempFolder(name)):
                for path in [folder, *folder.rglob("*")]:
                    os.utime(path, (old, old))
        pruneSessions()
        for folder in (mission_memory.missionFolder("x").parent, mission_memory.tempFolder("x").parent):
            self.assertEqual(sorted(path.name for path in folder.iterdir()), ["continued", "recent"])

    def testACodingAgentWithoutAFolderWorksInItsSession(self):
        loop = Loop(FakeAgent())
        loop.name = "Coder"
        self.assertEqual(agentWorkspace(loop), self.files / "agent-files" / "agent-workspaces" / "Coder")
        loop.session = WorkSession("swarm1", "run")
        self.assertEqual(agentWorkspace(loop), self.files / "agent-files" / "mission-specific-memory" / "swarm1" / ".swarmup" / "workspace" / "Coder")
        loop.setFolder(self.work)
        self.assertEqual(agentWorkspace(loop), self.work)

    def testASwarmSavesInTheFolderOfItsRunAndKeepsItWhenItGoesOn(self):
        swarm = Swarm("Bees mission")
        loop = SavingLoop(FakeAgent())
        loop.setFolder(self.work)
        swarm.addAgent("Leader", loop, "boss", "lead")
        self.assertEqual(swarm.run(), "done")
        self.assertTrue(swarm.runName.endswith("_bees-mission"))
        self.assertEqual((self.work / "swarmup-results" / swarm.runName / "Leader_note.md").read_text(encoding="utf-8"), "saved")
        self.assertIsNone(loop.session)
        self.assertTrue(mission_memory.missionFolder(swarm.id).is_dir())
        state = {**swarm.describeState(), "runName": "kept"}
        again = Swarm.restore(state, lambda name, data: SavingLoop(FakeAgent()))
        self.assertEqual(again.runName, "kept")
        again.run(resume=True)
        self.assertEqual(again.runName, "kept")
        again.run()
        self.assertNotEqual(again.runName, "kept")
        again.abandon()
        self.assertTrue(mission_memory.missionFolder(swarm.id).is_dir())


if __name__ == "__main__":
    unittest.main()
