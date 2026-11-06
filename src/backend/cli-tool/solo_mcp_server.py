# Solo as an MCP server (cli_solo.py): a model that runs on this computer works as the only agent of a swarm, with the tools of SwarmUP as its own
# tools, with their schemas. Start it with: swarmup_cli.py solo-mcp (stdio). For example, with Claude Code:
#   claude mcp add swarmup-solo -- python /path/to/swarm-up/src/backend/cli-tool/swarmup_cli.py solo-mcp
# It has the tools that lead the session (solo_...), and the tools of the turn that waits, which change with the work: a wheel that comes, or the
# tools of the architect when the work is done. It tells the client when they change.
import anyio.to_thread
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from cli_sessions import findSession, killSession, listSessions
from cli_solo import APPROVALS, DECISIONS, ask, renderView, startMission


TEXT = {"type": "string"}
INSTRUCTIONS = """SwarmUP gives you its tools, its wheels and its records for one mission. Ask your user how your actions must be approved, then call solo_start.
Work with the tools it gives (files__, transfer__, processes__, memory__, wheels__): only what you do with them teaches SwarmUP what repeats. Then give your result with
solo_answer. If patterns repeat, your next turn is then the work of an architect: you write wheels, which you get as tools in the next missions."""
LEADING_TOOLS = [
    types.Tool(name="solo_start", description="Start a mission of one agent: you. How your actions are approved is for your user to choose, so ask them first: " +
               "; ".join(f"{key}: {text}" for key, text in APPROVALS.items()) + ".", inputSchema={"type": "object", "required": ["mission", "approvals"], "properties": {
                   "mission": {"type": "string", "description": "What must be done."}, "approvals": {"type": "string", "enum": list(APPROVALS)},
                   "folder": {"type": "string", "description": "The folder of the work, as an absolute path."},
                   "plan": {"type": "boolean", "description": "Only plan in this round: nothing changes until the plan is done."},
                   "rules": {"type": "array", "items": TEXT, "description": "The rules files to follow, like Coder."}}}),
    types.Tool(name="solo_open", description="Continue a mission of the history: it goes on where it stopped, or waits for your next request (solo_next).",
               inputSchema={"type": "object", "required": ["mission"], "properties": {"mission": {"type": "string", "description": "Its id."}}}),
    types.Tool(name="solo_answer", description="Give the answer of this turn: your result, or your report as the architect. It is saved as the result of the mission.",
               inputSchema={"type": "object", "required": ["text"], "properties": {"text": TEXT}}),
    types.Tool(name="solo_decide", description="Decide about the card that waits, when your user chose the approvals model. forever: this version of a wheel, from now on.",
               inputSchema={"type": "object", "required": ["decision"], "properties": {"decision": {"type": "string", "enum": list(DECISIONS)}, "why": TEXT}}),
    types.Tool(name="solo_next", description="When a round is over: the next request of the mission. execute: do the plan of the round before.",
               inputSchema={"type": "object", "properties": {"text": TEXT, "execute": {"type": "boolean"}}}),
    types.Tool(name="solo_status", description="The turn that waits, and where the session stands. It waits for a turn when the agent is still at work.",
               inputSchema={"type": "object", "properties": {}}),
    types.Tool(name="solo_close", description="Close the session. The mission stays in the history.", inputSchema={"type": "object", "properties": {}}),
]


class SoloServer:
    def __init__(self):
        self.info, self.turnTools = None, []
        self.mcp = Server("swarmup-solo", version="1.0.0", instructions=INSTRUCTIONS)
        self.mcp.list_tools()(self.listTools)
        self.mcp.call_tool(validate_input=False)(self.callTool)

    async def listTools(self):
        return LEADING_TOOLS + [types.Tool(name=tool["name"], description=tool.get("description") or "", inputSchema=tool.get("parameters") or {"type": "object", "properties": {}})
                                for tool in self.turnTools]

    async def callTool(self, name, arguments):
        text, failed = await anyio.to_thread.run_sync(self.run, name, arguments or {})
        changed = await anyio.to_thread.run_sync(self.readTools)
        if changed:
            await self.mcp.request_context.session.send_tool_list_changed()
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=failed)

    def session(self):
        if self.info is None:
            sessions = listSessions()
            if not sessions:
                raise ValueError("No mission is open. Ask your user how your actions must be approved, then call solo_start.")
            self.info = findSession("", sessions)
        return self.info

    # The tools of the turn that waits. Between two turns the model keeps the tools it had, unless the session has no turn to come.
    def readTools(self):
        if self.info is None:
            return False
        try:
            view = ask(self.info, "view")
            if not view.get("turn") and view.get("state") not in ("over", "ended"):
                return False
            tools = ask(self.info, "tools").get("tools") or []
        except (OSError, ValueError):
            tools = []
        changed = [tool["name"] for tool in tools] != [tool["name"] for tool in self.turnTools]
        self.turnTools = tools
        return changed

    def run(self, name, arguments):
        try:
            return self.work(name, arguments)
        except (OSError, ValueError) as error:
            return str(error), True

    def work(self, name, arguments):
        if name == "solo_start":
            self.info, view = startMission(arguments.get("mission"), arguments.get("folder") or "", self.checkApprovals(arguments), arguments.get("plan"), arguments.get("rules"))
        elif name == "solo_open":
            self.info, view = startMission(opened=str(arguments.get("mission") or ""))
        elif name == "solo_close":
            killSession(self.session())
            self.info, self.turnTools = None, []
            return "The session is closed. The mission stays in the history.", False
        elif name == "solo_answer":
            view = ask(self.session(), "answer", text=arguments.get("text"))
        elif name == "solo_decide":
            view = ask(self.session(), "decide", decision=arguments.get("decision"), why=arguments.get("why"))
        elif name == "solo_next":
            view = ask(self.session(), "next", text=arguments.get("text"), execute=bool(arguments.get("execute")))
        elif name == "solo_status":
            view = ask(self.session(), "wait")
        else:
            view = ask(self.session(), "call", name=name, arguments=arguments)
        return self.render(view), view.get("type") == "error"

    def checkApprovals(self, arguments):
        if arguments.get("approvals") not in APPROVALS:
            raise ValueError("Ask your user how your actions must be approved, then give approvals: " + "; ".join(f"{key}: {text}" for key, text in APPROVALS.items()) + ".")
        return arguments["approvals"]

    # The tools are real tools here, so the text only keeps what they are for: the commands of the command line would mislead the model.
    def render(self, view):
        turn = view.get("turn")
        return renderView({**view, "turn": {**turn, "fresh": []} if turn else None}, self.info["id"] if self.info else "", "tools")


async def serveAsync():
    solo = SoloServer()
    async with stdio_server() as (reading, writing):
        await solo.mcp.run(reading, writing, solo.mcp.create_initialization_options(notification_options=NotificationOptions(tools_changed=True)))


def serveSoloMcp():
    anyio.run(serveAsync)
