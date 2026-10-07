import contextlib
import copy
import shutil
import sys
import tempfile
import types
import unittest
from patching import everywhere
from pathlib import Path
from unittest import mock

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import agent_conversation
import agent_prompts as prompts
import harness_utils
from agent_conversation import Conversation
from agent_storehouse import WorkSession
from agent_tools import Toolbox, undoChanges
from base_loop import Loop
from checking_loops import WorkerLoop
from harness_utils import SwarmStopped
from model_clients import ApiModel, LocalModel, claudeMessages, localMessages, openAiMessages, readToolCalls
from swarm_harness import Swarm
from writing_loops import AuthorLoop


def call(name, number=1, **arguments):
    return {"id": f"call{number}", "name": name, "arguments": arguments}


def say(text):
    return {"text": text, "calls": []}


def use(*calls, text=""):
    return {"text": text, "calls": list(calls)}


# A model that holds a conversation. Each reply is a dictionary, or a function of the loop that gives one (to act while the model thinks).
class FakeChat:
    def __init__(self, replies, loop=None):
        self.replies = list(replies)
        self.seen = []
        self.loop = loop

    def converse(self, system, messages, tools):
        self.seen.append({"system": system, "messages": copy.deepcopy(messages), "tools": [tool["name"] for tool in tools]})
        reply = self.replies.pop(0)
        return reply(self.loop) if callable(reply) else reply


# A loop that only asks its model, without the user.
class ChatLoop(Loop):
    def run(self):
        return self.askAgent("Do your task.")


class ConversationTestCase(unittest.TestCase):
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
        self.asked = []

    # A loop without a person: the permissions are answered with decision, and what the loop says is kept.
    def quiet(self, loop, decision="once", answers=()):
        replies = list(answers)
        loop.said = []
        loop.notifyUser = loop.said.append
        loop.askUser = lambda question: replies.pop(0) if replies else "yes"
        loop.askPermission = lambda request: self.asked.append(request) or {"decision": decision, "message": "not now"}
        loop.askQuestions = lambda questions: self.asked.append(questions) or {"answer": ["Blue"]}
        return loop

    def chatLoop(self, replies, folder=True, decision="once", kind=ChatLoop):
        loop = self.quiet(kind(None), decision)
        loop.agent = FakeChat(replies, loop)
        if folder:
            loop.setFolder(self.work)
        return loop


class ToolboxTests(ConversationTestCase):
    def testFilesAreReadWithTheNumbersOfTheirLines(self):
        box = Toolbox(self.chatLoop([]))
        self.assertEqual(box.run("read_file", {"path": "notes/ideas.md"}), ("notes/ideas.md (3 lines):\n     1\tBees\n     2\tHoney is sweet\n     3\tWax", False))
        text, error = box.run("read_file", {"path": "notes/ideas.md", "start_line": 2, "line_count": 1})
        self.assertIn("     2\tHoney is sweet\n[Lines 2 to 2 of 3. Read on with start_line=3.]", text)
        self.assertEqual(box.run("read_file", {"path": "../secret"})[1], True)
        self.assertEqual(box.run("read_file", {})[0], "Not done: read_file needs path.")
        self.assertEqual(box.run("read_file", {"path": 3})[0], "Not done: path must be a string.")
        self.assertIn("There is no tool fly here.", box.run("fly", {})[0])

    def testEveryChangeAsksTheUserAndCanBePutBack(self):
        loop = self.chatLoop([])
        box = Toolbox(loop)
        self.assertEqual(box.run("edit_file", {"path": "notes/ideas.md", "old_text": "Wax", "new_text": "Pollen"}), ("Done: change notes/ideas.md (3 lines).", False))
        self.assertEqual(self.asked[-1]["action"], "change a file")
        self.assertIn("-Wax\n+Pollen", self.asked[-1]["detail"])
        box.run("write_file", {"path": "new/plan.md", "content": "A plan", "reason": "the user asked"})
        self.assertEqual((self.work / "new" / "plan.md").read_text(), "A plan")
        self.assertEqual(self.asked[-1]["reason"], "the user asked")
        box.run("move_path", {"source": "notes/ideas.md", "target": "notes/bees.md"})
        box.run("delete_path", {"path": "new"})
        self.assertFalse((self.work / "notes" / "ideas.md").exists())
        self.assertEqual(loop.describePending().split(":")[0], "It changed 4 paths that will be put back")
        self.assertEqual(undoChanges(loop), 4)
        self.assertEqual((self.work / "notes" / "ideas.md").read_text(), "Bees\nHoney is sweet\nWax\n")
        self.assertFalse((self.work / "notes" / "bees.md").exists())
        self.assertFalse((self.work / "new").exists())
        self.assertIn("Put back the 4 files and folders it had changed.", [action["text"] for action in loop.actions])

    def testARefusalAndAPermissionUntilTheNextRun(self):
        loop = self.chatLoop([], decision="deny")
        self.assertEqual(Toolbox(loop).run("write_file", {"path": "a.txt", "content": "x"}), ("Not done: The user refused. not now Go on without it.", True))
        self.assertFalse((self.work / "a.txt").exists())
        loop = self.chatLoop([], decision="run")
        box = Toolbox(loop)
        box.run("write_file", {"path": "a.txt", "content": "x"})
        box.run("write_file", {"path": "b.txt", "content": "y"})
        self.assertEqual(len(self.asked), 2)

    def testAnEditMustFindItsTextOnce(self):
        box = Toolbox(self.chatLoop([]))
        (self.work / "twice.txt").write_text("a a")
        self.assertIn("is not in twice.txt", box.run("edit_file", {"path": "twice.txt", "old_text": "b", "new_text": "c"})[0])
        self.assertIn("is 2 times in twice.txt", box.run("edit_file", {"path": "twice.txt", "old_text": "a", "new_text": "c"})[0])
        box.run("edit_file", {"path": "twice.txt", "old_text": "a", "new_text": "c", "replace_all": True})
        self.assertEqual((self.work / "twice.txt").read_text(), "c c")
        self.assertIn("is not a path inside your folder", box.run("write_file", {"path": "../out.txt", "content": "x"})[0])

    def testACommandRunsInTheFolderOnceTheUserAllowsIt(self):
        loop = self.chatLoop([])
        text, error = Toolbox(loop).run("run_command", {"command": f'"{sys.executable}" -c "import os; print(os.getcwd())"'})
        self.assertEqual((text, error), (f"Exit code 0.\n{self.work}", False))
        self.assertEqual(self.asked[-1]["action"], "run a command")
        self.assertIn("Ran in", loop.actions[-1]["text"])

    def testPlanModeAndAgentsWithoutAFolder(self):
        loop = self.chatLoop([])
        loop.planning = True
        self.assertNotIn("write_file", Toolbox(loop).available())
        self.assertIn("read_file", Toolbox(loop).available())
        self.assertNotIn("send_message", Toolbox(loop).available())
        loop = self.chatLoop([], folder=False)
        loop.name = "Scratch"
        self.assertEqual(Toolbox(loop).run("write_file", {"path": "draft.txt", "content": "x"})[1], False)
        self.assertEqual(self.asked, [])
        self.assertTrue((self.files / "agent-files" / "agent-workspaces" / "Scratch" / "draft.txt").exists())

    def testTheUserCanBeAsked(self):
        self.assertEqual(Toolbox(self.chatLoop([])).run("ask_user", {"question": "Which color?", "options": ["Blue", "Red"]}), ("The user answered: Blue", False))
        self.assertEqual(self.asked[-1][0]["options"], [{"label": "Blue"}, {"label": "Red"}])


class ConversationTests(ConversationTestCase):
    def testTheModelReadsItsFilesThenAnswersAndKeepsTheConversation(self):
        loop = self.quiet(AuthorLoop(None, "bees", 50), answers=["shorter", "yes"])
        loop.agent = FakeChat([use(call("read_file", path="notes/ideas.md")), say("A long text about honey"), say("Honey.")], loop)
        loop.setFolder(self.work)
        self.assertEqual(loop.run(), "Honey.")
        first, second, third = loop.agent.seen
        self.assertIn("- notes/ideas.md (24 bytes)", first["system"])
        self.assertIn("THE RULES OF YOUR TASK", first["system"])
        self.assertIn("write_file", first["tools"])
        self.assertEqual(second["messages"][-1], {"role": "tool", "id": "call1", "name": "read_file", "content": "notes/ideas.md (3 lines):\n     1\tBees\n     2\tHoney is sweet\n     3\tWax", "error": False})
        self.assertEqual(third["messages"][-1], {"role": "user", "content": prompts.CONVERSATION_REVISION_PROMPT.format(feedback="shorter")})
        self.assertEqual(len(third["messages"]), 5)
        self.assertEqual(third["system"], first["system"])
        self.assertIn('[AuthorLoop] read_file("path": "notes/ideas.md")', loop.said)

    def testMessagesArriveBetweenTwoStepsOfTheWork(self):
        def working(loop):
            loop.receive("Writer", "Use the word nectar.")
            loop.receiveFromUser("Be brief.")
            return use(call("list_files"))
        loop = self.chatLoop([working, say("Done")])
        loop.receive("Leader", "Welcome.")
        self.assertEqual(loop.askAgent("Work."), "Done")
        first, second = loop.agent.seen
        self.assertEqual(first["messages"][0]["content"], "[Message from Leader] Welcome.\n\nWork.")
        self.assertEqual(second["messages"][-1], {"role": "user", "content": "[Message from Writer] Use the word nectar.\n[Message from the user] Be brief."})

    def testACutCallIsNotRunAndTheLastStepIsSaid(self):
        loop = self.chatLoop([{"text": "", "calls": [call("write_file", path="a.txt", content="x")], "cut": True}, say("Smaller")])
        self.assertEqual(loop.askAgent("Work."), "Smaller")
        self.assertEqual(loop.agent.seen[1]["messages"][-1]["content"], f"Not done: {prompts.CUT_CALL}")
        self.assertFalse((self.work / "a.txt").exists())
        loop = self.chatLoop([use(call("list_files"))] * 3)
        with mock.patch.object(agent_conversation, "MAX_STEPS", 3):
            self.assertEqual(loop.askAgent("Work."), "")
        self.assertEqual(loop.agent.seen[2]["messages"][-1]["content"], prompts.LAST_STEP)

    def testAStoppedSwarmStopsTheConversation(self):
        loop = self.chatLoop([use(call("list_files"))])
        loop.team = types.SimpleNamespace(shouldStop=lambda name: True)
        with self.assertRaises(SwarmStopped):
            loop.askAgent("Work.")

    def testTheLeaderThatBuildsOnlyLooksAndChecksGoWithoutTools(self):
        loop = self.chatLoop([say("A swarm")])
        loop.agent.input = lambda prompt: "OK"
        self.assertEqual(loop.askAgent("Build.", own=False, tools=True), "A swarm")
        self.assertIn(prompts.LOOK_ONLY_PROMPT, loop.agent.seen[0]["system"])
        self.assertNotIn("write_file", loop.agent.seen[0]["tools"])
        self.assertIsNone(loop.conversation)
        self.assertEqual(loop.checkRules("draft") if loop.rules else loop.askAgent("Check", tools=False), "OK")

    def testAConversationGoesOnAfterTheProgramStopped(self):
        loop = self.chatLoop([use(call("list_files"), call("find_files", 2, pattern="*.md"))])
        loop.name, loop.session = "Writer", WorkSession("swarm1", "run")
        conversation = Conversation(loop)
        conversation.system = "system"
        conversation.messages = [{"role": "user", "content": "Work."}, {"role": "assistant", "content": "", "calls": [call("list_files")], "raw": None}]
        conversation.save()
        loop.resumed = True
        loaded = Conversation.load(loop)
        self.assertEqual(loaded.messages[-1], {"role": "tool", "id": "call1", "name": "list_files", "content": "Not done: the program stopped before it ran.", "error": True})

    def testALongConversationDropsOldResultsExceptForClaude(self):
        loop = self.chatLoop([])
        conversation = Conversation(loop)
        conversation.messages = [{"role": "tool", "id": str(number), "name": "read_file", "content": "x" * 100} for number in range(20)]
        with mock.patch.object(agent_conversation, "CONTEXT_CHARS", 1000):
            loop.agent.appendOnly = True
            conversation.shorten()
            self.assertEqual(conversation.messages[0]["content"], "x" * 100)
            loop.agent.appendOnly = False
            conversation.shorten()
        self.assertEqual([message["content"] == prompts.OLD_RESULT for message in conversation.messages], [True] * 8 + [False] * 12)

    def testTheWorkerDoesTheWorkAndItIsPutBackIfNotApproved(self):
        replies = [use(call("write_file", path="sorted/bees.md", content="Bees")), say("I created sorted/bees.md.")]
        loop = self.chatLoop(replies, kind=lambda agent: WorkerLoop(agent, "sort my notes"))
        self.assertEqual(loop.run(), "I created sorted/bees.md.")
        self.assertTrue((self.work / "sorted" / "bees.md").exists())
        shutil.rmtree(self.work / "sorted")
        loop = self.chatLoop(list(replies), kind=lambda agent: WorkerLoop(agent, "sort my notes"))
        loop.askUser = lambda question: "no"
        self.assertIsNone(loop.run())
        self.assertFalse((self.work / "sorted").exists())
        self.assertIn("The work was not approved, so every file it changed is back to how it was.", loop.said)


class SwarmTests(ConversationTestCase):
    def testAgentsWriteToEachOtherAndToTheLeaderAndReadResults(self):
        swarm = Swarm("Bees")
        events = []
        swarm.addListener(events.append)
        leader = self.chatLoop([use(call("read_result", agent="writer")), say("Report")])
        writer = self.chatLoop([use(call("send_message", to="leader", text="The text is short.")), say("A text")])
        checker = self.chatLoop([use(call("team_status"), call("send_message", 2, to="Nobody", text="hi")), say("Checked")])
        swarm.addAgent("Leader", leader, "boss", "lead")
        swarm.addAgent("Writer", writer, "writer", "write")
        swarm.addAgent("Checker", checker, "checker", "check", waitsFor=["Writer"])
        self.assertEqual(swarm.run(), "Report")
        self.assertIn({"role": "tool", "id": "call1", "name": "read_result", "content": "The result of Writer:\nA text", "error": False}, leader.agent.seen[1]["messages"])
        self.assertIn("[Message from Writer] The text is short.", leader.agent.seen[0]["messages"][0]["content"])
        status = checker.agent.seen[1]["messages"][-2]["content"]
        self.assertIn("- Writer: the writer, done. Task: write Its result is ready (read_result).", status)
        self.assertIn("There is no agent called Nobody.", checker.agent.seen[1]["messages"][-1]["content"])
        self.assertIn({"kind": "message", "sender": "Writer", "receiver": "Leader", "direct": True}, [{key: event.get(key) for key in ("kind", "sender", "receiver", "direct")} for event in events])
        self.assertTrue(any(event["kind"] == "activity" and event["agent"] == "Checker" for event in events))
        self.assertEqual(swarm.getInfo("Writer")["activity"][0]["text"], 'send_message("to": "leader", "text": "The text is short.")')

    def testWhatAnAgentThatDidNotFinishChangedIsPutBack(self):
        swarm = Swarm("Bees")
        leader = self.chatLoop([say("Report")])
        worker = self.chatLoop([use(call("write_file", path="half.txt", content="half")), say("")], kind=lambda agent: WorkerLoop(agent, "half"))
        worker.askUser = lambda question: "no"
        swarm.addAgent("Leader", leader, "boss", "lead")
        swarm.addAgent("Worker", worker, "worker", "work")
        swarm.reviewer = None
        with mock.patch.object(WorkerLoop, "run", lambda self: self.askAgent("Work.") or None):
            swarm.run()
        self.assertEqual(swarm.getStatus("Worker"), "failed")
        self.assertFalse((self.work / "half.txt").exists())


class FakeBlock(types.SimpleNamespace):
    def model_dump(self, **options):
        return dict(vars(self))


class ClientTests(unittest.TestCase):
    def testClaudeGetsToolResultsAndNewsInOneMessage(self):
        messages = [{"role": "user", "content": "Work."}, {"role": "assistant", "content": "Let me look.", "calls": [call("list_files")], "raw": None},
                    {"role": "tool", "id": "call1", "name": "list_files", "content": "- a.md", "error": True}, {"role": "user", "content": "[Message from the user] Hi"},
                    {"role": "assistant", "content": "", "calls": [], "raw": {"claude": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "Done"}]}}]
        self.assertEqual(claudeMessages(messages), [
            {"role": "user", "content": [{"type": "text", "text": "Work."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "call1", "name": "list_files", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "- a.md", "is_error": True}, {"type": "text", "text": "[Message from the user] Hi"}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "Done"}]}])

    def testOpenAiAndLocalModelsGetTheirOwnForm(self):
        messages = [{"role": "user", "content": "Work."}, {"role": "assistant", "content": "", "calls": [call("read_file", path="a.md")], "raw": None},
                    {"role": "tool", "id": "call1", "name": "read_file", "content": "text"}, {"role": "user", "content": "News"}]
        self.assertEqual(openAiMessages("S", messages)[1:3], [{"role": "user", "content": "Work."}, {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.md"}'}}]}])
        self.assertEqual(openAiMessages("S", messages)[3], {"role": "tool", "tool_call_id": "call1", "content": "text"})
        native = localMessages("S", messages, [], True)
        self.assertEqual(native[2]["tool_calls"][0]["function"], {"name": "read_file", "arguments": {"path": "a.md"}})
        self.assertEqual(native[3], {"role": "tool", "name": "read_file", "tool_call_id": "call1", "content": "text"})
        written = localMessages("S", messages, [{"name": "read_file"}], False)
        self.assertIn("YOUR TOOLS", written[0]["content"])
        self.assertEqual(written[2]["content"], '<tool_call>{"name": "read_file", "arguments": {"path": "a.md"}}</tool_call>')
        self.assertEqual(written[3], {"role": "user", "content": "[Result of read_file]\ntext\n\nNews"})

    def testTheCallsALocalModelWritesAreRead(self):
        calls, text = readToolCalls('I look.\n<tool_call>\n{"name": "list_files", "arguments": {}}\n</tool_call>')
        self.assertEqual(([(item["name"], item["arguments"]) for item in calls], text), ([("list_files", {})], "I look."))
        calls, text = readToolCalls('[{"name": "read_file", "parameters": "{\\"path\\": \\"a\\"}"}]')
        self.assertEqual(([(item["name"], item["arguments"]) for item in calls], text), ([("read_file", {"path": "a"})], ""))
        self.assertEqual(readToolCalls('<tool_call>{"name": oops</tool_call>')[0][0]["problem"], "this tool call is not valid JSON. Write it again.")
        self.assertEqual(readToolCalls('{"answer": 42}'), ([], '{"answer": 42}'))
        self.assertEqual(readToolCalls(""), ([], ""))

    def testClaudeIsAskedWithItsToolsAndItsCache(self):
        message = types.SimpleNamespace(content=[FakeBlock(type="text", text="I read."), FakeBlock(type="tool_use", id="t1", name="read_file", input={"path": "a"})],
                                        stop_reason="tool_use", stop_details=None, usage=types.SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=3, cache_creation_input_tokens=0))
        asked = []
        class Stream:
            def __enter__(self):
                if len(asked) == 1:
                    raise ValueError("bad JSON")
                return types.SimpleNamespace(get_final_message=lambda: message)
            def __exit__(self, *problem):
                return False
        model = ApiModel("claude", "claude-opus-5-5", "key")
        model.client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **options: asked.append(options) or Stream()))
        model.connect = lambda: types.SimpleNamespace(APIError=RuntimeError)
        reply = model.converse("System", [{"role": "user", "content": "Work."}], [{"name": "read_file", "description": "Read", "parameters": {"type": "object"}}])
        self.assertEqual(reply, {"text": "I read.", "calls": [{"id": "t1", "name": "read_file", "arguments": {"path": "a"}}],
                                 "raw": {"claude": [{"type": "text", "text": "I read."}, {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}}]}, "cut": False})
        self.assertEqual(len(asked), 2)
        self.assertEqual(asked[1]["tools"], [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}, "eager_input_streaming": True}])
        self.assertEqual((asked[1]["cache_control"], asked[1]["system"][0]["cache_control"]), ({"type": "ephemeral"}, {"type": "ephemeral"}))
        self.assertEqual(model.usage["calls"], 1)
        self.assertTrue(model.appendOnly)

    def testOpenAiCallsAreReadAndGivenBackAsTheyCame(self):
        item = FakeBlock(id="c1", type="function", function=types.SimpleNamespace(name="read_file", arguments='{"path": "a"}'), extra_content={"google": {"thought_signature": "x"}})
        broken = FakeBlock(id="c2", type="function", function=types.SimpleNamespace(name="list_files", arguments="{oops"))
        response = types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(finish_reason="tool_calls", message=types.SimpleNamespace(
            content=None, refusal=None, tool_calls=[item, broken], reasoning_content="thinking"))])
        asked = []
        model = ApiModel("gemini", "gemini-3-pro", "key")
        model.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **options: asked.append(options) or response)))
        model.connect = lambda: types.SimpleNamespace(APIError=RuntimeError)
        reply = model.converse("System", [{"role": "user", "content": "Work."}], [{"name": "read_file", "description": "Read", "parameters": {"type": "object"}}])
        self.assertEqual(reply["calls"][0], {"id": "c1", "name": "read_file", "arguments": {"path": "a"}})
        self.assertEqual(reply["calls"][1]["problem"], "its arguments were not valid JSON. Write them again.")
        self.assertEqual(reply["raw"]["openai"]["tool_calls"][0]["extra_content"], {"google": {"thought_signature": "x"}})
        self.assertEqual(reply["raw"]["openai"]["reasoning_content"], "thinking")
        self.assertEqual(asked[0]["tools"][0], {"type": "function", "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}}})
        self.assertFalse(model.appendOnly)

    def testALocalModelUsesTheToolsOfItsTemplateOrWritesThem(self):
        class Inputs(dict):
            def to(self, device):
                return self
        class Output:
            shape = (1, 7)
            def __getitem__(self, index):
                return self
        asked = []
        model = LocalModel("Qwen/Qwen3-8B")
        model.model = types.SimpleNamespace(device="cpu", generate=lambda **options: Output())
        model.torch = types.SimpleNamespace(no_grad=contextlib.nullcontext)
        model.tokenizer = types.SimpleNamespace(chat_template="{% if tools %}...{% endif %}", decode=lambda tokens, skip_special_tokens: '<tool_call>{"name": "list_files", "arguments": {}}</tool_call>',
                                                apply_chat_template=lambda messages, **options: asked.append((messages, options)) or Inputs(input_ids=types.SimpleNamespace(shape=(1, 4))))
        tools = [{"name": "list_files", "description": "List", "parameters": {"type": "object"}}]
        reply = model.converse("System", [{"role": "user", "content": "Work."}], tools)
        self.assertEqual([item["name"] for item in reply["calls"]], ["list_files"])
        self.assertEqual(asked[0][1]["tools"], [{"type": "function", "function": tools[0]}])
        model.tokenizer.chat_template = "{{ messages }}"
        model.converse("System", [{"role": "user", "content": "Work."}], tools)
        self.assertIsNone(asked[1][1]["tools"])
        self.assertIn("YOUR TOOLS", asked[1][0][0]["content"])
        self.assertEqual((model.usage["calls"], model.usage["output"]), (2, 6))


if __name__ == "__main__":
    unittest.main()
