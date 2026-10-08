# The memory of the missions. A mission is one swarm (its id is the id of the swarm), with all its rounds: the first request of the user and every
# follow-up. It has three places of its own in agent-files, apart from the folder of the user:
# 1. The short-term memory, mission-specific-memory/<mission>: Markdown files about this mission only. SwarmUP keeps in it every prompt of the
#    user (USER_PROMPTS.md), the progress (PROGRESS.md), the approved plans (PLANS.md), the approved results of every agent (outputs/) and the
#    notes of every agent (notes/). The agents write their own files there too. Every agent reads all of it, but changes or deletes only what it
#    wrote; the leader changes everything. Another mission never sees it. MISSION.md is its index. What SwarmUP needs to continue the mission (the
#    conversations, the copies of the files the agents changed, the clipboards) is in its hidden .swarmup folder.
# 2. The long-term memory, long-term-memory: Markdown files that every swarm reads when it starts (the preferences of the user, the requests
#    that come back, the styles, the important notes of the user and about the system). Only a leader writes it, and the user is told each time.
#    MEMORY.md is its index.
# 3. The temp folder, mission-temp/<mission>: the scripts, the outputs of the commands that run in the background, the notes and the copies an
#    agent needs only for a while. Nothing there is the user's, so it is changed without asking.
# Several programs (sessions of the command line, the window) can run missions at the same time, and they share the long-term memory: a file is
# always written whole under another name and then renamed, and what reads a file and writes it again holds a lock of the system on it.
import difflib
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import harness_utils
from saved_swarms import swarmStatePath


LONG_TERM_FOLDER = "long-term-memory"
MISSIONS_FOLDER = "mission-specific-memory"
TEMP_FOLDER = "mission-temp"
STATE_FOLDER = ".swarmup"
OLD_SESSIONS_FOLDER = "sessions"
SESSION_DAYS = 14
SWARMUP = "SwarmUP"
MISSION_INDEX = "MISSION.md"
LONG_TERM_INDEX = "MEMORY.md"
USER_PROMPTS = "USER_PROMPTS.md"
PROGRESS = "PROGRESS.md"
PLANS = "PLANS.md"
OUTPUTS_FOLDER = "outputs"
NOTES_FOLDER = "notes"
NOTES_CHARS = 4000
DIFF_LINES = 40
DESCRIPTION_CHARS = 100
LOCK_TRIES = 600
# What the files that SwarmUP keeps are, for the indexes.
KNOWN_FILES = {USER_PROMPTS: "every prompt of the user: the mission, the follow-ups, the corrections, the messages to the agents and the answers",
               PROGRESS: "where the mission stands: the summaries of the leader, the agents that did not finish, the final reports",
               PLANS: "the plans the user approved in plan mode, by round and agent"}
# The blocks are read even when they are not written exactly as asked: a closing tag with a typo, or none (then the next block or the end
# of the answer ends it).
MEMORY_BLOCK = re.compile(r"<\s*swarmup_memory\s+file\s*=\s*[\"']?([^\"'>\s]+)[\"']?(?:\s+mode\s*=\s*[\"']?(\w+)[\"']?)?\s*>(.*?)"
                          r"(?:<\s*/[^>]{0,30}memory\s*>|(?=<\s*swarmup_memory\b)|\Z)", re.IGNORECASE | re.DOTALL)


class MemoryRefused(ValueError):
    pass


def longTermFolder():
    return harness_utils.AGENT_FILES / LONG_TERM_FOLDER


def missionFolder(missionId):
    return harness_utils.AGENT_FILES / MISSIONS_FOLDER / missionId


def tempFolder(missionId):
    return harness_utils.AGENT_FILES / TEMP_FOLDER / missionId


def oldSessionsFolder():
    return harness_utils.AGENT_FILES / OLD_SESSIONS_FOLDER


# The name of a file of an agent: WRITER_OUTPUTS.md for the outputs of Writer.
def agentFileName(agent, kind):
    return f"{re.sub(r'[^0-9A-Za-z]+', '_', agent or 'agent').strip('_').upper() or 'AGENT'}_{kind}.md"


# ==============
# Writing safely, also when several programs write at the same time.
# ==============
THREAD_LOCKS = {}
THREAD_LOCKS_GUARD = threading.Lock()


def threadLock(path):
    with THREAD_LOCKS_GUARD:
        return THREAD_LOCKS.setdefault(str(path), threading.RLock())


def lockHandle(handle, locked):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        if not locked:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        for attempt in range(LOCK_TRIES):
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("Another program keeps the memory locked.")
    import fcntl
    fcntl.flock(handle, fcntl.LOCK_EX if locked else fcntl.LOCK_UN)


# The lock of a folder of memory, for this program (its threads) and for the others (a lock file of the system).
@contextmanager
def folderLock(folder):
    folder = Path(folder)
    with threadLock(folder):
        (folder / STATE_FOLDER).mkdir(parents=True, exist_ok=True)
        with open(folder / STATE_FOLDER / "lock", "a+b") as handle:
            lockHandle(handle, True)
            try:
                yield
            finally:
                lockHandle(handle, False)


def writeText(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def readText(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError):
        return ""


def readJson(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def writeJson(path, value):
    writeText(path, json.dumps(value, ensure_ascii=False, indent=1))


def describeDiff(before, after, name):
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), f"{name} (before)", f"{name} (now)", lineterm="", n=1))[2:]
    shown = lines[:DIFF_LINES] + ([f"... and {len(lines) - DIFF_LINES} more lines"] if len(lines) > DIFF_LINES else [])
    return "\n".join(shown) or "(no change)"


def describeFile(path):
    if path.name in KNOWN_FILES:
        return KNOWN_FILES[path.name]
    if path.parent.name in (OUTPUTS_FOLDER, NOTES_FOLDER) and path.name.endswith(("_OUTPUTS.md", "_NOTES.md")):
        return f"the {'approved results' if path.parent.name == OUTPUTS_FOLDER else 'notes'} of one agent, by round"
    for line in readText(path).splitlines():
        text = line.strip().lstrip("#-* ").strip()
        if text:
            return text[:DESCRIPTION_CHARS] + ("..." if len(text) > DESCRIPTION_CHARS else "")
    return "(empty)"


def splitRecord(text):
    return re.split(r"(?m)^(?=## Round \d+: )", text or "")


def formatSize(size):
    return f"{size} bytes" if size < 1024 else f"{size / 1024:.1f} KB"


# The files of a folder of memory, without its index and its hidden state: [(relative path, Path)], sorted.
def isWithin(root, path):
    root = Path(root).resolve()
    return path == root or root in path.parents


def memoryFiles(folder, index):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_file() and relative != index and not any(part.startswith(".") for part in Path(relative).parts):
            found.append((relative, path))
    return found


# ==============
# The memory of one mission.
# ==============
class MissionMemory:
    def __init__(self, missionId, leader=None):
        self.id, self.leader = missionId, leader
        self.folder, self.longTerm, self.temp = missionFolder(missionId), longTermFolder(), tempFolder(missionId)
        self.state = self.folder / STATE_FOLDER
        # Called with (agent, file, the change as a diff) after every change of the long-term memory, so the user is always told.
        self.onLongTermChange = None
        for folder in (self.folder, self.longTerm, self.temp, self.state):
            folder.mkdir(parents=True, exist_ok=True)

    # ---------- Who wrote what. ----------
    def owners(self, long=False):
        return readJson((self.longTerm if long else self.folder) / STATE_FOLDER / "owners.json", {})

    def ownerOf(self, relative):
        return self.owners().get(relative)

    def setOwner(self, relative, owner, long=False):
        folder = self.longTerm if long else self.folder
        owners = self.owners(long)
        if owner is None:
            owners.pop(relative, None)
        else:
            owners[relative] = owner
        writeJson(folder / STATE_FOLDER / "owners.json", owners)

    # Whether agent may change (or delete) a file of the short-term memory (long=False) or of the long-term memory: the reason if it may not.
    def refusal(self, agent, relative, long=False):
        relative = Path(relative).as_posix()
        folder = (self.longTerm if long else self.folder).resolve()
        target = (folder / relative).resolve()
        if Path(relative).is_absolute() or (target != folder and folder not in target.parents) or target == folder:
            return f"{relative} is not a file inside the {'long-term memory' if long else 'memory of this mission'}."
        if any(part.startswith(".") for part in Path(relative).parts):
            return f"{relative} is kept by SwarmUP itself and cannot be reached."
        if relative == (LONG_TERM_INDEX if long else MISSION_INDEX):
            return f"{relative} is the index that SwarmUP writes again after every change. Change the files it lists instead."
        if long:
            return "" if agent == self.leader else "Only the leader of the swarm writes the long-term memory. Send what should be kept to the leader."
        if agent == self.leader:
            return ""
        if target.is_dir():
            others = sorted({self.ownerOf(f"{relative}/{name}") or SWARMUP for name, path in memoryFiles(target, None)} - {agent})
            return f"{relative} holds files written by {', '.join(others)}: only they or the leader can change them." if others else ""
        owner = self.ownerOf(relative)
        if owner is None and not target.exists() or owner == agent:
            return ""
        writer = owner or SWARMUP
        return f"{relative} was written by {writer}: only {writer if writer != SWARMUP else 'SwarmUP'} or the leader can change it. Write a file of your own instead, or send a message to {writer if writer != SWARMUP else 'the leader'}."

    def checkWrite(self, agent, relative, long=False):
        problem = self.refusal(agent, relative, long)
        if problem:
            raise MemoryRefused(problem)

    # What agent may do with a path of the computer that a coding agent names: "free" (in the temp folder, and what it may read or write of the
    # memory), "ask" (anywhere else: the user decides) or why it is refused.
    def ruleFor(self, agent, path, write):
        path = Path(path).expanduser().resolve()
        if isWithin(self.temp, path):
            return "free"
        for root, long in ((self.folder, False), (self.longTerm, True)):
            if isWithin(root, path):
                relative = path.relative_to(root.resolve()).as_posix()
                if any(part.startswith(".") for part in Path(relative).parts):
                    return f"{relative} is kept by SwarmUP itself and cannot be reached."
                return (self.refusal(agent, relative, long) if write else "") or "free"
        return "ask"

    def roots(self):
        return {"@temp": self.temp, "@memory": self.folder, "@long-term": self.longTerm}

    # ---------- Changing the memory: always through these, so the owners, the indexes and the user stay right. ----------
    # Writes a whole file for agent (a file it creates becomes its own) and gives back what changed. append adds the text at its end.
    def write(self, agent, relative, text, long=False, append=False, owner=None):
        relative = Path(relative).as_posix()
        folder = self.longTerm if long else self.folder
        with folderLock(folder):
            if agent != SWARMUP:
                self.checkWrite(agent, relative, long)
            path = folder / relative
            before = readText(path)
            after = before + ("" if not before or before.endswith("\n") else "\n") + text if append else text
            writeText(path, after)
            if long:
                self.setOwner(relative, f"{agent} of mission {self.id}", long)
            elif owner or not self.ownerOf(relative):
                self.setOwner(relative, owner or agent)
            self.writeIndex(long)
        change = describeDiff(before, after, relative)
        if long and self.onLongTermChange:
            self.onLongTermChange(agent, relative, change)
        return change

    def delete(self, agent, relative, long=False):
        relative = Path(relative).as_posix()
        folder = self.longTerm if long else self.folder
        with folderLock(folder):
            self.checkWrite(agent, relative, long)
            path = folder / relative
            before = readText(path) if path.is_file() else ""
            (shutil.rmtree if path.is_dir() else Path.unlink)(path)
            for name in [name for name in self.owners(long) if name == relative or name.startswith(relative + "/")]:
                self.setOwner(name, None, long)
            self.writeIndex(long)
        if long and self.onLongTermChange:
            self.onLongTermChange(agent, relative, describeDiff(before, "", relative))

    # A file or a folder that an agent made in the memory by other means (a copy): it becomes its own.
    def claim(self, agent, relative, long=False):
        folder = self.longTerm if long else self.folder
        with folderLock(folder):
            path = folder / relative
            names = [path] + (sorted(path.rglob("*")) if path.is_dir() else [])
            for item in names:
                if item.is_file():
                    self.setOwner(item.relative_to(folder).as_posix(), f"{agent} of mission {self.id}" if long else agent, long)
            self.writeIndex(long)
        if long and self.onLongTermChange:
            self.onLongTermChange(agent, Path(relative).as_posix(), "(copied in)")

    # ---------- The indexes. ----------
    def mission(self):
        return readJson(self.state / "mission.json", {})

    def setMission(self, mission, round):
        with folderLock(self.folder):
            writeJson(self.state / "mission.json", {**self.mission(), "mission": mission, "round": round})
            self.writeIndex(False)

    def writeIndex(self, long):
        folder = self.longTerm if long else self.folder
        owners = self.owners(long)
        lines = []
        for relative, path in memoryFiles(folder, LONG_TERM_INDEX if long else MISSION_INDEX):
            writer = f"last written by {owners[relative]}" if long and relative in owners else f"by {owners.get(relative, SWARMUP)}"
            lines.append(f"- {relative}: {describeFile(path)} [{writer}, {formatSize(path.stat().st_size)}]")
        if long:
            text = "# Long-term memory\nWhat every swarm reads when it starts. Only a leader changes it.\n\n" + ("\n".join(lines) or "Nothing yet.") + "\n"
            writeText(folder / LONG_TERM_INDEX, text)
            return
        mission = self.mission()
        heading = f"# Mission {self.id}\n{mission.get('mission', '')}\n\nRound {mission.get('round', 1)}. Updated {datetime.now():%Y-%m-%d %H:%M}.\n\n## Files\n"
        writeText(folder / MISSION_INDEX, heading + ("\n".join(lines) or "Nothing yet.") + "\n")

    # Written again each time, so the files a coding agent wrote with its own tools are listed too.
    def index(self, long=False):
        with folderLock(self.longTerm if long else self.folder):
            self.writeIndex(long)
        return readText((self.longTerm / LONG_TERM_INDEX) if long else (self.folder / MISSION_INDEX))

    # ---------- What SwarmUP records of the mission. ----------
    def record(self, relative, title, text, owner=None):
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        self.write(SWARMUP, relative, f"\n## {title} ({stamp})\n\n{str(text).strip()}\n", append=True, owner=owner or SWARMUP)

    # kind says what it is, and to whom when there is one: ("Message to", "Writer"), ("The mission", None).
    def addUserPrompt(self, round, text, to=None, kind="Message to"):
        self.record(USER_PROMPTS, f"Round {round}: {kind}" + (f" {to}" if to else ""), text)

    def addProgress(self, round, title, text):
        self.record(PROGRESS, f"Round {round}: {title}", text)

    def addPlan(self, round, agent, plan):
        self.record(PLANS, f"Round {round}: the plan of {agent}", plan)

    def addOutput(self, round, agent, text):
        self.record(f"{OUTPUTS_FOLDER}/{agentFileName(agent, 'OUTPUTS')}", f"Round {round}: the result of {agent}", text)

    def notesName(self, agent):
        return f"{NOTES_FOLDER}/{agentFileName(agent, 'NOTES')}"

    def addNotes(self, agent, notes):
        lines = "".join(f"- [{datetime.now():%Y-%m-%d %H:%M}] " + note.replace("\n", "\n  ") + "\n" for note in notes)
        if lines:
            self.write(SWARMUP, self.notesName(agent), lines, append=True, owner=agent)

    # The latest notes only, so a long mission does not fill the prompts.
    def readNotes(self, agent):
        text = readText(self.folder / self.notesName(agent))
        return text if len(text) <= NOTES_CHARS else "...\n" + text[-NOTES_CHARS:]

    # The part of a record about one round, for example the prompts of the user in this round. Only the headings of SwarmUP split it: a text
    # recorded in it can have headings of its own.
    def readRound(self, relative, round):
        return "".join(section for section in splitRecord(readText(self.folder / relative)) if section.startswith(f"## Round {round}:")).strip()

    # ---------- The leader updates the long-term memory with blocks. ----------
    # <swarmup_memory file="USER_PREFERENCES.md" mode="append">...</swarmup_memory>. It gives back the files changed, and the problems. An
    # answer that is neither NOTHING nor a block that can be read is a problem too, so the user knows the update was lost.
    def applyBlocks(self, agent, text):
        changed, problems = [], []
        blocks = MEMORY_BLOCK.findall(text or "")
        if not blocks and (text or "").strip() and not (text or "").strip().upper().startswith("NOTHING"):
            problems.append("its answer had no <swarmup_memory> block that SwarmUP could read")
        for name, mode, content in blocks:
            relative = Path(name.strip()).as_posix()
            if not relative.lower().endswith(".md") or relative.startswith(("/", "..")) or ".." in Path(relative).parts:
                problems.append(f"{name}: the long-term memory only holds .md files inside its folder.")
                continue
            try:
                self.write(agent, relative, content.strip() + "\n", long=True, append=(mode or "append").lower() != "replace")
                changed.append(relative)
            except (MemoryRefused, OSError) as error:
                problems.append(f"{name}: {error}")
        return changed, problems


# ==============
# The memory of a swarm while it runs: its mission memory, its temp folder, the name of the run and the round, and the folder where SwarmUP keeps
# what it needs to continue (the conversations, the copies of the changed files, the workspaces of the coding agents without a folder).
# ==============
class WorkSession:
    def __init__(self, sessionId, runName, round=1, leader=None):
        self.id, self.runName, self.round = sessionId, runName, round
        pruneSessions(keep=sessionId)
        self.memory = MissionMemory(sessionId, leader)
        self.folder, self.temp = self.memory.state, self.memory.temp
        moveOldSession(sessionId, self.memory)
        os.utime(self.memory.folder)

    def addNotes(self, agent, notes):
        self.memory.addNotes(agent, notes)

    def readNotes(self, agent):
        return self.memory.readNotes(agent)

    def workspace(self, agent):
        folder = self.folder / "workspace" / re.sub(r"[^\w.-]", "_", agent or "agent")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def clear(self):
        clearSession(self.id)


# A swarm saved before the mission memory existed kept its session in agent-files/sessions/<id>: it moves into the mission, so it goes on.
def moveOldSession(sessionId, memory):
    old = oldSessionsFolder() / sessionId
    if not old.is_dir():
        return
    for name in ("conversations", "backups", "workspace"):
        if (old / name).exists() and not (memory.state / name).exists():
            shutil.move(str(old / name), str(memory.state / name))
    for note in sorted((old / "notes").glob("*.md")):
        memory.addNotes(note.stem, [line[2:] for line in note.read_text(encoding="utf-8").splitlines() if line.startswith("- ")])
    shutil.rmtree(old, ignore_errors=True)


# Everything of a mission: its short-term memory, its temp folder, and its old session folder.
def clearSession(sessionId):
    for folder in (missionFolder(sessionId), tempFolder(sessionId), oldSessionsFolder() / sessionId):
        shutil.rmtree(folder, ignore_errors=True)


def lastChange(folder):
    try:
        return max([folder.stat().st_mtime, *(path.stat().st_mtime for path in folder.rglob("*"))])
    except OSError:
        return time.time()


# A mission is kept as long as its saved state is (the history). What is left of a mission without a saved state goes SESSION_DAYS after its
# last change, except the one kept.
def pruneSessions(keep=None):
    limit = time.time() - SESSION_DAYS * 24 * 3600
    for base in (oldSessionsFolder(), harness_utils.AGENT_FILES / MISSIONS_FOLDER, harness_utils.AGENT_FILES / TEMP_FOLDER):
        if not base.is_dir():
            continue
        for folder in base.iterdir():
            if folder.is_dir() and folder.name != keep and not swarmStatePath(folder.name).exists() and lastChange(folder) < limit:
                shutil.rmtree(folder, ignore_errors=True)
