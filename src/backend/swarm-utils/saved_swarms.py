import json
import os
from datetime import datetime

from harness_utils import AGENT_FILES, STATE_VERSION


# The state of a running swarm is saved in RUNS_FOLDER (inside agent-files) at every change, and also every HEARTBEAT_SECONDS.
# A saved swarm that has not been written for STALE_SECONDS is not running anymore. STOP_TIMEOUT is how long a stopped swarm waits for its agents.
RUNS_FOLDER = "swarm-runs"

STALE_SECONDS = 30


# ==============
# Saved swarms. While a swarm runs, its state is written to RUNS_FOLDER (inside agent-files) at every change and every few seconds,
# so a swarm that was interrupted (internet lost, computer turned off...) can go on later. Passwords, API keys and accounts are never saved.
# ==============
def swarmStatePath(swarmId):
    return AGENT_FILES / RUNS_FOLDER / f"swarm_{swarmId}.json"


# The file is written under another name and then renamed, so a computer that stops in the middle of a write never leaves a broken file.
def saveSwarmState(swarmId, state):
    path = swarmStatePath(swarmId)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        file.write(json.dumps(state, indent=2, ensure_ascii=False, default=str))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def clearSwarmState(swarmId):
    swarmStatePath(swarmId).unlink(missing_ok=True)


# The swarms that did not end, newest first: their saved states, with running: True if another program is working on one right now.
# A file that cannot be read is left alone. The swarm of this very program is not listed, because it is not interrupted.
def findUnfinishedSwarms():
    found = []
    for path in (AGENT_FILES / RUNS_FOLDER).glob("swarm_*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["version"] != STATE_VERSION or not state["members"] or not isinstance(state["members"], dict):
                continue
            alive = state["state"] in ("running", "paused") and datetime.now().timestamp() - state["heartbeat"] < STALE_SECONDS
            if alive and state["pid"] == os.getpid():
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        state["running"] = alive
        found.append(state)
    return sorted(found, key=lambda state: state["heartbeat"], reverse=True)
