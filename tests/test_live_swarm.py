import re
import sys
import tempfile
import threading
import unittest
from patching import everywhere
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import agent_prompts as prompts
import harness_utils
from base_loop import Loop
from models_library import getModelInfo
from swarm_harness import Swarm
from swarm_review import REMOVED_MESSAGE
from test_harness_utils import DraftLoop, LoopTestCase
from test_saved_swarms import waitUntil


# A model that answers every kind of prompt of a swarm, and that can free its memory like a local model.
class Model:
    def __init__(self, result="done"):
        self.result, self.unloaded = result, False

    def input(self, prompt):
        # The memory of the mission comes before the request, and the fake only reads the request.
        prompt = prompt.split(prompts.NOTES_PROMPT)[-1]
        names = re.findall(r"^- ([A-Za-z][\w.-]*) \(", prompt, re.M)
        if "summary" in prompt.lower() and names:
            return "Where the team stands: " + ", ".join(names) + "."
        return f"Plan or draft: {self.result}"

    def unload(self):
        self.unloaded = True


# An agent whose work waits for a gate, so a test can change the swarm while it works. It remembers if its work was put back.
class GatedLoop(Loop):
    def __init__(self, result, gate=None):
        super().__init__(Model(result))
        self.result, self.gate, self.rolledBack = result, gate, False

    def run(self):
        if self.gate is not None:
            self.gate.wait(10)
        return self.result

    def rollback(self):
        self.rolledBack = True


class LiveTeamTestCase(LoopTestCase):
    def setUp(self):
        super().setUp()
        self.events = []

    def start(self, swarm, mode="execute"):
        swarm.addListener(self.events.append)
        swarm.setMode(mode)
        swarm.startInBackground()
        self.addCleanup(lambda: (swarm.stopWork() if swarm.isRunning() else None, swarm.wait(10)))

    def statusOf(self, swarm, name):
        return lambda: name in swarm.members and swarm.getStatus(name) == "done"

    def news(self, swarm, name):
        return "\n".join(swarm.members[name]["agent"].inbox)


class RemoveTests(LiveTeamTestCase):
    def testAFinishedAgentLeavesAndItsResultStillReachesTheAgentsThatWaitForIt(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("A", GatedLoop("facts"), "researcher", "find facts")
        swarm.addAgent("B", GatedLoop("draft", gate), "writer", "write")
        swarm.addAgent("C", GatedLoop("report"), "editor", "edit", waitsFor=["A", "B"])
        self.start(swarm)
        waitUntil(self.statusOf(swarm, "A"), "A to finish")
        model = swarm.getMember("A")["agent"].agent
        swarm.removeAgent("A", "It is done, and its model can free the GPU.")
        self.assertEqual(swarm.getAgents(), ["Leader", "B", "C"])
        self.assertEqual(swarm.getMember("C")["waitsFor"], ["B"])
        gate.set()
        self.assertEqual(swarm.wait(10)["result"], "final")
        self.assertEqual([message["message"] for message in swarm.getMessages("A", "C")], ["facts"])
        self.assertEqual(swarm.getStatus("C"), "done")
        waitUntil(lambda: model.unloaded, "the model of A to free its memory")
        self.assertFalse(swarm.removed and any(event["kind"] == "removed" and event["agent"] != "A" for event in self.events))
        self.assertEqual((swarm.removed[0]["name"], swarm.removed[0]["status"], swarm.removed[0]["reason"]), ("A", "done", "It is done, and its model can free the GPU."))
        for name in ("Leader", "B", "C"):
            self.assertIn("A (researcher) left the swarm", self.news(swarm, name))
        self.assertIn("removed from the swarm when it was done", swarm.describeProgress())

    def testAnUnfinishedAgentIsStoppedItsWorkPutBackAndTheOthersGoOnWithoutIt(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("Slow", GatedLoop("late", gate), "coder", "code")
        swarm.addAgent("Next", GatedLoop("next"), "tester", "test", waitsFor=["Slow"])
        self.start(swarm)
        waitUntil(lambda: swarm.getStatus("Slow") == "working", "Slow to work")
        loop = swarm.getMember("Slow")["agent"]
        swarm.removeAgent("Slow", "It takes too long.")
        self.assertEqual(swarm.wait(10)["result"], "final")
        self.assertEqual(swarm.getStatus("Next"), "done")
        self.assertFalse(loop.rolledBack)
        gate.set()
        waitUntil(lambda: loop.rolledBack and loop.agent.unloaded, "the work of Slow to be put back and its model freed")
        self.assertEqual(swarm.getMessages("Slow", "Leader"), [])
        self.assertTrue(any(event["kind"] == "retired" and event["agent"] == "Slow" for event in self.events))

    def testAnAgentThatWaitsForTheUserLeavesAtOnce(self):
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("Drafter", DraftLoop(Model("text")), "writer", "write")
        swarm.getMember("Leader")["agent"].askUser = lambda question: threading.Event().wait(10) or "no"
        self.start(swarm)
        waitUntil(lambda: "Drafter" in swarm.members and swarm.getInfo("Drafter")["review"] == "ready", "the draft to wait for the user")
        thread = swarm.stage["threads"]["Drafter"]
        swarm.removeAgent("Drafter", "Not needed.")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(swarm.departed.get("Drafter", {"error": REMOVED_MESSAGE}).get("error") or REMOVED_MESSAGE, REMOVED_MESSAGE)

    def testTheLeaderCannotBeRemovedNorAnAgentThatIsNotThere(self):
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        with self.assertRaises(ValueError):
            swarm.removeAgent("Leader")
        with self.assertRaises(ValueError):
            swarm.removeAgent("Ghost")

    def testTheRemovedAgentsAreSavedAndComeBack(self):
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("A", GatedLoop("facts"), "researcher", "find")
        swarm.removeAgent("A", "Not needed.")
        state = swarm.describeState()
        self.assertEqual(list(state["members"]), ["Leader"])
        self.assertEqual(state["removed"][0]["name"], "A")
        restored = Swarm.restore(state, lambda name, data: GatedLoop("final"))
        self.assertEqual((restored.getAgents(), restored.removed[0]["reason"]), (["Leader"], "Not needed."))


class AddTests(LiveTeamTestCase):
    def testAnAgentJoinsTheWorkersAndReceivesTheResultsItWaitsFor(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("A", GatedLoop("facts"), "researcher", "find facts")
        swarm.addAgent("B", GatedLoop("draft", gate), "writer", "write")
        self.start(swarm)
        waitUntil(self.statusOf(swarm, "A"), "A to finish")
        swarm.addAgent("New", GatedLoop("checked"), "checker", "check the facts", waitsFor=["A"])
        waitUntil(self.statusOf(swarm, "New"), "the new agent to finish")
        gate.set()
        self.assertEqual(swarm.wait(10)["result"], "final")
        self.assertEqual([message["message"] for message in swarm.getMessages("A", "New")], ["facts"])
        self.assertEqual([message["message"] for message in swarm.getMessages("New", "Leader")], ["checker: checked"])
        self.assertIn("You are New, the checker", self.news(swarm, "New"))
        for name in ("Leader", "A", "B"):
            self.assertIn("New joined the swarm: checker. Task: check the facts It waits for A.", self.news(swarm, name))
        self.assertTrue(any(event["kind"] == "joined" and event["agent"] == "New" for event in self.events))

    def testAnAgentJoinsWhileTheSwarmPlansAndItsPlanIsReviewed(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", DraftLoop(Model("lead")), "leader", "lead")
        swarm.addAgent("A", DraftLoop(Model("a")), "writer", "write")
        swarm.getMember("Leader")["agent"].askUser = lambda question: gate.wait(10) and "yes"
        self.start(swarm, "plan")
        waitUntil(lambda: swarm.getInfo("A")["review"] == "ready", "the plan of A")
        swarm.addAgent("B", DraftLoop(Model("b")), "checker", "check")
        gate.set()
        self.assertIsNotNone(swarm.wait(10)["result"])
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "A": "done", "B": "done"})
        self.assertEqual(swarm.getInfo("B")["plan"], "Plan or draft: b")

    def testNobodyJoinsOnceTheLeaderStartedItsFinalWork(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final", gate), "leader", "lead")
        swarm.addAgent("A", GatedLoop("facts"), "researcher", "find")
        self.start(swarm)
        waitUntil(lambda: swarm.getStatus("Leader") == "working", "the leader to start its final work")
        with self.assertRaises(ValueError) as caught:
            swarm.addAgent("Late", GatedLoop("late"), "writer", "write")
        self.assertIn("leader already started its final work", str(caught.exception))
        self.assertNotIn("Late", swarm.getAgents())
        gate.set()
        swarm.wait(10)
        swarm.addAgent("Late", GatedLoop("late"), "writer", "write")
        self.assertIn("Late", swarm.getAgents())

    def testAnAgentCannotWaitForTheLeaderOrForAnAgentThatIsNotThere(self):
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        for waits in (["Leader"], ["Ghost"]):
            with self.assertRaises(ValueError):
                swarm.addAgent("A", GatedLoop("a"), "writer", "write", waitsFor=waits)


class ModelChangeTests(LiveTeamTestCase):
    def testOnlyAnAgentThatDidNotStartCanChangeItsModelWhileTheSwarmRuns(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("A", GatedLoop("facts", gate), "researcher", "find")
        swarm.addAgent("B", GatedLoop("old"), "writer", "write", waitsFor=["A"])
        info = getModelInfo("claude-haiku-4-5")
        self.start(swarm)
        waitUntil(lambda: swarm.getStatus("A") == "working", "A to work")
        with self.assertRaises(ValueError):
            swarm.setModel("A", info, GatedLoop("other"))
        oldModel = swarm.getMember("B")["agent"].agent
        swarm.setModel("B", info, GatedLoop("new"))
        gate.set()
        swarm.wait(10)
        self.assertEqual((swarm.getInfo("B")["result"], swarm.getInfo("B")["model"]["name"]), ("new", "claude-haiku-4-5"))
        self.assertIn("You are B, the writer", self.news(swarm, "B"))
        waitUntil(lambda: oldModel.unloaded, "the old model of B to free its memory")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = everywhere(harness_utils, "AGENT_FILES", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    # A worker that finished lets the memory of its local model go before the leader works, so the leader finds room on the GPUs. An API
    # model, and the models of plan mode (they execute next), are kept.
    def testAFinishedWorkerLetsItsLocalModelGo(self):
        swarm = Swarm("Mission")
        local, remote = Model("a"), Model("b")
        local.local = True
        seen = {}
        class Leader(Loop):
            def run(self):
                seen["unloaded"] = (local.unloaded, remote.unloaded)
                return "report"
        for name, loop in (("Leader", Leader(Model())), ("Local", Loop(local)), ("Remote", Loop(remote))):
            if name != "Leader":
                loop.run = lambda: "done"
            swarm.addAgent(name, loop, "role", "task")
        swarm.run()
        self.assertEqual(seen["unloaded"], (True, False))


if __name__ == "__main__":
    unittest.main()
