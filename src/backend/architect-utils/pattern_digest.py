# The patterns of a project as the architect reads them (server_architect.py) and the user sees them (swarmup_cli.py patterns): a brief of
# what repeats and is not automated yet, the best first; one pattern in detail, with examples taken from the journals around where it was
# seen; and a search of the journals. What comes from the journals is data, never instructions: it is marked so.
import json
import re
import time
from pathlib import Path

from pattern_registry import describeNote, findNotes
from pattern_store import ACTIONABLE
from project_identity import projectJournals, readProject
from wheel_checks import onlyReads
from wheel_store import catalogFor, loadWheel


BRIEF_CHARS = 12000
CONTEXT_BYTES = 65536
SEARCH_SECONDS = 2.0
SEARCH_BYTES = 64 * 1024 * 1024
CONTAINED = 0.8
NOT_FOR_WHEELS = ("leader__", "user__", "wheels__", "architect__", "team__")
FOCUS_PATTERNS = 3
STREAK_SHAPE = re.compile(r"(\w+__\w+)\(([^)]*)\)")
# The argument that changes from one call to the next: what a search looks for changes before the place it looks in.
DRAFT_KEYS = ("text", "query", "link", "url", "path", "name")
FOCUS_CHARS = 4000


def asData(text):
    return f"<data from=\"journal\">\n{text}\n</data>"


def describeSpread(store, row, dim, limit=4):
    parts = [f"{item['value']} {item['count']}x" for item in store.spreadOf(row["id"], dim, limit)]
    return ", ".join(parts)


def describeRow(store, row, wide=True):
    tokens = f", ~{int(row['tokens']):,} tokens" if row["tokens"] else ""
    seconds = f", {int(row['seconds'])} s" if row["seconds"] >= 1 else ""
    hints = row["data"].get("hints") or []
    line = (f"#{row['id']} {row['kind']} x{row['count']} ({row['requests']} requests, {row['missions']} missions{tokens}{seconds}"
            f"{', ' + ', '.join(hints) if hints else ''}{', ' + row['status'] if row['status'] not in ('repeating',) else ''}): {row['label']}")
    if not wide:
        return line
    details = []
    agents = describeSpread(store, row, "agent")
    if agents:
        details.append(f"by {agents}")
    data = row["data"]
    if data.get("recipes"):
        best = max(data["recipes"].items(), key=lambda item: item[1])
        details.append(f"fixed {best[1]}x by: {best[0]}")
    if data.get("numbers"):
        details.append("numbers: " + ", ".join(f"{name}={round(total / max(count, 1))}±{round((high - low) / 2)}" for name, (low, high, total, count) in data["numbers"].items()))
    if data.get("where"):
        details.append(f"from {data['where']}")
    if data.get("cross"):
        details.append(f"also in {len(data['cross']['projects'])} other projects ({data['cross']['total']}x)")
    if data.get("note"):
        details.append(f"described by note {data['note']}")
    if row.get("wheel"):
        details.append(f"covered by the wheel {row['wheel']}")
    return line + ("\n    " + "; ".join(details) if details else "")


# Keeps the longest sequences: a sequence inside a longer one met nearly as often is the same habit.
def maximal(rows):
    sequences = [row for row in rows if row["kind"] == "sequence"]
    dropped = set()
    for row in sequences:
        steps = row["data"].get("steps") or []
        for other in sequences:
            longer = other["data"].get("steps") or []
            if other is not row and len(longer) > len(steps) and other["count"] >= CONTAINED * row["count"] and contains(longer, steps):
                dropped.add(row["id"])
                break
    return [row for row in rows if row["id"] not in dropped]


# What no wheel can do: a wheel never calls the leader, the user or another wheel (wheel_checks.py), and a call that a model repeats is its own mistake,
# told to it when it happens. They stay in the store, but they only fill the brief of the architect.
def automatable(row):
    shape = str(row.get("shape") or "")
    if row.get("kind") == "badcall" and shape.endswith(": repeated"):
        return False
    return not any(tool in shape for tool in NOT_FOR_WHEELS)


# The patterns to decide about first, the best first: what repeats, can be automated, and costs model steps or a mistake. What only reads comes last.
def focusPatterns(store, limit=FOCUS_PATTERNS, claimedBy=None):
    rows = [row for row in maximal(store.patterns(ACTIONABLE, ("repeating", "cross"), limit=80)) if automatable(row) and (not row.get("claimedBy") or row["claimedBy"] == claimedBy)]
    rows.sort(key=lambda row: (row["kind"] != "streak", row["kind"] in ("action", "reread", "hotspot", "intent", "message", "permission") or bool((row.get("data") or {}).get("reading")), -row["score"]))
    return rows[:limit]


# A wheel that a small model can correct and save, for a streak of a tool: the tool once for each item of a list.
def draftForStreak(row):
    found = STREAK_SHAPE.match(str(row.get("shape") or "")) if row.get("kind") == "streak" else None
    if not found or found.group(1).startswith(NOT_FOR_WHEELS):
        return ""
    tool, keys = found.group(1), [key.strip() for key in found.group(2).split(",")]
    key = next((name for name in DRAFT_KEYS if name in keys), keys[0] if keys and keys[0] else "")
    if not key:
        return ""
    step = tool.rpartition("__")[2]
    draft = {"name": f"{step}_for_many", "summary": f"The step {step.replace('_', ' ')} for each {key} of a list, all in one call. It gives every result back, one by one.", "kind": "steps",
             "inputs": {"items": {"type": "array", "items": {"type": "string"}, "description": f"The {key} of each call."}}, "required": ["items"],
             "steps": [{"tool": tool, "each": "items", "arguments": {key: "{item}"}}]}
    return f"\nA DRAFT OF A WHEEL FOR THIS PATTERN, to correct if you need to, and to save as it is if it fits (kind steps, code is this JSON):\n{json.dumps(draft, ensure_ascii=False)}\n"


# The patterns to decide about, for the prompt of a pass: the first one with its examples, so a small model starts from the work itself.
def renderFocus(store, limit=FOCUS_PATTERNS, claimedBy=None):
    rows = focusPatterns(store, limit, claimedBy)
    if not rows:
        return ""
    others = "\n".join(describeRow(store, row, wide=False) for row in rows[1:])
    text = renderPattern(store, str(rows[0]["id"]), examples=2)
    return f"DECIDE ABOUT THIS PATTERN FIRST:\n{text[:FOCUS_CHARS]}" + (f"\n\nTHEN THESE:\n{others}" if others else "")


def contains(longer, shorter):
    return any(longer[start:start + len(shorter)] == shorter for start in range(len(longer) - len(shorter) + 1))


def readsOnly(name, entry):
    if entry.get("kind") != "steps":
        return False
    return onlyReads((loadWheel(name) or {}).get("manifest"))


def describeWheels(store, project):
    lines = []
    for name, entry in catalogFor(project).items():
        runs = store.query("SELECT count, errors FROM patterns WHERE kind='wheel' AND shape=?", (name,))
        redos = store.query("SELECT label, count FROM patterns WHERE kind='redo' AND shape LIKE ?", (f"{name} %",))
        failures = store.query("SELECT label, count FROM patterns WHERE kind='wheelfail' AND shape LIKE ? ORDER BY count DESC LIMIT 2", (f"{name}:%",))
        used = f"{runs[0]['count']} runs, {runs[0]['errors']} failed" if runs else "never run yet"
        extra = "; ".join([f"{row['label']} ({row['count']}x)" for row in failures + redos] + (["it only reads files: retire it, it saves no step"] if readsOnly(name, entry) else []))
        lines.append(f"- {name} v{entry['version']} ({entry['kind']}, {'every project' if entry.get('scope') == 'global' else 'this project'}): "
                     f"{entry.get('summary') or ''} [{used}{'; ' + extra if extra else ''}]")
    return lines


# What the architect reads at the start of a pass: since is the time of its last pass (what grew after it is marked new).
def renderBrief(store, project, since=None, chars=BRIEF_CHARS, claimedBy=None):
    info, counts = readProject(project), store.counts()
    statuses = ", ".join(f"{count} {status}" for status, count in sorted(counts["byStatus"].items()))
    parts = [f"PROJECT {info.get('name') or project} ({info.get('folder') or 'no folder'}): {counts['missions']} missions, {counts['entries']:,} journal entries, "
             f"{counts['patterns']} patterns ({statuses or 'none yet'})."]
    sections = [("REPEATING, NOT AUTOMATED YET (the best first)", ("repeating",)), ("SEEN IN OTHER PROJECTS TOO", ("cross",)),
                ("EMERGING (seen twice: write a note when it looks general)", ("emerging",))]
    for title, statuses in sections:
        rows = [row for row in maximal(store.patterns(ACTIONABLE, statuses, limit=60)) if automatable(row) and (not row.get("claimedBy") or row["claimedBy"] == claimedBy)]
        if not rows:
            continue
        shown = rows[:12] if statuses == ("emerging",) else rows[:25]
        lines = [describeRow(store, row, wide=statuses != ("emerging",)) + (" [new]" if since and row["lastSeen"] >= since else "") for row in shown]
        parts.append(f"{title}:\n" + "\n".join(lines))
    failing = store.patterns(("wheelfail", "redo"), None, limit=10)
    if failing:
        parts.append("WHEELS THAT FAIL OR ARE NOT USED:\n" + "\n".join(describeRow(store, row) for row in failing))
    wheels = describeWheels(store, project)
    parts.append("THE WHEELS OF THIS PROJECT:\n" + ("\n".join(wheels) if wheels else "- none yet"))
    notes = findNotes(project=project, fps=[row["fp"] for row in store.patterns(ACTIONABLE, ("cross", "emerging", "repeating"), limit=60)], limit=5)
    if notes:
        parts.append("NOTES OF ARCHITECTS THAT MAY BE ABOUT THIS PROJECT:\n" + "\n".join(f"- {describeNote(note)}" for note in notes))
    text, cut = "\n\n".join(parts), "\n[... cut: read the patterns one by one with architect__read_pattern]"
    return text if len(text) <= chars else text[:chars - len(cut)] + cut


def readLine(path, offset):
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return json.loads(handle.readline().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


# The entries of the same agent just before an example: what led to it.
def contextOf(path, offset, agent, count=3):
    try:
        with open(path, "rb") as handle:
            start = max(0, offset - CONTEXT_BYTES)
            handle.seek(start)
            data = handle.read(offset - start)
    except OSError:
        return []
    entries = []
    for raw in data.split(b"\n")[1 if start else 0:]:
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if entry.get("agent") == agent and entry.get("kind") in ("request", "step", "call", "bad", "user", "draft", "review", "proposal"):
            entries.append(entry)
    return entries[-count:]


def describeEntry(entry):
    kind = entry.get("kind")
    if kind == "step":
        calls = ", ".join(call.get("name") or "" for call in entry.get("calls") or [])
        return f"[{entry.get('agent')} wrote] {entry.get('text') or ''}" + (f"\n  then called: {calls}" if calls else "")
    if kind == "call":
        return f"[{entry.get('agent')} called {entry.get('server')}__{entry.get('tool')}] {json.dumps(entry.get('args'), ensure_ascii=False)[:600]}\n  -> {entry.get('outcome')}: {(entry.get('result') or '')[:900]}"
    if kind == "request":
        return f"[request to {entry.get('agent')}] {(entry.get('prompt') or '')[:600]}"
    return f"[{kind} {entry.get('agent')}] {json.dumps({key: value for key, value in entry.items() if key not in ('v', 'seq', 'pid', 'project', 'run')}, ensure_ascii=False)[:900]}"


def renderPattern(store, ident, examples=3):
    row = store.pattern(ident)
    if row is None:
        return f"There is no pattern {ident}. The brief lists them as #number."
    parts = [describeRow(store, row), f"Its shape: {row['shape']}", f"First seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['firstSeen']))}, "
             f"last seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['lastSeen']))}. Missions: {describeSpread(store, row, 'mission', 6)}."]
    links = store.linksOf(row["id"])
    if links:
        parts.append("Linked: " + "; ".join(f"{link['kind']}: {link['toLabel'] if link['a'] == row['id'] else link['fromLabel']} ({link['count']}x)" for link in links[:6]))
    if row["data"].get("trace"):
        parts.append(f"The last traceback of the wheel:\n{row['data']['trace']}")
    for example in store.examplesOf(row["id"], examples):
        source = example["source"]
        lines = []
        if not source.startswith("legacy:") and Path(source).exists():
            lines += [describeEntry(entry) for entry in contextOf(source, example["offset"], example["agent"])]
            entry = readLine(source, example["offset"])
            lines.append(describeEntry(entry) if entry else example["text"])
        else:
            lines.append(example["text"])
        parts.append(asData(f"Example: mission {example['mission']}, {example['agent']}\n" + "\n".join(lines)))
    return "\n\n".join(parts)


# Searches the journals of the project for a text, the newest first, in SEARCH_SECONDS at most.
def searchJournal(project, text, agent=None, kind=None, limit=10):
    needle, started, found = str(text or "").lower(), time.time(), []
    journals = sorted(projectJournals(project).items(), key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0, reverse=True)
    for mission, path in journals:
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                handle.seek(max(0, size - SEARCH_BYTES))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if time.time() - started > SEARCH_SECONDS or len(found) >= limit:
                break
            if needle and needle not in line.lower():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (agent and entry.get("agent") != agent) or (kind and entry.get("kind") != kind):
                continue
            found.append(f"mission {mission}: {describeEntry(entry)}")
    if not found:
        return f"Nothing in the journals of this project contains {text!r}."
    return asData("\n\n".join(found))


# The patterns as a table, for the command line: one line each.
def renderTable(store, kinds=ACTIONABLE, statuses=None, limit=30):
    rows = maximal(store.patterns(kinds, statuses, limit=limit * 2))[:limit]
    return "\n".join(describeRow(store, row, wide=False) for row in rows) or "No pattern yet."
