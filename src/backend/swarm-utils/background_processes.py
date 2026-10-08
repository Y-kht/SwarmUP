# The commands of the agents (the tools run_command, process_status and stop_process of agent_tools.py). A command runs in the folder of the
# agent or in its temp folder (mission_memory.py), always after the permission of the user, because it can change anything. It waits for its end,
# or it runs in the background while the agent works: what it prints goes to @temp/processes/<id>.log, and the agent follows it and stops it.
# A command of the background runs in a group of its own, so stopping it stops everything it started. The commands of a mission stop when its
# swarm stops running, when their agent leaves, and when the mission is deleted. Their numbers are written in the temp folder too, so a program
# that starts again can stop those of a program that ended without stopping them.
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime


COMMAND_SECONDS = 120
MAX_COMMAND_SECONDS = 600
RESULT_CHARS = 60000
STATUS_LINES = 60
STOP_SECONDS = 5
PROCESSES_FOLDER = "processes"
PIDS_FILE = "processes.json"
# The commands that run in the background in this program: {mission: {id: {"process", "agent", "command", "folder", "started", "log"}}}.
RUNNING = {}
RUNNING_LOCK = threading.Lock()


def pidsPath(temp):
    return temp / PROCESSES_FOLDER / PIDS_FILE


def savePids(missionId, temp):
    with RUNNING_LOCK:
        pids = {key: {"pid": entry["process"].pid, "agent": entry["agent"], "command": entry["command"]}
                for key, entry in RUNNING.get(missionId, {}).items() if entry["process"].poll() is None}
    try:
        pidsPath(temp).parent.mkdir(parents=True, exist_ok=True)
        pidsPath(temp).write_text(json.dumps(pids), encoding="utf-8")
    except OSError:
        pass


# Stops a command with everything it started: politely first, then by force.
def stopTree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stopEntry(entry):
    if entry["process"].poll() is None:
        stopTree(entry["process"].pid)
        try:
            entry["process"].wait(STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pass


# Stops the commands of a mission (only those of one agent, if given). With temp, also those that a program that ended left running.
def stopProcesses(missionId, agent=None, temp=None):
    with RUNNING_LOCK:
        entries = [entry for entry in RUNNING.get(missionId, {}).values() if agent is None or entry["agent"] == agent]
    for entry in entries:
        stopEntry(entry)
    if temp is not None and agent is None and pidsPath(temp).exists():
        try:
            left = json.loads(pidsPath(temp).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            left = {}
        for item in left.values():
            # A group of its own has the number of its first process: a number that went to another program since is left alone.
            if os.name != "nt" and isinstance(item, dict) and isGroup(item.get("pid")):
                stopTree(item["pid"])
        pidsPath(temp).unlink(missing_ok=True)
    return len(entries)


def isGroup(pid):
    try:
        return bool(pid) and os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def describeState(entry):
    code = entry["process"].poll()
    return "running" if code is None else f"ended with exit code {code}"


def tailOf(path, lines=STATUS_LINES):
    try:
        text = path.read_bytes()[-RESULT_CHARS:].decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


class ProcessTools:
    # The folder a command starts in: the folder of the agent, or a folder of it or of the temp folder.
    def commandFolder(self, cwd):
        if not cwd:
            return self.root
        spot = self.locate(cwd, folders=True)
        if spot.place not in ("", "@temp"):
            raise ValueError("A command runs in your folder or in @temp, not in the memory.")
        if not spot.path.is_dir():
            raise ValueError(f"{spot.name} is not a folder.")
        return spot.path

    def run_command(self, command, timeout_seconds=COMMAND_SECONDS, cwd="", background=False, reason=""):
        folder = self.commandFolder(cwd)
        if background and not self.session:
            raise ValueError("A command runs in the background only inside a swarm, which gives it a temp folder for its output.")
        detail = command + (f"\nIn: {folder}" if folder != self.root else "") + ("\n(It goes on in the background.)" if background else "")
        self.permit(f"run:{command.strip()}", "run a command", detail, reason)
        if background:
            return self.startProcess(command, folder)
        seconds = min(max(timeout_seconds, 1), MAX_COMMAND_SECONDS)
        try:
            done = subprocess.run(command, shell=True, cwd=folder, capture_output=True, text=True, errors="replace", timeout=seconds)
        except subprocess.TimeoutExpired:
            return f"The command ran for more than {seconds} seconds and was stopped. Run a long command with background."
        self.loop.logAction(f"Ran in {folder}: {command}")
        output = (done.stdout + done.stderr).strip()
        return f"Exit code {done.returncode}.\n" + (output[-RESULT_CHARS:] if output else "(it printed nothing)")

    def startProcess(self, command, folder):
        logs = self.session.temp / PROCESSES_FOLDER
        logs.mkdir(parents=True, exist_ok=True)
        with RUNNING_LOCK:
            running = RUNNING.setdefault(self.session.id, {})
            number = 1 + max([int(path.stem[1:]) for path in logs.glob("p*.log") if path.stem[1:].isdigit()] + [len(running)])
            key = f"p{number}"
            log = logs / f"{key}.log"
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            with open(log, "wb") as output:
                process = subprocess.Popen(command, shell=True, cwd=folder, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **options)
            running[key] = {"process": process, "agent": self.loop.name, "command": command, "folder": str(folder), "log": log,
                            "started": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
        savePids(self.session.id, self.session.temp)
        self.loop.logAction(f"Started in the background, in {folder}: {command}")
        return (f"Started {key} in {folder}. What it prints goes to @temp/{PROCESSES_FOLDER}/{key}.log. Follow it with process_status, stop it with "
                "stop_process. It is stopped when the swarm stops.")

    def ownProcesses(self):
        with RUNNING_LOCK:
            return {key: entry for key, entry in RUNNING.get(self.session.id, {}).items() if entry["agent"] == self.loop.name}

    def process_status(self, id=""):
        processes = self.ownProcesses()
        if not id:
            lines = [f"- {key}: {describeState(entry)}, since {entry['started']}: {entry['command']}" for key, entry in processes.items()]
            return "\n".join(lines) or "You have no command in the background."
        entry = processes.get(id.strip())
        if entry is None:
            raise ValueError(f"You have no command {id} in the background. Yours: {', '.join(processes) or 'none'}.")
        code = entry["process"].poll()
        state = "It still runs." if code is None else f"It ended with exit code {code}."
        return f"{id}: {entry['command']}\n{state} The end of what it printed:\n{tailOf(entry['log']) or '(nothing yet)'}"

    def stop_process(self, id):
        entry = self.ownProcesses().get(id.strip())
        if entry is None:
            raise ValueError(f"You have no command {id} in the background.")
        if entry["process"].poll() is not None:
            return f"{id} had already ended, with exit code {entry['process'].returncode}."
        stopEntry(entry)
        savePids(self.session.id, self.session.temp)
        self.loop.logAction(f"Stopped the command of the background {id}: {entry['command']}")
        return f"Done: {id} is stopped."
