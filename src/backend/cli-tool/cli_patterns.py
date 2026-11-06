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

from architect_admin import approveWheel, architectNotes, hasPatterns, mine, openStore, patternBrief, patternDetail, patternTable, requireWheel, retireByUser, revokeWheel, searchJournals, wheelTable
from pattern_store import ACTIONABLE
from project_identity import listProjects, projectFor, resolveProject
from wheel_store import describeWheel


PATTERN_ACTIONS = ("list", "brief", "show", "search", "mine", "projects")


def addPatternArguments(commands):
    patterns = commands.add_parser("patterns", help="the patterns the agents of a project repeat: steps, errors, reasoning (list, brief, show, search, mine, projects)")
    patterns.add_argument("action", nargs="?", default="list", help="list (the default, and it can be left out before a project), brief, show, search, mine or projects")
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
    if not hasPatterns(project):
        known = listProjects()
        if len(known) == 1:
            return known[0]["id"]
        raise ValueError("Name the project (its id, name or folder). See them with: swarmup_cli.py patterns projects")
    return project


def runPatterns(options):
    if options.action not in PATTERN_ACTIONS:
        if options.target:
            raise ValueError(f"There is no command patterns {options.action}. The commands: {', '.join(PATTERN_ACTIONS)}.")
        options.action, options.target = "list", options.action
    if options.action == "projects":
        projects = listProjects()
        return "\n".join(f"- {project['id']}: {project.get('folder') or 'no folder'}, {len(project.get('missions') or {})} missions" for project in projects) or \
            "No project yet: the architect learns from the swarms that run from now on."
    if options.action == "search":
        return searchJournals(projectOf(options.project), options.target, limit=options.top)
    if options.action == "show":
        return patternDetail(projectOf(options.project), options.target)
    project = projectOf(options.target or options.project)
    if options.action == "mine":
        return mine(project, options.rebuild)
    if options.action == "brief":
        return patternBrief(project)
    if options.json:
        store = openStore(project)
        try:
            return json.dumps(store.patterns(tuple(options.kind) or ACTIONABLE, options.status or None, limit=options.top), ensure_ascii=False, indent=1, default=str)
        finally:
            store.close()
    return patternTable(project, options.kind, options.status, options.top)


def runWheels(options):
    if options.action == "notes":
        return "\n".join(f"- {note['text']}" for note in architectNotes(options.query)) or "No note of an architect yet."
    if options.action == "list":
        return wheelTable(projectOf(options.project) if options.project else None)
    if not options.name:
        raise ValueError(f"Name the wheel: swarmup_cli.py wheels {options.action} NAME")
    if options.action == "show":
        wheel = requireWheel(options.name)
        return f"{describeWheel(wheel)}\nIt covers: {', '.join(wheel.get('covers') or []) or 'nothing recorded'}\nMade by: {wheel.get('author')}\n\n{wheel['text']}"
    if options.action == "approve":
        return approveWheel(options.name, "on the command line")
    if options.action == "revoke":
        return revokeWheel(options.name)
    return retireByUser(options.name, options.why)


def runArchitectCommand(options):
    print(runPatterns(options) if options.command == "patterns" else runWheels(options))
