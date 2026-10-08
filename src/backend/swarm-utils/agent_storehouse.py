# The storehouse of the agents: how every agent reaches the files of its folder, where the work it makes is saved, and what it remembers
# during a swarm session.
# 1. The files. Whatever its model, an agent sees the tree of its folder (every depth) and can ask, with blocks in its answer, to list a folder,
#    find files by name, search words in the files, and read a file (text, .docx, and .pdf with pypdf). It only reads: nothing is changed.
#    A path that leaves the folder is refused. The coding agents (Claude Code, Codex) have their own tools, so they do not use the blocks.
# 2. The results. What an agent saves goes in swarmup-results/<run> inside its folder, one folder for each run of the swarm, named by its time
#    and its mission. An agent without a folder saves in agent-files/results/<run>.
# 3. The memory. Each swarm is a mission with a memory of its own, the long-term memory of the user and a temp folder (mission_memory.py):
#    what an agent sees of them comes with its prompts (memory_cache.py), its notes included, also after a restart.
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import agent_prompts as prompts
import harness_utils
from memory_cache import describeMemory


RESULTS_FOLDER = "swarmup-results"
LOOSE_RESULTS_FOLDER = "results"
WORKSPACES_FOLDER = "agent-workspaces"
# The folders that are not listed in the tree (they are big and seldom useful). They can still be listed, searched and read when asked.
SKIPPED_FOLDERS = {"__pycache__", "node_modules", "venv", "env", "site-packages", "dist", "build", RESULTS_FOLDER}
TREE_LIMIT = 200
SCAN_LIMIT = 20000
READ_PART_CHARS = 30000
MAX_READ_BYTES = 20000000
ROUND_CHARS = 80000
FIND_LIMIT = 100
SEARCH_LIMIT = 80
SEARCH_FILE_BYTES = 2000000
LINE_CHARS = 300
MAX_TOOL_ROUNDS = 8
ICLOUD_WAIT_SECONDS = 20
BINARY_PROBE = 8192
NAME_SIGNS = re.compile(r"[^\w.-]")
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# The blocks are read even when they are not written exactly as asked: spaces, capitals, or a missing closing tag (then the line ends the block).
REQUEST_PATTERN = re.compile(r"<\s*swarmup_(read|list|find|search)\b([^>]*)>(.*?)(?:<\s*/\s*swarmup_\1\s*>|$)", re.IGNORECASE | re.MULTILINE)
NOTE_PATTERN = re.compile(r"<\s*swarmup_note\s*>(.*?)(?:<\s*/\s*swarmup_note\s*>|\Z)", re.IGNORECASE | re.DOTALL)
PART_PATTERN = re.compile(r"part\s*=\s*[\"']?(\d+)", re.IGNORECASE)
REQUEST_WORDS = {"read": "reads", "list": "lists", "find": "looks for", "search": "searches for"}


# ==============
# Paths. macOS writes some letters (like é) in two ways, and its disks ignore capitals: a path is compared in one form and without capitals.
# ==============
def safeName(name):
    return NAME_SIGNS.sub("_", name or "agent")


def comparable(text):
    return unicodedata.normalize("NFC", text).casefold()


def cleanRequest(text):
    text = str(text).strip().strip("`'\"").strip()
    return text[2:] if text.startswith("./") else text


def isInside(folder, path, base=None):
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base or folder) / candidate
    try:
        resolved, root = candidate.resolve(), Path(folder).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


# The places of a mission (MissionMemory.roots: @temp, @memory, @long-term) are written as a prefix of a path. It gives (the place, "" for the
# folder of the agent, its folder, and the path inside it).
def splitPlace(places, text, root):
    text = cleanRequest(text)
    for place, folder in places.items():
        if text == place or text.startswith(place + "/"):
            return place, Path(folder), text[len(place):].lstrip("/")
    if text.startswith("@"):
        raise ValueError(f"{text.split('/')[0]} is not a place. The places: {', '.join(places) or 'none, outside of a mission'}.")
    return "", Path(root), text


# A file that iCloud moved to the cloud to free space is a hidden stub, .name.icloud, until it is opened.
def cloudName(name):
    return name[1:-len(".icloud")] if name.startswith(".") and name.endswith(".icloud") and len(name) > len("..icloud") else ""


# Whether this program may read the folder: "" if it can, otherwise what the user must do. macOS protects some folders (Desktop, Documents,
# Downloads, iCloud Drive, external disks) until the user allows the program that started SwarmUP.
def folderProblem(path):
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except PermissionError:
        if sys.platform == "darwin":
            return (f"macOS does not let SwarmUP read {path}. Open System Settings > Privacy & Security > Files and Folders (or Full Disk Access), "
                    "allow the app that started SwarmUP (Terminal, iTerm or Visual Studio Code), then start SwarmUP again.")
        return f"SwarmUP is not allowed to read {path}. Give your account the permission to read it, or choose another folder."
    except OSError as error:
        return f"{path} cannot be read: {error.strerror or error}."
    return ""


# ==============
# The tree of a folder, at every depth.
# ==============
# Everything inside base, shallow files first: {"files": [(path, size, or None for a file in iCloud)], "folders": [path], "skipped": {path: why},
# "complete": False if the folder was too big to be scanned whole}. The paths are relative to root, with /.
def scanFolder(root, base=None):
    root = Path(root)
    found = {"files": [], "folders": [], "skipped": {}, "complete": True}
    pending, seen = deque([Path(base or root)]), 0
    while pending:
        folder = pending.popleft()
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name.lower())
        except OSError:
            found["skipped"][Path(folder).relative_to(root).as_posix()] = f"cannot be read: {folderProblem(folder) or 'it could not be opened'}"
            continue
        for entry in entries:
            seen += 1
            if seen > SCAN_LIMIT:
                found["complete"] = False
                return found
            relative = Path(entry.path).relative_to(root).as_posix()
            try:
                isFolder, isFile = entry.is_dir(follow_symlinks=False), entry.is_file()
                size = entry.stat().st_size if isFile else 0
            except OSError:
                continue
            if entry.name.startswith("."):
                if isFile and cloudName(entry.name):
                    found["files"].append((Path(relative).with_name(cloudName(entry.name)).as_posix(), None))
            elif isFolder and entry.name in SKIPPED_FOLDERS:
                found["skipped"][relative] = "not listed"
            elif isFolder:
                found["folders"].append(relative)
                pending.append(Path(entry.path))
            elif isFile:
                found["files"].append((relative, size))
    return found


def describeFile(path, size):
    return f"- {path} (in iCloud, not downloaded yet)" if size is None else f"- {path} ({size:,} bytes)"


# The tree an agent reads: every file with its size, or, for a folder with more than TREE_LIMIT files, the folders with how many files they hold.
def describeTree(folder, sub=""):
    root = Path(folder).resolve()
    base = findPath(root, sub, folders=True) if cleanRequest(sub) else root
    if not base.is_dir():
        raise ValueError(f"{cleanRequest(sub)} is a file, not a folder. Read it with <swarmup_read>.")
    found = scanFolder(root, base)
    skipped = [f"- {path}/ ({why})" for path, why in found["skipped"].items()]
    if len(found["files"]) <= TREE_LIMIT:
        lines = [describeFile(path, size) for path, size in found["files"]] + skipped
    else:
        counts = {}
        for path, size in found["files"]:
            parent = path.rpartition("/")[0] or "."
            counts[parent] = counts.get(parent, 0) + 1
        lines = [f"It holds {len(found['files']):,} files, too many to list one by one. The folders, with the number of files in each "
                 "(ask <swarmup_list>folder</swarmup_list> for the files of one, or <swarmup_find> to find a file by name):"]
        lines += [f"- {path}/ ({count} files)" for path, count in list(counts.items())[:TREE_LIMIT]] + skipped[:TREE_LIMIT]
        if len(counts) > TREE_LIMIT:
            lines.append(f"- ... and {len(counts) - TREE_LIMIT:,} more folders: ask <swarmup_find> to find them by name.")
    if not found["complete"]:
        lines.append(f"- ... the folder is very big, so only its first {SCAN_LIMIT:,} entries were looked at.")
    return "\n".join(lines) or "The folder is empty."


# ==============
# Finding, searching and reading the files.
# ==============
# The path of what was asked, inside the folder. A path that leaves it (with .., from the root, or by a link) is refused. A path written in
# another form or with other capitals still finds its file, and an unknown one gets the names that are close to it.
def findPath(root, text, folders=False):
    root, text = Path(root).resolve(), cleanRequest(text)
    candidate = Path(text).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    if not isInside(root, candidate):
        raise ValueError(f"{text} is outside the folder {root}. Only the files of the folder can be read.")
    if candidate.exists():
        return candidate.resolve()
    stub = candidate.with_name(f".{candidate.name}.icloud")
    if not folders and stub.is_file():
        return downloadFromCloud(stub, candidate)
    found = scanFolder(root)
    names = found["folders"] if folders else [path for path, size in found["files"]]
    same = [name for name in names if comparable(name) == comparable(text)]
    if len(same) == 1:
        return findPath(root, same[0], folders)
    close = difflib.get_close_matches(text, names, n=3, cutoff=0.5)
    close += [name for name in names if comparable(name.rpartition("/")[2]) == comparable(Path(text).name) and name not in close][:3]
    hint = f" Did you mean: {', '.join(close)}?" if close else " Ask <swarmup_find> to look for it by name."
    raise ValueError(f"There is no {'folder' if folders else 'file'} {text} in the folder.{hint}")


# macOS downloads a file that iCloud keeps in the cloud when it is asked to (brctl). Otherwise the user opens it once in the Finder.
def downloadFromCloud(stub, path):
    if sys.platform == "darwin" and shutil.which("brctl"):
        subprocess.run(["brctl", "download", str(path)], capture_output=True, timeout=ICLOUD_WAIT_SECONDS)
        deadline = time.monotonic() + ICLOUD_WAIT_SECONDS
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.5)
    if path.exists():
        return path.resolve()
    raise ValueError(f"{path.name} is in iCloud and not downloaded yet. The user can open it once in the Finder, then ask again.")


def readDocx(path):
    try:
        with zipfile.ZipFile(path) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise ValueError(f"{path.name} is not a Word document that can be read.") from None
    return "\n".join("".join(node.text or "" for node in paragraph.iter(f"{WORD_NAMESPACE}t")) for paragraph in document.iter(f"{WORD_NAMESPACE}p"))


def readPdf(path):
    try:
        import pypdf
    except ImportError:
        raise ValueError(f"{path.name} is a PDF. To let the agents read PDF files, install pypdf: pip install pypdf") from None
    try:
        return "\n\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(path)).pages)
    except Exception as error:
        raise ValueError(f"{path.name} could not be read as a PDF: {error}") from None


# The text of a file. Text files in UTF-8 (or UTF-16), Word documents and PDF files are read. Other binary files are refused.
def extractText(path):
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"{path.name} has {size:,} bytes, too many to be read.")
    if path.suffix.lower() == ".docx":
        return readDocx(path)
    if path.suffix.lower() == ".pdf":
        return readPdf(path)
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:BINARY_PROBE]:
        raise ValueError(f"{path.name} is a binary file ({size:,} bytes): it cannot be read as text.")
    return data.decode("utf-8", errors="replace")


# A long file comes in parts of READ_PART_CHARS characters, and the agent asks for the next part.
def readFile(folder, text, part=1):
    root = Path(folder).resolve()
    path = findPath(root, text)
    name = path.relative_to(root).as_posix()
    if path.is_dir():
        return f"{name} is a folder. Its files:\n{describeTree(root, name)}"
    content = extractText(path)
    parts = max(1, -(-len(content) // READ_PART_CHARS))
    part = min(max(part, 1), parts)
    piece = content[(part - 1) * READ_PART_CHARS:part * READ_PART_CHARS]
    heading = f"{name}" + (f" (part {part} of {parts})" if parts > 1 else "")
    following = f'\n[The file goes on: ask <swarmup_read part="{part + 1}">{name}</swarmup_read> for the next part.]' if part < parts else ""
    return f"{heading}:\n{piece or '(the file is empty)'}{following}"


# The files and folders whose name matches: a pattern with * or ?, or any part of the name.
def findFiles(folder, pattern):
    wanted = comparable(cleanRequest(pattern))
    if not wanted:
        raise ValueError("Write the name, or a part of the name, of what to find.")
    wild = any(sign in wanted for sign in "*?[")
    def matches(path):
        name = comparable(path)
        return fnmatch.fnmatchcase(name.rpartition("/")[2], wanted) or fnmatch.fnmatchcase(name, wanted) if wild else wanted in name
    found = scanFolder(folder)
    lines = [f"- {path}/ (folder)" for path in found["folders"] if matches(path)] + [describeFile(path, size) for path, size in found["files"] if matches(path)]
    more = f"\n- ... and {len(lines) - FIND_LIMIT} more: ask with a more precise name." if len(lines) > FIND_LIMIT else ""
    return "\n".join(lines[:FIND_LIMIT]) + more if lines else f"Nothing in the folder has a name like {cleanRequest(pattern)}."


# The lines of the text files that contain the words, without capitals, as path:line.
def searchFiles(folder, words):
    wanted, root, hits = comparable(cleanRequest(words)), Path(folder).resolve(), []
    if not wanted:
        raise ValueError("Write the words to search for.")
    for path, size in scanFolder(root)["files"]:
        if size is None or size > SEARCH_FILE_BYTES:
            continue
        try:
            data = (root / path).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:BINARY_PROBE]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if wanted in comparable(line):
                hits.append(f"- {path}:{number}: {line.strip()[:LINE_CHARS]}")
                if len(hits) >= SEARCH_LIMIT:
                    return "\n".join(hits) + "\n- ... more lines match: search for more precise words."
    return "\n".join(hits) or f"No text file of the folder contains {cleanRequest(words)}."


# ==============
# The blocks an agent writes to reach its folder, and the answers SwarmUP gives.
# ==============
# The requests of an answer, as (kind, what, part), in their order.
def findRequests(answer):
    requests = []
    for kind, attributes, value in REQUEST_PATTERN.findall(answer):
        part = PART_PATTERN.search(attributes)
        if cleanRequest(value) or kind.lower() == "list":
            requests.append((kind.lower(), cleanRequest(value), int(part.group(1)) if part else 1))
    return requests


# places are those of the mission (MissionMemory.roots): a request can name them, like <swarmup_read>@memory/PROGRESS.md</swarmup_read>.
def answerRequest(folder, kind, value, part, places=None):
    try:
        place, folder, value = splitPlace(places or {}, value, folder or ".")
        if not place and folder == Path("."):
            raise ValueError("You have no folder of your own. Read the places of your mission instead: " + ", ".join(places or {}) + ".")
        if place in ("@memory", "@long-term") and any(name.startswith(".") for name in Path(value).parts):
            raise ValueError(f"{place}/{value} is kept by SwarmUP itself and cannot be reached.")
        if kind == "read":
            return readFile(folder, value, part)
        if kind == "list":
            return describeTree(folder, value)
        return findFiles(folder, value) if kind == "find" else searchFiles(folder, value)
    except (ValueError, OSError) as error:
        return f"Not possible: {error}"


# What SwarmUP answers to the requests of one round. All together they stay under ROUND_CHARS characters.
def answerRequests(folder, requests, places=None):
    replies, used = [], 0
    for kind, value, part in requests:
        reply = answerRequest(folder, kind, value, part, places)
        if used + len(reply) > ROUND_CHARS:
            reply = reply[:max(ROUND_CHARS - used, 0)] + "\n[Cut here: too much was asked at once. Ask for the rest in your next answer.]"
        used += len(reply)
        replies.append(f"=== {kind} {value or '(the whole folder)'}{f' (part {part})' if part > 1 else ''} ===\n{reply}")
    return "\n\n".join(replies)


def describeRequests(requests):
    return ", ".join(f"{REQUEST_WORDS[kind]} {value or 'the whole folder'}" + (f" (part {part})" if part > 1 else "") for kind, value, part in requests)


# The notes of an answer go to the memory of the agent, and the answer is given back without them.
def takeNotes(answer, loop):
    if not NOTE_PATTERN.search(answer):
        return answer
    if loop.session:
        loop.session.addNotes(loop.name, [note.strip() for note in NOTE_PATTERN.findall(answer) if note.strip()])
    return NOTE_PATTERN.sub("", answer).strip()


# What an agent knows of its work place, before its task: the files of its folder and what it may ask of them (not for a coding agent, which
# has its own tools), and the notes it kept in this session. tools is False for the calls that only check a draft.
def describeWorkplace(loop, prompt, tools):
    if not tools:
        return prompt
    parts = []
    if loop.folder and not hasattr(loop.agent, "attach"):
        parts.append(prompts.FOLDER_TOOLS_PROMPT.format(folder=loop.folder, tree=describeTree(loop.folder), rounds=MAX_TOOL_ROUNDS))
    if loop.session:
        how = prompts.MEMORY_CODING_HOW if hasattr(loop.agent, "attach") else prompts.MEMORY_BLOCKS_HOW
        parts += [describeMemory(loop.session.memory, loop.name, how=how), prompts.NOTES_PROMPT]
    return "\n\n".join([*parts, prompt])


# Asks the model of the loop (ask gives its answer) and answers the requests of files it writes, for MAX_TOOL_ROUNDS rounds at most.
# The user is told what the agent reads. The answer that is given back has no request and no note left in it.
def consultFolder(loop, prompt, ask, tools):
    reaches = tools and not hasattr(loop.agent, "attach") and (loop.folder or loop.session)
    places = loop.session.memory.roots() if loop.session else {}
    for number in range(MAX_TOOL_ROUNDS + 1):
        answer = takeNotes(ask(prompt), loop) if tools else ask(prompt)
        requests = findRequests(answer) if reaches else []
        if not requests:
            return answer
        if number == MAX_TOOL_ROUNDS:
            return REQUEST_PATTERN.sub("", answer).strip()
        loop.notifyUser(f"[{loop.name}] {describeRequests(requests)} in its folder.")
        last = number + 1 == MAX_TOOL_ROUNDS
        prompt += "\n\n" + prompts.FOLDER_ANSWER_PROMPT.format(asked=answer.strip(), files=answerRequests(loop.folder, requests, places),
                                                              next=prompts.FOLDER_LAST_ROUND if last else prompts.FOLDER_NEXT_ROUND)
    return answer


# ==============
# Where the work is saved.
# ==============
# The name of one run of a swarm: its time, then the first words of its mission.
def makeRunName(mission, moment=None):
    slug = "-".join(re.findall(r"\w+", (mission or "").lower())[:6])[:40].strip("-_")
    return f"{moment or datetime.now():%Y-%m-%d_%H-%M-%S}" + (f"_{slug}" if slug else "")


def resultsFolder(folder, runName):
    return Path(folder) / RESULTS_FOLDER / runName if folder else harness_utils.AGENT_FILES / LOOSE_RESULTS_FOLDER / runName


# The folder where a coding agent works: the folder of its loop, or an empty one of its own (in the session of its swarm), so it never reads
# the files of the user unasked.
def agentWorkspace(loop):
    if loop is not None and loop.folder:
        return Path(loop.folder)
    if loop is not None and getattr(loop, "session", None):
        return loop.session.workspace(loop.name)
    folder = harness_utils.AGENT_FILES / WORKSPACES_FOLDER / safeName(loop.name if loop is not None else "agent")
    folder.mkdir(parents=True, exist_ok=True)
    return folder
