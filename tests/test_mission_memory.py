import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from patching import everywhere
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
sys.path[:0] = [str(folder) for folder in sorted(BACKEND.iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import agent_prompts as prompts
import harness_utils
import mission_memory
from base_loop import Loop
from memory_cache import describeMemory, noteUse
from mission_memory import SWARMUP, MemoryRefused, MissionMemory, WorkSession
from swarm_harness import Swarm


class FakeAgent:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def input(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else "NOTHING"


# An agent that drafts once and lets the swarm review it.
class DraftLoop(Loop):
    def run(self):
        return self.reviewLoop("Write it.", lambda draft: "")


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.home = Path(files.name).resolve() / "agent-files"
        self.home.mkdir()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.memory = MissionMemory("m1", leader="Leader")
        self.told = []
        self.memory.onLongTermChange = lambda agent, name, change: self.told.append((agent, name, change))


class PlacesTests(MemoryTestCase):
    def testEachMissionHasItsMemoryAndItsTempFolderAndAllShareTheLongTermMemory(self):
        other = MissionMemory("m2")
        self.assertEqual(self.memory.folder, self.home / "mission-specific-memory" / "m1")
        self.assertEqual(self.memory.temp, self.home / "mission-temp" / "m1")
        self.assertEqual(self.memory.longTerm, other.longTerm)
        self.assertNotEqual(self.memory.folder, other.folder)
        for folder in (self.memory.folder, self.memory.temp, self.memory.longTerm):
            self.assertTrue(folder.is_dir())

    def testNoPathLeavesItsMemory(self):
        MissionMemory("m2").write("Writer", "SECRET.md", "of the other mission")
        for path in ("../m2/SECRET.md", "/etc/passwd", "notes/../../m2/SECRET.md", "."):
            with self.assertRaisesRegex(MemoryRefused, "not a file inside"):
                self.memory.write("Leader", path, "x")
        with self.assertRaisesRegex(MemoryRefused, "not a file inside"):
            self.memory.write("Leader", "../outside.md", "x", long=True)
        self.assertEqual((self.home / "mission-specific-memory" / "m2" / "SECRET.md").read_text(encoding="utf-8"), "of the other mission")


class OwnerTests(MemoryTestCase):
    def testAnAgentChangesOnlyWhatItWroteAndTheLeaderChangesEverything(self):
        self.memory.write("Writer", "DRAFT_IDEAS.md", "# Ideas\n- bees\n")
        self.memory.write("Writer", "DRAFT_IDEAS.md", "- honey\n", append=True)
        self.assertEqual(self.memory.ownerOf("DRAFT_IDEAS.md"), "Writer")
        with self.assertRaisesRegex(MemoryRefused, "was written by Writer"):
            self.memory.write("Checker", "DRAFT_IDEAS.md", "mine now")
        with self.assertRaisesRegex(MemoryRefused, "was written by Writer"):
            self.memory.delete("Checker", "DRAFT_IDEAS.md")
        self.memory.write("Leader", "DRAFT_IDEAS.md", "- wax\n", append=True)
        self.assertEqual(self.memory.ownerOf("DRAFT_IDEAS.md"), "Writer")
        self.assertEqual((self.memory.folder / "DRAFT_IDEAS.md").read_text(encoding="utf-8"), "# Ideas\n- bees\n- honey\n- wax\n")
        self.memory.delete("Leader", "DRAFT_IDEAS.md")
        self.assertFalse((self.memory.folder / "DRAFT_IDEAS.md").exists())
        self.assertIsNone(self.memory.ownerOf("DRAFT_IDEAS.md"))

    def testWhatSwarmUpRecordsIsChangedOnlyByTheLeaderAndItsIndexAndStateByNobody(self):
        self.memory.addProgress(1, "Summary", "All good.")
        self.assertEqual(self.memory.ownerOf("PROGRESS.md"), SWARMUP)
        with self.assertRaisesRegex(MemoryRefused, "written by SwarmUP"):
            self.memory.write("Writer", "PROGRESS.md", "fake progress")
        self.memory.write("Leader", "PROGRESS.md", "\nA note of the leader.\n", append=True)
        for agent in ("Writer", "Leader"):
            with self.assertRaisesRegex(MemoryRefused, "index"):
                self.memory.write(agent, "MISSION.md", "x")
            with self.assertRaisesRegex(MemoryRefused, "kept by SwarmUP"):
                self.memory.write(agent, ".swarmup/owners.json", "{}")

    def testANoteBelongsToItsAgent(self):
        self.memory.addNotes("Writer", ["The user likes haiku."])
        self.assertEqual(self.memory.ownerOf("notes/WRITER_NOTES.md"), "Writer")
        self.assertIn("The user likes haiku.", self.memory.readNotes("Writer"))
        self.assertEqual(self.memory.readNotes("Checker"), "")


class RecordTests(MemoryTestCase):
    def testEveryPromptPlanAndOutputIsKeptByRoundWithAnIndex(self):
        self.memory.setMission("Write about bees.", 1)
        self.memory.addUserPrompt(1, "Write about bees.", kind="The mission")
        self.memory.addUserPrompt(1, "Shorter, please.", "Writer")
        self.memory.addPlan(1, "Writer", "1. Read. 2. Write.")
        self.memory.addOutput(1, "Writer", "Bees are great.")
        self.memory.addOutput(1, "Leader", "The report.")
        self.memory.addUserPrompt(2, "Now in French.", kind="Follow-up")
        self.memory.addUserPrompt(2, "## My own heading\nwith its text", "Writer")
        prompts_ = (self.memory.folder / "USER_PROMPTS.md").read_text(encoding="utf-8")
        self.assertIn("## Round 1: The mission", prompts_)
        self.assertIn("## Round 1: Message to Writer", prompts_)
        self.assertIn("Shorter, please.", self.memory.readRound("USER_PROMPTS.md", 1))
        self.assertNotIn("Now in French.", self.memory.readRound("USER_PROMPTS.md", 1))
        self.assertIn("Now in French.", self.memory.readRound("USER_PROMPTS.md", 2))
        self.assertIn("## My own heading\nwith its text", self.memory.readRound("USER_PROMPTS.md", 2))
        self.assertIn("Bees are great.", (self.memory.folder / "outputs" / "WRITER_OUTPUTS.md").read_text(encoding="utf-8"))
        self.assertTrue((self.memory.folder / "outputs" / "LEADER_OUTPUTS.md").exists())
        index = self.memory.index()
        self.assertIn("# Mission m1\nWrite about bees.", index)
        self.assertIn("- PLANS.md: the plans the user approved", index)
        self.assertIn("- outputs/WRITER_OUTPUTS.md: the approved results of one agent, by round [by SwarmUP", index)
        self.assertNotIn(".swarmup", index)


class LongTermTests(MemoryTestCase):
    def testOnlyTheLeaderWritesItAndTheUserIsAlwaysTold(self):
        with self.assertRaisesRegex(MemoryRefused, "Only the leader"):
            self.memory.write("Writer", "STYLE.md", "x", long=True)
        self.assertEqual(self.told, [])
        self.memory.write("Leader", "STYLE.md", "- short sentences\n", long=True)
        self.memory.write("Leader", "STYLE.md", "- no jargon\n", long=True, append=True)
        self.assertEqual([(agent, name) for agent, name, change in self.told], [("Leader", "STYLE.md"), ("Leader", "STYLE.md")])
        self.assertIn("+- no jargon", self.told[-1][2])
        index = MissionMemory("m2").index(long=True)
        self.assertIn("- STYLE.md: short sentences [last written by Leader of mission m1", index)
        self.memory.delete("Leader", "STYLE.md", long=True)
        self.assertIn("-- short sentences", self.told[-1][2])

    def testTheBlocksOfTheLeaderAreAppliedWhenTheyAreSafe(self):
        changed, problems = self.memory.applyBlocks("Leader", """Here is what I keep:
<swarmup_memory file="USER_PREFERENCES.md">- writes in French</swarmup_memory>
<swarmup_memory file='STYLE.md' mode="replace">- formal</swarmup_memory>
<swarmup_memory file="../evil.md">x</swarmup_memory>
<swarmup_memory file="run.sh">x</swarmup_memory>""")
        self.assertEqual(changed, ["USER_PREFERENCES.md", "STYLE.md"])
        self.assertEqual(len(problems), 2)
        self.assertEqual((self.memory.longTerm / "STYLE.md").read_text(encoding="utf-8"), "- formal\n")
        self.assertFalse((self.memory.longTerm.parent / "evil.md").exists())
        self.assertEqual(self.memory.applyBlocks("Writer", '<swarmup_memory file="X.md">x</swarmup_memory>')[0], [])

    # A model writes its blocks as it can: a closing tag with a typo, or none. An answer without any block is told as a problem.
    def testTheBlocksAreReadEvenWhenTheyAreNotExact(self):
        changed, problems = self.memory.applyBlocks("Leader", '<swarmup_memory file="USER_PREFERENCES.md" mode="append">\n- writes in French\n</swwarmup_memory>\n'
                                                              '<swarmup_memory file="SIGNATURE.md">- signs "The Bee Club team"')
        self.assertEqual((changed, problems), (["USER_PREFERENCES.md", "SIGNATURE.md"], []))
        self.assertEqual((self.memory.longTerm / "USER_PREFERENCES.md").read_text(encoding="utf-8"), "- writes in French\n")
        self.assertEqual((self.memory.longTerm / "SIGNATURE.md").read_text(encoding="utf-8"), '- signs "The Bee Club team"\n')
        self.assertEqual(self.memory.applyBlocks("Leader", "NOTHING"), ([], []))
        self.assertEqual(self.memory.applyBlocks("Leader", "I would keep that the user likes French."),
                         ([], ["its answer had no <swarmup_memory> block that SwarmUP could read"]))


class LockTests(MemoryTestCase):
    # Two programs add notes to the same mission at the same time, and every note is kept.
    def testTwoProgramsWritingAtOnceLoseNothing(self):
        script = (f"import sys; sys.path[:0] = {[str(folder) for folder in sorted(BACKEND.iterdir()) if folder.is_dir()]!r}\n"
                  "from mission_memory import MissionMemory\n"
                  "memory = MissionMemory('m1')\n"
                  "for number in range(40):\n"
                  "    memory.addNotes('Writer', [f'{sys.argv[1]}-{number}'])\n")
        environment = {**os.environ, "SWARMUP_HOME": str(self.home)}
        workers = [subprocess.Popen([sys.executable, "-c", script, name], env=environment) for name in ("a", "b")]
        self.assertEqual([worker.wait(60) for worker in workers], [0, 0])
        text = (self.memory.folder / "notes" / "WRITER_NOTES.md").read_text(encoding="utf-8")
        for name in ("a", "b"):
            for number in range(40):
                self.assertIn(f"] {name}-{number}\n", text)
        self.assertEqual(json.loads((self.memory.folder / ".swarmup" / "owners.json").read_text(encoding="utf-8"))["notes/WRITER_NOTES.md"], "Writer")


class CacheTests(MemoryTestCase):
    def testTheAgentSeesTheRulesTheLongTermMemoryAndWhatMattersMostOfTheMission(self):
        self.memory.write("Leader", "USER_PREFERENCES.md", "- answers in French\n", long=True)
        self.memory.addProgress(1, "Summary", "Writer is drafting.")
        self.memory.addNotes("Writer", ["the user likes haiku"])
        self.memory.write("Checker", "CHECKS.md", "# Checks\nall fine")
        text = describeMemory(self.memory, "Writer")
        self.assertIn("YOUR MEMORY AND YOUR TEMP FOLDER", text)
        self.assertIn(str(self.memory.temp), text)
        self.assertIn(prompts.MEMORY_AGENT_RIGHTS.split("\n")[0], text)
        self.assertIn("- answers in French", text)
        self.assertIn("Writer is drafting.", text)
        self.assertIn("the user likes haiku", text)
        self.assertIn("all fine", text)
        self.assertLess(text.index("answers in French"), text.index("THE MEMORY OF THIS MISSION"))
        self.assertIn(prompts.MEMORY_LEADER_RIGHTS.split("\n")[0], describeMemory(self.memory, "Leader"))

    # Within its budget the agent sees its own files and those used most. The others are listed by name, to read when needed.
    def testTheMostUsefulFilesComeFirstAndTheRestByName(self):
        for name in ("A_NOTES_OF_CHECKER.md", "B_DATA.md", "C_MINE.md"):
            self.memory.write("Writer" if name == "C_MINE.md" else "Checker", name, name[0] * 3000)
        for count in range(5):
            noteUse(self.memory.folder, "B_DATA.md", "Writer")
        old = time.time() - 30 * 24 * 3600
        os.utime(self.memory.folder / "A_NOTES_OF_CHECKER.md", (old, old))
        text = describeMemory(self.memory, "Writer", budget=10000)
        self.assertIn("--- @memory/C_MINE.md ---", text)
        self.assertIn("--- @memory/B_DATA.md ---", text)
        self.assertNotIn("--- @memory/A_NOTES_OF_CHECKER.md ---", text)
        self.assertIn("Not shown here (read them with read_file when you need them): @memory/A_NOTES_OF_CHECKER.md", text)

    def testALongFileShowsItsLatestPartWithWhereToReadTheRest(self):
        self.memory.addProgress(1, "Old", "x" * 5000)
        self.memory.addProgress(2, "New", "the latest news")
        text = describeMemory(self.memory, "Writer")
        self.assertIn("the latest news", text)
        self.assertIn("read @memory/PROGRESS.md for all of it", text)


class SessionTests(MemoryTestCase):
    # A swarm saved before the mission memory keeps its conversations and its notes: they move into the mission.
    def testAnOldSessionMovesIntoItsMission(self):
        old = self.home / "sessions" / "m9"
        (old / "conversations").mkdir(parents=True)
        (old / "conversations" / "Writer.json").write_text("{}", encoding="utf-8")
        (old / "notes").mkdir()
        (old / "notes" / "Writer.md").write_text("- [2026-10-01 10:00] the user likes haiku\n", encoding="utf-8")
        session = WorkSession("m9", "run")
        self.assertTrue((session.folder / "conversations" / "Writer.json").exists())
        self.assertIn("the user likes haiku", session.readNotes("Writer"))
        self.assertFalse(old.exists())


class SwarmTests(MemoryTestCase):
    def runSwarm(self, replies, decide):
        swarm = Swarm("Write about bees.")
        leader = DraftLoop(FakeAgent(replies))
        leader.notifyUser = lambda message: self.said.append(message)
        swarm.addAgent("Leader", leader, "writer", "write")
        swarm.addListener(lambda event: decide(swarm, event))
        self.said = []
        swarm.run()
        return swarm, leader

    def testARunKeepsTheMissionTheMessagesAndTheResult(self):
        def decide(swarm, event):
            if event["kind"] == "review" and event.get("review") == "ready":
                swarm.approveDraft("Leader")
        swarm, leader = self.runSwarm(["Bees are great."], decide)
        memory = MissionMemory(swarm.id)
        self.assertEqual(swarm.requests[0]["text"], "Write about bees.")
        self.assertIn("Write about bees.", memory.readRound("USER_PROMPTS.md", 1))
        self.assertIn("Approved its draft.", memory.readRound("USER_PROMPTS.md", 1))
        self.assertIn("Bees are great.", (memory.folder / "outputs" / "LEADER_OUTPUTS.md").read_text(encoding="utf-8"))
        self.assertIn("The final report of the leader", (memory.folder / "PROGRESS.md").read_text(encoding="utf-8"))
        # Only the mission and an approval: nothing to learn, so the leader was not asked about the long-term memory.
        self.assertEqual(len(leader.agent.prompts), 1)

    # After a correction the leader keeps what lasts in the long-term memory, and the user is told.
    def testAfterACorrectionTheLeaderUpdatesTheLongTermMemoryAndTellsTheUser(self):
        def decide(swarm, event):
            if event["kind"] == "review" and event.get("review") == "ready":
                if swarm.getInfo("Leader")["revision"] == 1:
                    swarm.correctDraft("Leader", "Always write in French.")
                else:
                    swarm.approveDraft("Leader")
        swarm, leader = self.runSwarm(["Bees are great.", "Les abeilles.", '<swarmup_memory file="USER_PREFERENCES.md">- writes in French</swarmup_memory>'], decide)
        self.assertIn("Always write in French.", leader.agent.prompts[-1])
        self.assertEqual((self.home / "long-term-memory" / "USER_PREFERENCES.md").read_text(encoding="utf-8"), "- writes in French\n")
        told = [message for message in self.said if "long-term memory" in message]
        self.assertEqual(len(told), 1)
        self.assertNotIn("FILE_NAME.md", (self.home / "long-term-memory" / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertIn("Leader wrote @long-term/USER_PREFERENCES.md", told[0])
        self.assertIn("+- writes in French", told[0])

    # The leader keeps the user in the loop even when it keeps nothing.
    def testTheUserIsToldWhenThereIsNothingToKeep(self):
        def decide(swarm, event):
            if event["kind"] == "review" and event.get("review") == "ready":
                if swarm.getInfo("Leader")["revision"] == 1:
                    swarm.correctDraft("Leader", "Fix the typo.")
                else:
                    swarm.approveDraft("Leader")
        self.runSwarm(["Bees ar great.", "Bees are great.", "NOTHING"], decide)
        self.assertIn("found nothing new to keep in the long-term memory", "\n".join(self.said))
        self.assertEqual(list((self.home / "long-term-memory").glob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
