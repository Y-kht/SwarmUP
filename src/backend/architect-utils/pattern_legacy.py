# The missions made before the journal existed (or whose journal is gone) still teach something: their calls (.swarmup/calls.jsonl), their
# conversations (.swarmup/conversations) and their saved state (agent-files/swarm-runs) are read once, as entries shaped like the entries of
# a journal (agent_journal.py), when the miner of their project is built again (pattern_miner.rebuild).
import json
from datetime import datetime

import harness_utils
from mission_memory import STATE_FOLDER, missionFolder
from project_identity import findProject
from saved_swarms import RUNS_FOLDER


def readJsonFile(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stamp(text):
    try:
        return int(datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except ValueError:
        return 0


# The missions of a project that have no journal: [(mission id, saved state, the root of the project)].
def legacyMissions(project):
    folder = harness_utils.AGENT_FILES / RUNS_FOLDER
    found = []
    for path in sorted(folder.glob("swarm_*.json")) if folder.is_dir() else []:
        state = readJsonFile(path)
        if not isinstance(state, dict) or not state.get("id"):
            continue
        members = (state.get("members") or {}).values()
        folders = [((member.get("recipe") or {}).get("answers") or {}).get("folder") for member in members]
        owner, root = findProject(folders)
        if (state.get("project") or owner) != project:
            continue
        if not (missionFolder(state["id"]) / STATE_FOLDER / "journal.jsonl").exists():
            found.append((state["id"], state, root))
    return found


def callEntries(missionId):
    path = missionFolder(missionId) / STATE_FOLDER / "calls.jsonl"
    entries = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return entries
    for line in lines:
        try:
            call = json.loads(line)
        except ValueError:
            continue
        entries.append({"t": stamp(call.get("time")), "mission": missionId, "round": call.get("round"), "agent": call.get("agent"), "kind": "call", "origin": "legacy",
                        "server": call.get("server"), "tool": call.get("tool"), "args": call.get("arguments"), "outcome": call.get("outcome"),
                        "seconds": call.get("seconds"), "size": call.get("size"), "result": call.get("note") or ""})
    return entries


def conversationEntries(missionId):
    entries = []
    for path in sorted((missionFolder(missionId) / STATE_FOLDER / "conversations").glob("*.json")):
        saved = readJsonFile(path)
        if not isinstance(saved, dict):
            continue
        agent, step = path.stem, 0
        for message in saved.get("messages") or []:
            if message.get("role") == "user":
                step = 0
                entries.append({"mission": missionId, "agent": agent, "kind": "request", "prompt": str(message.get("content") or "")[:2000]})
            elif message.get("role") == "assistant":
                step += 1
                calls = [{"id": call.get("id"), "name": call.get("name"), "args": call.get("arguments")} for call in message.get("calls") or []]
                entries.append({"mission": missionId, "agent": agent, "kind": "step", "n": step, "text": str(message.get("content") or "")[:3000],
                                "length": len(str(message.get("content") or "")), "calls": calls})
    return entries


def stateEntries(missionId, state):
    entries = [{"mission": missionId, "agent": "", "kind": "run", "agents": [{"name": name, "role": member.get("role"), "wheels": []}
                                                                             for name, member in (state.get("members") or {}).items()]}]
    for name, member in (state.get("members") or {}).items():
        if member.get("status") == "failed" and member.get("error"):
            entries.append({"mission": missionId, "agent": name, "kind": "status", "status": "failed", "error": str(member["error"])[:1500]})
    return entries


# Every entry of an old mission, with its reference: [((source, number), entry)].
def legacyEntries(missionId, state):
    entries = [*stateEntries(missionId, state), *conversationEntries(missionId), *callEntries(missionId)]
    source = f"legacy:{missionId}"
    return [((source, number), entry) for number, entry in enumerate(entries)]
