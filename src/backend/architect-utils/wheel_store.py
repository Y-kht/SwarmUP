# The wheels: the scripts the architect writes from what the agents did again and again (architect_manager.py). They are in the long-term
# memory, in a hidden folder (long-term-memory/.wheels) that no agent reaches with its tools and that is never put in a prompt: an agent gets
# a wheel only as a tool, when its leader gives it (server_wheels.py). A wheel is one of two kinds:
# - a script, <name>.py: a literal WHEEL = {name, summary, kind: "script", inputs, required, changes, network, timeout} and run(args, folder),
#   run by wheel_runner.py in a process of its own;
# - steps, <name>.json: {name, summary, kind: "steps", inputs, required, steps: [{tool, arguments, save}]}, a short list of calls of the tools
#   of the agent, run through the gateway like the calls of the agent itself.
# catalog.json keeps what SwarmUP knows of each wheel: its version, the hash of its text, its scope (the project it was made in, or every
# project), the patterns it covers, who made it, and whether it was retired (and into which wheel it was merged: the old name stays an alias).
# Every version is kept in .history/<name>@<hash8>: the version an agent was shown is the one that runs. approvals.json holds the versions the
# user allowed always (the pin, <name>@<hash8>): a changed wheel is a new version, and is asked again.
import ast
import hashlib
import json
from datetime import datetime
from pathlib import Path

from mission_memory import folderLock, longTermFolder, readJson, readText, writeJson, writeText


WHEELS_FOLDER = ".wheels"
CATALOG_FILE = "catalog.json"
APPROVALS_FILE = "approvals.json"
HISTORY_FOLDER = ".history"
SUFFIXES = {"script": ".py", "steps": ".json"}


def wheelsFolder():
    return longTermFolder() / WHEELS_FOLDER


def now():
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


def hashText(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def pinOf(name, digest):
    return f"{name}@{digest[:8]}"


# The manifest of a wheel from its text: the literal WHEEL of a script, or the JSON of steps. It raises ValueError when there is none.
def readManifest(text, kind):
    if kind == "steps":
        manifest = json.loads(text)
        if not isinstance(manifest, dict):
            raise ValueError("A wheel of steps is a JSON object.")
        return manifest
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "WHEEL" for target in node.targets):
            manifest = ast.literal_eval(node.value)
            if not isinstance(manifest, dict):
                raise ValueError("WHEEL must be a dictionary.")
            return manifest
    raise ValueError("A script wheel starts with WHEEL = {...}, a dictionary written out in full.")


def readCatalog():
    catalog = readJson(wheelsFolder() / CATALOG_FILE, {})
    catalog.setdefault("wheels", {})
    catalog.setdefault("aliases", {})
    return catalog


def writeCatalog(catalog):
    writeJson(wheelsFolder() / CATALOG_FILE, catalog)


def wheelPath(name, kind):
    return wheelsFolder() / f"{name}{SUFFIXES[kind]}"


def historyPath(name, digest, kind):
    return wheelsFolder() / HISTORY_FOLDER / f"{pinOf(name, digest)}{SUFFIXES[kind]}"


# A wheel as SwarmUP uses it: its entry of the catalog, its manifest and its text, at its current version or at a pinned one.
def loadWheel(name, pin=None):
    entry = readCatalog()["wheels"].get(name)
    if entry is None:
        return None
    digest = pin.partition("@")[2] if pin else entry["hash"]
    path = historyPath(name, digest, entry["kind"]) if pin else wheelPath(name, entry["kind"])
    text = readText(path)
    if not text:
        return None
    try:
        manifest = readManifest(text, entry["kind"])
    except (ValueError, SyntaxError):
        return None
    actual = hashText(text)
    return {**entry, "manifest": manifest, "text": text, "path": path if pin else historyPath(name, actual, entry["kind"]), "hash": actual,
            "pin": pinOf(name, actual)}


# The version of a wheel an agent is shown is kept, so it is the one that runs, even if the file of the wheel changes meanwhile.
def keepVersion(wheel):
    path = historyPath(wheel["name"], wheel["hash"], wheel["kind"])
    if not path.exists():
        writeText(path, wheel["text"])


def visibleIn(entry, project):
    return entry.get("scope") == "global" or entry.get("project") == project or project is None


# The live wheels a project sees (its own and the global ones), by name.
def catalogFor(project=None):
    return {name: entry for name, entry in sorted(readCatalog()["wheels"].items()) if not entry.get("retired") and visibleIn(entry, project)}


def realName(name, catalog=None):
    catalog = catalog or readCatalog()
    seen = set()
    while name in catalog["aliases"] and name not in seen:
        seen.add(name)
        name = catalog["aliases"][name]
    return name


# The wheels of names a project can use: ([wheels], [names that are not wheels of the project]). An old name of a merged wheel gives the new one.
def resolve(names, project=None):
    catalog = readCatalog()
    found, missing, seen = [], [], set()
    for name in names:
        real = realName(str(name).strip(), catalog)
        entry = catalog["wheels"].get(real)
        if entry is None or entry.get("retired") or not visibleIn(entry, project):
            missing.append(name)
            continue
        if real not in seen:
            seen.add(real)
            wheel = loadWheel(real)
            if wheel is None:
                missing.append(name)
            else:
                found.append(wheel)
    return found, missing


# Saves a new wheel or a new version of one. replaces: the wheels it merges, which are retired into it and keep their names as aliases.
def saveWheel(name, kind, text, summary, covers, scope, project, author, replaces=()):
    folder = wheelsFolder()
    digest = hashText(text)
    with folderLock(folder):
        catalog = readCatalog()
        entry = catalog["wheels"].get(name) or {"name": name, "version": 0, "created": now()}
        if entry.get("hash") == digest and not entry.get("retired"):
            return entry, False
        if entry.get("kind") and entry["kind"] != kind:
            wheelPath(name, entry["kind"]).unlink(missing_ok=True)
        entry.update(kind=kind, version=entry["version"] + 1, hash=digest, summary=summary, covers=sorted(set(covers)), scope=scope,
                     project=None if scope == "global" else project, author=author, updated=now(), retired=None)
        writeText(wheelPath(name, kind), text)
        writeText(historyPath(name, digest, kind), text)
        catalog["wheels"][name] = entry
        catalog["aliases"].pop(name, None)
        for old in replaces:
            if old != name and old in catalog["wheels"]:
                catalog["wheels"][old]["retired"] = {"time": now(), "why": f"merged into {name}", "into": name}
                catalog["aliases"][old] = name
                wheelPath(old, catalog["wheels"][old]["kind"]).unlink(missing_ok=True)
        writeCatalog(catalog)
    return entry, True


def retireWheel(name, why, by="architect"):
    with folderLock(wheelsFolder()):
        catalog = readCatalog()
        entry = catalog["wheels"].get(name)
        if entry is None or entry.get("retired"):
            return None
        entry["retired"] = {"time": now(), "why": why, "into": None, "by": by}
        wheelPath(name, entry["kind"]).unlink(missing_ok=True)
        writeCatalog(catalog)
    return entry


def readApprovals():
    return readJson(wheelsFolder() / APPROVALS_FILE, {})


def isApproved(pin):
    return pin in readApprovals()


def approve(pin, how):
    with folderLock(wheelsFolder()):
        approvals = readApprovals()
        approvals[pin] = {"time": now(), "how": how}
        writeJson(wheelsFolder() / APPROVALS_FILE, approvals)


# Takes back the approvals of a wheel (all its versions). It gives how many there were.
def revoke(name):
    with folderLock(wheelsFolder()):
        approvals = readApprovals()
        kept = {pin: value for pin, value in approvals.items() if pin.partition("@")[0] != name}
        writeJson(wheelsFolder() / APPROVALS_FILE, kept)
    return len(approvals) - len(kept)


# The wheels that cover a pattern (its fingerprint), live ones only.
def coveringWheels(fingerprint, project=None):
    return [name for name, entry in catalogFor(project).items() if fingerprint in (entry.get("covers") or [])]


def describeWheel(entry):
    scope = "every project" if entry.get("scope") == "global" else "this project"
    return f"{entry['name']} (v{entry['version']}, {entry['kind']}, {scope}): {entry.get('summary') or ''}"


def textOf(path):
    return readText(Path(path))
