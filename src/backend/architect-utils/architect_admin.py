# What the user sees and does with what the architect learned, for every program that asks: the command line (cli_patterns.py) and the window
# (session_architect.py). The projects, the patterns of a project, the journals read again, the wheels with their approvals, and the notes.
import time

from miner_service import LOCK_FILE, ProjectLock
from pattern_digest import describeWheels, renderBrief, renderPattern, renderTable, searchJournal
from pattern_miner import Miner
from pattern_registry import describeNote, findNotes
from pattern_store import ACTIONABLE, DB_FILE, PatternStore, hintsOf
from project_identity import listProjects, projectDir
from wheel_store import approve, catalogFor, describeWheel, isApproved, loadWheel, pinOf, readCatalog, retireWheel, revoke


NO_WHEEL = "There is no wheel {name}."


def openStore(project):
    path = projectDir(project) / DB_FILE
    if not path.exists():
        raise ValueError(f"The project {project} has no pattern yet: its swarms did not run since the architect exists, or read its journals now.")
    return PatternStore(path, readOnly=True)


def hasPatterns(project):
    return (projectDir(project) / DB_FILE).exists()


# Reads the journals of a project now: what is new, or everything again with rebuild. What the architect dismissed or noted stays.
def mine(project, rebuild=False):
    lock = ProjectLock(projectDir(project) / LOCK_FILE)
    if not lock.take():
        return "A swarm of this project mines it now: what it found is already in the store."
    started = time.time()
    try:
        miner = Miner(project)
        try:
            read = miner.rebuild() if rebuild else sum(iter(lambda: miner.poll(), 0))
            counts = miner.store.counts()
        finally:
            miner.close()
    finally:
        lock.free()
    return f"Read {read:,} entries in {time.time() - started:.1f} s. The project has {counts['patterns']:,} patterns: " + \
        ", ".join(f"{count} {status}" for status, count in sorted(counts["byStatus"].items()))


def describeProjects():
    return [{"id": project["id"], "name": project.get("name") or project["id"], "folder": project.get("folder"), "missions": len(project.get("missions") or {}),
             "patterns": hasPatterns(project["id"])} for project in listProjects()]


def patternRows(project, kinds=(), statuses=(), limit=30):
    store = openStore(project)
    try:
        return [{"id": row["id"], "kind": row["kind"], "count": row["count"], "requests": row["requests"], "missions": row["missions"], "status": row["status"],
                 "tokens": int(row["tokens"] or 0), "label": row["label"], "hints": hintsOf(row), "wheel": row.get("wheel")}
                for row in store.patterns(tuple(kinds) or ACTIONABLE, list(statuses) or None, limit=limit)]
    finally:
        store.close()


def patternTable(project, kinds=(), statuses=(), limit=30):
    store = openStore(project)
    try:
        return renderTable(store, tuple(kinds) or ACTIONABLE, list(statuses) or None, limit)
    finally:
        store.close()


def patternCounts(project):
    store = openStore(project)
    try:
        return store.counts()
    finally:
        store.close()


def patternBrief(project):
    store = openStore(project)
    try:
        return renderBrief(store, project)
    finally:
        store.close()


def patternDetail(project, ident, examples=3):
    store = openStore(project)
    try:
        return renderPattern(store, str(ident).strip().lstrip("#"), examples=examples)
    finally:
        store.close()


def searchJournals(project, text, limit=30):
    return searchJournal(project, text, limit=limit)


def isWheelApproved(entry):
    return isApproved(pinOf(entry["name"], entry["hash"]))


def wheelTable(project=None):
    catalog = catalogFor(project) if project else {name: entry for name, entry in readCatalog()["wheels"].items() if not entry.get("retired")}
    lines = [f"- {describeWheel(entry)}" + (" [approved always]" if isWheelApproved(entry) else "") for entry in catalog.values()]
    return "\n".join(lines) or "No wheel yet: the architect writes them from what the agents repeat."


# The live wheels a project sees, with how they did: for each, its version, its scope, whether this version is approved always, its runs and failures.
def wheelRows(project=None):
    catalog = catalogFor(project) if project else {name: entry for name, entry in readCatalog()["wheels"].items() if not entry.get("retired")}
    usage = {}
    if project and hasPatterns(project):
        store = openStore(project)
        try:
            usage = {line.split(" v", 1)[0].removeprefix("- "): line.rpartition("[")[2].rstrip("]") for line in describeWheels(store, project)}
        finally:
            store.close()
    return [{"name": name, "version": entry["version"], "kind": entry["kind"], "scope": entry.get("scope"), "summary": entry.get("summary") or "", "updated": entry.get("updated"),
             "approved": isWheelApproved(entry), "usage": usage.get(name, ""), "author": (entry.get("author") or {}).get("model") or ""} for name, entry in catalog.items()]


def requireWheel(name):
    wheel = loadWheel(name)
    if wheel is None:
        raise ValueError(NO_WHEEL.format(name=name))
    return wheel


def wheelDetail(name):
    wheel = requireWheel(name)
    return {"name": wheel["name"], "version": wheel["version"], "kind": wheel["kind"], "scope": wheel.get("scope"), "summary": wheel.get("summary") or "", "covers": wheel.get("covers") or [],
            "author": wheel.get("author") or {}, "approved": isApproved(wheel["pin"]), "text": wheel["text"], "changes": bool(wheel["manifest"].get("changes")),
            "network": bool(wheel["manifest"].get("network"))}


def approveWheel(name, how):
    wheel = requireWheel(name)
    approve(wheel["pin"], how)
    return f"Version {wheel['version']} of {name} runs without asking from now on. A new version asks again."


def revokeWheel(name):
    return f"{revoke(name)} approvals of {name} were taken back: it asks again before it runs."


def retireByUser(name, why):
    if retireWheel(name, str(why or "").strip() or "retired by the user", by="the user") is None:
        raise ValueError(f"There is no live wheel {name}.")
    return f"{name} is retired: no agent gets it anymore, and the architect will not write it again unless the agents need it."


def architectNotes(query="", limit=50):
    return [{"id": note["id"], "text": describeNote(note)} for note in findNotes(query, limit=limit)]
