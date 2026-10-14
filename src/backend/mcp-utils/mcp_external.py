# The MCP servers outside SwarmUP that the user adds (docs/MCP_DESIGN_BOOKLET.md, SEC-01 to SEC-06): a server of the user's computer (stdio)
# or one on the network (Streamable HTTP), like a server for GitHub or for a database. They are kept in agent-files/mcp-servers.json.
# 1. Nothing connects before the user approves the server (approveServer). The approval lists its tools and screens what they say about
#    themselves (hidden characters, instructions to the model, words about other tools, requests for secrets), and the user decides with that.
# 2. The approval keeps a hash of each tool as it was approved. A tool that changes later (a rug pull) is hidden from the agents, and the user
#    is told, until the user approves the server again.
# 3. A server is given to every agent ("agents": "all") or to the agents the user names. Its tools are named <server>__<tool>, so they never
#    take the name of a tool of SwarmUP or of another server, and what they give back is marked as data (Gateway.markData).
import contextlib
import hashlib
import json
import os
import re
import unicodedata

from mcp_kit import NAME_SEPARATOR


# What the servers of the user's computer write about themselves goes to agent-files/mcp-logs/<server>.log, not to the screen of the user.
CONFIG_FILE = "mcp-servers.json"
LOGS_FOLDER = "mcp-logs"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
RESERVED = ("files", "transfer", "processes", "web", "memory", "team", "user", "leader", "swarmup")
INSTRUCTION_WORDS = re.compile(r"\b(ignore (all |the )?(previous|above|prior)|disregard|system prompt|you must|do not tell|don't tell|before using any other tool|"
                               r"instead of the|always call|override)\b", re.I)
SECRET_WORDS = re.compile(r"\b(password|api[ _-]?key|secret|token|credential|private key|ssh)\b", re.I)


def configPath():
    import harness_utils
    return harness_utils.AGENT_FILES / CONFIG_FILE


def loadServers():
    try:
        data = json.loads(configPath().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("servers", {}) if isinstance(data, dict) else {}


def saveServers(servers):
    path = configPath()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": servers}, indent=2, ensure_ascii=False), encoding="utf-8")


def checkName(name):
    if not NAME_PATTERN.match(name or "") or NAME_SEPARATOR in name or name in RESERVED or name.startswith("skill_"):
        return (f"{name or 'This name'} cannot be the name of a server: use small letters, digits and _, start with a letter, and do not use "
                f"{', '.join(RESERVED)} or a name that starts with skill_.")
    return ""


# Adds a server, not approved yet. transport is stdio (command, args, env) or http (url, headers). agents is "all" or a list of names.
def addServer(name, transport, command="", args=(), env=None, url="", headers=None, agents="all"):
    problem = checkName(name)
    if problem:
        raise ValueError(problem)
    if transport == "stdio" and not command:
        raise ValueError("A stdio server needs the command that starts it.")
    if transport == "http" and not str(url).startswith(("http://", "https://")):
        raise ValueError("An HTTP server needs its address, starting with http:// or https://.")
    if transport not in ("stdio", "http"):
        raise ValueError("The transport is stdio or http.")
    servers = loadServers()
    servers[name] = {"name": name, "transport": transport, "command": command, "args": list(args), "env": dict(env or {}), "url": url, "headers": dict(headers or {}),
                     "agents": agents if agents == "all" else list(agents), "approved": False, "pins": {}}
    saveServers(servers)
    return servers[name]


def removeServer(name):
    servers = loadServers()
    if servers.pop(name, None) is None:
        raise ValueError(f"There is no server called {name}.")
    saveServers(servers)


def toolHash(tool):
    data = {"name": tool.name, "description": tool.description or "", "inputSchema": tool.inputSchema,
            "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# What the user must know about a tool before trusting it (SEC-05): its findings, in words.
def screenTool(tool):
    text = f"{tool.name}\n{tool.description or ''}\n{json.dumps(tool.inputSchema, ensure_ascii=False)}"
    findings = []
    hidden = [character for character in text if unicodedata.category(character) in ("Cf", "Co", "Cn") and character not in "\n\t"]
    if hidden:
        findings.append(f"it has {len(hidden)} hidden characters")
    if INSTRUCTION_WORDS.search(text):
        findings.append(f"it gives instructions to the model: \"{INSTRUCTION_WORDS.search(text).group(0)}\"")
    if NAME_SEPARATOR in (tool.description or "") or re.search(r"\b(files|processes|team|leader|memory)__\w+", tool.description or ""):
        findings.append("it speaks about the tools of another server")
    if SECRET_WORDS.search(tool.description or ""):
        findings.append(f"it asks about a secret: \"{SECRET_WORDS.search(tool.description or '').group(0)}\"")
    if len(tool.description or "") > 2000:
        findings.append("its description is very long")
    return findings


@contextlib.asynccontextmanager
async def openSession(config, callbacks):
    from mcp_gateway import TrackedSession
    if config["transport"] == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client
        logs = configPath().parent / LOGS_FOLDER
        logs.mkdir(parents=True, exist_ok=True)
        errors = open(logs / f"{config.get('name') or 'server'}.log", "a", encoding="utf-8")
        environment = {**os.environ, **config["env"]} if config.get("env") else None
        transport = stdio_client(StdioServerParameters(command=config["command"], args=config.get("args") or [], env=environment), errlog=errors)
    else:
        from mcp.client.streamable_http import streamablehttp_client
        transport = streamablehttp_client(config["url"], headers=config.get("headers") or None)
    async with transport as streams:
        async with TrackedSession(streams[0], streams[1], **callbacks) as session:
            await session.initialize()
            yield session


# Connects to a server once and lists its tools with their findings, for the approval: [{"name", "description", "findings", "hash"}].
async def inspectServer(config):
    async with openSession(config, {}) as session:
        tools = (await session.list_tools()).tools
    return [{"name": tool.name, "description": tool.description or "", "findings": screenTool(tool), "hash": toolHash(tool)} for tool in tools]


def describeInspection(name, tools):
    lines = [f"The server {name} gives {len(tools)} {'tool' if len(tools) == 1 else 'tools'}:"]
    for tool in tools:
        lines.append(f"- {name}{NAME_SEPARATOR}{tool['name']}: {tool['description'][:300]}")
        lines += [f"    WARNING: {finding}" for finding in tool["findings"]]
    return "\n".join(lines)


# The user approved what inspectServer showed: the server can be used, with these tools as they are now.
def approveServer(name, tools):
    servers = loadServers()
    if name not in servers:
        raise ValueError(f"There is no server called {name}.")
    servers[name].update(approved=True, pins={tool["name"]: tool["hash"] for tool in tools})
    saveServers(servers)


def serversFor(loop):
    return [(name, config) for name, config in loadServers().items()
            if config.get("approved") and (config.get("agents") == "all" or getattr(loop, "name", None) in (config.get("agents") or []))]


# The tools of an approved server that are still as they were approved. The others are hidden: the list of changed tools is given too.
def pinnedTools(config, tools):
    pins = config.get("pins") or {}
    kept, changed = [], []
    for tool in tools:
        (kept if pins.get(tool.name) == toolHash(tool) else changed).append(tool)
    return kept, [tool.name for tool in changed]
