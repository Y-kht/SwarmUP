# The leader server: what only the leader of a swarm does, as tools with schemas, never as text that SwarmUP looks for in its answers (ADP-09,
# CRD-08). The leader proposes; SwarmUP checks every proposal (leader_checks.py) and the user approves it:
# - while the swarm is built (designSwarm, leader_utils.py), the leader proposes the whole swarm with propose_swarm, and SwarmUP shows it to the
#   user when the leader ends its turn;
# - while the swarm runs (LeaderManager, leader_manager.py), it proposes to add an agent, to remove one, or to change a model: each proposal is
#   shown to the user at once, through the gateway (Work.propose), and the leader gets the decision as the result of its call;
# - at any time it keeps what the next swarms of the user should know in the long-term memory (keep_long_term), and the user is told.
# What the leader is doing now is its desk (LeaderDesk), on its loop: the catalog of what it can choose, and the manager of the running swarm.
import json
from pathlib import Path

from mcp_kit import ServerSpec, ToolProblem, ToolSpec


VERSION = "1.0.0"
TEXT = {"type": "string"}
WHY = {"type": "string", "description": "One or two sentences for the user that say how this change helps the mission."}
MODEL = {"type": "string", "description": "A model of the list of models, exactly as it is written there."}
BITS = {"type": "integer", "enum": [16, 8, 4], "description": "For a local model: 8 or 4 compresses it to half or a quarter of its memory."}
AGENT_FIELDS = {"name": {"type": "string", "description": "One word that starts with a letter, like Writer."},
                "task": {"type": "string", "description": "agent for a general agent, or the key of a specialised task of the list."},
                "model": MODEL, "bits": BITS,
                "waits_for": {"type": "array", "items": TEXT, "description": "The agents whose results it needs before it starts."},
                "settings": {"type": "object", "description": "The settings of its task, by their key in the list of tasks (prompt and rules for a general agent)."},
                "why": WHY, "role": {"type": "string", "description": "Its role, in a few words."}}
AGENT = {"type": "object", "properties": AGENT_FIELDS, "required": ["name", "model"], "additionalProperties": False}


class LeaderDesk:
    # building: the leader builds the swarm now. manager: the LeaderManager of the running swarm.
    def __init__(self, catalog, building=False, manager=None):
        self.catalog, self.building, self.manager = catalog, building, manager
        self.proposed, self.problems = None, []


def deskOf(agent):
    return getattr(agent.loop, "leaderDesk", None)


def forLeader(agent):
    team = getattr(agent.loop, "team", None)
    return deskOf(agent) is not None or (team is not None and getattr(team, "leader", None) == agent.name)


def building(agent):
    desk = deskOf(agent)
    return desk is not None and desk.building


def managing(agent):
    desk = deskOf(agent)
    return desk is not None and desk.manager is not None and not desk.building


def keeping(agent):
    return forLeader(agent) and getattr(agent.loop, "session", None) is not None


def proposeSwarm(work, agents, why=""):
    desk = work.loop.leaderDesk
    checked, problems = desk.catalog.checkBuild({"agents": agents})
    if problems:
        desk.problems = problems
        raise ToolProblem("SwarmUP cannot show this swarm to the user yet: " + " ".join(problems) + " Call leader__propose_swarm again with the whole swarm, without these problems.")
    desk.proposed = {"agents": checked, "why": why, "raw": json.dumps({"agents": agents}, ensure_ascii=False, indent=1)}
    return "Your swarm passed the checks of SwarmUP. It is shown to the user when you end your turn: end it now, with at most three sentences for the user about the swarm."


def proposeChange(action):
    def run(work, **arguments):
        problems, outcome = work.loop.leaderDesk.manager.propose({"action": action, "data": arguments}, work.propose)
        if problems:
            raise ToolProblem(" ".join(problems))
        return outcome
    return run


def keepLongTerm(work, file, text, mode="append"):
    relative = Path(str(file).strip()).as_posix()
    if not relative.lower().endswith(".md") or relative.startswith(("/", "..")) or ".." in Path(relative).parts:
        raise ToolProblem("The long-term memory only holds .md files inside its folder, like USER_PREFERENCES.md.")
    work.loop.session.memory.write(work.loop.name, relative, str(text).strip() + "\n", long=True, append=mode != "replace")
    return f"Kept in the long-term memory, in {relative}. The user is told, and every next swarm reads it."


TOOLS = [
    ToolSpec("propose_swarm", "Propose the whole swarm for the mission: its agents, their tasks, models and settings, and who waits for whom. SwarmUP checks it, "
             "tells you what to correct, and shows it to the user when you end your turn. Only the user can approve it.",
             {"agents": {"type": "array", "items": AGENT, "minItems": 1}, "why": TEXT}, ["agents"], run=proposeSwarm, readOnly=True, when=building),
    ToolSpec("add_agent", "Propose to add one agent while the swarm works. SwarmUP checks it and shows it to the user at once, with your reason; the result tells "
             "what the user decided.", AGENT_FIELDS, ["name", "model", "why"], run=proposeChange("add"), readOnly=True, when=managing, timeout=300),
    ToolSpec("remove_agent", "Propose to remove one agent while the swarm works: it stops, what it left half done is put back, and its model frees its memory. "
             "The user decides at once; the result tells what they decided.", {"name": TEXT, "why": WHY}, ["name", "why"],
             run=proposeChange("remove"), readOnly=True, when=managing, timeout=300),
    ToolSpec("change_model", "Propose another model for an agent that has not started yet. The user decides at once; the result tells what they decided.",
             {"name": TEXT, "model": MODEL, "bits": BITS, "why": WHY}, ["name", "model", "why"], run=proposeChange("model"), readOnly=True, when=managing, timeout=300),
    ToolSpec("keep_long_term", "Keep in the long-term memory what the next swarms of this user should know: their preferences, styles, repeated requests and "
             "important notes. Never a password, a key or a detail of this mission only. mode append adds at the end of the file, replace writes it again. "
             "The user is told of each change.",
             {"file": {"type": "string", "description": "A file with a clear name in capitals, like USER_PREFERENCES.md or STYLE.md."}, "text": TEXT,
              "mode": {"type": "string", "enum": ["append", "replace"]}}, ["file", "text"], run=keepLongTerm, readOnly=True, when=keeping),
]

SERVER = ServerSpec("leader", VERSION, "What only the leader of the swarm does.", TOOLS, when=forLeader)
