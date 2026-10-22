# The miner of patterns of one project: it reads the journals of the missions of the project (agent_journal.py) as they grow, finds in each
# entry the patterns it is an occurrence of (pattern_detectors.py), and counts them in the store of the project (pattern_store.py): how many
# times each step, problem or piece of reasoning came back, by which agent, in which missions. When a pattern starts to repeat (or repeats
# across projects, through the registry and the notes of the architects, pattern_registry.py), it leaves a signal for the architect.
# Each poll reads what the journals got since the last one and writes it in one transaction, with where it stopped in each journal, so an
# entry is counted once, even if the program stops in the middle. miner_service.py runs it in the background while a swarm of the project works.
import json
import time
import traceback

from pattern_detectors import Batch, Detectors
from pattern_legacy import legacyEntries, legacyMissions
from pattern_registry import Registry, matchNotes
from pattern_store import DB_FILE, EMERGING_COUNT, PatternStore, foldData, hintsOf, scoreOf, statusOf
from pattern_tail import POLL_BYTES, readNew
from agent_journal import JOURNAL_FILE
from mission_memory import STATE_FOLDER, missionFolder
from project_identity import listProjects, projectDir, projectJournals, readProject, registerMission, unregisterMission
from wheel_store import catalogFor


BATCH_LINES = 2000
CROSS_TOTAL = 3
SIGNAL_STATUSES = ("repeating", "cross")
# The kinds worth waking the architect for when they start to repeat (the others wait for its next pass).
SIGNALLING = ("decision", "reasoning", "fix", "sequence", "loop", "error", "command", "correction", "badcall", "hotspot", "redo", "wheelfail")
# A wheel that fails, or that an agent who had it worked around, is a wheel to fix: the architect hears of it the first time. A wheel the leader
# did not give (a redo missed) waits for a second time.
SIGNAL_COUNTS = {"wheelfail": 1, "ignored": 1, "missed": 2}


def signalCount(row):
    return SIGNAL_COUNTS.get(row["data"].get("how") if row["kind"] == "redo" else row["kind"])


class Miner:
    def __init__(self, project, store=None, registry=None):
        self.project = project
        self.store = store or PatternStore(projectDir(project) / DB_FILE)
        self.registry = registry
        info = readProject(project)
        self.detectors = Detectors(self.store, project, info.get("folder"))
        self.problems = 0

    def close(self):
        self.store.close()
        if self.registry is not None:
            self.registry.close()

    def registryOf(self):
        if self.registry is None:
            self.registry = Registry()
        return self.registry

    # The wheels of the project, by the fingerprints of the patterns they cover.
    def refreshCovers(self):
        covers = {}
        for name, entry in catalogFor(self.project).items():
            for fp in entry.get("covers") or []:
                covers[fp] = name
        self.detectors.covers = covers
        return covers

    # One poll: what the journals got since the last one. It gives how many entries it read.
    def poll(self, limit=POLL_BYTES):
        self.refreshCovers()
        known = self.store.sources()
        read = 0
        for mission, path in projectJournals(self.project).items():
            source = known.get(str(path)) or {}
            if source.get("gone"):
                continue
            lines, offset, inode, size, restarted = readNew(path, source.get("offset") or 0, source.get("inode") or 0, limit)
            if not lines and not restarted and offset == (source.get("offset") or 0) and source:
                continue
            read += self.apply(str(path), mission, lines, {"inode": inode, "size": size, "offset": offset, "restarted": restarted, "kind": "journal"}, source)
        return read

    # Writes what a part of a journal holds, and where the journal was read up to, in one transaction.
    def apply(self, path, mission, lines, cursor, source):
        batch, bad = Batch(), 0
        with self.store.transaction() as db:
            for offset, line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(entry, dict):
                    bad += 1
                    continue
                try:
                    self.detectors.process(db, entry, (path, offset), batch)
                except Exception:
                    bad += 1
                    self.problems += 1
                    if self.problems <= 3:
                        traceback.print_exc()
            touched = self.write(db, batch)
            self.detectors.saveTrails(db)
            self.store.setSource(db, path, mission=mission, kind=cursor["kind"], inode=cursor["inode"], size=cursor["size"], offset=cursor["offset"],
                                 lines=(0 if cursor.get("restarted") else source.get("lines") or 0) + len(lines), bad=(source.get("bad") or 0) + bad)
        self.share(touched)
        return len(lines)

    # The hits of a batch in the store: the counts, where they were seen, the examples, the links, then the status and the score of each pattern.
    def write(self, db, batch):
        if not batch.hits:
            return []
        rows = self.store.rowsOf(db, {hit["fp"] for hit in batch.hits})
        before = {fp: row["status"] for fp, row in rows.items()}
        for hit in batch.hits:
            row = rows.get(hit["fp"])
            if row is None:
                row = rows[hit["fp"]] = {"fp": hit["fp"], "kind": hit["kind"], "level": hit["level"], "shape": hit["shape"], "label": hit["label"], "count": 0, "requests": 0,
                                         "agents": 0, "missions": 0, "firstSeen": hit["t"], "lastSeen": hit["t"], "tokens": 0.0, "seconds": 0.0, "size": 0.0,
                                         "errors": 0, "status": "new", "statusAt": hit["t"], "score": 0.0, "data": {}}
                self.store.writeRow(db, row)
            row["count"] += 1
            row["lastSeen"] = max(row["lastSeen"] or 0, hit["t"])
            row["tokens"] += hit["tokens"]
            row["seconds"] += hit["seconds"]
            row["size"] += hit["size"]
            row["errors"] += 1 if hit["error"] else 0
            row["data"] = foldData(row["data"], hit["data"])
            for dim, value in (("agent", hit["agent"]), ("mission", hit["mission"]), ("request", hit["request"])):
                if value and self.store.addSpread(db, row["id"], dim, value):
                    row[{"agent": "agents", "mission": "missions", "request": "requests"}[dim]] += 1
            self.store.addExample(db, row["id"], hit["mission"], hit["agent"], hit["t"], hit["source"], hit["offset"], hit["example"])
        for first, second, kind in batch.links:
            if first in rows and second in rows:
                self.store.addLink(db, rows[first]["id"], rows[second]["id"], kind)
        covers, cross = self.detectors.covers, self.crossOf(rows)
        notes = matchNotes(list(rows.values()), self.project)
        now = time.time()
        for fp, row in rows.items():
            status = statusOf(row, cross=fp in cross or fp in notes, covered=covers.get(fp))
            if status != row["status"]:
                row["status"], row["statusAt"] = status, now
            row["wheel"] = covers.get(fp) or row.get("wheel")
            if fp in notes:
                row["data"]["note"] = notes[fp]["id"]
            if fp in cross:
                row["data"]["cross"] = cross[fp]
            row["data"]["hints"] = hintsOf(row)
            row["score"] = scoreOf(row, now)
            self.store.writeRow(db, row)
            if row["kind"] in SIGNALLING and (row["status"] != before.get(fp) and row["status"] in SIGNAL_STATUSES or signalCount(row) == row["count"]):
                self.store.addSignal(db, row["id"], row["status"] if row["status"] in SIGNAL_STATUSES else row["kind"])
        return [row for row in rows.values() if row["count"] >= EMERGING_COUNT]

    # The patterns other projects saw too: they repeat across projects when their counts together reach CROSS_TOTAL.
    def crossOf(self, rows):
        try:
            others = self.registryOf().others(self.project, rows.keys())
        except Exception:
            return {}
        return {fp: other for fp, other in others.items() if other["total"] + rows[fp]["count"] >= CROSS_TOTAL}

    def share(self, rows):
        try:
            self.registryOf().publish(self.project, rows)
        except Exception:
            traceback.print_exc()

    # Reads again everything of the project: its journals from their start, and the missions without a journal (pattern_legacy.py).
    def rebuild(self):
        path = self.store.path
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            (path.parent / f"{path.name}{suffix}").unlink(missing_ok=True)
        self.store = PatternStore(path)
        self.detectors.store = self.store
        self.detectors.trails = {}
        total = 0
        for mission, state, root in legacyMissions(self.project):
            registerMission(self.project, root, mission, missionFolder(mission) / STATE_FOLDER / JOURNAL_FILE)
            entries = legacyEntries(mission, state)
            for start in range(0, len(entries), BATCH_LINES):
                part = entries[start:start + BATCH_LINES]
                total += self.applyEntries(f"legacy:{mission}", mission, part)
        while True:
            read = self.poll()
            total += read
            if not read:
                return total

    def applyEntries(self, source, mission, items):
        batch = Batch()
        with self.store.transaction() as db:
            for ref, entry in items:
                try:
                    self.detectors.process(db, entry, ref, batch)
                except Exception:
                    self.problems += 1
            touched = self.write(db, batch)
            self.detectors.saveTrails(db)
            self.store.setSource(db, source, mission=mission, kind="legacy", offset=len(items), lines=len(items))
        self.share(touched)
        return len(items)


# A mission the user deleted: its project forgets it, and its examples leave the store (what it counted stays counted).
def forgetMission(missionId):
    for project in listProjects():
        if missionId not in (project.get("missions") or {}):
            continue
        path = projectDir(project["id"]) / DB_FILE
        if path.exists():
            store = PatternStore(path, readOnly=True)
            try:
                store.forgetMission(missionId)
            finally:
                store.close()
        unregisterMission(project["id"], missionId)
