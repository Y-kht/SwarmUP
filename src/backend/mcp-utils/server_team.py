# The team server: the agent works with the other agents of its swarm (swarm_team.py does the work): it writes to them, sees who does what,
# and reads their results. The leader also reads the records of every call of its mission (CRD-03).
from agent_tools import hasTeam
from mcp_kit import ResourceSpec, ServerSpec, ToolSpec
from mcp_records import describeRecords


VERSION = "1.0.0"


def isLeader(agent):
    team = getattr(agent.loop, "team", None)
    return team is not None and getattr(team, "leader", None) == agent.name


def teamResources(agent):
    team = getattr(agent.loop, "team", None)
    if team is None:
        return []
    base = f"swarmup://{team.id}/team"
    found = [ResourceSpec(f"{base}/status", "Team status", "The agents of the swarm, their tasks and where they stand.", lambda: team.describeTeamFor(agent.name))]
    found += [ResourceSpec(f"{base}/results/{name}", f"Result of {name}", f"The last result of {name}.", lambda name=name: team.resultOf(name))
              for name, member in list(team.members.items()) if member["result"] is not None and name != agent.name]
    if isLeader(agent) and agent.gateway is not None:
        found.append(ResourceSpec(f"swarmup://{team.id}/calls", "Calls of the mission", "Every call of a tool by the agents of the mission, the latest last.",
                                  lambda: describeRecords(agent.gateway.records.read())))
    return found


TOOLS = [
    ToolSpec("send_message", "Send a message to another agent of your swarm, or to your leader (write leader as to). It reads it with its next step. "
             "Use it to give them what they need from you, or to ask them something.", {"to": {"type": "string"}, "text": {"type": "string"}}, ["to", "text"],
             run=lambda work, to, text: work.loop.team.relay(work.loop.name, to, text), readOnly=True, when=hasTeam),
    ToolSpec("read_team", "See the agents of your swarm: their roles, their tasks, where they stand, and whose result is ready.",
             run=lambda work: work.loop.team.describeTeamFor(work.loop.name), readOnly=True, idempotent=True, when=hasTeam),
    ToolSpec("read_result", "Read the result of another agent of your swarm that finished.", {"agent": {"type": "string"}}, ["agent"],
             run=lambda work, agent: work.loop.team.resultOf(agent), readOnly=True, idempotent=True, when=hasTeam),
]

SERVER = ServerSpec("team", VERSION, "The other agents of your swarm.", TOOLS, resources=teamResources)
