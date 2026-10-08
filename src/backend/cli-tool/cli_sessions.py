# The sessions of the command line. Each session is a daemon (swarmup_cli.py serve) that runs a swarm away from any terminal: closing the terminal
# does not stop it, only killing the session does. The file of a session, agent-files/cli-sessions/<id>.json, says where the daemon listens (a port
# of 127.0.0.1) and its token. Only the user can read it. What the daemon prints goes to <id>.log next to it.
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import harness_utils


SESSIONS_FOLDER = "cli-sessions"
START_SECONDS = 30
KILL_SECONDS = 10
REQUEST_SECONDS = 3
LOG_TAIL = 2000
ENTRY = Path(__file__).resolve().parent / "swarmup_cli.py"
SESSION_ID = re.compile(r"s(\d+)")


def sessionsFolder():
    return harness_utils.AGENT_FILES / SESSIONS_FOLDER


def sessionPath(sessionId):
    return sessionsFolder() / f"{sessionId}.json"


def logPath(sessionId):
    return sessionsFolder() / f"{sessionId}.log"


# The file is written under another name and renamed, so it is never read half written. It holds the token, so only the user can read it.
def writeSession(info):
    sessionsFolder().mkdir(parents=True, exist_ok=True)
    path = sessionPath(info["id"])
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(info, file, ensure_ascii=False)
    os.replace(temporary, path)


def removeSession(sessionId):
    sessionPath(sessionId).unlink(missing_ok=True)


def readSession(path):
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("port") and info.get("token") else None


# One request to a daemon, and its answer.
def request(info, message, timeout=REQUEST_SECONDS):
    with socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout) as connection:
        connection.sendall((json.dumps({"token": info["token"], **message}) + "\n").encode("utf-8"))
        with connection.makefile("rb") as stream:
            line = stream.readline()
    return json.loads(line) if line else None


def pidAlive(pid):
    if not pid or os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# The sessions that run, the oldest first, each with what its daemon says of itself. The file of a daemon that is gone is removed.
def listSessions():
    sessions = []
    for path in sorted(sessionsFolder().glob("*.json")) if sessionsFolder().is_dir() else []:
        info = readSession(path)
        if info is None:
            continue
        try:
            status = request(info, {"type": "status"}, timeout=2)
        except (OSError, ValueError):
            status = None
        if status is None:
            if pidAlive(info.get("pid")):
                sessions.append({**info, "state": "not answering"})
            else:
                path.unlink(missing_ok=True)
            continue
        sessions.append({**info, **status})
    return sorted(sessions, key=lambda session: session.get("started", ""))


def newSessionId():
    taken = set()
    for path in sessionsFolder().glob("s*.json"):
        match = SESSION_ID.fullmatch(path.stem)
        if match:
            taken.add(int(match.group(1)))
    number = 1
    while number in taken:
        number += 1
    return f"s{number}"


# The session the user means: its id (s2), its number (2), the start of its id, or, without anything, the only session or the latest one.
def findSession(text, sessions):
    if not sessions:
        raise ValueError("No session runs. Start one with: swarmup_cli.py new")
    if not text:
        return sessions[-1]
    text = text.strip().lower()
    wanted = f"s{text}" if text.isdigit() else text
    exact = [session for session in sessions if session["id"].lower() == wanted]
    if exact:
        return exact[0]
    found = [session for session in sessions if session["id"].lower().startswith(wanted)]
    if len(found) == 1:
        return found[0]
    raise ValueError(f"There is no session {wanted}. The sessions: {', '.join(session['id'] for session in sessions)}.")


def tailLog(sessionId):
    try:
        return logPath(sessionId).read_text(encoding="utf-8", errors="replace")[-LOG_TAIL:]
    except OSError:
        return ""


# Starts the daemon of a new session, detached from this terminal: it has a session of its own on POSIX and no console on Windows, it reads nothing,
# and it writes to its log. It returns the file of the session once the daemon answers.
def startDaemon(sessionId, missionId=None):
    sessionsFolder().mkdir(parents=True, exist_ok=True)
    options = {"stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT, "close_fds": True, "env": {**os.environ, "PYTHONUNBUFFERED": "1"}}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with open(logPath(sessionId), "wb") as log:
        process = subprocess.Popen([sys.executable, str(ENTRY), "serve", "--session", sessionId, *(["--mission", missionId] if missionId else [])], stdout=log, **options)
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        info = readSession(sessionPath(sessionId))
        if info:
            try:
                if (request(info, {"type": "status"}, timeout=2) or {}).get("type") == "status":
                    return info
            except (OSError, ValueError):
                pass
        if process.poll() is not None:
            raise RuntimeError(f"The session could not start. Its log says:\n{tailLog(sessionId)}")
        time.sleep(0.2)
    raise RuntimeError(f"The session did not answer in {START_SECONDS} seconds. Its log is {logPath(sessionId)}.")


# Ends a session. Its daemon saves a swarm that runs and leaves; if it does not answer, it is stopped by the system.
def killSession(info):
    try:
        request(info, {"type": "kill"}, timeout=REQUEST_SECONDS)
    except (OSError, ValueError):
        pass
    deadline = time.monotonic() + KILL_SECONDS
    while time.monotonic() < deadline and (sessionPath(info["id"]).exists() or pidAlive(info.get("pid"))):
        time.sleep(0.2)
    if pidAlive(info.get("pid")) or (os.name == "nt" and sessionPath(info["id"]).exists()):
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except (OSError, KeyError):
            pass
    removeSession(info["id"])
