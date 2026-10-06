import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import harness_utils
import model_clients
from harness_utils import Loop
from model_clients import ClaudeCodeModel, CodexModel, ModelError, checkCodex, createModel, describeClaudeCodeRequest, describeRunRules, findMissingPackages, isSafeRead, unwrapCommand
from models_library import DEFAULT_CLI_MODEL, getModelInfo

HAS_CLAUDE_SDK = model_clients.importlib.util.find_spec("claude_agent_sdk") is not None
CODEX = checkCodex()


# A loop that answers the questions of a coding agent from a script, and keeps what it was asked.
class ScriptedLoop(Loop):
    def __init__(self, folder, decisions=(), answers=None):
        super().__init__(None)
        self.folder, self.name = Path(folder), "Tester"
        self.decisions, self.answers, self.asked, self.questions, self.said = list(decisions), answers or {}, [], [], []

    def askPermission(self, request):
        self.asked.append(request)
        decision = self.decisions.pop(0) if self.decisions else "deny"
        return {"decision": decision, "message": "not now" if decision == "deny" else ""}

    def askQuestions(self, questions):
        self.questions.append(questions)
        return {question["id"]: self.answers.get(question["id"], []) for question in questions}

    def notifyUser(self, message):
        self.said.append(message)


# A model on this computer that speaks like the real one. Each step is ("tool", name, input) or ("text", reply): the next step is given each
# time the agent sends a request. What the agent sends back after a tool (its result) is kept in results.
class FakeModelServer:
    def __init__(self, handler, steps, status=200):
        self.steps, self.status, self.results, self.requests = list(steps), status, [], 0
        owner = self
        class Handler(handler):
            server_owner = owner
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def nextStep(self):
        self.requests += 1
        return self.steps.pop(0) if self.steps else ("text", "Finished.")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def reply(self, status, data, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(404, b"{}", "application/json")


def events(pairs):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in pairs).encode()


class AnthropicHandler(QuietHandler):
    def do_POST(self):
        owner, body = self.server_owner, self.body()
        if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
            return self.reply(404, b"{}", "application/json")
        for message in body.get("messages", []):
            for part in message.get("content") if isinstance(message.get("content"), list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("tool_use_id") == f"toolu_{owner.requests}":
                    content = part.get("content")
                    owner.results.append(content if isinstance(content, str) else " ".join(item.get("text", "") for item in content or [] if isinstance(item, dict)))
        if owner.status != 200:
            return self.reply(owner.status, json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode(), "application/json")
        # Claude Code also makes small requests of its own, without tools: they get a plain answer and do not use the script.
        step = owner.nextStep() if body.get("tools") else ("text", "OK")
        start = {"id": f"msg_{owner.requests}", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                 "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}
        if step[0] == "tool":
            block, delta, stop = {"type": "tool_use", "id": f"toolu_{owner.requests}", "name": step[1], "input": {}}, {"type": "input_json_delta", "partial_json": json.dumps(step[2])}, "tool_use"
        else:
            block, delta, stop = {"type": "text", "text": ""}, {"type": "text_delta", "text": step[1]}, "end_turn"
        self.reply(200, events([("message_start", {"type": "message_start", "message": start}), ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block}),
                                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}), ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 12}}),
                                ("message_stop", {"type": "message_stop"})]), "text/event-stream")


class ResponsesHandler(QuietHandler):
    def do_POST(self):
        owner, body = self.server_owner, self.body()
        outputs = [item for item in body.get("input", []) if isinstance(item, dict) and item.get("type") == "function_call_output"]
        if outputs:
            owner.results.append(str(outputs[-1].get("output")))
        step = owner.nextStep()
        if step[0] == "tool":
            item = {"type": "function_call", "id": f"fc_{owner.requests}", "call_id": f"call_{owner.requests}", "name": step[1], "arguments": json.dumps(step[2])}
        else:
            item = {"type": "message", "id": f"msg_{owner.requests}", "role": "assistant", "content": [{"type": "output_text", "text": step[1], "annotations": []}]}
        response = {"id": f"resp_{owner.requests}", "object": "response", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
        self.reply(200, "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}, {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item}, {"type": "response.completed", "response": response}]).encode(), "text/event-stream")


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.folder = self.root / "work"
        self.folder.mkdir()
        (self.folder / "notes.txt").write_text("bees", encoding="utf-8")
        patcher = mock.patch.object(harness_utils, "AGENT_FILES", self.root / "agent-files")
        patcher.start()
        self.addCleanup(patcher.stop)


class ModelInfoTests(unittest.TestCase):
    def testACodingAgentIsAThirdKindOfModel(self):
        claude = getModelInfo("", cli="claude-code")
        self.assertEqual((claude["name"], claude["local"], claude["provider"], claude["vram"], claude["cli"]), (DEFAULT_CLI_MODEL, False, "claude", 0.0, "claude-code"))
        self.assertEqual(getModelInfo("gpt-6-luna", cli="codex")["provider"], None)
        self.assertIsNone(getModelInfo("claude-haiku-4-5")["cli"])
        with self.assertRaises(ValueError):
            getModelInfo("x", cli="other")

    def testClaudeCodeNeedsItsLibraryAndAKey(self):
        with mock.patch.object(model_clients.importlib.util, "find_spec", return_value=None):
            self.assertEqual(findMissingPackages(getModelInfo("", cli="claude-code")), ["claude-agent-sdk"])
        self.assertEqual(findMissingPackages(getModelInfo("", cli="codex")), [])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), self.assertRaises(ModelError):
            createModel(getModelInfo("", cli="claude-code"))
        self.assertIsInstance(createModel(getModelInfo("", cli="claude-code"), {"claude": "sk-ant-x"}), ClaudeCodeModel)
        self.assertIsInstance(createModel(getModelInfo("", cli="codex")), CodexModel)


class SafeReadTests(FolderTestCase):
    def request(self, command, actions=("read",), cwd=None):
        return {"command": f"/bin/bash -lc {json.dumps(command)}" if command else "", "cwd": str(cwd or self.folder),
                "commandActions": [{"type": kind} for kind in actions]}

    @unittest.skipIf(os.name == "nt", "every command is asked on Windows")
    def testOnlyPlainReadsInsideTheFolderRunWithoutAsking(self):
        (self.folder / "link").symlink_to("/etc")
        allowed = ["cat notes.txt", f"cat {self.folder}/notes.txt", "ls", "ls -la sub", "grep -n bees notes.txt", "rg bees", "head -n 3 notes.txt", "wc -l notes.txt"]
        asked = ["cat /etc/hostname", "cat ../outside.txt", "cat link/hostname", "cat notes.txt; rm notes.txt", "cat notes.txt | sh", "cat $HOME/x", "cat ~/x",
                 "rg --pre=sh bees", "tail -f notes.txt", "sed -n 1p notes.txt", "find . -delete", "python -c 1", "cat *", "echo `id`"]
        for command in allowed:
            self.assertTrue(isSafeRead(self.request(command), self.folder), command)
        for command in asked:
            self.assertFalse(isSafeRead(self.request(command), self.folder), command)
        self.assertFalse(isSafeRead(self.request("cat notes.txt", actions=("unknown",)), self.folder))
        self.assertFalse(isSafeRead(self.request("cat notes.txt", actions=()), self.folder))
        self.assertFalse(isSafeRead(self.request("cat notes.txt", cwd=self.root), self.folder))

    def testTheShellAroundACommandIsRemovedForTheUser(self):
        self.assertEqual(unwrapCommand("/bin/bash -lc 'npm test'"), "npm test")
        self.assertEqual(unwrapCommand("npm test"), "npm test")


class CodexVersionTests(unittest.TestCase):
    def fakeCodex(self, version):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "codex"
        path.write_text(f"#!/bin/sh\necho 'codex-cli {version}'\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    @unittest.skipIf(os.name == "nt", "the fake program is a shell script")
    def testOnlyTheTestedVersionsOfCodexAreUsed(self):
        tested = ".".join(str(part) for part in model_clients.CODEX_TESTED)
        with mock.patch.dict(os.environ, {"SWARMUP_CODEX": self.fakeCodex(f"{tested}.7"), "SWARMUP_ALLOW_UNTESTED_CODEX": ""}):
            self.assertEqual(checkCodex()["problem"], "")
        with mock.patch.dict(os.environ, {"SWARMUP_CODEX": self.fakeCodex("0.1.0"), "SWARMUP_ALLOW_UNTESTED_CODEX": ""}):
            self.assertIn(f"npm install -g @openai/codex@{tested}", checkCodex()["problem"])
        with mock.patch.dict(os.environ, {"SWARMUP_CODEX": self.fakeCodex("0.1.0"), "SWARMUP_ALLOW_UNTESTED_CODEX": "1"}):
            self.assertEqual(checkCodex()["problem"], "")
        with mock.patch.dict(os.environ, {"SWARMUP_CODEX": ""}), mock.patch.object(model_clients.shutil, "which", return_value=None):
            self.assertIn("npm install -g @openai/codex", checkCodex()["problem"])


class PermissionWordsTests(FolderTestCase):
    def testTheRequestsOfClaudeCodeAreWrittenForTheUser(self):
        request = describeClaudeCodeRequest("Bash", {"command": "npm test", "description": "Run the tests"}, self.folder)
        self.assertEqual((request["action"], request["detail"], request["reason"]), ("run a command", "npm test", "Run the tests"))
        self.assertEqual(describeClaudeCodeRequest("WebSearch", {"query": "bees"}, self.folder)["action"], "search the web")
        self.assertEqual(describeClaudeCodeRequest("Mystery", {"x": 1}, self.folder)["action"], "use its tool Mystery")

    def testWhatIsAllowedUntilTheNextRunFollowsTheSuggestionsOfClaudeCode(self):
        suggestion = SimpleNamespace(type="addRules", behavior="allow", rules=[SimpleNamespace(tool_name="Bash", rule_content="npm test *")])
        self.assertEqual(describeRunRules("Bash", {"command": "npm test"}, [suggestion]), ["Bash(npm test *)"])
        self.assertEqual(describeRunRules("Bash", {"command": "ls -la"}, []), ["Bash(ls -la)"])
        self.assertEqual(describeRunRules("WebFetch", {"url": "x"}, []), ["WebFetch"])

    def testTheConsoleAsksForPermissionsAndQuestions(self):
        replies = iter(["yes please", "always", "no not that", "use git instead", "2", "my own words"])
        loop = Loop(None)
        loop.askUser = lambda question: next(replies)
        loop.notifyUser = lambda message: None
        request = {"action": "run a command", "detail": "npm test", "folder": str(self.folder), "reason": ""}
        self.assertEqual([loop.askPermission(request)["decision"] for attempt in range(4)], ["once", "run", "deny", "deny"])
        questions = [{"id": "q1", "question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}, {"id": "q2", "question": "Why?", "options": []}]
        self.assertEqual(loop.askQuestions(questions), {"q1": ["B"], "q2": ["my own words"]})

    def testWithoutALoopNothingIsAllowed(self):
        self.assertEqual(model_clients.askPermission(None, {"action": "x"})["decision"], "deny")
        loop = Loop(None)
        loop.askPermission = lambda request: ""
        self.assertEqual(model_clients.askPermission(loop, {"action": "x"})["decision"], "deny")


@unittest.skipUnless(HAS_CLAUDE_SDK, "claude-agent-sdk is not installed")
class ClaudeCodeTests(FolderTestCase):
    def setUp(self):
        super().setUp()
        self.servers = []
        for name in ("ANTHROPIC_BASE_URL",):
            patcher = mock.patch.dict(os.environ, {name: ""})
            patcher.start()
            self.addCleanup(patcher.stop)

    def run(self, result=None):
        return super().run(result)

    def agent(self, steps, decisions=(), answers=None, status=200):
        server = FakeModelServer(AnthropicHandler, steps, status)
        self.addCleanup(server.close)
        os.environ["ANTHROPIC_BASE_URL"] = server.url
        model = ClaudeCodeModel(DEFAULT_CLI_MODEL, "sk-ant-test")
        loop = ScriptedLoop(self.folder, decisions, answers)
        model.attach(loop)
        return model, loop, server

    def testACommandRunsOnlyAfterTheUserAllowsIt(self):
        model, loop, server = self.agent([("tool", "Bash", {"command": "echo hi > made.txt", "description": "Make a file"}), ("text", "Done.")], ["once"])
        self.assertEqual(model.input("Make a file."), "Done.")
        self.assertTrue((self.folder / "made.txt").exists())
        self.assertEqual([(request["action"], request["detail"], request["reason"]) for request in loop.asked], [("run a command", "echo hi > made.txt", "Make a file")])
        model, loop, server = self.agent([("tool", "Bash", {"command": "echo hi > other.txt"}), ("text", "Done.")], ["deny"])
        model.input("Make a file.")
        self.assertFalse((self.folder / "other.txt").exists())
        self.assertIn("The user refused this. not now", server.results)

    def testReadingInsideTheFolderNeedsNoPermissionButReadingOutsideDoes(self):
        model, loop, server = self.agent([("tool", "Read", {"file_path": str(self.folder / "notes.txt")}), ("tool", "Read", {"file_path": str(self.root / "secret.txt")}),
                                          ("text", "Read.")], ["deny"])
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        model.input("Read.")
        self.assertEqual([request["action"] for request in loop.asked], ["read a file outside its folder"])
        self.assertIn("bees", server.results[0])

    def testWhatTheUserAllowsUntilTheNextRunIsNotAskedAgain(self):
        model, loop, server = self.agent([("tool", "Bash", {"command": "echo a > a.txt"}), ("text", "One.")], ["run"])
        model.input("First.")
        server.steps = [("tool", "Bash", {"command": "echo a > a.txt"}), ("text", "Two.")]
        self.assertEqual(model.input("Second."), "Two.")
        self.assertEqual(len(loop.asked), 1)
        self.assertFalse((self.folder / ".claude").exists())
        model.newRun()
        server.steps = [("tool", "Bash", {"command": "echo a > a.txt"}), ("text", "Three.")]
        model.input("Third.")
        self.assertEqual(len(loop.asked), 2)

    def testTheQuestionsOfClaudeGoToTheUser(self):
        question = {"question": "Which tone?", "header": "Tone", "options": [{"label": "Formal", "description": ""}, {"label": "Warm", "description": ""}], "multiSelect": False}
        model, loop, server = self.agent([("tool", "AskUserQuestion", {"questions": [question]}), ("text", "Warm it is.")], answers={"Which tone?": ["Warm"]})
        self.assertEqual(model.input("Write."), "Warm it is.")
        self.assertEqual(loop.questions[0][0]["options"][1]["label"], "Warm")
        self.assertIn("Warm", server.results[0])

    def testARefusedKeyStopsAtOnce(self):
        model, loop, server = self.agent([], status=401)
        started = time.monotonic()
        with self.assertRaises(ModelError) as caught:
            model.input("Hello.")
        self.assertIn("refused the API key", str(caught.exception))
        self.assertLess(time.monotonic() - started, 30)

    def testASubscriptionLoginOfTheEnvironmentIsNeverUsed(self):
        model, loop, server = self.agent([("text", "Hello.")])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-not-real", "ANTHROPIC_AUTH_TOKEN": "not-real"}):
            self.assertEqual(model.input("Hello."), "Hello.")
        self.assertEqual(model.usage["calls"], 1)


@unittest.skipIf(CODEX["problem"], f"Codex cannot be used here: {CODEX['problem']}")
class CodexTests(FolderTestCase):
    def agent(self, steps, decisions=(), answers=None):
        server = FakeModelServer(ResponsesHandler, steps)
        self.addCleanup(server.close)
        home = self.root / "codex-home"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text(f'model_provider = "fake"\nmodel = "fake-model"\n[model_providers.fake]\nname = "fake"\nbase_url = "{server.url}/v1"\n'
                                          'wire_api = "responses"\nenv_key = "FAKE_KEY"\n', encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(home), "FAKE_KEY": "x"})
        patcher.start()
        self.addCleanup(patcher.stop)
        model = CodexModel(DEFAULT_CLI_MODEL)
        self.addCleanup(model.close)
        loop = ScriptedLoop(self.folder, decisions, answers)
        model.attach(loop)
        return model, loop, server

    def testAReadInsideTheFolderRunsAndACommandWaitsForTheUser(self):
        model, loop, server = self.agent([("tool", "exec_command", {"cmd": "cat notes.txt"}), ("tool", "exec_command", {"cmd": "touch made.txt"}), ("text", "Done.")], ["once"])
        self.assertEqual(model.input("Work."), "Done.")
        self.assertIn("bees", server.results[0])
        self.assertEqual([(request["action"], request["detail"]) for request in loop.asked], [("run a command", "touch made.txt")])
        self.assertTrue((self.folder / "made.txt").exists())

    def testARefusedCommandDoesNotRun(self):
        model, loop, server = self.agent([("tool", "exec_command", {"cmd": "touch made.txt"}), ("text", "Done.")], ["deny"])
        model.input("Work.")
        self.assertFalse((self.folder / "made.txt").exists())
        self.assertIn("rejected by user", server.results[0])

    def testWhatTheUserAllowsUntilTheNextRunIsNotAskedAgain(self):
        model, loop, server = self.agent([("tool", "exec_command", {"cmd": "touch made.txt"}), ("text", "One.")], ["run"])
        model.input("First.")
        server.steps = [("tool", "exec_command", {"cmd": "touch made.txt"}), ("text", "Two.")]
        self.assertEqual(model.input("Second."), "Two.")
        self.assertEqual(len(loop.asked), 1)
        model.newRun()
        server.steps = [("tool", "exec_command", {"cmd": "touch made.txt"}), ("text", "Three.")]
        model.input("Third.")
        self.assertEqual(len(loop.asked), 2)

    def testWithoutAFolderTheAgentWorksInAnEmptyOneOfItsOwn(self):
        model, loop, server = self.agent([("tool", "exec_command", {"cmd": "touch made.txt"}), ("text", "Done.")], ["once"])
        loop.folder = None
        model.input("Work.")
        self.assertTrue((harness_utils.AGENT_FILES / model_clients.WORKSPACES_FOLDER / "Tester" / "made.txt").exists())
        self.assertFalse((self.folder / "made.txt").exists())


if __name__ == "__main__":
    unittest.main()
