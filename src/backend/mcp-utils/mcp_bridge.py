# The bridge of the coding agents (docs/MCP_DESIGN_BOOKLET.md, ADP-06). Claude Code and Codex run their own loop: they call tools themselves.
# The bridge gives them the tools of SwarmUP as one MCP server ("swarmup") over Streamable HTTP, on 127.0.0.1 only, and each turn of an agent
# gets a token of its own (SEC-08): the server lists exactly the tools the agent has now, and runs each call through its link to the gateway
# (runTool), so its permissions, its records and its events are those of every other agent. A request without a known token is refused.
import contextlib
import secrets
import socket
import threading
import time

import anyio
import anyio.to_thread
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route


BRIDGE_NAME = "swarmup"
START_SECONDS = 15
PATH = "/mcp"


class Offer:
    def __init__(self, token, tools, runTool):
        self.token, self.tools, self.runTool = token, tools, runTool


def bearerOf(headers):
    value = dict(headers).get(b"authorization", b"").decode("latin-1")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


class Endpoint:
    def __init__(self, bridge):
        self.bridge = bridge

    async def __call__(self, scope, receive, send):
        if bearerOf(scope.get("headers") or []) not in self.bridge.offers:
            await PlainTextResponse("This token is not known to SwarmUP.", status_code=401)(scope, receive, send)
            return
        await self.bridge.manager.handle_request(scope, receive, send)


class McpBridge:
    def __init__(self):
        self.offers, self.lock = {}, threading.Lock()
        self.server, self.port, self.thread = None, None, None
        self.mcp = Server(BRIDGE_NAME, version="1.0.0", instructions="The tools of SwarmUP for this agent. Every action goes through the permissions of the user.")
        self.mcp.list_tools()(self.listTools)
        self.mcp.call_tool(validate_input=False)(self.callTool)
        self.manager = None

    def offerOf(self):
        request = self.mcp.request_context.request
        return self.offers.get(bearerOf(request.scope.get("headers") or []) if request is not None else "")

    async def listTools(self):
        offer = self.offerOf()
        return [types.Tool(name=tool["name"], description=tool["description"], inputSchema=tool["parameters"]) for tool in (offer.tools if offer else [])]

    async def callTool(self, name, arguments):
        offer = self.offerOf()
        if offer is None:
            return types.CallToolResult(content=[types.TextContent(type="text", text="Not done: this turn is over.")], isError=True)
        text, error = await anyio.to_thread.run_sync(lambda: offer.runTool(name, arguments or {}), abandon_on_cancel=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)

    def start(self):
        with self.lock:
            if self.server is not None and self.thread.is_alive():
                return
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]

            @contextlib.asynccontextmanager
            async def lifespan(app):
                async with self.manager.run():
                    yield
            self.manager = StreamableHTTPSessionManager(app=self.mcp, json_response=True, stateless=True)
            app = Starlette(routes=[Route(PATH, endpoint=Endpoint(self), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
            self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"))
            self.thread = threading.Thread(target=self.server.run, name="swarmup-mcp-bridge", daemon=True)
            self.thread.start()
            deadline = time.monotonic() + START_SECONDS
            while not self.server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            if not self.server.started:
                raise RuntimeError("The bridge of the tools of SwarmUP did not start.")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}{PATH}"

    # Gives tools to one turn of a coding agent. token: a token the agent keeps (Codex starts once with its token), or a new one.
    @contextlib.contextmanager
    def offer(self, tools, runTool, token=None):
        self.start()
        token = token or secrets.token_urlsafe(24)
        self.offers[token] = Offer(token, tools, runTool)
        try:
            yield self.url, token
        finally:
            self.offers.pop(token, None)


BRIDGE = McpBridge()
