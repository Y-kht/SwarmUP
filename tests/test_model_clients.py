import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import model_clients
from harness_utils import ConnectionLost
from model_clients import (ApiModel, LocalModel, ModelConnectionError, ModelError, createModel, findMissingPackages, getApiKey, getHubFolder, isDownloaded,
                           lookupHuggingFace)
from models_library import getModelInfo

HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None
HAS_OPENAI = importlib.util.find_spec("openai") is not None


def sse(*events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def claudeStream(text="The answer.", stopReason="end_turn", stopDetails=None):
    delta = {"stop_reason": stopReason, "stop_sequence": None}
    if stopDetails:
        delta["stop_details"] = stopDetails
    start = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 12, "output_tokens": 1}}
    blocks = [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "secret thoughts"}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
              {"type": "content_block_stop", "index": 0}]
    if text:
        blocks += [{"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text[:4]}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text[4:]}},
                   {"type": "content_block_stop", "index": 1}]
    return sse({"type": "message_start", "message": start}, *blocks, {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 5}}, {"type": "message_stop"})


# A small fake API. do_POST and log_message are the names the standard library requires.
class FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    def reply(self, code, body, kind="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"path": self.path, "headers": {key.lower(): value for key, value in self.headers.items()}, "body": body})
        mode = self.server.mode
        if mode in ("401", "403", "404", "429", "500", "400"):
            error = {"401": "authentication_error", "403": "permission_error", "404": "not_found_error", "429": "rate_limit_error", "500": "api_error",
                     "400": "invalid_request_error"}[mode]
            return self.reply(int(mode), json.dumps({"type": "error", "error": {"type": error, "message": f"fake {mode}", "code": error}}).encode())
        if self.path.endswith("/messages"):
            if mode == "refusal":
                return self.reply(200, claudeStream("", "refusal", {"type": "refusal", "category": "cyber", "explanation": "no"}), "text/event-stream")
            return self.reply(200, claudeStream(), "text/event-stream")
        message = {"role": "assistant", "content": "Chat answer.", "refusal": None}
        if mode == "refusal":
            message = {"role": "assistant", "content": None, "refusal": "I cannot help with that."}
        if mode == "empty":
            message = {"role": "assistant", "content": None, "refusal": None}
        self.reply(200, json.dumps({"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}).encode())


class ProviderTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server = HTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.server.requests = []
        cls.server.mode = "ok"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.requests.clear()
        self.server.mode = "ok"
        urls = {"claude": self.url, "gpt": f"{self.url}/openai", "gemini": f"{self.url}/gemini", "deepseek": f"{self.url}/deepseek"}
        for patcher in (mock.patch.dict(model_clients.BASE_URLS, urls), mock.patch.object(model_clients, "API_RETRIES", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)


@unittest.skipUnless(HAS_ANTHROPIC, "the anthropic package is not installed")
class ClaudeTests(ProviderTestCase):
    def testTheAnswerIsTheTextWithoutTheThinkingAndTheRequestIsRight(self):
        model = ApiModel("claude", "claude-opus-5-5", "key-123")
        self.assertEqual(model.input("Say hi"), "The answer.")
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/messages")
        self.assertEqual(request["headers"]["x-api-key"], "key-123")
        self.assertIn("anthropic-version", request["headers"])
        self.assertEqual(request["body"]["model"], "claude-opus-5-5")
        self.assertEqual(request["body"]["max_tokens"], model_clients.CLAUDE_MAX_TOKENS)
        self.assertEqual(request["body"]["messages"], [{"role": "user", "content": "Say hi"}])
        self.assertTrue(request["body"]["stream"])
        self.assertFalse({"temperature", "top_p", "top_k", "thinking"} & set(request["body"]))
        self.assertEqual(model.usage, {"calls": 1, "input": 12, "output": 5})
        model.input("Again")
        self.assertEqual(model.usage, {"calls": 2, "input": 24, "output": 10})

    def testARefusalIsAnErrorWithItsCategory(self):
        self.server.mode = "refusal"
        with self.assertRaisesRegex(ModelError, r"declined this request \(cyber\)"):
            ApiModel("claude", "claude-fable-5-1", "key").input("x")

    def testEachFailureHasAMessageForTheUser(self):
        for mode, words in (("401", "refused the API key"), ("403", "does not allow"), ("404", "does not know a model called claude-x"),
                            ("429", "too many requests"), ("500", "error 500"), ("400", "error 400")):
            self.server.mode = mode
            with self.assertRaisesRegex(ModelError, words):
                ApiModel("claude", "claude-x", "key").input("x")

    def testAServerThatCannotBeReachedIsReported(self):
        with mock.patch.dict(model_clients.BASE_URLS, {"claude": "http://127.0.0.1:1"}):
            with self.assertRaisesRegex(ModelError, "could not be reached"):
                ApiModel("claude", "claude-x", "key").input("x")

    def testAnUnreachableServerIsALostConnectionButAServerThatAnswersIsNot(self):
        with mock.patch.dict(model_clients.BASE_URLS, {"claude": "http://127.0.0.1:1"}):
            with self.assertRaises(ModelConnectionError) as lost:
                ApiModel("claude", "claude-x", "key").input("x")
        self.assertIsInstance(lost.exception, ModelError)
        self.assertIsInstance(lost.exception, ConnectionLost)
        for mode in ("401", "403", "404", "429", "500", "400", "refusal"):
            self.server.mode = mode
            with self.assertRaises(ModelError) as failure:
                ApiModel("claude", "claude-x", "key").input("x")
            self.assertNotIsInstance(failure.exception, ConnectionLost, mode)

    def testATimeoutIsALostConnectionToo(self):
        import anthropic
        import httpx
        timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "http://127.0.0.1:1"))
        with mock.patch.object(ApiModel, "askClaude", side_effect=timeout), self.assertRaisesRegex(ModelConnectionError, "took too long to answer"):
            ApiModel("claude", "claude-x", "key").input("x")


@unittest.skipUnless(HAS_OPENAI, "the openai package is not installed")
class OpenAiCompatibleTests(ProviderTestCase):
    def testGptGeminiAndDeepseekAreAskedOnTheirOwnAddress(self):
        for provider, path in (("gpt", "/openai/chat/completions"), ("gemini", "/gemini/chat/completions"), ("deepseek", "/deepseek/chat/completions")):
            model = ApiModel(provider, f"{provider}-model", f"key-{provider}")
            self.assertEqual(model.input("Say hi"), "Chat answer.")
            request = self.server.requests[-1]
            self.assertEqual(request["path"], path)
            self.assertEqual(request["headers"]["authorization"], f"Bearer key-{provider}")
            self.assertEqual(request["body"]["model"], f"{provider}-model")
            self.assertEqual(request["body"]["messages"], [{"role": "user", "content": "Say hi"}])
            self.assertEqual(model.usage, {"calls": 1, "input": 7, "output": 3})

    def testARefusalAndAnEmptyAnswer(self):
        self.server.mode = "refusal"
        with self.assertRaisesRegex(ModelError, "declined this request: I cannot help"):
            ApiModel("gpt", "gpt-x", "key").input("x")
        self.server.mode = "empty"
        self.assertEqual(ApiModel("gpt", "gpt-x", "key").input("x"), "")

    def testEachFailureHasAMessageForTheUser(self):
        for mode, words in (("401", "OpenAI refused the API key"), ("404", "OpenAI does not know a model called gpt-x"), ("429", "no credit left"),
                            ("500", "error 500")):
            self.server.mode = mode
            with self.assertRaisesRegex(ModelError, words):
                ApiModel("gpt", "gpt-x", "key").input("x")

    def testAnUnreachableServerIsALostConnectionForEveryProviderAndAnAnswerIsNot(self):
        for provider in ("gpt", "gemini", "deepseek"):
            with mock.patch.dict(model_clients.BASE_URLS, {provider: "http://127.0.0.1:1"}), self.assertRaises(ModelConnectionError):
                ApiModel(provider, f"{provider}-x", "key").input("x")
        for mode in ("401", "404", "429", "500", "refusal"):
            self.server.mode = mode
            with self.assertRaises(ModelError) as failure:
                ApiModel("gpt", "gpt-x", "key").input("x")
            self.assertNotIsInstance(failure.exception, ConnectionLost, mode)
        self.server.mode = "401"
        with self.assertRaisesRegex(ModelError, "DeepSeek refused"):
            ApiModel("deepseek", "deepseek-x", "key").input("x")


class ApiSetupTests(unittest.TestCase):
    def testAMissingLibraryTellsTheUserWhatToInstall(self):
        real = model_clients.importlib.import_module
        def fake(name, *arguments):
            if name in ("anthropic", "openai"):
                raise ImportError(name)
            return real(name, *arguments)
        with mock.patch.object(model_clients.importlib, "import_module", fake):
            with self.assertRaisesRegex(ModelError, "pip install -U anthropic"):
                ApiModel("claude", "claude-x", "key").input("x")
            with self.assertRaisesRegex(ModelError, "pip install -U openai"):
                ApiModel("deepseek", "deepseek-v4-pro", "key").input("x")

    def testThePackagesAModelNeedsAreKnownBeforeItRuns(self):
        installed = {"openai", "torch", "transformers", "accelerate"}
        with mock.patch.object(model_clients.importlib.util, "find_spec", lambda name: object() if name in installed else None):
            self.assertEqual(findMissingPackages(getModelInfo("claude-opus-5-5")), ["anthropic"])
            self.assertEqual(findMissingPackages(getModelInfo("gpt-6-astra")), [])
            self.assertEqual(findMissingPackages(getModelInfo("deepseek-v4-pro")), [])
            self.assertEqual(findMissingPackages(getModelInfo("Qwen/Qwen3.5-9B")), [])
            self.assertEqual(findMissingPackages(getModelInfo("Qwen/Qwen3.5-9B", bits=4)), ["bitsandbytes"])
        with mock.patch.object(model_clients.importlib.util, "find_spec", lambda name: None):
            self.assertEqual(findMissingPackages(getModelInfo("Qwen/Qwen3.5-9B")), ["torch", "transformers", "accelerate"])

    def testTheKeyComesFromTheEnvironmentOrFromTheUser(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "  from-env  ", "OPENAI_API_KEY": ""}):
            self.assertEqual(getApiKey("claude"), "from-env")
            self.assertEqual(getApiKey("gpt"), "")
            claude = createModel(getModelInfo("claude-opus-5-5"))
            self.assertEqual((claude.provider, claude.name, claude.apiKey), ("claude", "claude-opus-5-5", "from-env"))
            typed = createModel(getModelInfo("claude-opus-5-5"), {"claude": "typed"})
            self.assertEqual(typed.apiKey, "typed")
            with self.assertRaisesRegex(ModelError, "no API key for OpenAI. Set OPENAI_API_KEY"):
                createModel(getModelInfo("gpt-6-astra"))

    def testALocalModelIsCreatedWithItsPrecisionAndToken(self):
        reports = []
        model = createModel(getModelInfo("Qwen/Qwen3.5-9B", bits=4), token="hf_x", report=reports.append)
        self.assertIsInstance(model, LocalModel)
        self.assertEqual((model.name, model.bits, model.token, model.report), ("Qwen/Qwen3.5-9B", 4, "hf_x", reports.append))

    def testTheModelCacheIsWhereTheUserPutsIt(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with mock.patch.dict(os.environ, {"HF_HOME": folder.name}, clear=False):
            os.environ.pop("HF_HUB_CACHE", None)
            self.assertEqual(getHubFolder(), Path(folder.name) / "hub")
            self.assertFalse(isDownloaded("Qwen/Qwen3.5-0.8B"))
            (Path(folder.name) / "hub" / "models--Qwen--Qwen3.5-0.8B").mkdir(parents=True)
            self.assertTrue(isDownloaded("Qwen/Qwen3.5-0.8B"))
            self.assertFalse(isDownloaded("Qwen/Qwen3.5-2B"))
            os.environ["HF_HUB_CACHE"] = "/elsewhere/hub"
            self.assertEqual(getHubFolder(), Path("/elsewhere/hub"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(getHubFolder(), Path("~/.cache/huggingface").expanduser() / "hub")


class HuggingFaceLookupTests(unittest.TestCase):
    def asking(self, body=None, error=None):
        seen = []
        def fetch(url, login=None, limit=0):
            seen.append(url)
            if error:
                raise error
            return json.dumps(body).encode() if not isinstance(body, bytes) else body
        patcher = mock.patch.object(model_clients, "fetchUrl", fetch)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def testTheSizeAndTheGatingOfAModelAreRead(self):
        seen = self.asking({"safetensors": {"total": 8190735360, "parameters": {"BF16": 8190735360}}, "gated": False})
        self.assertEqual(lookupHuggingFace("Qwen/Qwen3-8B"), {"billions": 8.2, "gated": False})
        self.assertEqual(seen, ["https://huggingface.co/api/models/Qwen/Qwen3-8B"])
        self.asking({"safetensors": {"total": 1235814400}, "gated": "manual"})
        self.assertEqual(lookupHuggingFace("meta-llama/Llama-3.2-1B-Instruct"), {"billions": 1.2, "gated": True})

    def testAModelThatDoesNotPublishItsSizeHasNone(self):
        self.asking({"gated": False})
        self.assertEqual(lookupHuggingFace("someone/model"), {"billions": None, "gated": False})
        self.asking({"safetensors": None})
        self.assertIsNone(lookupHuggingFace("someone/model")["billions"])

    def testEachProblemHasAMessageForTheUser(self):
        self.asking(error=urllib.error.HTTPError("u", 404, "Not Found", {}, None))
        with self.assertRaisesRegex(ModelError, "no model called a/b on Hugging Face. Check how it is written"):
            lookupHuggingFace("a/b")
        self.asking(error=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
        with self.assertRaisesRegex(ModelError, "error 401 for a/b. A private model needs a token"):
            lookupHuggingFace("a/b")
        self.asking(error=OSError("no network"))
        with self.assertRaisesRegex(ModelError, "Hugging Face could not be asked"):
            lookupHuggingFace("a/b")
        self.asking(body=b"not json")
        with self.assertRaisesRegex(ModelError, "could not be asked"):
            lookupHuggingFace("a/b")


class Ids(list):
    @property
    def shape(self):
        return (len(self),)

    def __getitem__(self, item):
        result = super().__getitem__(item)
        return Ids(result) if isinstance(item, slice) else result


class Output(list):
    @property
    def shape(self):
        return (len(self), len(self[0]))


class Batch(dict):
    def to(self, device):
        self.device = device
        return self


# Pretend torch, transformers, accelerate and bitsandbytes, so the logic of LocalModel is tested without a GPU or a download.
# The names of their functions (is_available, from_pretrained, apply_chat_template...) are the ones of the real libraries.
class FakeLibraries:
    def __init__(self, cuda=True, answer="<think>hmm</think> The answer.", causalFails=False):
        self.calls = {"loaded": [], "template": [], "generate": [], "tokenizers": [], "cache": 0}
        calls = self.calls
        class Cuda:
            @staticmethod
            def is_available():
                return cuda

            @staticmethod
            def empty_cache():
                calls["cache"] += 1
        class NoGrad:
            def __enter__(self):
                return self

            def __exit__(self, *arguments):
                return False
        class Tokenizer:
            @classmethod
            def from_pretrained(cls, name, token=None):
                calls["tokenizers"].append((name, token))
                return cls()

            def apply_chat_template(self, messages, **options):
                calls["template"].append((messages, options))
                return Batch(input_ids=Ids(range(5)))

            def decode(self, ids, skip_special_tokens):
                calls["decoded"] = (list(ids), skip_special_tokens)
                return answer
        class Model:
            device = "cuda:0"

            def generate(self, **inputs):
                calls["generate"].append(inputs)
                return Output([list(range(5)) + [100, 101, 102]])
        def loader(kind):
            class Loader:
                @staticmethod
                def from_pretrained(name, **options):
                    if kind == "causal" and causalFails:
                        raise ValueError("Unrecognized configuration class")
                    calls["loaded"].append((kind, name, options))
                    return Model()
            return Loader
        class Config:
            def __init__(self, **options):
                self.options = options
        self.torch = type("torch", (), {"cuda": Cuda, "no_grad": staticmethod(NoGrad), "bfloat16": "bf16"})
        self.transformers = type("transformers", (), {"AutoTokenizer": Tokenizer, "AutoModelForCausalLM": loader("causal"),
                                                      "AutoModelForImageTextToText": loader("image"), "BitsAndBytesConfig": Config})

    def patch(self):
        return mock.patch.dict(sys.modules, {"torch": self.torch, "transformers": self.transformers, "accelerate": mock.Mock(), "bitsandbytes": mock.Mock()})


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(model_clients, "isOnline", lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def testADownloadThatFailsBecauseTheInternetIsGoneIsALostConnection(self):
        libraries = FakeLibraries()
        with libraries.patch(), mock.patch.object(libraries.transformers.AutoModelForCausalLM, "from_pretrained", side_effect=OSError("We couldn't connect to 'https://huggingface.co'")):
            with mock.patch.object(model_clients, "isOnline", lambda: False), self.assertRaisesRegex(ModelConnectionError, "Qwen/Qwen3-8B could not be loaded because it needs the internet"):
                LocalModel("Qwen/Qwen3-8B").input("x")
            with self.assertRaises(ModelError) as failure:
                LocalModel("Qwen/Qwen3-8B").input("x")
            self.assertNotIsInstance(failure.exception, ConnectionLost)

    def testItLoadsOnTheFirstQuestionAndAnswersWithoutTheThinking(self):
        libraries, reports = FakeLibraries(), []
        with libraries.patch():
            model = LocalModel("Qwen/Qwen3.5-9B", token="hf_x", report=reports.append, maxNewTokens=50)
            self.assertEqual(libraries.calls["loaded"], [])
            self.assertEqual(model.input("Say hi"), "The answer.")
            self.assertEqual(model.input("Again"), "The answer.")
        kind, name, options = libraries.calls["loaded"][0]
        self.assertEqual((len(libraries.calls["loaded"]), kind, name), (1, "causal", "Qwen/Qwen3.5-9B"))
        self.assertEqual(options, {"device_map": "auto", "token": "hf_x", "dtype": "auto"})
        self.assertEqual(libraries.calls["tokenizers"], [("Qwen/Qwen3.5-9B", "hf_x")])
        messages, templateOptions = libraries.calls["template"][0]
        self.assertEqual(messages, [{"role": "user", "content": "Say hi"}])
        self.assertTrue(templateOptions["add_generation_prompt"])
        self.assertFalse(templateOptions["enable_thinking"])
        self.assertEqual(libraries.calls["generate"][0]["max_new_tokens"], 50)
        self.assertEqual(libraries.calls["decoded"], ([100, 101, 102], True))
        self.assertEqual(model.usage, {"calls": 2, "input": 10, "output": 6})
        self.assertEqual(len(reports), 2)
        self.assertIn("Loading Qwen/Qwen3.5-9B", reports[0])

    def testTheUserIsOnlyWarnedAboutTheDownloadWhenThereIsOne(self):
        for downloaded, warned in ((False, True), (True, False)):
            reports = []
            with FakeLibraries().patch(), mock.patch.object(model_clients, "isDownloaded", lambda name: downloaded):
                LocalModel("Qwen/Qwen3.5-9B", report=reports.append).input("x")
            self.assertEqual("downloaded" in reports[0], warned)
            self.assertTrue(reports[0].startswith("Loading Qwen/Qwen3.5-9B on the GPUs."))

    def testCompressedModelsAreLoadedWithBitsAndBytes(self):
        for bits, key in ((4, "load_in_4bit"), (8, "load_in_8bit")):
            libraries = FakeLibraries()
            with libraries.patch():
                LocalModel("Qwen/Qwen3.5-9B", bits=bits).input("x")
            options = libraries.calls["loaded"][0][2]
            self.assertNotIn("dtype", options)
            self.assertTrue(options["quantization_config"].options[key])
            self.assertEqual(options["quantization_config"].options["load_in_8bit"], bits == 8)

    def testAModelThatIsNotACausalModelIsLoadedAsAnImageTextModel(self):
        libraries = FakeLibraries(causalFails=True)
        with libraries.patch():
            self.assertEqual(LocalModel("Qwen/Qwen3.5-0.8B").input("x"), "The answer.")
        self.assertEqual(libraries.calls["loaded"][0][0], "image")

    def testItRefusesToRunOnTheProcessorAndTellsAboutMissingPackages(self):
        with FakeLibraries(cuda=False).patch():
            with self.assertRaisesRegex(ModelError, "does not see any GPU"):
                LocalModel("Qwen/Qwen3.5-9B").input("x")
        real = model_clients.importlib.import_module
        def fake(name, *arguments):
            if name == "transformers":
                raise ImportError(name)
            return real(name, *arguments)
        with FakeLibraries().patch(), mock.patch.object(model_clients.importlib, "import_module", fake):
            with self.assertRaisesRegex(ModelError, "pip install -U transformers"):
                LocalModel("Qwen/Qwen3.5-9B").input("x")

    def testAnAnswerThatNeverLeavesTheThinkingIsEmpty(self):
        with FakeLibraries(answer="<think>I keep thinking and never answer").patch():
            self.assertEqual(LocalModel("Qwen/Qwen3.5-9B").input("x"), "")
        with FakeLibraries(answer="<think>a</think>One<think>b</think> and two").patch():
            self.assertEqual(LocalModel("Qwen/Qwen3.5-9B").input("x"), "One and two")

    def testAFailedDownloadAndAFullGpuAreToldInWords(self):
        libraries = FakeLibraries()
        with libraries.patch():
            with mock.patch.object(libraries.transformers.AutoModelForCausalLM, "from_pretrained", side_effect=OSError("401 Client Error. You are trying to access a gated repo.\nMore")):
                with self.assertRaisesRegex(ModelError, r"meta-llama/Llama-3.2-1B-Instruct could not be downloaded or opened \(401 Client Error.*gated model: accept its license on https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct"):
                    LocalModel("meta-llama/Llama-3.2-1B-Instruct").input("x")
                with self.assertRaises(ModelError) as plain:
                    LocalModel("Qwen/Qwen3-8B").input("x")
                self.assertNotIn("accept its license", str(plain.exception))
            with mock.patch.object(libraries.transformers.AutoModelForCausalLM, "from_pretrained", side_effect=RuntimeError("CUDA out of memory. Tried to allocate 2 GiB")):
                with self.assertRaisesRegex(ModelError, "GPUs ran out of memory with Qwen/Qwen3-8B"):
                    LocalModel("Qwen/Qwen3-8B").input("x")
            with mock.patch.object(libraries.transformers.AutoModelForCausalLM, "from_pretrained", side_effect=RuntimeError("something else")):
                with self.assertRaisesRegex(RuntimeError, "something else"):
                    LocalModel("Qwen/Qwen3-8B").input("x")

    def testUnloadingFreesTheMemoryAndTheModelLoadsAgainWhenNeeded(self):
        libraries = FakeLibraries()
        with libraries.patch():
            model = LocalModel("Qwen/Qwen3.5-9B")
            model.input("x")
            model.unload()
            self.assertIsNone(model.model)
            self.assertEqual(libraries.calls["cache"], 1)
            model.input("x")
        self.assertEqual(len(libraries.calls["loaded"]), 2)

    def testOnlyOneModelIsLoadedAtATime(self):
        libraries, active, worst = FakeLibraries(), [0], [0]
        original = LocalModel.load
        def slowLoad(self):
            active[0] += 1
            worst[0] = max(worst[0], active[0])
            threading.Event().wait(0.05)
            original(self)
            active[0] -= 1
        with libraries.patch(), mock.patch.object(LocalModel, "load", slowLoad):
            threads = [threading.Thread(target=LocalModel(f"owner/model-{number}").input, args=("x",)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual((worst[0], len(libraries.calls["loaded"])), (1, 4))


if __name__ == "__main__":
    unittest.main()
