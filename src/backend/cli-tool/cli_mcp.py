# The MCP commands of the command line (swarmup_cli.py):
#   mcp list                                   the MCP servers of the user, approved or not, and the agents they are given to
#   mcp add NAME --command CMD [--args "..."]  a server of this computer (stdio), or --url URL [--header K=V] one on the network
#            [--env K=V] [--agents A,B]         its environment, and the agents that get it (every agent by default)
#   mcp approve NAME [--yes]                   connect once, show its tools and what was found in them, and approve it as it is now
#   mcp remove NAME                            remove it
#   calls MISSION [--agent A] [--server S]     every call of a tool in a mission, with how it ended (the latest last)
import shlex

import mcp_external
from mcp_records import CallRecords, describeRecords
from mcp_runtime import RUNTIME
from mission_memory import STATE_FOLDER, missionFolder


INSPECT_SECONDS = 60


def addMcpArguments(commands):
    mcp = commands.add_parser("mcp", help="the MCP servers of other programs that your agents can use (list, add, approve, remove)")
    mcp.add_argument("action", choices=("list", "add", "approve", "remove"))
    mcp.add_argument("name", nargs="?", help="the name of the server: small letters, digits and _")
    mcp.add_argument("--command", default="", help="the program that starts a server of this computer (stdio)")
    mcp.add_argument("--args", default="", help="its arguments, in one string")
    mcp.add_argument("--env", action="append", default=[], help="a variable of its environment, as KEY=VALUE (several times for several)")
    mcp.add_argument("--url", default="", help="the address of a server on the network (Streamable HTTP)")
    mcp.add_argument("--header", action="append", default=[], help="a header to send to it, as KEY=VALUE")
    mcp.add_argument("--agents", default="all", help="the agents that get it, like Writer,Coder (every agent by default)")
    mcp.add_argument("--yes", action="store_true", help="approve without asking (only after you read its tools)")
    calls = commands.add_parser("calls", help="every call of a tool in a mission: its time, agent, tool, how long it took and how it ended")
    calls.add_argument("mission", help="its number in the history (1 is the latest), or its id")
    calls.add_argument("--agent", default=None)
    calls.add_argument("--server", default=None)
    calls.add_argument("--lines", type=int, default=200)


def pairs(items):
    found = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"{item} is not KEY=VALUE.")
        found[key.strip()] = value
    return found


def describeServer(name, config):
    where = f"{config['command']} {' '.join(config.get('args') or [])}".strip() if config["transport"] == "stdio" else config["url"]
    agents = "every agent" if config.get("agents") == "all" else ", ".join(config.get("agents") or []) or "no agent"
    state = f"approved, {len(config.get('pins') or {})} tools" if config.get("approved") else "NOT approved yet: swarmup_cli.py mcp approve " + name
    return f"{name:<16} {config['transport']:<5} {state:<28} for {agents}\n                 {where}"


def approve(name, confirmed, ask=input):
    servers = mcp_external.loadServers()
    if name not in servers:
        raise ValueError(f"There is no server called {name}. See them with: swarmup_cli.py mcp list")
    tools = RUNTIME.call(mcp_external.inspectServer(servers[name]), INSPECT_SECONDS)
    print(mcp_external.describeInspection(name, tools))
    warned = any(tool["findings"] for tool in tools)
    if warned:
        print("\nSome tools say things a tool should not say. Approve the server only if you trust it.")
    if not confirmed and ask(f"Approve {name}, with these tools as they are now? (y/N) ").strip().lower() not in ("y", "yes"):
        print("Not approved: its tools are not given to the agents.")
        return False
    mcp_external.approveServer(name, tools)
    print(f"{name} is approved. If one of its tools changes, it is hidden until you approve the server again.")
    return True


def runMcp(options, ask=input):
    if options.action == "list":
        servers = mcp_external.loadServers()
        print("\n".join(describeServer(name, config) for name, config in servers.items()) if servers else
              "No MCP server yet. Add one with: swarmup_cli.py mcp add NAME --command ... (or --url ...)")
        return
    if not options.name:
        raise ValueError(f"Say which server: swarmup_cli.py mcp {options.action} NAME")
    if options.action == "remove":
        mcp_external.removeServer(options.name)
        print(f"{options.name} is removed.")
    elif options.action == "add":
        agents = "all" if options.agents.strip() in ("", "all") else [name.strip() for name in options.agents.split(",") if name.strip()]
        mcp_external.addServer(options.name, "http" if options.url else "stdio", options.command, shlex.split(options.args), pairs(options.env), options.url,
                               pairs(options.header), agents)
        print(f"{options.name} is added. Nothing is given to the agents before you approve it:")
        approve(options.name, options.yes, ask)
    else:
        approve(options.name, options.yes, ask)


def showCalls(mission, agent=None, server=None, lines=200):
    records = CallRecords(missionFolder(mission["id"]) / STATE_FOLDER)
    print(describeRecords(records.read(agent, server, lines)))
    counts = records.countsOf()
    if counts:
        print("\n" + "\n".join(f"{name}: {count['calls']} calls, {count['errors']} errors, {count['refused']} refused, {count['cancelled']} stopped, "
                                f"{count['timeout']} too long, {count['seconds']:.1f} s" for name, count in counts.items()))
