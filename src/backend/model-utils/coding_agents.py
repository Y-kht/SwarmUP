# The coding agents (Claude Code and Codex) think for an agent like a model. They run their own loop, so they are adapters that run their
# tools themselves (runsTools, model_adapters.py): the tools of SwarmUP reach them through the bridge (mcp_bridge.py), as one MCP server, and
# every action they want to do with their own tools goes to the policy of the gateway (mcp_policy.py), like the actions of every other agent.
# This module has what both need, and Claude Code. Codex is in codex_agent.py.
import asyncio
import contextlib
import importlib.util
import json
import threading

import harness_utils
# Where a coding agent works, and what is inside its folder, are part of the storehouse of the agents (agent_storehouse.py).
from agent_storehouse import WORKSPACES_FOLDER, agentWorkspace, isInside
from harness_utils import isOnline
from mcp_bridge import BRIDGE, BRIDGE_NAME
from mcp_policy import decidePermission
from model_support import ModelConnectionError, ModelError, importLibrary, recordCall
from models_library import DEFAULT_CLI_MODEL


# ==============
# Coding agents: Claude Code and Codex, which run on the computer of the user. The loop gives itself to them (attach), so they work in its
# folder (or in a folder of their own, empty, if it has none). They change files, copy, run commands and talk with the swarm with the tools of
# SwarmUP (the server swarmup, through the bridge). What they still do with their own tools (Claude Code only reads; Codex also runs commands)
# goes to the policy of the gateway: reading inside the folder and the free places needs no permission, every other action shows the user a
# card, and what the user allows "until the swarm runs again" is remembered in the loop (loop.allowed), for every agent kind the same way.
# ==============
CODING_AGENT_RULES = """You work as one agent of a swarm run by SwarmUP, a program that asks its user before anything is done.
Reply with exactly what the request asks for (a text, a plan, an email, the content of a file...) and nothing else: SwarmUP shows your reply to the user and acts on it only after the user approves it.
Use the tools of the MCP server swarmup to change files, copy, run commands, write to the other agents and ask the user: they keep a backup of
every change and show each action to the user, who may refuse it: then go on without it. Do not create, change or delete files unless the
request asks for it: SwarmUP saves the approved result itself."""


# Without a loop (a test of the connection), nobody can be asked, so every action is refused. Otherwise the policy of the gateway decides.
def askPermission(loop, request):
    if loop is None:
        return {"decision": "deny", "message": "Nobody can approve actions during a test of the connection."}
    # A request without a key is only remembered for itself: the same action and the same detail.
    answer = decidePermission(loop, {"key": f"{request.get('action')}:{request.get('detail') or ''}", **request})
    return answer if isinstance(answer, dict) and answer.get("decision") in ("once", "run", "deny") else {"decision": "deny", "message": ""}


def askQuestions(loop, questions):
    answer = loop.askQuestions(questions) if loop is not None else {}
    return answer if isinstance(answer, dict) else {}


# ---------- Claude Code, through its official Python library (claude-agent-sdk), which contains Claude Code itself. ----------
# It is only used with an Anthropic API key, because Anthropic does not allow third-party products to use a Claude subscription. To be sure
# of it, the other credentials of the environment are blanked, Claude Code gets a configuration folder of its own (so it finds no login of
# the user), and the run stops if Claude Code says it uses anything else than the key. The settings files of the folder are not loaded
# (setting_sources=[]), so a project cannot allow commands on its own. Claude Code retries a refused key for minutes: the first refusal ends it.
# Claude Code keeps only its tools that read: everything else goes through the tools of SwarmUP (mcp__swarmup__...), which the gateway decides on.
CLAUDE_CODE_TOOLS = ["Read", "Glob", "Grep"]
CLAUDE_CODE_CREDENTIALS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
CLAUDE_CODE_HOME = "claude-code-home"
REFUSED_STATUSES = (401, 403)
# What Claude Code wants to do with each tool, in words, and the field of its input that says what exactly.
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


class ClaudeCodeModel:
    runsTools = True

    def __init__(self, name, apiKey, report=None):
        self.name = name or DEFAULT_CLI_MODEL
        self.apiKey = apiKey
        self.report = report or (lambda message: None)
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.loop = None
        self.client = None
        self.lock = threading.Lock()

    def attach(self, loop):
        self.loop = loop

    def newRun(self):
        pass

    def options(self, sdk, folder, bridge=None):
        environment = {**{name: "" for name in CLAUDE_CODE_CREDENTIALS}, "ANTHROPIC_API_KEY": self.apiKey,
                       "CLAUDE_CONFIG_DIR": str(harness_utils.AGENT_FILES / CLAUDE_CODE_HOME)}
        servers = {BRIDGE_NAME: {"type": "http", "url": bridge[0], "headers": {"Authorization": f"Bearer {bridge[1]}"}}} if bridge else {}
        return sdk.ClaudeAgentOptions(cwd=str(folder), add_dirs=missionFolders(self.loop), permission_mode="default", tools=list(CLAUDE_CODE_TOOLS),
                                      allowed_tools=[f"mcp__{BRIDGE_NAME}"] if bridge else [], mcp_servers=servers, setting_sources=[], can_use_tool=self.decide,
                                      env=environment, model=None if self.name == DEFAULT_CLI_MODEL else self.name,
                                      system_prompt={"type": "preset", "preset": "claude_code", "append": CODING_AGENT_RULES},
                                      extra_args={"no-session-persistence": None})

    # Claude Code asks before it uses a tool. A tool of SwarmUP is decided by the gateway when it runs. Its own tools only read: inside the
    # folder and the free places they need nothing, elsewhere the policy decides, as for every agent.
    async def decide(self, tool, data, context):
        types = importlib.import_module("claude_agent_sdk.types")
        if tool.startswith(f"mcp__{BRIDGE_NAME}__"):
            return types.PermissionResultAllow(updated_input=data)
        if tool not in CLAUDE_CODE_TOOLS:
            return types.PermissionResultDeny(message=f"{tool} is not used in SwarmUP: use the tools of the server {BRIDGE_NAME}.")
        field = CLAUDE_CODE_ACTIONS.get(tool, ("", None))[1]
        path = data.get(field) if field else None
        rule = placeRule(self.loop, path, False)
        if rule == "free" or (path and self.loop is not None and isInside(agentWorkspace(self.loop), path)):
            return types.PermissionResultAllow(updated_input=data)
        if rule != "ask":
            return types.PermissionResultDeny(message=rule)
        request = {**describeClaudeCodeRequest(tool, data, agentWorkspace(self.loop)), "key": f"read:{path or json.dumps(data, sort_keys=True)}", "readOnly": True}
        answer = await asyncio.to_thread(askPermission, self.loop, request)
        if answer["decision"] == "deny":
            return types.PermissionResultDeny(message=f"The user refused this. {answer.get('message') or ''}".strip())
        return types.PermissionResultAllow(updated_input=data)

    # One step of the core loop is one turn of Claude Code, with the conversation so far, and the tools of SwarmUP through the bridge.
    def converse(self, system, messages, tools, onText=None, runTool=None):
        from model_adapters import renderPrompt
        prompt = renderPrompt(system, messages, [])
        if not tools or runTool is None:
            return {"text": self.input(prompt), "calls": [], "raw": None, "cut": False}
        with BRIDGE.offer(tools, runTool) as bridge:
            return {"text": self.input(prompt, bridge), "calls": [], "raw": None, "cut": False}

    def cancel(self):
        client = self.client
        if client is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(client.interrupt(), self.turnLoop)

    async def runTurn(self, prompt, bridge=None):
        sdk = importLibrary("claude_agent_sdk")
        folder = agentWorkspace(self.loop)
        (harness_utils.AGENT_FILES / CLAUDE_CODE_HOME).mkdir(parents=True, exist_ok=True)
        result, retries = None, set()
        async with sdk.ClaudeSDKClient(self.options(sdk, folder, bridge)) as client:
            self.client, self.turnLoop = client, asyncio.get_running_loop()
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

    def input(self, prompt, bridge=None):
        with self.lock:
            try:
                return asyncio.run(self.runTurn(prompt, bridge))
            except ModelError:
                raise
            except Exception as error:
                if not isOnline():
                    raise ModelConnectionError("Claude Code cannot reach Anthropic: the internet connection is lost.") from error
                raise ModelError(f"Claude Code failed: {type(error).__name__}: {error}") from error
            finally:
                self.client = None
