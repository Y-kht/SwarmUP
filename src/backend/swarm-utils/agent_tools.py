# The tools of the agents. Every agent that talks with its model (agent_conversation.py) has the same tools as Claude Code and Codex, written for
# SwarmUP: it looks at the files of its folder, changes them, runs commands, reads web pages, writes to the other agents of its swarm, keeps notes
# and asks the user. The tools are described once (TOOLS) and every model client translates them for its provider.
# 1. Looking needs no permission, inside the folder of the agent (or its own empty workspace when it has none). Nothing outside it can be reached.
# 2. Changing a file, running a command or reading a web page asks the user first (Loop.askPermission, the same card as the coding agents):
#    once, until the swarm runs again, or never. In plan mode, and for a leader that builds its swarm, the agent can only look.
# 3. Every change can be put back. Before the first change of a file in a run, the original is copied into the session (or the file is noted as
#    new), and undoChanges puts everything back: when the agent is removed, when the swarm is stopped, or when its work is not approved.
import difflib
import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import harness_utils
from agent_storehouse import (MAX_READ_BYTES, agentWorkspace, describeTree, extractText, findFiles, findPath, isInside, safeName, searchFiles)
from harness_utils import FETCH_ERRORS, describeError, fetchUrl, stripTags


READ_LINES = 2000
LINE_CHARS = 2000
RESULT_CHARS = 60000
PREVIEW_LINES = 120
COMMAND_SECONDS = 120
MAX_COMMAND_SECONDS = 600
PAGE_CHARS = 40000
BACKUPS_FOLDER = "backups"


def tool(name, description, properties=None, required=()):
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}}


TEXT = {"type": "string"}
# kind says what a tool does: look, change, run and web are about the files and the internet, team, memory and user about the swarm and the user.
TOOLS = {
    "list_files": ("look", tool("list_files", "List the files of your folder, or of one folder inside it, at every depth, with their sizes.",
                                {"path": {"type": "string", "description": "A folder inside your folder. Leave it empty for the whole folder."}})),
    "find_files": ("look", tool("find_files", "Find the files and folders whose name matches a pattern like *.tex, or contains some letters, at every depth.",
                                {"pattern": TEXT}, ["pattern"])),
    "search_files": ("look", tool("search_files", "Find the lines of the text files of your folder that contain some words (capitals do not matter).", {"text": TEXT}, ["text"])),
    "read_file": ("look", tool("read_file", "Read a file of your folder (text, .docx or .pdf), with the number of each line. A long file is read from start_line, line_count lines at a time.",
                               {"path": TEXT, "start_line": {"type": "integer", "description": "The first line to read, 1 by default."},
                                "line_count": {"type": "integer", "description": f"How many lines to read, {READ_LINES} at most."}}, ["path"])),
    "write_file": ("change", tool("write_file", "Create a file of your folder, or replace all of its text. Prefer edit_file to change a part of a file that exists. The user approves it first.",
                                  {"path": TEXT, "content": {"type": "string", "description": "The whole text of the file."}, "reason": {"type": "string", "description": "Why, for the user."}},
                                  ["path", "content"])),
    "edit_file": ("change", tool("edit_file", "Replace a passage of a text file of your folder by another one. old_text must be copied exactly from the file (read it first) and be found once, "
                                 "unless replace_all is true. The user approves it first.",
                                 {"path": TEXT, "old_text": TEXT, "new_text": TEXT, "replace_all": {"type": "boolean"}, "reason": {"type": "string", "description": "Why, for the user."}},
                                 ["path", "old_text", "new_text"])),
    "make_folder": ("change", tool("make_folder", "Create a folder inside your folder. The user approves it first.", {"path": TEXT, "reason": TEXT}, ["path"])),
    "move_path": ("change", tool("move_path", "Move or rename a file or a folder inside your folder. The user approves it first.", {"source": TEXT, "target": TEXT, "reason": TEXT}, ["source", "target"])),
    "delete_path": ("change", tool("delete_path", "Delete a file or a folder of your folder. The user approves it first, and it can be put back.", {"path": TEXT, "reason": TEXT}, ["path"])),
    "run_command": ("run", tool("run_command", "Run a command line in your folder (to test code, convert a file, count...) and read what it prints. The user approves it first. "
                                "SwarmUP cannot put back the files a command changes.",
                                {"command": TEXT, "timeout_seconds": {"type": "integer", "description": f"At most {MAX_COMMAND_SECONDS}, {COMMAND_SECONDS} by default."}, "reason": TEXT},
                                ["command"])),
    "fetch_page": ("web", tool("fetch_page", "Read the text of a web page (http or https). The user approves it first.", {"url": TEXT, "reason": TEXT}, ["url"])),
    "send_message": ("team", tool("send_message", "Send a message to another agent of your swarm, or to your leader (write leader). It reads it with its next step. "
                                  "Use it to give them what they need from you, or to ask them something.", {"to": TEXT, "text": TEXT}, ["to", "text"])),
    "team_status": ("team", tool("team_status", "See the agents of your swarm: their roles, their tasks, where they stand, and whose result is ready.")),
    "read_result": ("team", tool("read_result", "Read the result of another agent of your swarm that finished.", {"agent": TEXT}, ["agent"])),
    "remember": ("memory", tool("remember", "Write down something you must not forget for the rest of the mission (a fact, a file that matters, a decision). "
                                "Your notes are shown to you with every request.", {"note": TEXT}, ["note"])),
    "ask_user": ("user", tool("ask_user", "Ask the user a question when you cannot go on without the answer. Give options when the answer is a choice.",
                              {"question": TEXT, "options": {"type": "array", "items": {"type": "string"}}}, ["question"])),
}
LOOK_KINDS = ("look", "team", "memory", "user")
JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list}


class ToolProblem(Exception):
    pass


def clip(text, limit=RESULT_CHARS):
    return text if len(text) <= limit else text[:limit] + f"\n[Cut: {len(text) - limit:,} more characters. Ask for a smaller part.]"


# Checks the arguments a model wrote against the description of the tool: the model may have cut them, or given another type.
def checkArguments(name, arguments):
    schema = TOOLS[name][1]["parameters"]
    if not isinstance(arguments, dict):
        raise ToolProblem("The arguments must be a JSON object.")
    missing = [key for key in schema["required"] if key not in arguments]
    unknown = [key for key in arguments if key not in schema["properties"]]
    if missing or unknown:
        raise ToolProblem(f"{name} needs {', '.join(schema['required']) or 'no argument'}" + (f", and has no {', '.join(unknown)}" if unknown else "") + ".")
    for key, value in arguments.items():
        wanted = JSON_TYPES.get(schema["properties"][key].get("type"))
        if wanted and not (isinstance(value, wanted) and not (wanted is int and isinstance(value, bool))):
            raise ToolProblem(f"{key} must be a {schema['properties'][key]['type']}.")
    return arguments


def describeDiff(before, after, name):
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), f"{name} (now)", f"{name} (after)", lineterm="", n=2))
    shown = lines[:PREVIEW_LINES] + ([f"... and {len(lines) - PREVIEW_LINES} more lines of changes"] if len(lines) > PREVIEW_LINES else [])
    return "\n".join(shown) or "(no change)"


# ==============
# The toolbox of one agent, for one request of its conversation.
# ==============
class Toolbox:
    # readOnly: the agent may only look (plan mode, a leader that builds its swarm).
    def __init__(self, loop, readOnly=False):
        self.loop = loop
        self.readOnly = readOnly or loop.planning
        self.root = Path(agentWorkspace(loop)).resolve()
        # The workspace of SwarmUP itself (an agent without a folder) is not the user's: changing it needs no permission.
        self.own = not loop.folder

    def available(self):
        team = getattr(self.loop, "team", None) is not None
        names = []
        for name, (kind, description) in TOOLS.items():
            if (self.readOnly and kind not in LOOK_KINDS) or (kind == "team" and not team) or (kind == "memory" and not self.loop.session):
                continue
            names.append(name)
        return names

    def schemas(self):
        return [TOOLS[name][1] for name in self.available()]

    # Runs one call of the model: it returns (the text for the model, whether it is an error).
    def run(self, name, arguments):
        if name not in self.available():
            return f"There is no tool {name} here. Your tools: {', '.join(self.available())}.", True
        try:
            arguments = checkArguments(name, arguments)
            return clip(getattr(self, name)(**arguments)), False
        except (ToolProblem, ValueError) as problem:
            return f"Not done: {problem}", True
        except OSError as error:
            return f"Not done: {error.strerror or error}.", True

    def name(self, path):
        return path.relative_to(self.root).as_posix() if path != self.root else "."

    # The path of a file that may not exist yet, inside the folder.
    def target(self, text):
        text = str(text).strip().strip("`'\"")
        candidate = Path(text).expanduser()
        candidate = candidate if candidate.is_absolute() else self.root / candidate
        if not text or not isInside(self.root, candidate) or candidate.resolve() == self.root:
            raise ToolProblem(f"{text or 'This path'} is not a path inside your folder {self.root}.")
        return candidate.parent.resolve() / candidate.name

    # The user decides, unless the same kind of action was allowed until the swarm runs again. key is what such a permission covers.
    def permit(self, key, action, detail, reason):
        if self.own and key == "change":
            return
        if key in self.loop.allowed:
            return
        answer = self.loop.askPermission({"action": action, "detail": detail, "folder": str(self.root), "reason": reason or ""})
        decision = answer.get("decision") if isinstance(answer, dict) else "deny"
        if decision == "run":
            self.loop.allowed.add(key)
        elif decision != "once":
            raise ToolProblem(f"The user refused. {answer.get('message') if isinstance(answer, dict) and answer.get('message') else ''} Go on without it.".strip())

    # The first change of a path in this run keeps a copy of its original, so undoChanges can put it back.
    def keep(self, path):
        changes = self.loop.progress.setdefault("changes", [])
        if any(change["path"] == str(path) for change in changes):
            return
        backup = None
        if path.exists():
            folder = backupsFolder(self.loop)
            backup = folder / f"{len(changes)}_{safeName(path.name)}"
            folder.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if path.is_dir() else shutil.copy2)(path, backup)
        changes.append({"path": str(path), "backup": str(backup) if backup else None})
        self.loop.saveProgress()

    # The folders a change creates are new too: the highest one is kept, so putting back removes all of them.
    def keepParents(self, path):
        missing = None
        for parent in path.parents:
            if parent == self.root or parent.exists():
                break
            missing = parent
        if missing is not None:
            self.keep(missing)

    # ---------- Looking. ----------
    def list_files(self, path=""):
        return describeTree(self.root, path)

    def find_files(self, pattern):
        return findFiles(self.root, pattern)

    def search_files(self, text):
        return searchFiles(self.root, text)

    def read_file(self, path, start_line=1, line_count=READ_LINES):
        found = findPath(self.root, path)
        if found.is_dir():
            return f"{self.name(found)} is a folder. Its files:\n{describeTree(self.root, self.name(found))}"
        lines = extractText(found).splitlines()
        start, count = max(start_line, 1), min(max(line_count, 1), READ_LINES)
        shown = [f"{number:>6}\t{line[:LINE_CHARS]}" for number, line in enumerate(lines[start - 1:start - 1 + count], start)]
        end = start - 1 + len(shown)
        more = f"\n[Lines {start} to {end} of {len(lines)}. Read on with start_line={end + 1}.]" if end < len(lines) else ""
        return f"{self.name(found)} ({len(lines)} lines):\n" + ("\n".join(shown) or "(nothing here)") + more

    # ---------- Changing. ----------
    def currentText(self, path):
        if not path.exists():
            return None
        if path.is_dir():
            raise ToolProblem(f"{self.name(path)} is a folder.")
        if path.suffix.lower() in (".docx", ".pdf") or path.stat().st_size > MAX_READ_BYTES:
            raise ToolProblem(f"{self.name(path)} is not a text file that can be changed here: write a new file instead.")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolProblem(f"{self.name(path)} is a binary file: it cannot be changed as text.")
        return data.decode("utf-8", errors="replace")

    def store(self, path, text, before, reason, verb):
        detail = f"{self.name(path)}\n" + (describeDiff(before, text, self.name(path)) if before is not None else "\n".join(text.splitlines()[:PREVIEW_LINES]))
        self.permit("change", f"{verb} a file", detail, reason)
        self.keepParents(path)
        self.keep(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.loop.logAction(f"{verb.capitalize()} {path}.")
        return f"Done: {verb} {self.name(path)} ({len(text.splitlines())} lines)."

    def write_file(self, path, content, reason=""):
        target = self.target(path)
        before = self.currentText(target)
        return self.store(target, content, before, reason, "create" if before is None else "replace")

    def edit_file(self, path, old_text, new_text, replace_all=False, reason=""):
        target = findPath(self.root, path)
        before = self.currentText(target)
        found = before.count(old_text) if old_text else 0
        if not found:
            raise ToolProblem(f"old_text is not in {self.name(target)}. Read the file again and copy the passage exactly, spaces and line breaks included.")
        if found > 1 and not replace_all:
            raise ToolProblem(f"old_text is {found} times in {self.name(target)}. Give more of the text around it, or set replace_all to true.")
        return self.store(target, before.replace(old_text, new_text), before, reason, "change")

    def make_folder(self, path, reason=""):
        target = self.target(path)
        if target.is_dir():
            return f"{self.name(target)} already exists."
        self.permit("change", "create a folder", self.name(target), reason)
        self.keepParents(target)
        self.keep(target)
        target.mkdir(parents=True)
        self.loop.logAction(f"Created the folder {target}.")
        return f"Done: created {self.name(target)}."

    def move_path(self, source, target, reason=""):
        origin, destination = findPath(self.root, source), self.target(target)
        if destination.exists():
            raise ToolProblem(f"{self.name(destination)} already exists.")
        self.permit("change", "move or rename", f"{self.name(origin)} -> {self.name(destination)}", reason)
        self.keep(origin)
        self.keepParents(destination)
        self.keep(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origin), str(destination))
        self.loop.logAction(f"Moved {origin} to {destination}.")
        return f"Done: moved {self.name(origin)} to {self.name(destination)}."

    def delete_path(self, path, reason=""):
        target = findPath(self.root, path)
        if target == self.root:
            raise ToolProblem("Your folder itself cannot be deleted.")
        self.permit("change", "delete", self.name(target) + (" (a folder, with everything in it)" if target.is_dir() else ""), reason)
        self.keep(target)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        self.loop.logAction(f"Deleted {target}.")
        return f"Done: deleted {self.name(target)}. It can be put back."

    # ---------- Commands and the web. ----------
    def run_command(self, command, timeout_seconds=COMMAND_SECONDS, reason=""):
        seconds = min(max(timeout_seconds, 1), MAX_COMMAND_SECONDS)
        self.permit(f"run:{command.strip()}", "run a command", command, reason)
        try:
            done = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True, errors="replace", timeout=seconds)
        except subprocess.TimeoutExpired:
            return f"The command ran for more than {seconds} seconds and was stopped."
        self.loop.logAction(f"Ran in {self.root}: {command}")
        output = (done.stdout + done.stderr).strip()
        return f"Exit code {done.returncode}.\n" + (output[-RESULT_CHARS:] if output else "(it printed nothing)")

    def fetch_page(self, url, reason=""):
        host = urllib.parse.urlparse(url.strip()).hostname or url
        self.permit(f"web:{host}", "read a web page", url, reason)
        try:
            data = fetchUrl(url)
        except FETCH_ERRORS as error:
            raise ToolProblem(describeError(error)) from None
        text = data.decode("utf-8", errors="replace")
        return clip(stripTags(text) if "<html" in text[:2000].lower() or "<body" in text.lower() else text, PAGE_CHARS)

    # ---------- The swarm, the memory and the user. ----------
    def send_message(self, to, text):
        return self.loop.team.relay(self.loop.name, to, text)

    def team_status(self):
        return self.loop.team.describeTeamFor(self.loop.name)

    def read_result(self, agent):
        return self.loop.team.resultOf(agent)

    def remember(self, note):
        self.loop.session.addNotes(self.loop.name, [note])
        return "Noted."

    def ask_user(self, question, options=()):
        answers = self.loop.askQuestions([{"id": "answer", "header": self.loop.name, "question": question, "options": [{"label": str(option)} for option in options],
                                           "multiple": False, "secret": False}])
        reply = (answers or {}).get("answer") if isinstance(answers, dict) else None
        reply = ", ".join(str(item) for item in reply) if isinstance(reply, list) else str(reply or "")
        return f"The user answered: {reply}" if reply.strip() else "The user gave no answer. Go on as well as you can."


def backupsFolder(loop):
    base = loop.session.folder if loop.session else harness_utils.AGENT_FILES / "sessions" / "loose"
    return base / BACKUPS_FOLDER / safeName(loop.name)


# Puts back every change of the agent in this run, the last first: the originals are copied back and the new files and folders removed.
def undoChanges(loop):
    changes = loop.progress.get("changes") or []
    for change in reversed(changes):
        path, backup = Path(change["path"]), change["backup"]
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
            if backup and Path(backup).exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree if Path(backup).is_dir() else shutil.copy2)(backup, path)
        except OSError as error:
            loop.notifyUser(f"[{loop.name}] {path} could not be put back: {error.strerror or error}.")
    if changes:
        loop.progress["changes"] = []
        loop.logAction(f"Put back the {len(changes)} {'file' if len(changes) == 1 else 'files and folders'} it had changed.")
    return len(changes)


def describeChanges(loop):
    changes = loop.progress.get("changes") or []
    return f"It changed {len(changes)} {'path' if len(changes) == 1 else 'paths'} that will be put back: " + ", ".join(Path(change["path"]).name for change in changes[:10]) if changes else ""


def describeCall(name, arguments):
    shown = {key: (value if len(str(value)) <= 80 else str(value)[:77] + "...") for key, value in (arguments or {}).items() if key not in ("content", "new_text", "old_text")}
    return f"{name}({json.dumps(shown, ensure_ascii=False)[1:-1]})"
