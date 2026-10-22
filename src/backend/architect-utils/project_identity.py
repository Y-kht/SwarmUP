# The project a mission belongs to. The patterns of the agents are counted by project: what an agent does again and again in the same code
# is worth a wheel, even across missions. A project is the folder the swarm works in, or the root of its git repository when the folder is
# inside one (a mission on src/ and a mission on tests/ of the same repository are the same project). A mission without a folder belongs to
# the project "general". What SwarmUP knows of a project (its missions and their journals) is kept by the architect, in the hidden folder of
# the long-term memory, where no agent reaches it: long-term-memory/.architect/projects/<id>/.
import hashlib
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from mission_memory import folderLock, longTermFolder, missionFolder, readJson, writeJson, STATE_FOLDER


ARCHITECT_FOLDER = ".architect"
PROJECTS_FOLDER = "projects"
PROJECT_FILE = "project.json"
GENERAL = "general"


def architectFolder():
    return longTermFolder() / ARCHITECT_FOLDER


def projectDir(projectId):
    return architectFolder() / PROJECTS_FOLDER / projectId


def gitRoot(path):
    for folder in (path, *path.parents):
        if (folder / ".git").exists():
            return folder
    return None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "project"


# The project of a folder: (its id, its root). The id stays the same for the same root, whatever program asks.
def projectFor(folder):
    if not folder:
        return GENERAL, None
    path = Path(folder).expanduser().resolve()
    root = gitRoot(path) or path
    return f"{slug(root.name)}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}", root


# The project of a swarm: the folder most of its agents work in (the first one when they are as many).
def findProject(folders):
    folders = [str(Path(folder).expanduser().resolve()) for folder in folders if folder]
    if not folders:
        return projectFor(None)
    counts = Counter(folders)
    best = max(counts.values())
    return projectFor(next(folder for folder in folders if counts[folder] == best))


def projectOf(swarm):
    return findProject([getattr(member["agent"], "folder", None) for member in list(swarm.members.values())])


def readProject(projectId):
    return readJson(projectDir(projectId) / PROJECT_FILE, {})


# Writes down that a mission belongs to the project, so the miner of the project reads its journal, whatever program runs it.
def registerMission(projectId, root, missionId, journalPath):
    folder = projectDir(projectId)
    now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    with folderLock(folder):
        project = readProject(projectId)
        project.update(id=projectId, name=root.name if root else GENERAL, folder=str(root) if root else None)
        project.setdefault("created", now)
        missions = project.setdefault("missions", {})
        missions.setdefault(missionId, {"first": now})
        missions[missionId].update(journal=str(journalPath), last=now)
        writeJson(folder / PROJECT_FILE, project)
    return project


def unregisterMission(projectId, missionId):
    folder = projectDir(projectId)
    with folderLock(folder):
        project = readProject(projectId)
        (project.get("missions") or {}).pop(missionId, None)
        writeJson(folder / PROJECT_FILE, project)


# The journals of the missions of a project that still exist: {mission id: path}.
def projectJournals(projectId):
    found = {}
    for missionId, mission in (readProject(projectId).get("missions") or {}).items():
        path = Path(mission.get("journal") or missionFolder(missionId) / STATE_FOLDER / "journal.jsonl")
        if path.exists():
            found[missionId] = path
    return found


def listProjects():
    folder = architectFolder() / PROJECTS_FOLDER
    if not folder.is_dir():
        return []
    return [project for project in (readProject(path.name) for path in sorted(folder.iterdir()) if path.is_dir()) if project.get("id")]


# The project a user means on the command line: an id, a name, or a folder.
def resolveProject(text):
    if not text:
        return None
    projects = listProjects()
    for project in projects:
        if text in (project["id"], project.get("name")):
            return project["id"]
    if Path(text).expanduser().exists():
        return projectFor(text)[0]
    return None
