# The commands of the architect in the command line (swarmup_cli.py):
#   patterns [list] [PROJECT] [--kind K] [--status S] [--top N]   the patterns the agents of a project repeat, the best first
#   patterns brief [PROJECT]                                        what the architect reads at the start of a pass
#   patterns show ID [--project P]                                  one pattern, with examples from the journals
#   patterns search TEXT [--project P]                              the entries of the journals that contain a text
#   patterns mine [PROJECT] [--rebuild]                             read the journals now (rebuild: read everything again, old missions too)
#   patterns projects                                               the projects SwarmUP knows
#   wheels [list] [--project P] | show NAME | approve NAME | revoke NAME | retire NAME [--why W] | notes [--query Q]
# A project is given by its id, its name or its folder; without it, the project of the current folder.
import json
import os
import time

from miner_service import ProjectLock, LOCK_FILE
from pattern_digest import renderBrief, renderPattern, renderTable, searchJournal
from pattern_miner import Miner
from pattern_registry import describeNote, findNotes
from pattern_store import ACTIONABLE, DB_FILE, PatternStore
from project_identity import listProjects, projectDir, projectFor, resolveProject
from wheel_store import catalogFor, loadWheel, readApprovals, readCatalog, revoke, approve, retireWheel, describeWheel


def addPatternArguments(commands):
    patterns = commands.add_parser("patterns", help="the patterns the agents of a project repeat: steps, errors, reasoning (list, brief, show, search, mine, projects)")
    patterns.add_argument("action", nargs="?", default="list", choices=("list", "brief", "show", "search", "mine", "projects"))
    patterns.add_argument("target", nargs="?", default="", help="the project (id, name or folder), the id of a pattern for show, or the text for search")
    patterns.add_argument("--project", default="", help="the project, when the first argument is a pattern or a text")
    patterns.add_argument("--kind", action="append", default=[], help="only patterns of this kind (several times for several)")
    patterns.add_argument("--status", action="append", default=[], help="only patterns with this status: new, emerging, repeating, cross, covered, noted, dismissed")
    patterns.add_argument("--top", type=int, default=30)
    patterns.add_argument("--json", action="store_true", help="as JSON, for scripts")
    patterns.add_argument("--rebuild", action="store_true", help="with mine: read everything again from the start, the missions without a journal too")
    wheels = commands.add_parser("wheels", help="the wheels the architect wrote (list, show, approve, revoke, retire, notes)")
    wheels.add_argument("action", nargs="?", default="list", choices=("list", "show", "approve", "revoke", "retire", "notes"))
    wheels.add_argument("name", nargs="?", default="")
    wheels.add_argument("--project", default="", help="only the wheels a project sees")
    wheels.add_argument("--why", default="retired by the user")
    wheels.add_argument("--query", default="", help="with notes: only the notes about these words")


def projectOf(text):
    if text:
        project = resolveProject(text)
        if project is None:
            raise ValueError(f"No project is known as {text!r}. See them with: swarmup_cli.py patterns projects")
        return project
    project = projectFor(os.getcwd())[0]
    if not (projectDir(project) / DB_FILE).exists():
        known = listProjects()
        if len(known) == 1:
            return known[0]["id"]
        raise ValueError("Name the project (its id, name or folder). See them with: swarmup_cli.py patterns projects")
    return project


def openStore(project):
    path = projectDir(project) / DB_FILE
    if not path.exists():
        raise ValueError(f"The project {project} has no pattern yet: its swarms did not run since the architect exists, or mine it with: swarmup_cli.py patterns mine {project}")
    return PatternStore(path, readOnly=True)


def mine(project, rebuild):
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


def runPatterns(options):
    if options.action == "projects":
        projects = listProjects()
        return "\n".join(f"- {project['id']}: {project.get('folder') or 'no folder'}, {len(project.get('missions') or {})} missions" for project in projects) or \
            "No project yet: the architect learns from the swarms that run from now on."
    if options.action in ("show", "search"):
        project = projectOf(options.project)
        if options.action == "search":
            return searchJournal(project, options.target, limit=options.top)
        store = openStore(project)
        try:
            return renderPattern(store, options.target.lstrip("#"), examples=3)
        finally:
            store.close()
    project = projectOf(options.target or options.project)
    if options.action == "mine":
        return mine(project, options.rebuild)
    store = openStore(project)
    try:
        if options.action == "brief":
            return renderBrief(store, project)
        kinds = tuple(options.kind) or ACTIONABLE
        if options.json:
            return json.dumps(store.patterns(kinds, options.status or None, limit=options.top), ensure_ascii=False, indent=1, default=str)
        return renderTable(store, kinds, options.status or None, options.top)
    finally:
        store.close()


def runWheels(options):
    if options.action == "notes":
        notes = findNotes(options.query, limit=50)
        return "\n".join(f"- {describeNote(note)}" for note in notes) or "No note of an architect yet."
    if options.action == "list":
        catalog = catalogFor(projectOf(options.project)) if options.project else {name: entry for name, entry in readCatalog()["wheels"].items() if not entry.get("retired")}
        approvals = readApprovals()
        lines = [f"- {describeWheel(entry)}" + (" [approved always]" if any(pin.startswith(f"{name}@{entry['hash'][:8]}") for pin in approvals) else "")
                 for name, entry in catalog.items()]
        return "\n".join(lines) or "No wheel yet: the architect writes them from what the agents repeat."
    if not options.name:
        raise ValueError(f"Name the wheel: swarmup_cli.py wheels {options.action} NAME")
    wheel = loadWheel(options.name)
    if options.action == "show":
        if wheel is None:
            raise ValueError(f"There is no wheel {options.name}.")
        return f"{describeWheel(wheel)}\nIt covers: {', '.join(wheel.get('covers') or []) or 'nothing recorded'}\nMade by: {wheel.get('author')}\n\n{wheel['text']}"
    if options.action == "approve":
        if wheel is None:
            raise ValueError(f"There is no wheel {options.name}.")
        approve(wheel["pin"], "on the command line")
        return f"Version {wheel['version']} of {options.name} runs without asking from now on. A new version asks again."
    if options.action == "revoke":
        return f"{revoke(options.name)} approvals of {options.name} were taken back: it asks again before it runs."
    if retireWheel(options.name, options.why, by="the user") is None:
        raise ValueError(f"There is no live wheel {options.name}.")
    return f"{options.name} is retired: no agent gets it anymore, and the architect will not write it again unless the agents need it."


def runArchitectCommand(options):
    print(runPatterns(options) if options.command == "patterns" else runWheels(options))
