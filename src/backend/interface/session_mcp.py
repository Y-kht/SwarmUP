# What the window shows of the MCP layer (docs/MCP_DESIGN_BOOKLET.md, OBS-03 and SEC-01): the record of every call of a tool in the mission,
# with the counts of each agent, and the MCP servers of other programs that the user adds, inspects, approves and removes.
import mcp_external
from interface_views import FormError
from mcp_records import CallRecords
from mcp_runtime import RUNTIME
from mission_memory import STATE_FOLDER, missionFolder


INSPECT_SECONDS = 60
SHOWN_CALLS = 300


def describeServer(name, config):
    return {"name": name, "transport": config["transport"], "where": f"{config['command']} {' '.join(config.get('args') or [])}".strip() if config["transport"] == "stdio" else config["url"],
            "agents": config.get("agents", "all"), "approved": bool(config.get("approved")), "tools": len(config.get("pins") or {})}


class SessionMcp:
    def recordsOf(self):
        swarm = self.swarm
        if swarm is None:
            return None
        gateway = getattr(swarm, "gateway", None)
        return gateway.records if gateway is not None else CallRecords(missionFolder(swarm.id) / STATE_FOLDER)

    # The calls of the mission, the latest last, for an agent or a server if asked.
    def calls(self, payload):
        records = self.recordsOf()
        if records is None:
            return {"calls": [], "counts": {}}
        return {"calls": records.read(payload.get("agent") or None, payload.get("server") or None, SHOWN_CALLS), "counts": records.countsOf()}

    def mcpServers(self, payload=None):
        return {"servers": [describeServer(name, config) for name, config in mcp_external.loadServers().items()]}

    def inspect(self, name):
        servers = mcp_external.loadServers()
        if name not in servers:
            raise ValueError(f"There is no server called {name}.")
        try:
            return RUNTIME.call(mcp_external.inspectServer(servers[name]), INSPECT_SECONDS)
        except Exception as error:
            raise ValueError(f"SwarmUP could not connect to {name}: {error}") from None

    def mcpAdd(self, payload):
        name = str(payload.get("name") or "").strip()
        transport = "http" if payload.get("transport") == "http" else "stdio"
        agents = str(payload.get("agents") or "").strip()
        try:
            mcp_external.addServer(name, transport, str(payload.get("command") or "").strip(), str(payload.get("args") or "").split(), None, str(payload.get("url") or "").strip(),
                                   None, "all" if agents in ("", "all") else [item.strip() for item in agents.split(",") if item.strip()])
        except ValueError as error:
            raise FormError({"name": str(error)}) from None
        return {"inspection": self.inspect(name), **self.mcpServers()}

    def mcpInspect(self, payload):
        return {"inspection": self.inspect(str(payload.get("name") or ""))}

    # The user approves exactly what they saw: if a tool changed since, nothing is approved.
    def mcpApprove(self, payload):
        name = str(payload.get("name") or "")
        tools = self.inspect(name)
        if sorted(tool["hash"] for tool in tools) != sorted(payload.get("hashes") or []):
            raise ValueError(f"The tools of {name} changed while you read them. Inspect it again.")
        mcp_external.approveServer(name, tools)
        return self.mcpServers()

    def mcpRemove(self, payload):
        mcp_external.removeServer(str(payload.get("name") or ""))
        return self.mcpServers()
