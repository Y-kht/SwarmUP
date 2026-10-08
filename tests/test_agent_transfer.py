import os
import sys
import tempfile
import time
import unittest
from patching import everywhere
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import background_processes
import harness_utils
from agent_storehouse import answerRequest
from agent_tools import Toolbox, undoChanges
from base_loop import Loop
from coding_agents import placeRule
from mission_memory import MissionMemory, WorkSession


class TransferTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.files = Path(files.name).resolve()
        (self.files / "agent-files").mkdir()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.files / "agent-files")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = self.files / "project"
        (self.work / "notes").mkdir(parents=True)
        (self.work / "notes" / "ideas.md").write_text("Bees\nHoney is sweet\nWax\n", encoding="utf-8")
        (self.work / "windows.txt").write_bytes("one\r\ntwo\r\nthree".encode("utf-8"))
        self.session = WorkSession("m1", "run", leader="Leader")
        self.asked = []
        self.addCleanup(lambda: background_processes.stopProcesses("m1"))

    def box(self, name="Writer", decision="once", folder=True):
        loop = Loop(None)
        loop.name, loop.session = name, self.session
        loop.said, loop.actions = [], []
        loop.notifyUser = loop.said.append
        loop.askPermission = lambda request: self.asked.append(request) or {"decision": decision, "message": "no"}
        if folder:
            loop.setFolder(self.work)
        return Toolbox(loop)

    def ok(self, box, name, **arguments):
        text, error = box.run(name, arguments)
        self.assertFalse(error, text)
        return text

    def refused(self, box, name, **arguments):
        text, error = box.run(name, arguments)
        self.assertTrue(error, text)
        return text


class PlaceTests(TransferTestCase):
    def testEveryLookingToolReachesThePlacesOfTheMission(self):
        box = self.box()
        temp, memory = self.session.temp, self.session.memory
        (temp / "scratch.md").write_text("in temp", encoding="utf-8")
        memory.write("Checker", "FINDINGS.md", "# Findings\nthe hive is full")
        memory.write("Leader", "STYLE.md", "short sentences", long=True)
        self.assertIn("in temp", self.ok(box, "read_file", path="@temp/scratch.md"))
        self.assertIn("the hive is full", self.ok(box, "read_file", path="@memory/FINDINGS.md"))
        self.assertIn("short sentences", self.ok(box, "read_file", path="@long-term/STYLE.md"))
        self.assertIn("FINDINGS.md", self.ok(box, "list_files", path="@memory"))
        self.assertNotIn(".swarmup", self.ok(box, "list_files", path="@memory"))
        self.assertIn("@memory/FINDINGS.md:2", "@memory/" + self.ok(box, "search_files", text="hive", path="@memory").lstrip("- "))
        self.assertIn("scratch.md", self.ok(box, "find_files", pattern="*.md", path="@temp"))
        self.assertIn("is kept by SwarmUP itself", self.refused(box, "read_file", path="@memory/.swarmup/owners.json"))
        self.assertIn("is not a place", self.refused(box, "read_file", path="@other/x.md"))
        self.assertIn("is outside the folder", self.refused(box, "read_file", path="@temp/../../mission-specific-memory/m1/FINDINGS.md"))

    # A model asked one prompt at a time reads the places with its requests.
    def testTheRequestsOfAOnePromptModelReachThePlacesToo(self):
        (self.session.temp / "scratch.md").write_text("in temp", encoding="utf-8")
        places = self.session.memory.roots()
        self.assertIn("in temp", answerRequest(self.work, "read", "@temp/scratch.md", 1, places))
        self.assertIn("in temp", answerRequest(None, "read", "@temp/scratch.md", 1, places))
        self.assertIn("You have no folder of your own", answerRequest(None, "read", "notes/ideas.md", 1, places))
        self.assertIn("kept by SwarmUP itself", answerRequest(self.work, "read", "@memory/.swarmup/owners.json", 1, places))

    def testEachPlaceHasItsRulesForAChange(self):
        writer, checker, leader = self.box(), self.box("Checker"), self.box("Leader")
        self.ok(writer, "write_file", path="@temp/run.py", content="print(1)\n")
        self.assertEqual(self.asked, [])
        self.ok(writer, "write_file", path="@memory/DRAFT.md", content="# Draft\n")
        self.ok(writer, "edit_file", path="@memory/DRAFT.md", old_text="# Draft", new_text="# My draft")
        self.assertEqual(self.session.memory.ownerOf("DRAFT.md"), "Writer")
        self.assertIn("was written by Writer", self.refused(checker, "edit_file", path="@memory/DRAFT.md", old_text="# My draft", new_text="mine"))
        self.assertIn("was written by Writer", self.refused(checker, "delete_path", path="@memory/DRAFT.md"))
        self.assertIn("Only the leader", self.refused(writer, "write_file", path="@long-term/STYLE.md", content="x"))
        self.ok(leader, "write_file", path="@long-term/STYLE.md", content="- short\n")
        self.ok(leader, "delete_path", path="@memory/DRAFT.md")
        self.assertEqual(self.asked, [])
        self.ok(writer, "write_file", path="new.md", content="x")
        self.assertEqual(self.asked[-1]["action"], "create a file")


class CopyTests(TransferTestCase):
    def testAFolderGoesToTheTempFolderAndBackExactly(self):
        box = self.box()
        os.symlink("ideas.md", self.work / "notes" / "link.md")
        self.ok(box, "copy_path", source="notes", target="@temp")
        copied = self.session.temp / "notes"
        self.assertEqual((copied / "ideas.md").read_bytes(), (self.work / "notes" / "ideas.md").read_bytes())
        self.assertTrue((copied / "link.md").is_symlink())
        self.assertEqual(self.asked, [])
        self.ok(box, "copy_path", source="windows.txt", target="@temp/backup/windows.txt")
        self.assertEqual((self.session.temp / "backup" / "windows.txt").read_bytes(), b"one\r\ntwo\r\nthree")
        (copied / "ideas.md").write_text("Bees\nPollen\n", encoding="utf-8")
        self.assertIn("already exists", self.refused(box, "copy_path", source="@temp/notes/ideas.md", target="notes/ideas.md"))
        text = self.ok(box, "copy_path", source="@temp/notes/ideas.md", target="notes/ideas.md", overwrite=True, reason="the edited copy")
        self.assertIn("(1 file, 12 bytes)", text)
        self.assertEqual(self.asked[-1]["action"], "copy")
        self.assertIn("in place of what is there", self.asked[-1]["detail"])
        self.assertEqual((self.work / "notes" / "ideas.md").read_text(encoding="utf-8"), "Bees\nPollen\n")
        undoChanges(box.loop)
        self.assertEqual((self.work / "notes" / "ideas.md").read_text(encoding="utf-8"), "Bees\nHoney is sweet\nWax\n")
        self.assertEqual(list(self.session.temp.glob(".*swarmup-copy*")) + list(self.work.rglob(".*swarmup-copy*")), [])

    def testWhatACopyMayNotDo(self):
        box = self.box()
        self.assertIn("cannot be copied into itself", self.refused(box, "copy_path", source="notes", target="notes/inner"))
        self.assertIn("cannot be copied into itself", self.refused(box, "copy_path", source="notes", target="notes"))
        (self.session.temp / "notes").write_text("a file", encoding="utf-8")
        self.assertIn("they cannot replace each other", self.refused(box, "copy_path", source="notes", target="@temp/notes", overwrite=True))
        self.assertIn("is outside the folder", self.refused(box, "copy_path", source="../", target="@temp"))
        self.assertIn("The user refused", self.refused(self.box(decision="deny"), "copy_path", source="@temp/notes", target="copied.txt"))
        self.assertFalse((self.work / "copied.txt").exists())
        with everywhere(background_processes, "STOP_SECONDS", 1), everywhere(__import__("agent_transfer"), "COPY_FILES", 0):
            self.assertIn("too big to be copied", self.refused(box, "copy_path", source="notes", target="@temp/big"))

    def testACopyIntoTheMemoryBelongsToItsAgent(self):
        box = self.box()
        self.ok(box, "copy_path", source="notes", target="@memory/sources")
        self.assertEqual(self.session.memory.ownerOf("sources/ideas.md"), "Writer")
        self.assertIn("sources/ideas.md", self.session.memory.index())
        self.assertIn("holds files written by Writer", self.refused(self.box("Checker"), "delete_path", path="@memory/sources"))
        self.ok(box, "move_path", source="@memory/sources", target="@temp/sources")
        self.assertIsNone(self.session.memory.ownerOf("sources/ideas.md"))
        self.assertTrue((self.session.temp / "sources" / "ideas.md").exists())


class ClipboardTests(TransferTestCase):
    def testABlockIsCopiedAndPastedWithItsExactCharacters(self):
        box = self.box()
        self.assertIn("lines 1 to 2", self.ok(box, "copy_text", path="windows.txt", start_line=1, end_line=2))
        self.ok(box, "paste_text", path="@temp/out.txt", at_end=True)
        self.assertEqual((self.session.temp / "out.txt").read_bytes(), b"one\r\ntwo\r\n")
        self.ok(box, "copy_text", text="élan ✓", clip="word")
        self.ok(box, "paste_text", path="@temp/out.txt", clip="word", after_text="one")
        self.assertEqual((self.session.temp / "out.txt").read_bytes(), "oneélan ✓\r\ntwo\r\n".encode("utf-8"))
        self.ok(box, "copy_text", path="notes/ideas.md", start_line=3, end_line=3, clip="last")
        self.ok(box, "paste_text", path="windows.txt", clip="last", at_line=2)
        self.assertEqual((self.work / "windows.txt").read_bytes(), b"one\r\nWax\ntwo\r\nthree")
        self.assertEqual(self.asked[-1]["action"], "change a file")
        self.ok(box, "paste_text", path="windows.txt", clip="word", at_end=True)
        self.assertTrue((self.work / "windows.txt").read_bytes().endswith("three\r\nélan ✓".encode("utf-8")))
        self.ok(box, "paste_text", path="notes/ideas.md", clip="word", replace_text="Honey is sweet")
        self.assertEqual((self.work / "notes" / "ideas.md").read_text(encoding="utf-8"), "Bees\nélan ✓\nWax\n")
        listed = self.ok(box, "list_clips")
        self.assertIn("- main: 2 lines from windows.txt, lines 1 to 2", listed)
        self.assertIn("- word: 1 lines from your text", listed)

    def testAPasteMustSayWhereAndFindItsPlace(self):
        box = self.box()
        self.assertIn("There is no clipboard main", self.refused(box, "paste_text", path="@temp/x.md", at_end=True))
        self.ok(box, "copy_text", text="x")
        self.assertIn("exactly one of", self.refused(box, "paste_text", path="windows.txt"))
        self.assertIn("exactly one of", self.refused(box, "paste_text", path="windows.txt", at_line=1, at_end=True))
        self.assertIn("must be found exactly once", self.refused(box, "paste_text", path="notes/ideas.md", after_text="nothing like it"))
        self.assertIn("at_line must be between 1 and 4", self.refused(box, "paste_text", path="notes/ideas.md", at_line=9))
        self.assertIn("does not exist. Paste with at_end", self.refused(box, "paste_text", path="@temp/new.md", at_line=1))
        self.assertIn("start_line and end_line must be between", self.refused(box, "copy_text", path="notes/ideas.md", start_line=3, end_line=2))
        self.assertIn("either a path", self.refused(box, "copy_text", path="notes/ideas.md", text="x"))

    # The clipboards are kept in the session, so a swarm that goes on keeps them, and each agent has its own.
    def testTheClipboardsLastAndBelongToTheirAgent(self):
        self.ok(self.box(), "copy_text", text="kept")
        self.assertIn("kept", self.ok(self.box(), "list_clips"))
        self.assertIn("empty", self.ok(self.box("Checker"), "list_clips"))


class ProcessTests(TransferTestCase):
    def testACommandRunsInTheBackgroundAndIsFollowedAndStopped(self):
        box = self.box()
        script = self.session.temp / "count.py"
        script.write_text("import time\nfor number in range(1000):\n    print(number, flush=True)\n    time.sleep(0.05)\n", encoding="utf-8")
        text = self.ok(box, "run_command", command=f'"{sys.executable}" count.py', cwd="@temp", background=True)
        self.assertIn("Started p1", text)
        self.assertIn("(It goes on in the background.)", self.asked[-1]["detail"])
        deadline = time.monotonic() + 10
        while "\n2" not in self.ok(box, "process_status", id="p1") and time.monotonic() < deadline:
            time.sleep(0.1)
        status = self.ok(box, "process_status", id="p1")
        self.assertIn("It still runs.", status)
        self.assertIn("p1: running", self.ok(box, "process_status"))
        self.assertIn("You have no command in the background", self.ok(self.box("Checker"), "process_status"))
        self.assertEqual(self.ok(box, "stop_process", id="p1"), "Done: p1 is stopped.")
        self.assertIn("It ended", self.ok(box, "process_status", id="p1"))
        self.assertTrue((self.session.temp / "processes" / "p1.log").exists())
        self.assertIn("A command runs in your folder or in @temp", self.refused(box, "run_command", command="ls", cwd="@memory"))

    # The swarm stops the commands of a mission, and a program that starts again stops those that an ended program left running.
    def testTheCommandsOfAMissionAreStoppedWithIt(self):
        box = self.box()
        self.ok(box, "run_command", command=f'"{sys.executable}" -c "import time; time.sleep(60)"', background=True)
        process = background_processes.RUNNING["m1"]["p1"]["process"]
        background_processes.RUNNING.pop("m1")
        self.assertEqual(background_processes.stopProcesses("m1", temp=self.session.temp), 0)
        process.wait(10)
        self.assertIsNotNone(process.returncode)
        self.assertFalse((self.session.temp / "processes" / "processes.json").exists())


class CodingAgentTests(TransferTestCase):
    def testACodingAgentFollowsTheRulesOfThePlaces(self):
        loop = self.box().loop
        memory = self.session.memory
        memory.write("Checker", "CHECKS.md", "x")
        self.assertEqual(placeRule(loop, self.session.temp / "a.py", True), "free")
        self.assertEqual(placeRule(loop, memory.folder / "CHECKS.md", False), "free")
        self.assertIn("was written by Checker", placeRule(loop, memory.folder / "CHECKS.md", True))
        self.assertEqual(placeRule(loop, memory.folder / "MINE.md", True), "free")
        self.assertIn("Only the leader", placeRule(loop, memory.longTerm / "STYLE.md", True))
        self.assertIn("kept by SwarmUP", placeRule(loop, memory.state / "owners.json", False))
        self.assertEqual(placeRule(loop, self.work / "notes" / "ideas.md", True), "ask")
        self.assertEqual(placeRule(Loop(None), self.session.temp / "a.py", True), "ask")
        self.assertEqual(MissionMemory("m2").ruleFor("Writer", self.session.temp / "a.py", True), "ask")


if __name__ == "__main__":
    unittest.main()
