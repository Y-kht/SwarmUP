# The wheels as the leader sees them: the leader gives each agent only the wheels its task needs, when it proposes the agent (the field wheels)
# or while the swarm works (leader__give_wheels). An agent then gets those wheels as its tools, and nothing else of the long-term memory.
# The leader reads the wheels of the project in its prompt (the most relevant first, in a few lines each), and finds the others with
# leader__find_wheels.
import difflib
import re

from harness_utils import ARCHITECT_NAME
from project_identity import projectFor
from user_settings import currentSetting
from wheel_store import catalogFor, readCatalog, realName


CATALOG_CHARS = 3000
FOUND_WHEELS = 10
# The most tools an agent has without wheels (a leader that manages its swarm has 26, with some room for the tools of other programs).
BASE_TOOLS = 30
WORD = re.compile(r"[a-z][a-z0-9]{2,}")


def projectOfFolder(folder):
    return projectFor(folder)[0]


def wordsOf(text):
    return set(WORD.findall(str(text or "").lower().replace("_", " ")))


def readWheelNames(value):
    if value in (None, "", []):
        return []
    parts = re.split(r",|;|\n", value) if isinstance(value, str) else value if isinstance(value, list) else None
    if parts is None:
        return None
    return [str(part).strip().strip("\"'").removeprefix("wheels__") for part in parts if str(part).strip()]


# The wheels the leader gave an agent, as the project knows them: (the names, the errors to tell the leader).
def checkWheels(value, project):
    names = readWheelNames(value)
    if names is None:
        return [], ["wheels is a list of names of wheels, like [\"run_unit_tests\"]."]
    if not names:
        return [], []
    catalog, everything = catalogFor(project), readCatalog()
    found, errors = [], []
    for name in names:
        real = realName(name, everything)
        if real in catalog:
            if real not in found:
                found.append(real)
            continue
        close = difflib.get_close_matches(name, list(catalog), 3)
        errors.append(f"There is no wheel {name} in this project." + (f" Did you mean {', '.join(close)}?" if close else
                                                                     f" Its wheels: {', '.join(catalog) or 'none yet'}."))
    room = max(currentSetting("maxTools") - BASE_TOOLS, 0)
    if len(found) > room:
        errors.append(f"An agent can take {room} wheels with the most tools of an agent the user set ({currentSetting('maxTools')}): give only the wheels its task needs.")
    return found, errors


def describeEntry(name, entry):
    return f"- {name}: {entry.get('summary') or ''}"


# The wheels of the project for the prompt of the leader, the most relevant to the mission first.
def describeWheelsFor(project, mission, limit=CATALOG_CHARS):
    catalog = catalogFor(project)
    if not catalog:
        return "No wheel yet: the architect writes them from what the agents of this project repeat."
    words = wordsOf(mission)
    ranked = sorted(catalog.items(), key=lambda item: (len(words & wordsOf(f"{item[0]} {item[1].get('summary')}")), item[1].get("scope") != "global",
                                                         item[1].get("updated") or ""), reverse=True)
    lines, size = [], 0
    for name, entry in ranked:
        line = describeEntry(name, entry)
        if size + len(line) > limit:
            lines.append(f"... and {len(ranked) - len(lines)} more: find them with leader__find_wheels.")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def findWheels(project, query, limit=FOUND_WHEELS):
    catalog, words = catalogFor(project), wordsOf(query)
    scored = sorted(((len(words & wordsOf(f"{name} {entry.get('summary')}")), name, entry) for name, entry in catalog.items()), key=lambda item: (item[0], item[1]),
                    reverse=True)
    found = [describeEntry(name, entry) for score, name, entry in scored if score or not words][:limit]
    return "\n".join(found) or (f"No wheel of this project is about {query!r}." if catalog else "This project has no wheel yet.")


# Gives an agent of a running swarm its wheels (they replace the ones it had): it lists its tools again at its next step, and is told.
def giveWheels(swarm, name, wheels, why=""):
    member = swarm.members.get(name)
    if member is None or name == ARCHITECT_NAME:
        raise ValueError(f"There is no agent {name} in the swarm. Its agents: {', '.join(swarm.getAgents())}.")
    names, errors = checkWheels(wheels, swarm.project or projectOfFolder(member["agent"].folder))
    if errors:
        raise ValueError(" ".join(errors))
    agent = member["agent"]
    agent.wheels = list(names)
    recipe = member.get("recipe")
    if recipe is not None:
        recipe.setdefault("answers", {})["wheels"] = list(names)
    link = swarm.gateway.find(name) if swarm.gateway is not None else None
    if link is not None:
        link.stale = True
    if names:
        catalog = catalogFor(swarm.project)
        agent.receive(swarm.getLeader(), "Your leader gave you wheels for your task: " + "; ".join(f"wheels__{wheel}: {catalog[wheel].get('summary') or ''}" for wheel in names) +
                      " Use a wheel instead of doing its steps by hand.")
    if swarm.journal is not None:
        swarm.journal.add("wheel", swarm.getLeader(), action="given", to=name, wheels=list(names), why=str(why or "")[:500])
    swarm.emit("wheel", name, action="given", wheels=list(names), why=str(why or ""))
    return names
