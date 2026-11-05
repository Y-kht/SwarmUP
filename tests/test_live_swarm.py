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
        # The conversation comes before the request, and the fake only reads the newest request.
        prompt = prompt.split("\n\n[SwarmUP]\n")[-1]
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


class FilesTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = everywhere(harness_utils, "AGENT_FILES", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)


class ReleaseTests(FilesTestCase):
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


    # After a plan all the models are loaded: an agent that waits for another one lets its memory go until it starts. So does one that sits a round out.
    def testAnAgentThatWaitsAfterAPlanHoldsNoMemoryUntilItStarts(self):
        gate, started = threading.Event(), threading.Event()
        first, second = GatedLoop("facts", gate), GatedLoop("text")
        for loop in (first, second):
            loop.agent.local = True
        second.run = lambda: started.set() or "text"
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("report"), "leader", "lead")
        swarm.addAgent("First", first, "researcher", "find")
        swarm.addAgent("Second", second, "writer", "write", waitsFor=["First"])
        swarm.startInBackground()
        self.addCleanup(lambda: (gate.set(), swarm.wait(10)))
        waitUntil(lambda: second.agent.unloaded, "the agent that waits to let its model go")
        self.assertFalse(first.agent.unloaded)
        self.assertFalse(started.is_set())
        gate.set()
        swarm.wait(10)
        self.assertTrue(started.is_set())

    def testAnAgentThatSitsARoundOutHoldsNoMemory(self):
        loop = GatedLoop("facts")
        loop.agent.local = True
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("report"), "leader", "lead")
        swarm.addAgent("Writer", loop, "writer", "write")
        swarm.run()
        loop.agent.unloaded = False
        swarm.followUp("Now only the leader works.")
        swarm.routeFollowUp = lambda request, report=None: {}
        swarm.run()
        self.assertTrue(swarm.getInfo("Writer")["sitsOut"])
        self.assertTrue(loop.agent.unloaded)


# A model that holds memory of the GPUs like a local model.
class Held(Model):
    def __init__(self, gb, gpus, loaded=True):
        super().__init__()
        self.local, self.isLoaded, self.held = True, loaded, {"gb": gb, "gpus": gpus}


LOCAL = {"name": "Local", "local": True, "vram": 10.0}
TWO_GPUS = [{"index": 2, "name": "A", "total": 24.0, "free": 8.0}, {"index": 3, "name": "B", "total": 24.0, "free": 20.0}]


class LiveVramTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("readGpus", lambda: [dict(gpu) for gpu in TWO_GPUS]), ("programMemory", lambda gpus: [14.0, 3.0])):
            patcher = everywhere(harness_utils, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def swarm(self, **models):
        swarm = Swarm("Mission")
        for name, model in models.items():
            swarm.addAgent(name, GatedLoop("done"), "role", "task", model=LOCAL)
            swarm.members[name]["agent"].agent = model
        return swarm

    # The numbers of the GPUs are those of the machine (GPU 2 and 3 here), and only what is in memory counts, not what the swarm is expected to need.
    def testEachGpuShowsItsMemoryAndTheAgentsThatHoldSome(self):
        live = self.swarm(Leader=Held(9.0, [0]), Worker=Held(2.5, [1]), Waiting=Held(0, [], loaded=False)).getLiveVram()
        self.assertEqual((live["total"], live["used"], live["free"], live["program"], live["others"], live["needed"]), (48.0, 20.0, 28.0, 17.0, 3.0, 30.0))
        self.assertEqual([(gpu["index"], gpu["used"], gpu["program"], gpu["others"], gpu["agents"]) for gpu in live["gpus"]], [(2, 16.0, 14.0, 2.0, ["Leader"]), (3, 4.0, 3.0, 1.0, ["Worker"])])
        self.assertEqual([(holder["name"], holder["gb"], holder["gpus"]) for holder in live["agents"]], [("Leader", 9.0, [2]), ("Worker", 2.5, [3])])

    # An agent that left holds its memory until its last step ended, and an architect with a model of its own holds some too.
    def testAnAgentThatLeftAndTheArchitectCountWhileTheirModelIsInMemory(self):
        swarm = self.swarm(Leader=Held(9.0, [0]))
        swarm.departed["Gone"] = {"agent": GatedLoop("done"), "model": LOCAL, "status": "failed"}
        swarm.departed["Gone"]["agent"].agent = Held(4.0, [1])
        swarm.architect = type("Architect", (), {"how": "own", "client": Held(6.0, [1]), "info": LOCAL, "state": "working"})()
        self.assertEqual([holder["name"] for holder in swarm.getLiveVram()["agents"]], ["Leader", "Gone", "Architect"])
        swarm.architect.how = "share"
        self.assertEqual([holder["name"] for holder in swarm.getLiveVram()["agents"]], ["Leader", "Gone"])

    def testAModelThatCannotTellHoldsWhatItWasExpectedToNeed(self):
        model = Model()
        model.isLoaded = True
        self.assertEqual(self.swarm(Leader=model).getLiveVram()["agents"][0]["gb"], 10.0)

    def testTheUserInterfacesAreToldOnlyWhenTheMemoryChanged(self):
        swarm, events = self.swarm(Leader=Held(9.0, [0])), []
        swarm.addListener(events.append)
        swarm.noteVram()
        swarm.noteVram()
        self.assertEqual(len([event for event in events if event["kind"] == "vram"]), 1)
        swarm.members["Leader"]["agent"].agent.isLoaded = False
        swarm.noteVram()
        self.assertEqual(len([event for event in events if event["kind"] == "vram"]), 2)

    def testASwarmWithoutLocalModelsReadsNothing(self):
        swarm, events = Swarm("Mission"), []
        swarm.addListener(events.append)
        swarm.addAgent("Leader", GatedLoop("done"), "role", "task", model={"name": "api", "local": False, "vram": 0})
        with everywhere(harness_utils, "readGpus", side_effect=AssertionError("the GPUs must not be read")):
            swarm.noteVram()
        self.assertEqual(events, [])


# The leader of LostAgentTests: it writes its decision about an agent that lost what it waited for.
class Deciding(Model):
    def __init__(self, *replies):
        super().__init__("final")
        self.replies, self.asked = list(replies), []

    def input(self, prompt):
        if "will not give it" in prompt:
            self.asked.append(prompt)
            return self.replies.pop(0)
        return super().input(prompt)


class Failing(GatedLoop):
    def __init__(self):
        super().__init__("never")
        self.agent.local = True

    def run(self):
        raise RuntimeError("the model broke")


KEEP = '{"decision": "keep", "why": "It can read the sources by itself."}'
REMOVE = '{"decision": "remove", "why": "It only checks the notes of Reader."}'


# An agent that failed for good leaves the swarm and frees its memory. The leader decides about the agents that waited for it.
class LostAgentTests(LiveTeamTestCase):
    def team(self, *replies, leaderKind=GatedLoop):
        swarm, self.said = Swarm("Review the papers"), []
        leader = leaderKind("final")
        leader.agent = self.leaderModel = Deciding(*replies)
        leader.notifyUser = self.said.append
        swarm.addAgent("Leader", leader, "leader", "lead")
        swarm.addAgent("Reader", Failing(), "reader", "read the papers")
        return swarm

    def testAnAgentThatFailsLeavesTheSwarmAndItsModelFreesItsMemory(self):
        swarm = self.team()
        reader = swarm.members["Reader"]["agent"]
        self.assertEqual(swarm.run(), "final")
        self.assertEqual(swarm.getAgents(), ["Leader"])
        self.assertEqual(swarm.getNeededVram(), 0)
        self.assertEqual((swarm.removed[0]["name"], swarm.removed[0]["status"]), ("Reader", "failed"))
        self.assertIn("It failed: RuntimeError: the model broke", swarm.removed[0]["reason"])
        waitUntil(lambda: reader.agent.unloaded and "Reader" not in swarm.departed, "the model to free its memory")
        self.assertEqual(self.leaderModel.asked, [])

    def testTheLeaderKeepsAnAgentThatCanStillDoItsTask(self):
        swarm = self.team(KEEP)
        swarm.addAgent("Writer", GatedLoop("text"), "writer", "write the review", waitsFor=["Reader"])
        writer = swarm.members["Writer"]["agent"]
        self.assertEqual(swarm.run(), "final")
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "Writer": "done"})
        self.assertEqual(swarm.getInfo("Writer")["result"], "text")
        [asked] = self.leaderModel.asked
        self.assertIn("Writer (writer) waited for the result of agents that will not give it", asked)
        self.assertIn("RuntimeError: the model broke", asked)
        self.assertIn("write the review", asked)
        self.assertIn("The leader decided that you go on without it: It can read the sources by itself.", "\n".join(writer.inbox))
        self.assertIn("[Leader] Writer waited for Reader, which did not finish. It goes on without it: It can read the sources by itself.", self.said)

    # The agent that leaves frees its memory, and the agent that waited for it is decided about in the same way.
    def testTheLeaderRemovesAnAgentThatCannotAndTheAgentsAfterItAreDecidedAboutToo(self):
        swarm = self.team(REMOVE, KEEP)
        checker, editor = GatedLoop("never"), GatedLoop("edited")
        checker.agent.local = True
        swarm.addAgent("Checker", checker, "checker", "check the notes", waitsFor=["Reader"])
        swarm.addAgent("Editor", editor, "editor", "edit the review", waitsFor=["Checker"])
        self.assertEqual(swarm.run(), "final")
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "Editor": "done"})
        self.assertEqual([item["name"] for item in swarm.removed], ["Reader", "Checker"])
        self.assertIn("It only checks the notes of Reader.", swarm.removed[1]["reason"])
        waitUntil(lambda: checker.agent.unloaded, "the model of the agent that left to free its memory")
        self.assertEqual(len(self.leaderModel.asked), 2)
        self.assertIn("Checker", self.leaderModel.asked[1])

    # A small model takes "the model could not be opened" for a problem of the files, and copies the values of an example: the prompt tells what is missing,
    # whether the task names the agent, and gives no value to copy.
    def testTheLeaderIsToldWhatIsMissingAndWhetherTheTaskNamesTheAgent(self):
        swarm = self.team(KEEP, REMOVE)
        swarm.addAgent("Writer", GatedLoop("text"), "writer", "write from notes.txt", waitsFor=["Reader"])
        swarm.addAgent("Checker", GatedLoop("never"), "checker", "check what Reader gives you", waitsFor=["Reader"])
        swarm.run()
        byName = {name: next(prompt for prompt in self.leaderModel.asked if f"{name} (" in prompt) for name in ("Writer", "Checker")}
        self.assertIn("This task does not name them", byName["Writer"])
        self.assertIn("This task names Reader", byName["Checker"])
        self.assertIn("the files of the folder and the tools of Checker still work", byName["Checker"])
        self.assertNotIn('"decision": "keep"', byName["Checker"])

    def testALeaderThatCannotDecideLetsTheAgentTry(self):
        swarm = self.team(*["I am not sure."] * 5)
        swarm.addAgent("Writer", GatedLoop("text"), "writer", "write", waitsFor=["Reader"])
        swarm.run()
        self.assertEqual(swarm.getInfo("Writer")["result"], "text")
        self.assertIn("[Leader] Writer waited for Reader, which did not finish. It goes on without it: the leader could not decide, so it tries", self.said)

    def testAUserWhoLeadsTheSwarmDecidesInstead(self):
        for answer, result in (("yes", {"Leader": "done", "Writer": "done"}), ("no", {"Leader": "done"})):
            swarm = self.team()
            leader = swarm.members["Leader"]["agent"]
            leader.agent.approvesItself = True
            questions = []
            leader.askUser = lambda question: questions.append(question) or answer
            with everywhere(sys.modules["swarm_harness"], "adapterOf", lambda agent: agent):
                swarm.addAgent("Writer", GatedLoop("text"), "writer", "write", waitsFor=["Reader"])
                swarm.run()
            self.assertEqual(swarm.getStatuses(), result)
            self.assertIn("(yes/no)", questions[0])
            self.assertEqual(self.leaderModel.asked, [])

    # The user stopped the swarm: nobody failed, and the mission can go on later.
    def testAStoppedSwarmRemovesNobodyAndAsksNothing(self):
        gate = threading.Event()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead")
        swarm.addAgent("Worker", GatedLoop("never", gate), "worker", "work")
        swarm.addAgent("After", GatedLoop("never"), "after", "later", waitsFor=["Worker"])
        swarm.startInBackground()
        waitUntil(lambda: swarm.getStatus("Worker") == "working", "the agent to work")
        swarm.stopWork()
        gate.set()
        swarm.wait(10)
        self.assertEqual(swarm.removed, [])
        self.assertEqual(sorted(swarm.getAgents()), ["After", "Leader", "Worker"])

    def testALeaderThatFailsStaysAndLetsItsMemoryGo(self):
        leader = Failing()
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", leader, "leader", "lead")
        self.assertIsNone(swarm.run())
        self.assertEqual(swarm.getAgents(), ["Leader"])
        self.assertTrue(leader.agent.unloaded)

    # In plan mode nobody waits, so the decision comes when the agent would have started.
    def testAnAgentThatFailsItsPlanLeavesAndTheLeaderDecidesBeforeTheWork(self):
        swarm = self.team(KEEP)
        class Planner(GatedLoop):
            def makePlan(self, task):
                return "plan"
        swarm.members["Leader"]["agent"].makePlan = lambda task: "plan"
        swarm.members["Reader"]["agent"].makePlan = lambda task: (_ for _ in ()).throw(RuntimeError("no plan"))
        swarm.addAgent("Writer", Planner("text"), "writer", "write", waitsFor=["Reader"])
        swarm.members["Leader"]["agent"].notifyUser = self.said.append
        swarm.setMode("plan")
        swarm.run()
        self.assertEqual(sorted(swarm.getAgents()), ["Leader", "Writer"])
        self.assertEqual(swarm.getInfo("Writer")["waitsFor"], [])
        self.assertEqual(self.leaderModel.asked, [])
        swarm.setMode("execute")
        self.assertEqual(swarm.run(), "final")
        self.assertEqual(len(self.leaderModel.asked), 1)
        self.assertEqual(swarm.getInfo("Writer")["result"], "text")

    def testWhatAnAgentLostIsSavedWithTheSwarm(self):
        swarm = self.team()
        swarm.addAgent("Writer", GatedLoop("text"), "writer", "write", waitsFor=["Reader"])
        swarm.removeAgent("Reader", "The user does not need it.")
        lost = swarm.describeState()["members"]["Writer"]["lost"]
        self.assertEqual(lost, [{"name": "Reader", "why": "The user does not need it."}])
        back = Swarm.restore(swarm.describeState(), lambda name, data: GatedLoop("text"))
        self.assertEqual(back.members["Writer"]["lost"], lost)

    def testTheDecisionIsInTheJournalAndInTheMemoryOfTheMission(self):
        swarm = self.team(KEEP)
        swarm.addAgent("Writer", GatedLoop("text"), "writer", "write", waitsFor=["Reader"])
        swarm.run()
        lines = swarm.journal.path.read_text(encoding="utf-8")
        self.assertIn('"kind": "waiter"', lines)
        self.assertIn("It can read the sources by itself.", lines)
        self.assertIn("It can read the sources by itself.", swarm.session.memory.readRound("PROGRESS.md", 1))


# What the user who leads a swarm reads and answers: the summary of the drafts that wait.
class SummaryTests(FilesTestCase):
    def makeSwarm(self, draft):
        swarm = Swarm("Mission")
        for name in ("Leader", "Writer", "Checker"):
            swarm.addAgent(name, Loop(Model()), "role", "task")
        swarm.leaderIsUser = lambda: True
        for name in ("Writer", "Checker"):
            swarm.members[name].update(status="working", review="ready", draft=draft, revision=1)
        leader = swarm.getMember("Leader")["agent"]
        leader.said = []
        leader.notifyUser = leader.said.append
        return swarm, leader

    # In a real run the question stayed open after the user approved each agent alone, and took the next command as its answer.
    def testTheQuestionIsWithdrawnOnceTheUserDecidedEveryDraftAlone(self):
        swarm, leader = self.makeSwarm("A short draft")
        asked = threading.Event()
        def askUntil(question, stale):
            asked.set()
            waitUntil(stale, "the decisions of the user")
            return ""
        leader.askUntil = askUntil
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("reply", swarm.reviewRound({"Writer": 1, "Checker": 1}, None)))
        thread.start()
        self.assertTrue(asked.wait(5))
        swarm.approveDraft("Writer")
        self.assertTrue(thread.is_alive())
        swarm.rejectDraft("Checker")
        thread.join(5)
        self.assertEqual(result["reply"], "")
        self.assertEqual((swarm.members["Writer"]["review"], swarm.members["Checker"]["review"]), ("approved", "rejected"))
        self.assertFalse(any("changed while you were reading" in line for line in leader.said))

    # A user who leads reads the summary itself: every draft is cut, and its name shows how to read all of it.
    def testTheSummaryForAUserWhoLeadsCutsEachDraft(self):
        swarm, leader = self.makeSwarm("word " * 1000)
        leader.askUntil = lambda question, stale: "yes"
        swarm.reviewRound({"Writer": 1, "Checker": 1}, None)
        summary = next(line for line in leader.said if "Summary of the results" in line)
        self.assertIn("more characters: type Writer to read it all]", summary)
        self.assertLess(len(summary), 2000)
        self.assertEqual(swarm.members["Writer"]["review"], "approved")


if __name__ == "__main__":
    unittest.main()
