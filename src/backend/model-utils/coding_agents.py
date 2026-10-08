# The coding agents (Claude Code and Codex) think for an agent like a model, and ask the user through its loop before they act.
# This module has what both need, and Claude Code. Codex is in codex_agent.py.
import asyncio
import importlib.util
import json
import threading

import harness_utils
# Where a coding agent works, and what is inside its folder, are part of the storehouse of the agents (agent_storehouse.py).
from agent_storehouse import WORKSPACES_FOLDER, agentWorkspace, isInside
from harness_utils import isOnline
from model_support import ModelConnectionError, ModelError, importLibrary, recordCall
from models_library import DEFAULT_CLI_MODEL


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
CLAUDE_CODE_WRITERS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
CLAUDE_CODE_ACTIONS = {"Bash": ("run a command", "command"), "Write": ("create or replace a file", "file_path"), "Edit": ("change a file", "file_path"),
                       "MultiEdit": ("change a file", "file_path"), "NotebookEdit": ("change a notebook", "notebook_path"),
                       "Read": ("read a file outside its folder", "file_path"), "Glob": ("look for files outside its folder", "path"),
                       "Grep": ("search in files outside its folder", "path"), "WebFetch": ("open a web page", "url"), "WebSearch": ("search the web", "query")}


# What the rules of the mission say of a path a coding agent names (MissionMemory.ruleFor): "free" in its temp folder and for what it may do in
# the memory, "ask" anywhere else, or why it is refused. write is True for a change.
def placeRule(loop, path, write):
    session = getattr(loop, "session", None) if loop is not None else None
    if session is None or not path:
        return "ask"
    return session.memory.ruleFor(loop.name, path, write)


# The places of the mission a coding agent may reach besides its folder.
def missionFolders(loop):
    session = getattr(loop, "session", None) if loop is not None else None
    return [str(folder) for folder in session.memory.roots().values()] if session else []


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
        return sdk.ClaudeAgentOptions(cwd=str(folder), add_dirs=missionFolders(self.loop), permission_mode="default", tools=list(CLAUDE_CODE_TOOLS), allowed_tools=list(self.runRules),
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
        field = CLAUDE_CODE_ACTIONS.get(tool, ("", None))[1]
        rule = placeRule(self.loop, data.get(field) if field and tool not in ("Bash", "WebFetch", "WebSearch") else None, tool in CLAUDE_CODE_WRITERS)
        if rule == "free":
            return types.PermissionResultAllow(updated_input=data)
        if rule != "ask":
            return types.PermissionResultDeny(message=rule)
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
