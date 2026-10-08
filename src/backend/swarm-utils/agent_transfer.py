# Copying and pasting, for every agent (the tools copy_path, copy_text, paste_text and list_clips of agent_tools.py).
# 1. Files and folders are copied exactly (their bytes, their dates, the links as links), also from one place to another: from the folder of the
#    agent to its temp folder to work on a copy or keep a backup, into the memory of the mission, and back. A copy is written under another name
#    first and then renamed, so a copy that fails half way never leaves half a file, and a target that exists is only replaced when asked (in the
#    folder of the user, its original is kept, so it can be put back). A folder is never copied into itself, and a copy too big is refused.
# 2. Blocks of text go through clipboards that the agent names, kept in the session of its swarm (so a swarm that goes on after a stop keeps
#    them). A block is copied with its exact characters, line ends included, so the model never types a long text again, and pasted before a
#    line, after or in place of a passage, or at the end of a file.
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

from agent_storehouse import cleanRequest, extractText, safeName, splitPlace
from mission_memory import readJson, writeJson


COPY_FILES = 20000
COPY_BYTES = 2 * 1024 ** 3
CLIPS_FOLDER = "clipboards"
DEFAULT_CLIP = "main"
PREVIEW_CHARS = 80


def describeSize(files, size):
    amount = f"{size} bytes" if size < 1024 else f"{size / 1024:.1f} KB" if size < 1024 ** 2 else f"{size / 1024 ** 2:.1f} MB"
    return f"{files} {'file' if files == 1 else 'files'}, {amount}"


# How much a copy holds, without following the links. A copy above the limits is refused before anything is written.
def measure(path):
    if not path.is_dir() or path.is_symlink():
        return 1, path.lstat().st_size
    files, size = 0, 0
    for folder, folders, names in os.walk(path):
        for name in names:
            files += 1
            size += (Path(folder) / name).lstat().st_size
            if files > COPY_FILES or size > COPY_BYTES:
                raise ValueError(f"{path.name} is too big to be copied at once (more than {COPY_FILES:,} files or {COPY_BYTES // 1024 ** 3} GB). Copy its parts.")
    return files, size


# Copies under a hidden name next to the target, then renames it: the target is either the old one or the whole copy, never half of it.
def copyExactly(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.swarmup-copy-{os.getpid()}-{threading.get_ident()}"
    try:
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, temporary, symlinks=True)
        else:
            shutil.copy2(source, temporary, follow_symlinks=False)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        os.replace(temporary, target)
    finally:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)
        elif temporary.exists() or temporary.is_symlink():
            temporary.unlink()


# The text of a file with its exact line ends. A Word or PDF document gives its text.
def exactText(path, name):
    if path.is_dir():
        raise ValueError(f"{name} is a folder: copy it with copy_path.")
    if path.suffix.lower() in (".docx", ".pdf"):
        return extractText(path)
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ValueError(f"{name} is a binary file: copy it whole with copy_path.")
    return data.decode("utf-8", errors="replace")


def lineEnd(text):
    return "\r\n" if "\r\n" in text else "\n"


def firstLine(text):
    line = (text.strip().splitlines() or [""])[0]
    return line if len(line) <= PREVIEW_CHARS else line[:PREVIEW_CHARS - 3] + "..."


class TransferTools:
    # ---------- Files and folders. ----------
    # The target of a copy: a folder that exists (or a place itself, like @temp) receives the copy inside it, under the name of the source.
    def copyTarget(self, text, name):
        place, root, rest = splitPlace(self.places, text, self.root)
        rest = cleanRequest(rest)
        base = root / rest if rest else root
        if base.is_dir() and not base.is_symlink():
            inside = (Path(rest) / name).as_posix() if rest else name
            return self.target(f"{place}/{inside}" if place else inside)
        return self.target(text)

    def copy_path(self, source, target, overwrite=False, reason=""):
        origin = self.locate(source)
        destination = self.copyTarget(target, origin.path.name if origin.path != origin.root else (origin.place.strip("@") or self.root.name))
        if destination.path == origin.path or (origin.path.is_dir() and origin.path in destination.path.parents):
            raise ValueError(f"{origin.name} cannot be copied into itself.")
        exists = destination.path.exists() or destination.path.is_symlink()
        if exists and not overwrite:
            raise ValueError(f"{destination.name} already exists. Set overwrite to true to replace it, or copy to another name.")
        if exists and destination.path.is_dir() != origin.path.is_dir():
            raise ValueError(f"{destination.name} is a {'folder' if destination.path.is_dir() else 'file'} and {origin.name} is not: they cannot replace each other.")
        size = describeSize(*measure(origin.path))
        self.allowChange([destination], "copy", f"{origin.name} -> {destination.name} ({size})" + (", in place of what is there" if exists else ""), reason)
        copyExactly(origin.path, destination.path)
        self.settleMemory(destination)
        self.noteUse(origin, "read")
        self.logChange(destination, f"Copied {origin.path} to {destination.path}.")
        return f"Done: copied {origin.name} to {destination.name} ({size})."

    # ---------- Blocks of text. ----------
    def clipsPath(self):
        return self.session.folder / CLIPS_FOLDER / f"{safeName(self.loop.name)}.json"

    def readClips(self):
        return readJson(self.clipsPath(), {})

    def copy_text(self, path="", start_line=None, end_line=None, text=None, clip=DEFAULT_CLIP):
        name = clip.strip() or DEFAULT_CLIP
        if text is not None and path:
            raise ValueError("Give either a path (with its lines) or a text, not both.")
        if text is None:
            if not path:
                raise ValueError("Give the path of a file (and its lines), or the text to copy.")
            spot = self.locate(path)
            lines = exactText(spot.path, spot.name).splitlines(keepends=True)
            start, end = start_line or 1, end_line or len(lines)
            if not lines or not 1 <= start <= end <= len(lines):
                raise ValueError(f"{spot.name} has {len(lines)} lines: start_line and end_line must be between 1 and {len(lines)}, and start_line first.")
            block, source = "".join(lines[start - 1:end]), f"{spot.name}, lines {start} to {end}"
            self.noteUse(spot, "read")
        else:
            block, source = text, "your text"
        clips = self.readClips()
        clips[name] = {"text": block, "source": source, "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
        writeJson(self.clipsPath(), clips)
        return f"Copied {source} into the clipboard {name} ({len(block.splitlines())} lines, {len(block)} characters). Paste it with paste_text."

    def list_clips(self):
        clips = self.readClips()
        lines = [f"- {name}: {len(clip['text'].splitlines())} lines from {clip['source']} ({clip['time']}), starting with: {firstLine(clip['text'])}" for name, clip in clips.items()]
        return "\n".join(lines) or "Your clipboards are empty. Copy a block with copy_text."

    def paste_text(self, path, clip=DEFAULT_CLIP, at_line=None, after_text=None, replace_text=None, at_end=False, reason=""):
        name, clips = (clip or DEFAULT_CLIP).strip(), self.readClips()
        if name not in clips:
            raise ValueError(f"There is no clipboard {name}. Your clipboards: {', '.join(clips) or 'none yet, copy a block with copy_text'}.")
        if sum(choice for choice in (at_line is not None, after_text is not None, replace_text is not None, bool(at_end))) != 1:
            raise ValueError("Say where to paste with exactly one of at_line, after_text, replace_text or at_end.")
        block, spot = clips[name]["text"], self.target(path)
        if not spot.path.exists():
            if not at_end:
                raise ValueError(f"{spot.name} does not exist. Paste with at_end to create it.")
            return self.store(spot, block, None, reason, "create")
        spot = self.locate(path)
        before = self.currentText(spot)
        return self.store(spot, self.pasted(spot, before, block, at_line, after_text, replace_text), before, reason, "change")

    # The new text of the file. A block pasted between two lines ends with a line end, so it never joins the line after it.
    def pasted(self, spot, before, block, at_line, after_text, replace_text):
        if at_line is not None:
            lines = before.splitlines(keepends=True)
            if not 1 <= at_line <= len(lines) + 1:
                raise ValueError(f"{spot.name} has {len(lines)} lines: at_line must be between 1 and {len(lines) + 1} (after the last line).")
            if at_line <= len(lines) and not block.endswith(("\n", "\r")):
                block += lineEnd(before)
            if at_line > len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
                block = lineEnd(before) + block
            return "".join(lines[:at_line - 1]) + block + "".join(lines[at_line - 1:])
        if after_text is None and replace_text is None:
            return before + (lineEnd(before) if before and not before.endswith(("\n", "\r")) else "") + block
        passage = after_text if after_text is not None else replace_text
        found = before.count(passage) if passage else 0
        if found != 1:
            raise ValueError(f"The passage is {'not' if not found else f'{found} times'} in {spot.name}: it must be found exactly once. Read the file and copy a passage that is.")
        return before.replace(passage, passage + block if after_text is not None else block, 1)
