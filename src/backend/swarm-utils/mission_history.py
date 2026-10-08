# The history of the missions, as the list of the chats of a chatbot. Every swarm that ran is a mission, and its saved state stays in
# agent-files/swarm-runs after it ends (saved_swarms.py): finished (the user can follow up with a new request), stopped, interrupted (it can be
# continued where it stopped), or running in a program right now. Several missions can run at the same time, in sessions of the command line and
# in the window. Deleting a mission deletes its saved state, its memory and its temp folder (mission_memory.py), after its commands that still
# run in the background are stopped. What the agents saved in the folders of the user (swarmup-results) is the user's, and stays.
import json
import os
from datetime import datetime

from background_processes import stopProcesses
from harness_utils import AGENT_FILES, STATE_VERSION
from mission_memory import clearSession, tempFolder
from saved_swarms import RUNS_FOLDER, STALE_SECONDS, UNFINISHED_STATES, clearSwarmState, swarmStatePath


def readMission(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["version"] != STATE_VERSION or not isinstance(state["members"], dict) or not state["members"]:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return state


# Whether a program works on the mission right now: its state is renewed every few seconds while it runs.
def isAlive(state):
    return state.get("state") in ("running", "paused") and datetime.now().timestamp() - state.get("heartbeat", 0) < STALE_SECONDS


# Every mission of the history, the latest first: what a list of the missions shows of it.
def listMissions():
    found = []
    for path in sorted((AGENT_FILES / RUNS_FOLDER).glob("swarm_*.json")):
        state = readMission(path)
        if state is None:
            continue
        alive = isAlive(state)
        requests = state.get("requests") or [{"round": 1, "text": state["mission"]}]
        # A program that stopped at once (the computer turned off) left its mission saved as running: it is interrupted.
        shown = "running" if alive else "interrupted" if state["state"] in ("running", "paused") else state["state"]
        found.append({"id": state["id"], "mission": state["mission"], "state": shown, "here": alive and state.get("pid") == os.getpid(),
                      "round": state.get("round", 1), "latest": requests[-1]["text"], "savedAt": state.get("savedAt", ""), "heartbeat": state.get("heartbeat", 0),
                      "leader": state["leader"], "agents": list(state["members"]), "mode": state.get("mode", "execute"),
                      "canContinue": state["state"] in UNFINISHED_STATES and not alive, "canFollowUp": state["state"] in ("finished", "stopped") and not alive,
                      "canResume": all(member.get("recipe") for member in state["members"].values())})
    return sorted(found, key=lambda mission: mission["heartbeat"], reverse=True)


# The saved state of a mission, to bring its swarm back (Swarm.restore). A mission that runs in another program is left to it.
def loadMission(missionId):
    state = readMission(swarmStatePath(missionId))
    if state is None:
        raise ValueError("This mission is not in the history anymore.")
    if isAlive(state) and state.get("pid") != os.getpid():
        raise ValueError("This mission is running in another session of SwarmUP. Go to that session, or wait until it ends.")
    return state


# Deletes a mission for good: its saved state, its memory and its temp folder. One that a program is running is refused: stop it first.
def deleteMission(missionId):
    state = readMission(swarmStatePath(missionId))
    if state is not None and isAlive(state):
        raise ValueError("This mission is running. Stop it first, then delete it.")
    if state is None and not swarmStatePath(missionId).exists():
        raise ValueError("This mission is not in the history anymore.")
    stopProcesses(missionId, temp=tempFolder(missionId))
    clearSwarmState(missionId)
    clearSession(missionId)


def describeMission(mission):
    states = {"running": "running now", "paused": "paused", "interrupted": "interrupted, can be continued", "finished": "finished, can be followed up",
              "stopped": "stopped"}
    rounds = f", round {mission['round']}" if mission["round"] > 1 else ""
    return f"{states.get(mission['state'], mission['state'])}{rounds}, {len(mission['agents'])} {'agent' if len(mission['agents']) == 1 else 'agents'}, {mission['savedAt']}"
