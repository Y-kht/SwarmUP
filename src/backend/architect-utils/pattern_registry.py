# What the architects of all projects share, in the hidden folder of the architect (long-term-memory/.architect), which only architects read:
# - the registry (registry.sqlite): for each pattern seen at least twice in a project, how many times, in which projects. A step that repeats
#   a little in each of several projects repeats in fact, and is worth a wheel (status cross);
# - the notes (notes.jsonl): what an architect saw that did not repeat enough yet for a wheel, in a few sentences, with the patterns it is
#   about. A later architect of another project reads them, and recognises the same pattern in its own project.
import json
import re
import sqlite3
import threading
import time
from datetime import datetime

from mission_memory import folderLock, readText
from project_identity import architectFolder


REGISTRY_FILE = "registry.sqlite"
NOTES_FILE = "notes.jsonl"
NOTE_CHARS = 1200
CROSS_TOTAL = 3
NOTE_MATCH = 0.35
WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
COMMON = frozenset("the and for with that this from then into when what which are was were has have had not but you your its their them they will "
                   "can could should would after before again each other than more most some such only also very just agent agents step steps".split())


class Registry:
    def __init__(self, path=None):
        self.path = path or architectFolder() / REGISTRY_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS shared(fp TEXT, project TEXT, kind TEXT, shape TEXT, label TEXT, count INTEGER, status TEXT, lastSeen REAL, "
                                "PRIMARY KEY(fp, project))")

    def close(self):
        with self.lock:
            self.connection.close()

    def publish(self, project, rows):
        values = [(row["fp"], project, row["kind"], row["shape"], row["label"], row["count"], row["status"], row["lastSeen"]) for row in rows]
        if not values:
            return
        with self.lock:
            self.connection.executemany("INSERT INTO shared(fp, project, kind, shape, label, count, status, lastSeen) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(fp, project) DO UPDATE SET "
                                        "count=excluded.count, status=excluded.status, lastSeen=excluded.lastSeen, label=excluded.label", values)

    # For each fingerprint, what the other projects saw of it: {fp: {"projects": [ids], "total": count}}.
    def others(self, project, fps):
        found = {}
        fps = list(fps)
        with self.lock:
            for start in range(0, len(fps), 500):
                part = fps[start:start + 500]
                for fp, other, count in self.connection.execute(f"SELECT fp, project, count FROM shared WHERE project<>? AND fp IN ({','.join('?' * len(part))})",
                                                                [project, *part]).fetchall():
                    entry = found.setdefault(fp, {"projects": [], "total": 0})
                    entry["projects"].append(other)
                    entry["total"] += count
        return found

    def forget(self, project):
        with self.lock:
            self.connection.execute("DELETE FROM shared WHERE project=?", (project,))


def notesPath():
    return architectFolder() / NOTES_FILE


def keywords(text):
    return {word for word in WORD.findall(str(text or "").lower()) if word not in COMMON}


def readNotes():
    notes = []
    for line in readText(notesPath()).splitlines():
        try:
            note = json.loads(line)
        except ValueError:
            continue
        if isinstance(note, dict) and note.get("id"):
            notes.append(note)
    return notes


def saveNote(project, projectName, mission, patterns, text, author=""):
    folder = architectFolder()
    with folderLock(folder):
        notes = readNotes()
        note = {"id": f"N{len(notes) + 1}", "time": f"{datetime.now():%Y-%m-%d %H:%M}", "project": project, "projectName": projectName, "mission": mission,
                "author": author, "text": str(text).strip()[:NOTE_CHARS],
                "patterns": [{"fp": row["fp"], "kind": row["kind"], "shape": row["shape"][:200], "count": row["count"]} for row in patterns]}
        with open(notesPath(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(note, ensure_ascii=False) + "\n")
    return note


# The notes that match a text or a project, the best first: those of other projects about the same patterns, then by the words they share.
def findNotes(query="", project=None, fps=(), limit=10):
    words, wanted = keywords(query), set(fps)
    ranked = []
    for note in readNotes():
        same = len(wanted & {pattern["fp"] for pattern in note.get("patterns") or []})
        noteWords = keywords(note["text"] + " " + " ".join(pattern["shape"] for pattern in note.get("patterns") or []))
        overlap = len(words & noteWords) / max(len(words | noteWords), 1) if words else 0.0
        if wanted or words:
            if not same and overlap <= 0:
                continue
        ranked.append((same, overlap, note["time"], note))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in ranked[:limit]]


# The patterns of a project that a note of another project is about, or that it describes in near the same words: {fp: note}.
def matchNotes(rows, project):
    notes = [note for note in readNotes() if note.get("project") != project]
    if not notes:
        return {}
    byFp = {}
    for note in notes:
        for pattern in note.get("patterns") or []:
            byFp.setdefault(pattern["fp"], note)
    described = [(note, keywords(note["text"] + " " + " ".join(pattern["shape"] for pattern in note.get("patterns") or []))) for note in notes]
    found = {}
    for row in rows:
        if row["fp"] in byFp:
            found[row["fp"]] = byFp[row["fp"]]
            continue
        words = keywords(f"{row['label']} {row['shape']}")
        if len(words) < 3:
            continue
        best = max(described, key=lambda item: len(words & item[1]) / max(len(words | item[1]), 1))
        if len(words & best[1]) / max(len(words | best[1]), 1) >= NOTE_MATCH:
            found[row["fp"]] = best[0]
    return found


def describeNote(note):
    shapes = "; ".join(pattern["shape"][:80] for pattern in (note.get("patterns") or [])[:3])
    return f"{note['id']} ({note.get('projectName') or note.get('project')}, {note['time']}): {note['text']}" + (f" [patterns: {shapes}]" if shapes else "")


def now():
    return time.time()
