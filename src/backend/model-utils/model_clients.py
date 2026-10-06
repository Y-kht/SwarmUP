import asyncio
import dataclasses
import gc
import importlib
import importlib.util
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import harness_utils
from harness_utils import ConnectionLost, describeError, fetchUrl, isOnline, remember
from models_library import API_KEYS, DEFAULT_CLI_MODEL, MODELS_CLI, isGated

# A model client is any object with an input(prompt) method that returns the answer as text: it is the agent of a loop.
# ApiModel asks the API of a provider with its official library (anthropic for Claude, openai for GPT and for the OpenAI-compatible
# APIs of Gemini and DeepSeek). LocalModel runs a model of Hugging Face on the GPUs of the user with transformers.
# The libraries are only imported when a model is used, so a user who only needs one kind of model does not install the others.
# The Hugging Face cache is the folder of the HF_HOME environment variable, so the user chooses where the models are stored.
API_LIBRARIES = {"claude": "anthropic", "gpt": "openai", "gemini": "openai", "deepseek": "openai"}
BASE_URLS = {"claude": None, "gpt": None, "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/", "deepseek": "https://api.deepseek.com"}
API_RETRIES = 2
CLAUDE_MAX_TOKENS = 32000
LOCAL_MAX_TOKENS = 4096
THINKING_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


# One call of a model, for the cost of the mission (MissionCosts in harness_utils.py): the tokens it read (input, without the cache), read from
# the cache (cachedInput), wrote to the cache (cacheWrite) and wrote (output, thinking included). cost is the exact cost when the model bills
# itself (Claude Code), and missing says the provider did not tell the tokens (the call is then never counted as free). The totals are what
# the table of the tokens shows. The time is in UTC, because DeepSeek bills by the hour of the day.
def recordCall(usage, input=0, cachedInput=0, cacheWrite=0, output=0, cost=None, missing=False):
    input, cachedInput, cacheWrite, output = (max(0, int(value or 0)) for value in (input, cachedInput, cacheWrite, output))
    usage["calls"] += 1
    usage["input"] += input + cachedInput + cacheWrite
    usage["output"] += output
    usage.setdefault("records", []).append({"time": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S}", "input": input, "cachedInput": cachedInput,
                                            "cacheWrite": cacheWrite, "output": output, "cost": cost, "missing": missing})


# An error whose message is written for the user. The agent that meets it fails, and its message is shown to the user.
class ModelError(Exception):
    pass


# The model cannot be reached because the connection is lost. A swarm pauses the agent instead of failing it (see ConnectionLost).
class ModelConnectionError(ModelError, ConnectionLost):
    pass


def importLibrary(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        raise ModelError(f"The {name} package is needed for this model. Install it with: pip install -U {name}") from None


# The packages a chosen model needs and the computer does not have, for the user to know before the swarm runs.
# Codex is a program and not a package: checkCodex says if it is installed.
def findMissingPackages(info):
    if info.get("cli"):
        package = MODELS_CLI[info["cli"]]["package"]
        return [package] if package and importlib.util.find_spec(package.replace("-", "_")) is None else []
    if info["local"]:
        needed = ["torch", "transformers", "accelerate"] + (["bitsandbytes"] if info["bits"] < 16 else [])
    else:
        needed = [API_LIBRARIES[info["provider"]]]
    return [name for name in needed if importlib.util.find_spec(name) is None]


def getApiKey(provider):
    return os.environ.get(API_KEYS[provider]["variable"], "").strip()


def getHubFolder():
    return Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or "~/.cache/huggingface").expanduser() / "hub")


def isDownloaded(name):
    return (getHubFolder() / f"models--{name.replace('/', '--')}").exists()


# What Hugging Face says about a model that is not in the list of the library: its number of parameters in billions (None if it does not
# say) and whether it is gated. It raises a ModelError, with a message for the user, if the model cannot be found. The answer is kept a week
# (see remember in harness_utils.py), and given from the last connection without internet.
HUGGING_FACE_REFRESH_SECONDS = 7 * 24 * 3600


def readHuggingFace(name):
    data = json.loads(fetchUrl(f"https://huggingface.co/api/models/{name}"))
    if not isinstance(data, dict):
        raise ValueError("Hugging Face gave an answer that cannot be read")
    total = (data.get("safetensors") or {}).get("total")
    return {"billions": round(total / 1000000000, 1) if total else None, "gated": bool(data.get("gated"))}


def lookupHuggingFace(name):
    try:
        return remember(f"huggingface-{name.strip().lower()}", lambda: readHuggingFace(name), HUGGING_FACE_REFRESH_SECONDS, errors=(OSError, ValueError))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ModelError(f"There is no model called {name} on Hugging Face. Check how it is written.") from None
        raise ModelError(f"Hugging Face answered with error {error.code} for {name}. A private model needs a token.") from None
    except (OSError, ValueError) as error:
        raise ModelError(f"Hugging Face could not be asked: {describeError(error)}.") from None


# What went wrong with a call to an API, in words a user understands. The libraries of Anthropic and OpenAI have the same error classes.
def explainApiError(library, error, company, name):
    if isinstance(error, library.APITimeoutError):
        return f"{company} took too long to answer."
    if isinstance(error, library.APIConnectionError):
        return f"{company} could not be reached. Check the internet connection."
    if isinstance(error, library.AuthenticationError):
        return f"{company} refused the API key. Check that it is correct and still active."
    if isinstance(error, library.PermissionDeniedError):
        return f"{company} does not allow this API key to use {name} (permissions, region, or an access request is missing)."
    if isinstance(error, library.NotFoundError):
        return f"{company} does not know a model called {name}, or this account cannot use it."
    if isinstance(error, library.RateLimitError):
        return f"{company} says there are too many requests, or that the account has no credit left. Try again later or check the billing page."
    if isinstance(error, library.APIStatusError):
        return f"{company} answered with error {error.status_code}: {getattr(error, 'message', error)}"
    return f"{company} failed: {error}"


# The tokens of an answer of the OpenAI API, and of the APIs like it. The cached tokens are part of prompt_tokens (OpenAI gives them in
# prompt_tokens_details, DeepSeek in prompt_cache_hit_tokens). The thinking of a model is billed as output: it is in completion_tokens for
# OpenAI and DeepSeek, and Gemini only counts it in total_tokens, so the output is what the total has beyond the prompt.
def readOpenAiUsage(usage):
    if usage is None:
        return {"missing": True}
    prompt = usage.prompt_tokens or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
    cached = min(prompt, cached or getattr(usage, "prompt_cache_hit_tokens", None) or 0)
    output = max(usage.completion_tokens or 0, (usage.total_tokens or 0) - prompt)
    return {"input": prompt - cached, "cachedInput": cached, "output": output}


class ApiModel:
    def __init__(self, provider, name, apiKey):
        self.provider = provider
        self.name = name
        self.apiKey = apiKey
        self.company = API_KEYS[provider]["company"]
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.client = None
        self.lock = threading.Lock()

    def connect(self):
        library = importLibrary(API_LIBRARIES[self.provider])
        if self.client is None:
            options = {"api_key": self.apiKey, "max_retries": API_RETRIES}
            if BASE_URLS[self.provider]:
                options["base_url"] = BASE_URLS[self.provider]
            self.client = (library.Anthropic if self.provider == "claude" else library.OpenAI)(**options)
        return library

    def addUsage(self, **tokens):
        with self.lock:
            recordCall(self.usage, **tokens)

    # Claude is asked with a stream and not with a single request, because a long answer would hit the time limit of a single request.
    # A refusal is a normal answer of the API (HTTP 200), so it is checked before the text is read. Thinking blocks are not part of the answer.
    def askClaude(self, prompt):
        with self.client.messages.stream(model=self.name, max_tokens=CLAUDE_MAX_TOKENS, messages=[{"role": "user", "content": prompt}]) as stream:
            message = stream.get_final_message()
        usage = message.usage
        self.addUsage(input=usage.input_tokens, cachedInput=getattr(usage, "cache_read_input_tokens", 0), cacheWrite=getattr(usage, "cache_creation_input_tokens", 0),
                      output=usage.output_tokens)
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise ModelError(f"{self.name} declined this request ({category or 'no reason given'}). Choose another model for this agent, or change what it is asked.")
        return "".join(block.text for block in message.content if block.type == "text")

    def askOpenAi(self, prompt):
        response = self.client.chat.completions.create(model=self.name, messages=[{"role": "user", "content": prompt}])
        self.addUsage(**readOpenAiUsage(response.usage))
        choice = response.choices[0]
        if choice.message.refusal and not choice.message.content:
            raise ModelError(f"{self.name} declined this request: {choice.message.refusal}")
        return choice.message.content or ""

    def input(self, prompt):
        library = self.connect()
        try:
            return self.askClaude(prompt) if self.provider == "claude" else self.askOpenAi(prompt)
        except library.APIError as error:
            lost = isinstance(error, library.APIConnectionError)
            raise (ModelConnectionError if lost else ModelError)(explainApiError(library, error, self.company, self.name)) from error


# A Hugging Face model on the GPUs. It is loaded when it is first asked something, one model at a time (a swarm has many),
# spread over all the GPUs. bits below 16 load it compressed (it needs bitsandbytes), and token is the Hugging Face token of gated models.
# report is a function that tells the user what takes time (the download and the loading of a big model take minutes).
class LocalModel:
    loading = threading.Lock()

    def __init__(self, name, bits=16, token=None, report=None, maxNewTokens=LOCAL_MAX_TOKENS):
        self.name = name
        self.bits = bits
        self.token = token
        self.report = report or (lambda message: None)
        self.maxNewTokens = maxNewTokens
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.model = None
        self.tokenizer = None
        self.torch = None
        self.lock = threading.Lock()

    def load(self):
        torch, transformers = importLibrary("torch"), importLibrary("transformers")
        importLibrary("accelerate")
        if not torch.cuda.is_available():
            raise ModelError("PyTorch does not see any GPU, so the model would run on the processor and be extremely slow. Install the CUDA version of PyTorch.")
        options = {"device_map": "auto", "token": self.token}
        if self.bits < 16:
            importLibrary("bitsandbytes")
            options["quantization_config"] = transformers.BitsAndBytesConfig(load_in_8bit=self.bits == 8, load_in_4bit=self.bits == 4,
                                                                             bnb_4bit_compute_dtype=torch.bfloat16)
        else:
            options["dtype"] = "auto"
        self.report(f"Loading {self.name} on the GPUs." + ("" if isDownloaded(self.name) else " The first time it is downloaded, which can take a long time."))
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(self.name, token=self.token)
        try:
            self.model = transformers.AutoModelForCausalLM.from_pretrained(self.name, **options)
        except ValueError:
            multimodal = getattr(transformers, "AutoModelForImageTextToText", None)
            if multimodal is None:
                raise ModelError(f"{self.name} needs a newer version of transformers. Update it with: pip install -U transformers") from None
            self.model = multimodal.from_pretrained(self.name, **options)
        self.torch = torch
        self.report(f"{self.name} is ready.")

    def answer(self, prompt):
        with self.lock:
            if self.model is None:
                with LocalModel.loading:
                    self.load()
            messages = [{"role": "user", "content": prompt}]
            inputs = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt", return_dict=True, enable_thinking=False)
            inputs = inputs.to(self.model.device)
            with self.torch.no_grad():
                output = self.model.generate(**inputs, max_new_tokens=self.maxNewTokens)
            start = inputs["input_ids"].shape[-1]
            self.usage["calls"] += 1
            self.usage["input"] += start
            self.usage["output"] += output.shape[-1] - start
            text = self.tokenizer.decode(output[0][start:], skip_special_tokens=True)
            return THINKING_PATTERN.sub("", text).split("<think>")[0].strip()

    # The two things that go wrong most with a local model are told in words a user understands.
    def input(self, prompt):
        try:
            return self.answer(prompt)
        except OSError as error:
            if not isOnline():
                raise ModelConnectionError(f"{self.name} could not be loaded because it needs the internet and the connection is lost.") from error
            gated = f" It is a gated model: accept its license on https://huggingface.co/{self.name} and give a Hugging Face token (HF_TOKEN)." if isGated(self.name) else ""
            raise ModelError(f"{self.name} could not be downloaded or opened ({str(error).splitlines()[0] if str(error) else type(error).__name__}).{gated}") from error
        except RuntimeError as error:
            if "out of memory" not in str(error).lower():
                raise
            raise ModelError(f"The GPUs ran out of memory with {self.name}. Other jobs may be using them. Free some memory or choose a smaller model.") from error

    # Frees the memory of the GPUs.
    def unload(self):
        with self.lock:
            self.model = self.tokenizer = None
            gc.collect()
            if self.torch is not None:
                self.torch.cuda.empty_cache()


# ==============
# Coding agents: Claude Code and Codex, which run on the computer of the user. For a loop they are a model like the others (input gives
# the reply), but they can also read files, run commands and use the web. The loop gives itself to them (attach), so they work in its
# folder (or in a folder of their own, empty, if it has none), and they ask the user through it: askPermission before an action,
# askQuestions when they have questions. Reading inside the folder needs no permission, every other action does. What the user allows
# "until the swarm runs again" is remembered until newRun. Nothing that an agent asks is ever answered without the user.
# ==============
CODING_AGENT_RULES = """You work as one agent of a swarm run by SwarmUP, a program that asks its user before anything is done.
Reply with exactly what the request asks for (a text, a plan, an email, the content of a file...) and nothing else: SwarmUP shows your reply to the user and acts on it only after the user approves it.
You may read files and run commands to do the task well. Every action other than reading your own folder is shown to the user first, who may refuse it: then go on without it.
Do not create, change or delete files unless the request asks for it: SwarmUP saves the approved result itself."""
WORKSPACES_FOLDER = "agent-workspaces"
NAME_SIGNS = re.compile(r"[^\w.-]")


# The folder where a coding agent works: the folder of its loop, or an empty one of its own, so it never reads the files of the user unasked.
def agentWorkspace(loop):
    if loop is not None and loop.folder:
        return Path(loop.folder)
    folder = harness_utils.AGENT_FILES / WORKSPACES_FOLDER / NAME_SIGNS.sub("_", loop.name if loop is not None else "agent")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def isInside(folder, path, base=None):
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base or folder) / candidate
    try:
        resolved, root = candidate.resolve(), Path(folder).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


# Without a loop (a test of the connection), nobody can be asked, so every action is refused.
def askPermission(loop, request):
    if loop is None:
        return {"decision": "deny", "message": "Nobody can approve actions during a test of the connection."}
    answer = loop.askPermission(request)
    return answer if isinstance(answer, dict) and answer.get("decision") in ("once", "run", "deny") else {"decision": "deny", "message": ""}


def askQuestions(loop, questions):
    answer = loop.askQuestions(questions) if loop is not None else {}
    return answer if isinstance(answer, dict) else {}


# ---------- Claude Code, through its official Python library (claude-agent-sdk), which contains Claude Code itself. ----------
# It is only used with an Anthropic API key, because Anthropic does not allow third-party products to use a Claude subscription. To be sure
# of it, the other credentials of the environment are blanked, Claude Code gets a configuration folder of its own (so it finds no login of
# the user), and the run stops if Claude Code says it uses anything else than the key. The settings files of the folder are not loaded
# (setting_sources=[]), so a project cannot allow commands on its own. Claude Code retries a refused key for minutes: the first refusal ends it.
CLAUDE_CODE_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch", "AskUserQuestion"]
CLAUDE_CODE_CREDENTIALS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
CLAUDE_CODE_HOME = "claude-code-home"
REFUSED_STATUSES = (401, 403)
# What Claude Code wants to do with each tool, in words, and the field of its input that says what exactly.
CLAUDE_CODE_ACTIONS = {"Bash": ("run a command", "command"), "Write": ("create or replace a file", "file_path"), "Edit": ("change a file", "file_path"),
                       "MultiEdit": ("change a file", "file_path"), "NotebookEdit": ("change a notebook", "notebook_path"),
                       "Read": ("read a file outside its folder", "file_path"), "Glob": ("look for files outside its folder", "path"),
                       "Grep": ("search in files outside its folder", "path"), "WebFetch": ("open a web page", "url"), "WebSearch": ("search the web", "query")}


def describeClaudeCodeRequest(tool, data, folder):
    action, field = CLAUDE_CODE_ACTIONS.get(tool, (f"use its tool {tool}", None))
    detail = str(data.get(field) or "") if field else ""
    if tool == "Glob":
        detail = f"{data.get('pattern', '')} in {data.get('path') or folder}"
    return {"action": action, "detail": detail or json.dumps(data, ensure_ascii=False)[:2000], "folder": str(folder), "reason": str(data.get("description") or "")}


# The rules that allow again what the user allowed until the swarm runs again, written like Claude Code writes them ("Bash(npm test *)").
def describeRunRules(tool, data, suggestions):
    rules = [f"{rule.tool_name}({rule.rule_content})" if rule.rule_content else rule.tool_name for update in suggestions
             if update.type == "addRules" and update.behavior == "allow" for rule in update.rules or []]
    if rules:
        return rules
    return [f"Bash({data['command']})"] if tool == "Bash" and data.get("command") else [tool]


# The questions of Claude (AskUserQuestion) in the form of Loop.askQuestions, and the answers in the form Claude expects.
def readClaudeQuestions(data):
    return [{"id": question.get("question", ""), "header": question.get("header", ""), "question": question.get("question", ""),
             "options": [{"label": option.get("label", ""), "description": option.get("description", "")} for option in question.get("options") or []],
             "multiple": bool(question.get("multiSelect")), "secret": False} for question in data.get("questions") or []]


class ClaudeCodeModel:
    def __init__(self, name, apiKey, report=None):
        self.name = name or DEFAULT_CLI_MODEL
        self.apiKey = apiKey
        self.report = report or (lambda message: None)
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.loop = None
        self.runRules = []
        self.runExact = set()
        self.lock = threading.Lock()

    def attach(self, loop):
        self.loop = loop

    def newRun(self):
        self.runRules, self.runExact = [], set()

    def options(self, sdk, folder):
        environment = {**{name: "" for name in CLAUDE_CODE_CREDENTIALS}, "ANTHROPIC_API_KEY": self.apiKey,
                       "CLAUDE_CONFIG_DIR": str(harness_utils.AGENT_FILES / CLAUDE_CODE_HOME)}
        return sdk.ClaudeAgentOptions(cwd=str(folder), permission_mode="default", tools=list(CLAUDE_CODE_TOOLS), allowed_tools=list(self.runRules),
                                      setting_sources=[], can_use_tool=self.decide, env=environment, model=None if self.name == DEFAULT_CLI_MODEL else self.name,
                                      system_prompt={"type": "preset", "preset": "claude_code", "append": CODING_AGENT_RULES},
                                      extra_args={"no-session-persistence": None})

    async def decide(self, tool, data, context):
        types = importlib.import_module("claude_agent_sdk.types")
        folder = agentWorkspace(self.loop)
        if tool == "AskUserQuestion":
            answers = await asyncio.to_thread(askQuestions, self.loop, readClaudeQuestions(data))
            chosen = {question: values if len(values) != 1 else values[0] for question, values in answers.items()}
            return types.PermissionResultAllow(updated_input={"questions": data.get("questions") or [], "answers": chosen})
        # Claude Code asks again for some commands its rules cover (those that write with >): the exact action the user allowed is not asked again.
        exact = (tool, json.dumps(data, sort_keys=True))
        if exact in self.runExact:
            return types.PermissionResultAllow(updated_input=data)
        answer = await asyncio.to_thread(askPermission, self.loop, describeClaudeCodeRequest(tool, data, folder))
        if answer["decision"] == "deny":
            return types.PermissionResultDeny(message=f"The user refused this. {answer.get('message') or ''}".strip())
        if answer["decision"] == "once":
            return types.PermissionResultAllow(updated_input=data)
        rules = describeRunRules(tool, data, context.suggestions or [])
        self.runRules += [rule for rule in rules if rule not in self.runRules]
        self.runExact.add(exact)
        # Claude Code suggests rules saved in the folder of the user (localSettings): they are kept in memory for this session instead.
        update = types.PermissionUpdate(type="addRules", rules=[types.PermissionRuleValue(tool_name=rule.split("(", 1)[0], rule_content=rule[len(rule.split("(", 1)[0]) + 1:-1] or None)
                                                                for rule in rules], behavior="allow", destination="session")
        return types.PermissionResultAllow(updated_input=data, updated_permissions=[update])

    async def converse(self, prompt):
        sdk = importLibrary("claude_agent_sdk")
        folder = agentWorkspace(self.loop)
        (harness_utils.AGENT_FILES / CLAUDE_CODE_HOME).mkdir(parents=True, exist_ok=True)
        result, retries = None, set()
        async with sdk.ClaudeSDKClient(self.options(sdk, folder)) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init" and message.data.get("apiKeySource") != "ANTHROPIC_API_KEY":
                    raise ModelError(f"Claude Code did not use your Anthropic API key (it used: {message.data.get('apiKeySource')}), so it was stopped. "
                                     "SwarmUP only runs Claude Code with an API key.")
                if isinstance(message, sdk.SystemMessage) and message.subtype == "api_retry":
                    status = message.data.get("error_status")
                    if status in REFUSED_STATUSES:
                        raise ModelError("Anthropic refused the API key of Claude Code. Check that it is correct and still active.")
                    if not isOnline():
                        raise ModelConnectionError("Claude Code cannot reach Anthropic: the internet connection is lost.")
                    if message.data.get("attempt") not in retries:
                        retries.add(message.data.get("attempt"))
                        self.report(f"Anthropic did not answer Claude Code ({message.data.get('error') or status}). Trying again, attempt "
                                    f"{message.data.get('attempt')} of {message.data.get('max_retries')}.")
                if isinstance(message, sdk.ResultMessage):
                    result = message
        if result is None:
            raise ModelError("Claude Code stopped without an answer.")
        self.recordResult(result)
        if result.is_error:
            if not isOnline():
                raise ModelConnectionError("Claude Code cannot reach Anthropic: the internet connection is lost.")
            raise ModelError(f"Claude Code could not finish: {result.result or result.subtype}")
        return result.result or ""

    # Claude Code tells what the call cost (total_cost_usd, for its whole conversation, and every call of SwarmUP is a conversation of its own),
    # and the tokens of every model it used (it also uses a small model for its own work).
    def recordResult(self, result):
        models = result.model_usage or {}
        if models:
            tokens = {field: sum((usage.get(key) or 0) for usage in models.values()) for field, key in
                      (("input", "inputTokens"), ("cachedInput", "cacheReadInputTokens"), ("cacheWrite", "cacheCreationInputTokens"), ("output", "outputTokens"))}
        else:
            usage = result.usage or {}
            tokens = {"input": usage.get("input_tokens"), "cachedInput": usage.get("cache_read_input_tokens"), "cacheWrite": usage.get("cache_creation_input_tokens"),
                      "output": usage.get("output_tokens")}
        cost = result.total_cost_usd
        if cost is None and models and all(isinstance(usage.get("costUSD"), (int, float)) for usage in models.values()):
            cost = sum(usage["costUSD"] for usage in models.values())
        recordCall(self.usage, **tokens, cost=cost)

    def input(self, prompt):
        with self.lock:
            try:
                return asyncio.run(self.converse(prompt))
            except ModelError:
                raise
            except Exception as error:
                if not isOnline():
                    raise ModelConnectionError("Claude Code cannot reach Anthropic: the internet connection is lost.") from error
                raise ModelError(f"Claude Code failed: {type(error).__name__}: {error}") from error


# ---------- Codex, through its app-server: JSON-RPC messages on its standard input and output. ----------
# OpenAI calls the app-server experimental, so SwarmUP only accepts the versions it was tested with (CODEX_TESTED: same major and minor
# version), unless SWARMUP_ALLOW_UNTESTED_CODEX is set. Codex uses the ChatGPT plan of the user: it is signed in through Codex itself
# (CodexLogin), which keeps the sign-in the way it does for its own window. Every thread is ephemeral (nothing is kept by Codex), works in the
# folder of the agent, and asks before every command (approval policy untrusted). SwarmUP answers alone only for a command that only reads
# inside the folder: one program among CODEX_READERS, without any sign of the shell, whose paths all stay inside the folder, and that Codex
# itself describes as reading. The web search of Codex (which never asks), its sub-agents and the MCP servers of the user are switched off.
CODEX_TESTED = (0, 160)
CODEX_SETTINGS = ["-c", 'web_search="disabled"', "-c", "features.multi_agent=false", "-c", "mcp_servers={}"]
CODEX_CLIENT = {"name": "swarmup", "title": "SwarmUP", "version": "1.0"}
CODEX_REQUEST_TIMEOUT = 60
CODEX_READERS = ("cat", "head", "tail", "ls", "wc", "grep", "rg")
CODEX_READ_ACTIONS = ("read", "listFiles", "search")
SHELL_SIGNS = set(";&|<>$`(){}[]\\~!#*?\n\r'\"")
DANGEROUS_OPTIONS = ("--pre", "-z", "--search-zip", "-f", "--follow")
CODEX_PROBLEMS = {"usageLimitExceeded": "Your ChatGPT plan reached its Codex limit for now. Try again later, or choose another model for this agent.",
                  "unauthorized": "Codex is not signed in anymore. Sign in with ChatGPT again in the step of the models.",
                  "contextWindowExceeded": "The task was too long for the model of Codex."}


def findCodex():
    return os.environ.get("SWARMUP_CODEX") or shutil.which("codex")


# Whether Codex is installed and has a version SwarmUP was tested with: {"path", "version", "problem"}.
def checkCodex():
    path = findCodex()
    if not path:
        return {"path": None, "version": None, "problem": "Codex is not installed. Install it with: npm install -g @openai/codex (it needs Node.js)."}
    try:
        output = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as error:
        return {"path": path, "version": None, "problem": f"Codex could not be started: {error}"}
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", output.stdout + output.stderr)
    if not found:
        return {"path": path, "version": None, "problem": "The version of Codex could not be read."}
    version = tuple(int(part) for part in found.groups())
    tested = ".".join(str(part) for part in CODEX_TESTED)
    problem = ""
    if version[:2] != CODEX_TESTED and not os.environ.get("SWARMUP_ALLOW_UNTESTED_CODEX"):
        problem = (f"SwarmUP was tested with Codex {tested}.x, and this computer has {found.group(0)}. The connection of SwarmUP to Codex is experimental "
                   f"for OpenAI, so another version may not work. Install the tested one with: npm install -g @openai/codex@{tested}")
    return {"path": path, "version": found.group(0), "problem": problem}


# One app-server of Codex. onRequest(method, params) answers the questions of Codex (its result, or None if SwarmUP does not handle them),
# and onNotify(method, params) receives its news. The questions are answered in their own thread, because the user can take long.
class CodexConnection:
    def __init__(self, path, onRequest=None, onNotify=None):
        self.onRequest = onRequest or (lambda method, params: None)
        self.onNotify = onNotify or (lambda method, params: None)
        self.pending = {}
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()
        self.closed = False
        try:
            self.process = subprocess.Popen([path, "app-server", *CODEX_SETTINGS], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                            encoding="utf-8", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise ModelError(f"Codex could not be started: {error}") from None
        threading.Thread(target=self.read, daemon=True).start()
        self.request("initialize", {"clientInfo": CODEX_CLIENT})
        self.send({"method": "initialized", "params": {}})

    def send(self, message):
        try:
            with self.lock:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError):
            raise ModelError("Codex stopped unexpectedly.") from None

    def read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "method" in message and "id" in message:
                threading.Thread(target=self.answer, args=(message,), daemon=True).start()
            elif "method" in message:
                self.onNotify(message["method"], message.get("params") or {})
            elif message.get("id") in self.pending:
                holder = self.pending.pop(message["id"])
                holder[1] = message
                holder[0].set()
        self.closed = True
        for holder in list(self.pending.values()):
            holder[0].set()
        self.onNotify("connection/closed", {})

    def request(self, method, params, timeout=CODEX_REQUEST_TIMEOUT):
        number, holder = next(self.numbers), [threading.Event(), None]
        self.pending[number] = holder
        self.send({"method": method, "id": number, "params": params})
        if not holder[0].wait(timeout):
            self.pending.pop(number, None)
            raise ModelError(f"Codex did not answer in time ({method}).")
        if holder[1] is None:
            raise ModelError("Codex stopped unexpectedly.")
        if "error" in holder[1]:
            raise ModelError(f"Codex refused {method}: {holder[1]['error'].get('message', holder[1]['error'])}")
        return holder[1].get("result") or {}

    def answer(self, message):
        try:
            result = self.onRequest(message["method"], message.get("params") or {})
        except Exception as error:
            result, problem = None, str(error)
        else:
            problem = "SwarmUP does not handle this request."
        try:
            self.send({"id": message["id"], "result": result} if result is not None else {"id": message["id"], "error": {"code": -32601, "message": problem}})
        except ModelError:
            pass

    def close(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def openCodex(onRequest=None, onNotify=None):
    status = checkCodex()
    if status["problem"]:
        raise ModelError(status["problem"])
    return CodexConnection(status["path"], onRequest, onNotify)


# Who is signed in to Codex: {"signedIn", "type" (chatgpt or apiKey), "email", "plan"}.
def readCodexAccount():
    connection = openCodex()
    try:
        account = connection.request("account/read", {}).get("account")
    finally:
        connection.close()
    if not account:
        return {"signedIn": False, "type": None, "email": None, "plan": None}
    return {"signedIn": True, "type": account.get("type"), "email": account.get("email"), "plan": account.get("planType")}


# The models the plan of the user can use in Codex: [{"id", "name", "description", "isDefault"}]. Codex is asked every time, and when it cannot
# answer (no internet), the list of the last time is given (see remember in harness_utils.py).
def listCodexModels():
    def fetch():
        connection = openCodex()
        try:
            models = connection.request("model/list", {"limit": 100}).get("data") or []
        finally:
            connection.close()
        return [{"id": model["id"], "name": model.get("displayName") or model["id"], "description": model.get("description") or "", "isDefault": bool(model.get("isDefault"))}
                for model in models if not model.get("hidden")]
    return remember("codex-models", fetch, 0, errors=(ModelError, OSError))


# A sign-in with ChatGPT, through Codex. start gives what the user must open: {"url"} for the browser (Codex receives the answer on
# this computer), or {"url", "code"} for a code typed on the page (works when the browser cannot come back to this computer).
# wait gives "" when the user is signed in, or what went wrong. cancel stops it.
class CodexLogin:
    def __init__(self):
        self.done = threading.Event()
        self.error = ""
        self.loginId = None
        self.connection = openCodex(onNotify=self.notice)

    def notice(self, method, params):
        if method == "account/login/completed" and params.get("loginId") in (None, self.loginId):
            self.error = "" if params.get("success") else params.get("error") or "The sign-in did not finish."
            self.done.set()
        elif method == "connection/closed" and not self.done.is_set():
            self.error = "Codex stopped during the sign-in."
            self.done.set()

    def start(self, kind="browser"):
        result = self.connection.request("account/login/start", {"type": "chatgptDeviceCode" if kind == "code" else "chatgpt"})
        self.loginId = result.get("loginId")
        return {"url": result.get("verificationUrl") or result.get("authUrl"), "code": result.get("userCode")}

    def wait(self, timeout=None):
        self.done.wait(timeout)
        self.connection.close()
        return self.error if self.done.is_set() else "The sign-in took too long."

    def cancel(self):
        if self.loginId and not self.done.is_set():
            try:
                self.connection.request("account/login/cancel", {"loginId": self.loginId})
            except ModelError:
                pass
        self.error = self.error or "The sign-in was cancelled."
        self.done.set()
        self.connection.close()


# The command inside the shell that Codex wraps it in (/bin/bash -lc '...').
def unwrapCommand(command):
    try:
        words = shlex.split(command or "")
    except ValueError:
        return command or ""
    if len(words) == 3 and Path(words[0]).name in ("bash", "sh", "zsh") and words[1] in ("-lc", "-c"):
        return words[2]
    return command or ""


# A command that only reads inside the folder, which SwarmUP lets run without asking. On Windows every command is asked.
def isSafeRead(params, folder):
    if os.name == "nt":
        return False
    actions = params.get("commandActions") or []
    if not actions or any(action.get("type") not in CODEX_READ_ACTIONS for action in actions):
        return False
    command = unwrapCommand(params.get("command"))
    if not command or any(sign in SHELL_SIGNS for sign in command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    cwd = params.get("cwd") or str(folder)
    if not words or words[0] not in CODEX_READERS or not isInside(folder, cwd):
        return False
    for word in words[1:]:
        if word.startswith(DANGEROUS_OPTIONS):
            return False
        value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else None if word.startswith("-") else word
        if value is not None and not isInside(folder, value, cwd):
            return False
    return True


def describeCodexPermissions(permissions):
    parts = []
    files = (permissions or {}).get("fileSystem") or {}
    for entry in files.get("entries") or []:
        parts.append(json.dumps(entry, ensure_ascii=False))
    parts += [f"read {path}" for path in files.get("read") or []] + [f"write {path}" for path in files.get("write") or []]
    if ((permissions or {}).get("network") or {}).get("enabled"):
        parts.append("use the internet")
    return "; ".join(parts) or json.dumps(permissions, ensure_ascii=False)


class CodexModel:
    def __init__(self, name, report=None):
        self.name = name or DEFAULT_CLI_MODEL
        self.report = report or (lambda message: None)
        self.usage = {"calls": 0, "input": 0, "output": 0}
        # How Codex is signed in, read when it connects: chatgpt (the plan pays, unless the plan is billed by use) or apiKey (OpenAI bills every
        # use), see priceOfModel.
        self.accountType = "chatgpt"
        self.planType = ""
        self.loop = None
        self.connection = None
        self.turn = None
        self.runCommands = set()
        self.runKinds = set()
        self.lock = threading.Lock()

    def attach(self, loop):
        self.loop = loop

    def newRun(self):
        self.runCommands, self.runKinds = set(), set()

    def connect(self):
        if self.connection is None or self.connection.closed:
            self.connection = openCodex(self.answer, self.notice)
            account = self.connection.request("account/read", {})
            if not account.get("account") and account.get("requiresOpenaiAuth", True):
                self.close()
                raise ModelError("Codex is not signed in. Sign in with ChatGPT in the step of the models, then start again.")
            self.accountType = (account.get("account") or {}).get("type") or "other"
            self.planType = (account.get("account") or {}).get("planType") or ""
        return self.connection

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def unload(self):
        self.close()

    def notice(self, method, params):
        turn = self.turn
        if turn is None or (params.get("threadId") and params["threadId"] != turn["thread"]):
            return
        if method == "item/started" and (params.get("item") or {}).get("type") == "fileChange":
            item = params["item"]
            turn["files"][item.get("id")] = [change.get("path") for change in item.get("changes") or [] if isinstance(change, dict) and change.get("path")]
        elif method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
            turn["messages"].append(params["item"].get("text") or "")
        elif method == "thread/tokenUsage/updated":
            turn["usage"] = (params.get("tokenUsage") or {}).get("total") or turn["usage"]
        elif method == "error" and params.get("willRetry"):
            self.report(f"Codex: {(params.get('error') or {}).get('message', 'a problem')}. It tries again.")
        elif method == "turn/completed":
            turn["result"] = params.get("turn") or {}
            turn["done"].set()
        elif method == "connection/closed":
            turn["done"].set()

    def answer(self, method, params):
        folder = agentWorkspace(self.loop)
        if method == "item/commandExecution/requestApproval":
            command = unwrapCommand(params.get("command"))
            if isSafeRead(params, folder) or command in self.runCommands:
                return {"decision": "accept"}
            network = params.get("networkApprovalContext")
            action = f"connect to {network.get('host')}" if network else "run a command"
            decision = askPermission(self.loop, {"action": action, "detail": command, "folder": params.get("cwd") or str(folder), "reason": params.get("reason") or ""})
            if decision["decision"] == "run":
                self.runCommands.add(command)
            return {"decision": "decline" if decision["decision"] == "deny" else "accept"}
        if method == "item/fileChange/requestApproval":
            if "fileChange" in self.runKinds:
                return {"decision": "accept"}
            files = (self.turn or {}).get("files", {}).get(params.get("itemId")) or []
            detail = ", ".join(files) or params.get("reason") or "files of its folder"
            action = f"write anywhere in {params['grantRoot']}" if params.get("grantRoot") else "change files"
            decision = askPermission(self.loop, {"action": action, "detail": detail, "folder": str(folder), "reason": params.get("reason") or ""})
            if decision["decision"] == "run":
                self.runKinds.add("fileChange")
            return {"decision": "decline" if decision["decision"] == "deny" else "accept"}
        if method == "item/permissions/requestApproval":
            decision = askPermission(self.loop, {"action": "get more permissions", "detail": describeCodexPermissions(params.get("permissions")),
                                                 "folder": params.get("cwd") or str(folder), "reason": params.get("reason") or ""})
            if decision["decision"] == "deny":
                return {"permissions": {}}
            return {"permissions": params.get("permissions") or {}, "scope": "session" if decision["decision"] == "run" else "turn"}
        if method == "item/tool/requestUserInput":
            questions = [{"id": question["id"], "header": question.get("header", ""), "question": question.get("question", ""),
                          "options": [{"label": option.get("label", ""), "description": option.get("description", "")} for option in question.get("options") or []],
                          "multiple": False, "secret": bool(question.get("isSecret"))} for question in params.get("questions") or []]
            answers = askQuestions(self.loop, questions)
            return {"answers": {question["id"]: {"answers": [str(value) for value in answers.get(question["id"]) or []]} for question in questions}}
        return None

    def input(self, prompt):
        with self.lock:
            connection = self.connect()
            folder = agentWorkspace(self.loop)
            thread = connection.request("thread/start", {"cwd": str(folder), "approvalPolicy": "untrusted", "sandbox": "workspace-write", "ephemeral": True,
                                                         "model": None if self.name == DEFAULT_CLI_MODEL else self.name, "developerInstructions": CODING_AGENT_RULES})
            self.turn = {"thread": thread["thread"]["id"], "done": threading.Event(), "messages": [], "files": {}, "usage": None, "result": None}
            connection.request("turn/start", {"threadId": self.turn["thread"], "input": [{"type": "text", "text": prompt}]})
            self.turn["done"].wait()
            turn, self.turn = self.turn, None
        usage = turn["usage"] or {}
        cached = min(usage.get("cachedInputTokens") or 0, usage.get("inputTokens") or 0)
        recordCall(self.usage, input=(usage.get("inputTokens") or 0) - cached, cachedInput=cached, output=usage.get("outputTokens"), missing=not usage)
        result = turn["result"]
        if result is None:
            self.close()
            raise ModelError("Codex stopped unexpectedly.")
        if result.get("status") != "completed":
            error = result.get("error") or {}
            if not isOnline():
                raise ModelConnectionError("Codex cannot reach OpenAI: the internet connection is lost.")
            kind = error.get("codexErrorInfo")
            raise ModelError(CODEX_PROBLEMS.get(kind) if isinstance(kind, str) and kind in CODEX_PROBLEMS else f"Codex could not finish: {error.get('message') or result.get('status')}")
        return turn["messages"][-1] if turn["messages"] else ""


# The client of a model chosen with getModelInfo (models_library.py). apiKeys is {provider: key}, and the environment variable of the provider is the other source.
# Claude Code takes the key of Anthropic, and Codex needs none: it is signed in with ChatGPT through Codex itself.
def createModel(info, apiKeys=None, token=None, report=None):
    if info.get("cli") == "codex":
        return CodexModel(info["name"], report)
    if info["local"]:
        return LocalModel(info["name"], info["bits"], token, report)
    key = (apiKeys or {}).get(info["provider"]) or getApiKey(info["provider"])
    if not key:
        raise ModelError(f"There is no API key for {API_KEYS[info['provider']]['company']}. Set {API_KEYS[info['provider']]['variable']} or give the key.")
    if info.get("cli") == "claude-code":
        return ClaudeCodeModel(info["name"], key, report)
    return ApiModel(info["provider"], info["name"], key)
