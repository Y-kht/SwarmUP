# What every MCP server of SwarmUP is made of (docs/MCP_DESIGN_BOOKLET.md, section 4.3). A server is described once (ServerSpec): its tools
# (ToolSpec: the name, what it does, its arguments, its hints and its time limit), its resources and its prompt templates. buildServer makes
# the MCP server of one agent from it: the server knows the agent it serves (AgentContext), so no argument ever says who calls (SES-05).
# A tool is an ordinary function of a Work, run in a worker thread: it can ask the user for a permission or for answers (elicitation, which
# the gateway receives and decides on with its policy, mcp_policy.py), tell its progress, and see that the swarm was stopped.
import json
import threading

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.shared.message import ServerMessageMetadata

from harness_utils import SwarmStopped


DEFAULT_TIMEOUT = 120
RESULT_CHARS = 60000
DESCRIPTION_CHARS = 800
NAME_SEPARATOR = "__"
DECISION_SCHEMA = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["once", "run", "deny"]}, "message": {"type": "string"}}, "required": ["decision"]}
PROPOSAL_SCHEMA = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["approve", "reject"]}, "message": {"type": "string"}}, "required": ["decision"]}
ANSWERS_SCHEMA = {"type": "object", "properties": {"answers": {"type": "string", "description": "The answers, as JSON: {question id: [answers]}."}}, "required": ["answers"]}


class ToolProblem(Exception):
    pass


def clip(text, limit=RESULT_CHARS):
    return text if len(text) <= limit else text[:limit] + f"\n[Cut: {len(text) - limit:,} more characters are not shown. Ask for a smaller part.]"


def fullName(server, tool):
    return f"{server}{NAME_SEPARATOR}{tool}"


def splitName(name):
    server, separator, tool = str(name).partition(NAME_SEPARATOR)
    return (server, tool) if separator else ("", str(name))


class ToolSpec:
    # run(work, **arguments) gives the text for the model, or (text, structured result). when(agent) says if the agent has the tool.
    def __init__(self, name, description, properties=None, required=(), run=None, readOnly=False, destructive=False, idempotent=False, openWorld=False,
                 timeout=DEFAULT_TIMEOUT, output=None, when=None, title=None):
        self.name, self.description, self.run, self.timeout, self.output, self.when = name, description, run, timeout, output, when
        self.properties, self.required, self.title = properties or {}, list(required), title
        self.hints = {"readOnlyHint": readOnly, "destructiveHint": destructive, "idempotentHint": idempotent, "openWorldHint": openWorld}

    def schema(self):
        return {"type": "object", "properties": self.properties, "required": self.required, "additionalProperties": False}

    def shown(self, agent):
        return self.when is None or bool(self.when(agent))

    def asTool(self):
        return types.Tool(name=self.name, title=self.title, description=self.description, inputSchema=self.schema(), outputSchema=self.output,
                          annotations=types.ToolAnnotations(**self.hints), _meta={"swarmup": {"timeout": self.timeout}})


class ResourceSpec:
    def __init__(self, uri, name, description, read, mimeType="text/markdown"):
        self.uri, self.name, self.description, self.read, self.mimeType = uri, name, description, read, mimeType


class PromptSpec:
    def __init__(self, name, description, text, arguments=()):
        self.name, self.description, self.text, self.arguments = name, description, text, list(arguments)


class ServerSpec:
    # resources(agent) and prompts(agent) give the lists of ResourceSpec and PromptSpec of the agent, when the server has some.
    def __init__(self, name, version, instructions, tools, resources=None, prompts=None, when=None):
        self.name, self.version, self.instructions, self.tools = name, version, instructions, list(tools)
        self.resources, self.prompts, self.when = resources, prompts, when

    def shown(self, agent):
        return self.when is None or bool(self.when(agent))


# The agent a server serves: its loop, its gateway, and the stop of its work (set when the swarm stops or the agent leaves).
class AgentContext:
    def __init__(self, loop, gateway=None):
        self.loop, self.gateway = loop, gateway
        self.cancelled = threading.Event()
        self.asking, self.lock = 0, threading.Lock()

    # While a call waits for the user (a card, a question), its time limit waits too.
    @property
    def waiting(self):
        return self.asking > 0

    def wait(self, change):
        with self.lock:
            self.asking += change

    @property
    def name(self):
        return self.loop.name


# One call of a tool, in its worker thread.
class Work:
    def __init__(self, agent, session=None, requestId=None, progressToken=None):
        self.agent, self.session, self.requestId, self.progressToken = agent, session, requestId, progressToken

    @property
    def loop(self):
        return self.agent.loop

    def elicit(self, message, schema, meta):
        if self.session is None:
            raise ToolProblem("Nobody can answer here.")
        params = types.ElicitRequestFormParams(message=message, requestedSchema=schema, _meta={"swarmup": meta})
        request = types.ServerRequest(types.ElicitRequest(params=params))
        self.agent.wait(1)
        try:
            return anyio.from_thread.run(self.session.send_request, request, types.ElicitResult, None, ServerMessageMetadata(related_request_id=self.requestId))
        finally:
            self.agent.wait(-1)

    # Asks to act: request is {action, detail, folder, reason, key, place}. The answer is {"decision": once|run|deny, "message"}.
    def permit(self, request):
        self.checkCancelled()
        result = self.elicit(f"{self.agent.name} wants to {request.get('action')}.", DECISION_SCHEMA, {"permission": request})
        content = result.content or {}
        if result.action != "accept" or content.get("decision") not in ("once", "run"):
            return {"decision": "deny", "message": str(content.get("message") or "")}
        return {"decision": content["decision"], "message": ""}

    # Shows the user a change the leader proposes (Loop.askProposal): {action, agent, why, text, summary...}. The answer is {"decision": approve|reject, "message"}.
    def propose(self, proposal):
        self.checkCancelled()
        result = self.elicit(proposal.get("text") or "A change of the swarm.", PROPOSAL_SCHEMA, {"proposal": proposal})
        content = result.content or {}
        if result.action != "accept" or content.get("decision") != "approve":
            return {"decision": "reject", "message": str(content.get("message") or "")}
        return {"decision": "approve", "message": ""}

    # Asks the user questions ({id, header, question, options, multiple, secret}); the answer is {id: [answers]}.
    def ask(self, questions):
        self.checkCancelled()
        result = self.elicit(questions[0]["question"] if questions else "", ANSWERS_SCHEMA, {"questions": questions})
        if result.action != "accept":
            return {}
        try:
            answers = json.loads((result.content or {}).get("answers") or "{}")
        except ValueError:
            return {}
        return answers if isinstance(answers, dict) else {}

    def progress(self, done, total=None, message=None):
        if self.session is not None and self.progressToken is not None:
            anyio.from_thread.run(self.session.send_progress_notification, self.progressToken, float(done), None if total is None else float(total), message)

    @property
    def cancelled(self):
        return self.agent.cancelled.is_set()

    def checkCancelled(self):
        if self.cancelled:
            raise SwarmStopped("The swarm was stopped.")


def errorResult(text):
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


# Runs a tool for the model, and turns what can go wrong into a result the model can act on (ERR-01 to ERR-03).
def runTool(tool, work, arguments):
    try:
        result = tool.run(work, **arguments)
    except SwarmStopped:
        return errorResult("Not done: the swarm was stopped.")
    except (ToolProblem, ValueError) as problem:
        return errorResult(f"Not done: {problem}")
    except OSError as error:
        return errorResult(f"Not done: {error.strerror or error}.")
    except Exception as error:
        return errorResult(f"Not done: SwarmUP met an unexpected problem in {tool.name} ({type(error).__name__}). Try another way, or tell your leader.")
    text, structured = result if isinstance(result, tuple) else (result, None)
    return types.CallToolResult(content=[types.TextContent(type="text", text=clip(str(text)))], structuredContent=structured, isError=False)


def buildServer(spec, agent):
    server = Server(spec.name, version=spec.version, instructions=spec.instructions)
    tools = {tool.name: tool for tool in spec.tools}

    @server.list_tools()
    async def listTools():
        return [tool.asTool() for tool in spec.tools if tool.shown(agent)]

    @server.call_tool(validate_input=True)
    async def callTool(name, arguments):
        tool = tools.get(name)
        if tool is None or not tool.shown(agent):
            return errorResult(f"Not done: {name} is not a tool of {spec.name} for {agent.name}.")
        context = server.request_context
        token = context.meta.progressToken if context.meta else None
        work = Work(agent, context.session, context.request_id, token)
        return await anyio.to_thread.run_sync(lambda: runTool(tool, work, arguments or {}), abandon_on_cancel=True)

    if spec.resources is not None:
        @server.list_resources()
        async def listResources():
            return [types.Resource(uri=item.uri, name=item.name, description=item.description, mimeType=item.mimeType) for item in spec.resources(agent)]

        @server.read_resource()
        async def readResource(uri):
            item = next((item for item in spec.resources(agent) if item.uri == str(uri)), None)
            if item is None:
                raise ValueError(f"There is no resource {uri} for {agent.name}.")
            text = await anyio.to_thread.run_sync(item.read)
            return [ReadResourceContents(content=text, mime_type=item.mimeType)]

    if spec.prompts is not None:
        @server.list_prompts()
        async def listPrompts():
            return [types.Prompt(name=item.name, description=item.description, arguments=[types.PromptArgument(name=name, required=False) for name in item.arguments])
                    for item in spec.prompts(agent)]

        @server.get_prompt()
        async def getPrompt(name, arguments):
            item = next((item for item in spec.prompts(agent) if item.name == name), None)
            if item is None:
                raise ValueError(f"There is no prompt {name}.")
            text = item.text(**(arguments or {})) if callable(item.text) else item.text
            return types.GetPromptResult(description=item.description, messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))])

    return server
