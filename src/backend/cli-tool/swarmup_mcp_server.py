# SwarmUP as an MCP server (docs/MCP_DESIGN_BOOKLET.md, EXT-01 and EXT-02): another AI agent (Claude Code, for example) starts, leads and
# follows the swarms of SwarmUP with MCP, as a person does with the command line. Start it with: swarmup_cli.py mcp-serve (stdio). For example,
# with Claude Code: claude mcp add swarmup -- python src/backend/cli-tool/swarmup_cli.py mcp-serve
# Its tools are the commands of the command line, over the sessions of SwarmUP (cli_sessions.py): a session asks its questions in its windows,
# the agent reads them (read_window) and answers them (send_line), like a person who types. So the agent can do exactly what a person can do in
# the command line, and nothing more: every permission card, every proposal and every approval still waits for an answer typed in a window.
import json

from mcp import types
from mcp.server.fastmcp import FastMCP

from cli_console import shorten
from cli_hub import SWARM_WINDOW
from cli_sessions import findSession, killSession, listSessions, newSessionId, request, startDaemon
from mcp_records import CallRecords, describeRecords
from mission_history import deleteMission, describeMission, listMissions
from mission_memory import STATE_FOLDER, missionFolder


INSTRUCTIONS = """SwarmUP builds and runs swarms of AI agents for one person. A session runs one swarm, away from any terminal.
Start a session with start_session, then read its window swarm with read_window: it asks the questions that build the swarm (or the mission for
the leader). Answer each question with send_line. While the swarm runs, each agent has a window of its own (session_status lists them); a
permission card, a draft to approve or a question waits in a window until you answer it with send_line. A follow-up is typed in the window
swarm once a round is over. To lead the swarm yourself, choose "Me" as the model of the leader when you are asked."""

server = FastMCP("swarmup", instructions=INSTRUCTIONS, log_level="WARNING")
LOOKS = types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
ACTS = types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DELETES = types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def sessionOf(session):
    return findSession(session, listSessions())


def missionOf(text):
    missions = listMissions()
    text = str(text).strip()
    if text.isdigit() and 1 <= int(text) <= len(missions):
        return missions[int(text) - 1]
    found = [mission for mission in missions if mission["id"].startswith(text)]
    if len(found) != 1:
        raise ValueError(f"There is no mission {text} in the history. See them with list_missions.")
    return found[0]


@server.tool(description="List the missions of the history, the latest first: their id, state, rounds, agents and request.", annotations=LOOKS)
def list_missions() -> str:
    missions = listMissions()
    return "\n".join(f"{number}. {mission['id']}  {describeMission(mission)}\n   {shorten(mission['mission'], 120)}" for number, mission in enumerate(missions, 1)) or \
        "The history is empty."


@server.tool(description="List the sessions that run now, with their state, their mission and whether one waits for an answer.", annotations=LOOKS)
def list_sessions() -> str:
    sessions = listSessions()
    return "\n".join(f"{session['id']}: {session.get('state', '?')}, waiting in {session.get('waiting') or 'no window'}: {shorten(session.get('mission') or '', 80)}"
                     for session in sessions) or "No session runs. Start one with start_session."


@server.tool(description="Start a new session. It asks how to build the swarm in its window swarm: read it with read_window and answer with send_line.",
             annotations=ACTS)
def start_session() -> str:
    info = startDaemon(newSessionId())
    return f"Session {info['id']} started. Read its window swarm with read_window to see its first question."


@server.tool(description="Open a mission of the history in a new session: it continues an interrupted mission, or asks for the next request of a "
             "finished one.", annotations=ACTS)
def open_mission(mission: str) -> str:
    found = missionOf(mission)
    info = startDaemon(newSessionId(), found["id"])
    return f"Session {info['id']} opened the mission {found['id']}. Read its window swarm with read_window."


@server.tool(description="Where a session stands: its state, its windows, the tree of its swarm, and the question it waits for, if any.", annotations=LOOKS)
def session_status(session: str) -> str:
    info = sessionOf(session)
    return json.dumps(request(info, {"type": "status"}), ensure_ascii=False, indent=1)


@server.tool(description="Read the latest lines of a window of a session: swarm (the default) or the name of an agent.", annotations=LOOKS)
def read_window(session: str, window: str = SWARM_WINDOW, lines: int = 120) -> str:
    info = sessionOf(session)
    text = request(info, {"type": "capture", "window": window, "lines": max(1, min(lines, 2000))}).get("text")
    return text if text is not None else f"Session {info['id']} has no window {window}."


@server.tool(description="Type one line in a window of a session, as a person does: an answer to its question, a decision on a card (yes, always, all, "
             "no), a correction, a follow-up, or a command. As the leader, a line that starts with / calls one of your tools. To answer a question, give "
             "its id (session_status shows it): the line is then refused if another question waits by then, so it never answers the wrong one.",
             annotations=ACTS)
def send_line(session: str, text: str, window: str = SWARM_WINDOW, question: int | None = None) -> str:
    info = sessionOf(session)
    reply = request(info, {"type": "input", "window": window, "text": text, "question": question})
    if reply.get("type") == "error":
        raise ValueError(reply["message"])
    return f"Typed in {window} of session {info['id']}. Read the window again to see what follows."


@server.tool(description="Every call of a tool in a mission, with its agent, how long it took and how it ended.", annotations=LOOKS)
def mission_calls(mission: str, agent: str = "", tool_server: str = "") -> str:
    found = missionOf(mission)
    return describeRecords(CallRecords(missionFolder(found["id"]) / STATE_FOLDER).read(agent or None, tool_server or None))


@server.tool(description="Close a session. A swarm that runs is saved, and can be continued later with open_mission.", annotations=ACTS)
def close_session(session: str) -> str:
    info = sessionOf(session)
    killSession(info)
    return f"Session {info['id']} is closed."


@server.tool(description="Delete a mission of the history for good, with its memory and its temp folder. What its agents saved in the folders of the "
             "user stays. A mission open in a session must be closed first.", annotations=DELETES)
def delete_mission(mission: str) -> str:
    found = missionOf(mission)
    if any(found["id"] in (session.get("missionId"), session.get("opened")) for session in listSessions()):
        raise ValueError(f"The mission {found['id']} is open in a session. Close that session first.")
    deleteMission(found["id"])
    return f"Deleted the mission {found['id']}, with its memory and its temp folder."


def serveMcp():
    server.run("stdio")
