# The tools of the agents. Every agent that talks with its model (agent_conversation.py) has the same tools as Claude Code and Codex, written for
# SwarmUP: it looks at the files of its folder, changes them, copies and pastes files, folders and blocks of text, runs commands (also in the
# background), reads web pages, writes to the other agents of its swarm, keeps notes and asks the user. The tools are described once (TOOLS) and
# every model client translates them for its provider.
# 1. The places. A path is in the folder of the agent (or its own empty workspace when it has none), or, with a prefix, in a place of its
#    mission (mission_memory.py): @temp/ its temp folder, @memory/ the memory of the mission, @long-term/ the long-term memory. Nothing outside
#    them can be reached.
# 2. Looking needs no permission. Changing a file of the folder, running a command or reading a web page asks the user first
#    (Loop.askPermission, the same card as the coding agents): once, until the swarm runs again, or never. The temp folder is SwarmUP's, so it
#    changes without asking, and the memory follows its own rules (an agent changes only what it wrote, only the leader writes the long-term
#    memory). In plan mode, and for a leader that builds its swarm, the agent can only look.
# 3. Every change of the folder can be put back. Before the first change of a file in a run, the original is copied into the session (or the file
#    is noted as new), and undoChanges puts everything back: when the agent is removed, when the swarm is stopped, or when its work is not approved.
# Copying and pasting are in agent_transfer.py, and the commands in background_processes.py.
import difflib
import json
import shutil
import urllib.parse
from pathlib import Path

import harness_utils
from agent_storehouse import (MAX_READ_BYTES, agentWorkspace, cleanRequest, describeTree, extractText, findFiles, findPath, isInside, safeName, searchFiles,
                              splitPlace)
from agent_transfer import TransferTools
from background_processes import COMMAND_SECONDS, MAX_COMMAND_SECONDS, ProcessTools
from harness_utils import FETCH_ERRORS, describeError, fetchUrl, stripTags
from memory_cache import noteUse


READ_LINES = 2000
LINE_CHARS = 2000
RESULT_CHARS = 60000
PREVIEW_LINES = 120
PAGE_CHARS = 40000
BACKUPS_FOLDER = "backups"
MEMORY_PLACES = ("@memory", "@long-term")


def tool(name, description, properties=None, required=()):
    return {"name": name, "description": description, "parameters": {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}}


TEXT = {"type": "string"}
PATH = {"type": "string", "description": "A path of your folder, or of @temp/, @memory/ or @long-term/."}
WHERE = {"type": "string", "description": "Where: a folder of your folder, or @temp, @memory or @long-term (or a folder inside them). Empty for your folder."}
REASON = {"type": "string", "description": "Why, for the user."}
LINE = {"type": "integer"}
# kind says what a tool does: look, change, run and web are about the files and the internet, team, memory and user about the swarm and the user.
TOOLS = {
    "list_files": ("look", tool("list_files", "List the files of your folder, or of one folder inside it or of @temp, @memory or @long-term, at every depth, with their sizes.", {"path": WHERE})),
    "find_files": ("look", tool("find_files", "Find the files and folders whose name matches a pattern like *.tex, or contains some letters, at every depth.",
                                {"pattern": TEXT, "path": WHERE}, ["pattern"])),
    "search_files": ("look", tool("search_files", "Find the lines of the text files that contain some words (capitals do not matter).", {"text": TEXT, "path": WHERE}, ["text"])),
    "read_file": ("look", tool("read_file", "Read a file (text, .docx or .pdf), with the number of each line. A long file is read from start_line, line_count lines at a time.",
                               {"path": PATH, "start_line": {"type": "integer", "description": "The first line to read, 1 by default."},
                                "line_count": {"type": "integer", "description": f"How many lines to read, {READ_LINES} at most."}}, ["path"])),
    "write_file": ("change", tool("write_file", "Create a file, or replace all of its text. Prefer edit_file to change a part of a file that exists. A file of your folder waits for the user.",
                                  {"path": PATH, "content": {"type": "string", "description": "The whole text of the file."}, "reason": REASON}, ["path", "content"])),
    "edit_file": ("change", tool("edit_file", "Replace a passage of a text file by another one. old_text must be copied exactly from the file (read it first) and be found once, "
                                 "unless replace_all is true. A file of your folder waits for the user.",
                                 {"path": PATH, "old_text": TEXT, "new_text": TEXT, "replace_all": {"type": "boolean"}, "reason": REASON}, ["path", "old_text", "new_text"])),
    "make_folder": ("change", tool("make_folder", "Create a folder. In your folder, the user approves it first.", {"path": PATH, "reason": REASON}, ["path"])),
    "move_path": ("change", tool("move_path", "Move or rename a file or a folder, also from one place to another. In your folder, the user approves it first.",
                                 {"source": PATH, "target": PATH, "reason": REASON}, ["source", "target"])),
    "delete_path": ("change", tool("delete_path", "Delete a file or a folder. In your folder the user approves it first, and it can be put back.", {"path": PATH, "reason": REASON}, ["path"])),
    "copy_path": ("change", tool("copy_path", "Copy a file or a folder with everything in it, exactly, also from one place to another (for example from your folder to @temp to "
                                 "work on a copy or keep a backup, and back). A target that is a folder that exists receives the copy inside it. An existing file is "
                                 "only replaced with overwrite. A copy into your folder waits for the user.",
                                 {"source": PATH, "target": PATH, "overwrite": {"type": "boolean"}, "reason": REASON}, ["source", "target"])),
    "copy_text": ("look", tool("copy_text", "Copy a block of text into your clipboard, exactly: lines start_line to end_line of a file (both included; the whole file without them), "
                               "or the text you give. Several clipboards can be kept, by name.",
                               {"path": PATH, "start_line": LINE, "end_line": LINE, "text": TEXT, "clip": {"type": "string", "description": "The name of the clipboard, main by default."}})),
    "paste_text": ("change", tool("paste_text", "Paste a clipboard into a text file, exactly: before the line at_line, after the passage after_text, in place of the passage "
                                  "replace_text (found once), or at the end (at_end, also to create a file). A file of your folder waits for the user.",
                                  {"path": PATH, "clip": TEXT, "at_line": LINE, "after_text": TEXT, "replace_text": TEXT, "at_end": {"type": "boolean"}, "reason": REASON}, ["path"])),
    "list_clips": ("look", tool("list_clips", "List your clipboards, with their size and their first line.")),
    "run_command": ("run", tool("run_command", "Run a command line and read what it prints. It starts in your folder, or in cwd (a folder of yours or of @temp). With background, "
                                "it goes on while you work: its output goes to a file of @temp, and process_status and stop_process follow it. The user approves it "
                                "first, and SwarmUP cannot put back the files a command changes. Write the scripts you run in @temp.",
                                {"command": TEXT, "timeout_seconds": {"type": "integer", "description": f"At most {MAX_COMMAND_SECONDS}, {COMMAND_SECONDS} by default."},
                                 "cwd": TEXT, "background": {"type": "boolean"}, "reason": REASON}, ["command"])),
    "process_status": ("run", tool("process_status", "See if a command of the background still runs, and the end of what it printed. Without id, all of yours.", {"id": TEXT})),
    "stop_process": ("run", tool("stop_process", "Stop a command of the background, with everything it started.", {"id": TEXT}, ["id"])),
    "fetch_page": ("web", tool("fetch_page", "Read the text of a web page (http or https). The user approves it first.", {"url": TEXT, "reason": REASON}, ["url"])),
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
PLACE_TOOLS = ("copy_text", "paste_text", "list_clips", "process_status", "stop_process")
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


# A path that a tool reaches: its place ("" for the folder of the agent, @temp, @memory or @long-term), the root of the place, and the path.
class Spot:
    def __init__(self, place, root, path):
        self.place, self.root, self.path = place, root, path

    @property
    def relative(self):
        return self.path.relative_to(self.root).as_posix() if self.path != self.root else ""

    @property
    def name(self):
        return f"{self.place}/{self.relative}".rstrip("/") if self.place else (self.relative or ".")

    @property
    def long(self):
        return self.place == "@long-term"


# ==============
# The toolbox of one agent, for one request of its conversation.
# ==============
class Toolbox(TransferTools, ProcessTools):
    # readOnly: the agent may only look (plan mode, a leader that builds its swarm).
    def __init__(self, loop, readOnly=False):
        self.loop = loop
        self.readOnly = readOnly or loop.planning
        self.root = Path(agentWorkspace(loop)).resolve()
        # The workspace of SwarmUP itself (an agent without a folder) is not the user's: changing it needs no permission.
        self.own = not loop.folder
        self.session = getattr(loop, "session", None)
        self.places = {name: Path(folder).resolve() for name, folder in self.session.memory.roots().items()} if self.session else {}

    def available(self):
        team = getattr(self.loop, "team", None) is not None
        names = []
        for name, (kind, description) in TOOLS.items():
            if (self.readOnly and kind not in LOOK_KINDS) or (kind == "team" and not team) or (kind == "memory" and not self.session):
                continue
            if name in PLACE_TOOLS and not self.session:
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

    # ---------- The places. ----------
    # What exists at a path (a file or a folder), in its place. The hidden folder of SwarmUP in the memory is never reached.
    def locate(self, text, folders=False):
        place, root, rest = splitPlace(self.places, text, self.root)
        path = findPath(root, rest, folders) if cleanRequest(rest) else root
        return self.checked(Spot(place, root, path))

    # The path of a file or a folder that may not exist yet, inside its place.
    def target(self, text):
        place, root, rest = splitPlace(self.places, text, self.root)
        candidate = Path(cleanRequest(rest)).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        if not cleanRequest(rest) or not isInside(root, candidate) or candidate.resolve() == root:
            where = f"{place}" if place else f"your folder {self.root}"
            raise ToolProblem(f"{cleanRequest(text) or 'This path'} is not a path inside {where}.")
        return self.checked(Spot(place, root, candidate.parent.resolve() / candidate.name))

    def checked(self, spot):
        if spot.place in MEMORY_PLACES and any(part.startswith(".") for part in Path(spot.relative).parts):
            raise ToolProblem(f"{spot.name} is kept by SwarmUP itself and cannot be reached.")
        return spot

    def folderSpot(self, text):
        return self.locate(text or "", folders=True) if text else Spot("", self.root, self.root)

    def noteUse(self, spot, kind):
        if spot.place in MEMORY_PLACES and spot.relative:
            noteUse(spot.root, spot.relative, self.loop.name, kind)

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

    # A change of a place: in the folder of the agent, the user decides and the original is kept so it can be put back; in the temp folder
    # nothing is asked; in the memory, its rules decide (MissionMemory.refusal).
    def allowChange(self, spots, action, detail, reason):
        if any(spot.place == "" for spot in spots):
            self.permit("change", action, detail, reason)
        for spot in spots:
            if spot.place in MEMORY_PLACES:
                self.session.memory.checkWrite(self.loop.name, spot.relative, spot.long)
        for spot in spots:
            if spot.place == "":
                self.keepParents(spot.path)
                self.keep(spot.path)

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
            (shutil.copytree if path.is_dir() else shutil.copy2)(path, backup, **({"symlinks": True} if path.is_dir() else {"follow_symlinks": False}))
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

    # After a change of the memory, its owners and its index follow (a file made by a copy or a move becomes the agent's own).
    def settleMemory(self, spot, removed=False):
        if spot.place not in MEMORY_PLACES:
            return
        memory = self.session.memory
        if removed:
            for name in [name for name in memory.owners(spot.long) if name == spot.relative or name.startswith(spot.relative + "/")]:
                memory.setOwner(name, None, spot.long)
            memory.index(spot.long)
        else:
            memory.claim(self.loop.name, spot.relative, spot.long)

    def logChange(self, spot, text):
        if spot.place == "":
            self.loop.logAction(text)

    # ---------- Looking. ----------
    def list_files(self, path=""):
        spot = self.folderSpot(path)
        return describeTree(spot.root, spot.relative)

    def find_files(self, pattern, path=""):
        spot = self.folderSpot(path)
        return findFiles(spot.path, pattern)

    def search_files(self, text, path=""):
        spot = self.folderSpot(path)
        return searchFiles(spot.path, text)

    def read_file(self, path, start_line=1, line_count=READ_LINES):
        spot = self.locate(path)
        if spot.path.is_dir():
            return f"{spot.name} is a folder. Its files:\n{describeTree(spot.root, spot.relative)}"
        lines = extractText(spot.path).splitlines()
        self.noteUse(spot, "read")
        start, count = max(start_line, 1), min(max(line_count, 1), READ_LINES)
        shown = [f"{number:>6}\t{line[:LINE_CHARS]}" for number, line in enumerate(lines[start - 1:start - 1 + count], start)]
        end = start - 1 + len(shown)
        more = f"\n[Lines {start} to {end} of {len(lines)}. Read on with start_line={end + 1}.]" if end < len(lines) else ""
        return f"{spot.name} ({len(lines)} lines):\n" + ("\n".join(shown) or "(nothing here)") + more

    # ---------- Changing. ----------
    def currentText(self, spot):
        path = spot.path
        if not path.exists():
            return None
        if path.is_dir():
            raise ToolProblem(f"{spot.name} is a folder.")
        if path.suffix.lower() in (".docx", ".pdf") or path.stat().st_size > MAX_READ_BYTES:
            raise ToolProblem(f"{spot.name} is not a text file that can be changed here: write a new file instead.")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolProblem(f"{spot.name} is a binary file: it cannot be changed as text.")
        return data.decode("utf-8", errors="replace")

    # Writes the whole new text of a file, exactly (the line ends are kept as they are), after the rules of its place.
    def store(self, spot, text, before, reason, verb):
        if spot.place in MEMORY_PLACES:
            self.session.memory.write(self.loop.name, spot.relative, text, long=spot.long)
            self.noteUse(spot, "write")
        else:
            detail = f"{spot.name}\n" + (describeDiff(before, text, spot.name) if before is not None else "\n".join(text.splitlines()[:PREVIEW_LINES]))
            self.allowChange([spot], f"{verb} a file", detail, reason)
            spot.path.parent.mkdir(parents=True, exist_ok=True)
            with open(spot.path, "w", encoding="utf-8", newline="") as file:
                file.write(text)
            self.logChange(spot, f"{verb.capitalize()} {spot.path}.")
        return f"Done: {verb} {spot.name} ({len(text.splitlines())} lines)."

    def write_file(self, path, content, reason=""):
        spot = self.target(path)
        before = self.currentText(spot)
        return self.store(spot, content, before, reason, "create" if before is None else "replace")

    def edit_file(self, path, old_text, new_text, replace_all=False, reason=""):
        spot = self.locate(path)
        before = self.currentText(spot)
        found = before.count(old_text) if old_text else 0
        if not found:
            raise ToolProblem(f"old_text is not in {spot.name}. Read the file again and copy the passage exactly, spaces and line breaks included.")
        if found > 1 and not replace_all:
            raise ToolProblem(f"old_text is {found} times in {spot.name}. Give more of the text around it, or set replace_all to true.")
        return self.store(spot, before.replace(old_text, new_text), before, reason, "change")

    def make_folder(self, path, reason=""):
        spot = self.target(path)
        if spot.path.is_dir():
            return f"{spot.name} already exists."
        self.allowChange([spot], "create a folder", spot.name, reason)
        spot.path.mkdir(parents=True)
        self.logChange(spot, f"Created the folder {spot.path}.")
        return f"Done: created {spot.name}."

    def move_path(self, source, target, reason=""):
        origin, destination = self.locate(source), self.target(target)
        if destination.path.exists():
            raise ToolProblem(f"{destination.name} already exists.")
        if origin.path == origin.root:
            raise ToolProblem(f"{origin.name} itself cannot be moved.")
        if destination.path == origin.path or origin.path in destination.path.parents:
            raise ToolProblem(f"{origin.name} cannot be moved inside itself.")
        self.allowChange([origin, destination], "move or rename", f"{origin.name} -> {destination.name}", reason)
        destination.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(origin.path), str(destination.path))
        self.settleMemory(origin, removed=True)
        self.settleMemory(destination)
        self.logChange(origin if origin.place == "" else destination, f"Moved {origin.path} to {destination.path}.")
        return f"Done: moved {origin.name} to {destination.name}."

    def delete_path(self, path, reason=""):
        spot = self.locate(path)
        if spot.path == spot.root:
            raise ToolProblem("Your folder itself cannot be deleted." if spot.place == "" else f"{spot.place} itself cannot be deleted.")
        if spot.place in MEMORY_PLACES:
            self.session.memory.delete(self.loop.name, spot.relative, spot.long)
            return f"Done: deleted {spot.name}."
        self.allowChange([spot], "delete", spot.name + (" (a folder, with everything in it)" if spot.path.is_dir() else ""), reason)
        if spot.path.is_dir() and not spot.path.is_symlink():
            shutil.rmtree(spot.path)
        else:
            spot.path.unlink()
        self.logChange(spot, f"Deleted {spot.path}.")
        return f"Done: deleted {spot.name}." + (" It can be put back." if spot.place == "" else "")

    # ---------- The web. ----------
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
        self.session.addNotes(self.loop.name, [note])
        return "Noted."

    def ask_user(self, question, options=()):
        answers = self.loop.askQuestions([{"id": "answer", "header": self.loop.name, "question": question, "options": [{"label": str(option)} for option in options],
                                           "multiple": False, "secret": False}])
        reply = (answers or {}).get("answer") if isinstance(answers, dict) else None
        reply = ", ".join(str(item) for item in reply) if isinstance(reply, list) else str(reply or "")
        if self.session and reply.strip():
            self.session.memory.addUserPrompt(self.session.round, f"{question}\n\nThe answer: {reply}", self.loop.name, kind="Answer to a question of")
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
            if backup and (Path(backup).exists() or Path(backup).is_symlink()):
                path.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree if Path(backup).is_dir() else shutil.copy2)(backup, path, **({"symlinks": True} if Path(backup).is_dir() else {"follow_symlinks": False}))
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
