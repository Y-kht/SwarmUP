# The architect server (docs/MCP_DESIGN_BOOKLET.md, Table 4.1): the tools of the architect of a swarm, and of nobody else
# (architect_manager.py). It reads the patterns the miner found in the journals of the project (pattern_digest.py), writes the wheels
# (wheel_store.py, after the checks of wheel_checks.py), and keeps the notes the architects of all projects share (pattern_registry.py).
# Its tools change nothing outside SwarmUP: the wheels and the notes are in the hidden folders of the long-term memory, and the user is told
# of each wheel the architect saves or retires.
from mcp_kit import ServerSpec, ToolProblem, ToolSpec
from pattern_digest import describeRow, describeWheels, maximal, renderBrief, renderPattern, searchJournal
from pattern_registry import describeNote, findNotes, saveNote
from pattern_store import ACTIONABLE
from wheel_checks import checkWheel
from wheel_store import catalogFor, describeWheel, loadWheel, realName, retireWheel, saveWheel


VERSION = "1.0.0"
TEXT = {"type": "string"}
PATTERN_IDS = {"type": "array", "items": {"type": "string"}, "description": "The ids of the patterns, like #12, as the brief shows them."}
WHEEL_FIELDS = {"name": {"type": "string", "description": "The name of the wheel, like run_unit_tests, as in its WHEEL."},
                "kind": {"type": "string", "enum": ["script", "steps"], "description": "script: Python with WHEEL and run(args, folder). steps: JSON with steps."},
                "code": {"type": "string", "description": "The whole text of the wheel."},
                "covers": PATTERN_IDS,
                "scope": {"type": "string", "enum": ["project", "global"], "description": "project (the default), or global for a pattern that repeats across projects."},
                "replaces": {"type": "array", "items": TEXT, "description": "The wheels this one merges: they are retired, and their names lead to it."}}


def isArchitect(agent):
    return bool(getattr(agent.loop, "isArchitect", False))


def deskOf(work):
    desk = getattr(work.loop, "desk", None)
    if desk is None:
        raise ToolProblem("only the architect of a swarm has this tool.")
    return desk


def storeOf(work):
    store = deskOf(work).store()
    if store is None:
        raise ToolProblem("the miner has not read the journals of this project yet. Wait for the next pass.")
    return store


def readPatterns(work, status="", kind="", limit=20):
    desk, store = deskOf(work), storeOf(work)
    if not status and not kind:
        return renderBrief(store, desk.project, desk.since, claimedBy=desk.passId)
    kinds = (kind,) if kind else ACTIONABLE
    rows = maximal(store.patterns(kinds, (status,) if status else None, limit=max(1, min(int(limit), 50))))
    return "\n".join(describeRow(store, row) for row in rows) or "No pattern matches."


def readPattern(work, id, examples=3):
    return renderPattern(storeOf(work), str(id).strip().lstrip("#"), max(1, min(int(examples), 6)))


def searchTheJournal(work, text, agent="", kind="", limit=10):
    return searchJournal(deskOf(work).project, text, agent or None, kind or None, max(1, min(int(limit), 30)))


def readWheels(work, query=""):
    desk, store = deskOf(work), storeOf(work)
    words = {word for word in str(query).lower().split() if len(word) > 2}
    lines = [line for line in describeWheels(store, desk.project) if not words or words & set(line.lower().replace("_", " ").split())]
    return "\n".join(lines) or ("No wheel matches." if words else "No wheel yet in this project.")


def readWheel(work, name):
    wheel = loadWheel(realName(str(name).strip()))
    if wheel is None:
        raise ToolProblem(f"there is no wheel {name}. See them with architect__read_wheels.")
    return f"{describeWheel(wheel)}\nIt covers the patterns: {', '.join(wheel.get('covers') or []) or 'none recorded'}\n\n{wheel['text']}"


def coveredRows(store, covers):
    rows, missing = [], []
    for ident in covers or []:
        row = store.pattern(str(ident).strip().lstrip("#"))
        (rows.append(row) if row else missing.append(str(ident)))
    return rows, missing


def checkProblems(work, name, kind, code, covers, scope, replaces):
    desk, store = deskOf(work), storeOf(work)
    rows, missing = coveredRows(store, covers)
    live = {fp: wheel for wheel, entry in catalogFor(desk.project).items() for fp in entry.get("covers") or []}
    manifest, problems = checkWheel(code, kind, name, rows, scope, live, tuple(replaces or ()))
    if missing:
        problems.insert(0, f"There is no pattern {', '.join(missing)}: give the ids of the brief, like #12.")
    return manifest, rows, problems


def checkTheWheel(work, name, kind, code, covers=(), scope="project", replaces=()):
    manifest, rows, problems = checkProblems(work, name, kind, code, covers, scope, replaces)
    return "It passes the checks: save it with architect__save_wheel." if not problems else "Not ready yet:\n- " + "\n- ".join(problems)


def saveTheWheel(work, name, kind, code, covers=(), scope="project", replaces=(), why=""):
    desk = deskOf(work)
    manifest, rows, problems = checkProblems(work, name, kind, code, covers, scope, replaces)
    if problems:
        raise ToolProblem("the wheel is not saved:\n- " + "\n- ".join(problems))
    manager = desk.manager
    entry, changed = saveWheel(name, kind, code, manifest["summary"], [row["fp"] for row in rows], scope, desk.project,
                               {"mission": manager.missionId, "model": manager.modelName}, [str(old) for old in replaces or ()])
    if not changed:
        return f"{name} is already saved as it is: nothing changed."
    store = desk.store()
    for row in rows:
        store.setStatus(row["id"], "covered", wheel=name)
    manager.onWheel("saved" if entry["version"] == 1 else "updated", entry, why)
    return (f"Saved {name} v{entry['version']} ({entry['kind']}, {'every project' if scope == 'global' else 'this project'}). It covers "
            f"{', '.join('#' + str(row['id']) for row in rows)}. Leaders can give it to agents from now on.")


def retireTheWheel(work, name, why):
    desk = deskOf(work)
    entry = retireWheel(realName(str(name).strip()), str(why).strip() or "retired by the architect")
    if entry is None:
        raise ToolProblem(f"there is no live wheel {name}.")
    desk.manager.onWheel("retired", entry, why)
    return f"{name} is retired: no agent gets it anymore."


def saveTheNote(work, patterns, text):
    desk, store = deskOf(work), storeOf(work)
    rows, missing = coveredRows(store, patterns)
    if missing:
        raise ToolProblem(f"there is no pattern {', '.join(missing)}: give the ids of the brief, like #12.")
    if len(str(text).strip()) < 20:
        raise ToolProblem("a note says in a few sentences what the pattern is and why it may repeat elsewhere.")
    note = saveNote(desk.project, desk.manager.projectName, desk.manager.missionId, rows, text, desk.manager.modelName)
    for row in rows:
        store.setStatus(row["id"], row["status"], note=note["id"])
    desk.manager.onNote(note)
    return f"Kept the note {note['id']}: the architects of the next swarms, of every project, read it."


def readTheNotes(work, query="", limit=10):
    notes = findNotes(query, limit=max(1, min(int(limit), 30)))
    return "\n".join(f"- {describeNote(note)}" for note in notes) or "No note of an architect matches."


def dismissPattern(work, id, why):
    row = storeOf(work).setStatus(str(id).strip().lstrip("#"), "dismissed", note=f"dismissed: {str(why).strip()[:300]}")
    if row is None:
        raise ToolProblem(f"there is no pattern {id}.")
    return f"#{row['id']} is dismissed: it comes back only if it repeats twice as much."


TOOLS = [
    ToolSpec("read_patterns", "Read the patterns the agents of this project repeat, the best first: without filters, the brief of your pass (what repeats and "
             "is not automated, the emerging patterns, the wheels and how they do, the notes that may match). It changes nothing.",
             {"status": {"type": "string", "description": "Only this status: new, emerging, repeating, cross, covered, noted or dismissed."},
              "kind": {"type": "string", "description": "Only this kind, like sequence, error, fix, reasoning, decision, command."},
              "limit": {"type": "integer", "description": "How many, 20 by default, 50 at most."}}, [], readPatterns, readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("read_pattern", "Read one pattern in detail: where and how often it was seen, what it is linked to (its fixes, its decisions), and examples taken "
             "from the journals with what led to them. Read a pattern before you automate it. It changes nothing.",
             {"id": {"type": "string", "description": "Its id, like #12."}, "examples": {"type": "integer", "description": "How many examples, 3 by default, 6 at most."}},
             ["id"], readPattern, readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("search_journal", "Search the journals of the missions of this project for a text, the newest first: what the agents wrote, called and got. Use it "
             "to check a pattern. It changes nothing.", {"text": TEXT, "agent": {"type": "string", "description": "Only this agent."},
                                                       "kind": {"type": "string", "description": "Only this kind of entry: step, call, request, bad, draft, review, user."},
                                                       "limit": {"type": "integer", "description": "How many entries, 10 by default."}},
             ["text"], searchTheJournal, readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("read_wheels", "List the wheels this project can use, with their summary, their runs and failures, and when agents did their work by hand. Read "
             "them before you write a wheel: update or merge one instead of writing a twin. It changes nothing.",
             {"query": {"type": "string", "description": "Only the wheels about these words."}}, [], readWheels, readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("read_wheel", "Read the whole text of a wheel, with the patterns it covers. It changes nothing.", {"name": TEXT}, ["name"], readWheel,
             readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("check_wheel", "Check a wheel without saving it: its WHEEL, its code (nothing runs when it loads, no eval, no secrets, no network unless "
             "declared), its summary, and that the patterns it covers repeat. It gives every problem to correct. It runs nothing and changes nothing.",
             dict(WHEEL_FIELDS), ["name", "kind", "code", "covers"], checkTheWheel, readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("save_wheel", "Save a wheel that passes the checks: a new one, or a new version of one (replaces merges other wheels into it). Leaders can give "
             "it to agents at once, and the user approves each new version the first time it runs. The user is told.",
             {**WHEEL_FIELDS, "why": {"type": "string", "description": "In a sentence, the repetition it saves, for the user."}}, ["name", "kind", "code", "covers", "why"],
             saveTheWheel, readOnly=True, when=isArchitect),
    ToolSpec("retire_wheel", "Retire a wheel that is wrong, unused or replaced: no agent gets it anymore. To merge it into another one, save that one with "
             "replaces instead. The user is told.", {"name": TEXT, "why": TEXT}, ["name", "why"], retireTheWheel, readOnly=True, when=isArchitect),
    ToolSpec("save_note", "Keep a short note about patterns that do not repeat enough yet for a wheel but may repeat in other projects: what they are, and "
             "what to look for. Only architects read the notes, in every project.", {"patterns": PATTERN_IDS, "text": TEXT}, ["patterns", "text"],
             saveTheNote, readOnly=True, when=isArchitect),
    ToolSpec("read_notes", "Read the notes of the architects of all projects, the most relevant first: a pattern they noted that you see too repeats "
             "across projects. It changes nothing.", {"query": {"type": "string", "description": "Words of what you look for."},
                                                      "limit": {"type": "integer", "description": "How many, 10 by default."}},
             [], readTheNotes, readOnly=True, idempotent=True, when=isArchitect),
    ToolSpec("dismiss_pattern", "Mark a pattern that cannot be automated (it differs every time, or it is the real work of the task): it leaves your brief "
             "until it repeats twice as much.", {"id": {"type": "string", "description": "Its id, like #12."}, "why": TEXT}, ["id", "why"], dismissPattern,
             readOnly=True, when=isArchitect),
]

SERVER = ServerSpec("architect", VERSION, "The tools of the architect: the patterns of the agents, the wheels, and the notes of the architects.", TOOLS,
                    when=isArchitect)
