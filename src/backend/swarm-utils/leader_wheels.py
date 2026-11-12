# The wheels as the leader sees them: the leader gives each agent only the wheels its task needs, when it proposes the agent (the field wheels)
# or while the swarm works (leader__give_wheels). An agent then gets those wheels as its tools, and nothing else of the long-term memory.
# The leader reads the wheels of the project in its prompt (the most relevant first, in a few lines each), and finds the others with
# leader__find_wheels. The agents nobody chose wheels for get them when a run starts: an agent alone gets the wheels of its project, and in a
# swarm the leader is asked once which wheels each of them needs (giveStartingWheels).
import difflib
import json
import re
import threading

import agent_prompts as prompts
from harness_utils import ARCHITECT_NAME, stripFences
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


# The words of a text, without the s of a plural, so that "the tests" finds run_unit_test and "a test" finds run_unit_tests.
def wordsOf(text):
    return {word[:-1] if word.endswith("s") and len(word) > 3 else word for word in WORD.findall(str(text or "").lower().replace("_", " "))}


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


# An agent alone in its swarm has no leader to give it wheels: it gets the wheels of its project by itself, the most relevant first, at the start of
# a run and as soon as the architect writes a new one. A list the user chose stays (the answer wheels, even an empty one).
def giveLoneWheels(swarm):
    if len(swarm.members) != 1:
        return []
    name, member = next(iter(swarm.members.items()))
    if "wheels" in ((member.get("recipe") or {}).get("answers") or {}):
        return member["agent"].wheels
    catalog = catalogFor(swarm.project or projectOfFolder(member["agent"].folder))
    ranked = sorted(catalog.items(), key=lambda item: (len(wordsOf(swarm.currentRequest()) & wordsOf(f"{item[0]} {item[1].get('summary')}")), item[1].get("updated") or ""), reverse=True)
    names = [wheel for wheel, entry in ranked][:max(currentSetting("maxTools") - BASE_TOOLS, 0)]
    return names if set(names) == set(member["agent"].wheels) else giveWheels(swarm, name, names, "it works alone, so it gets the wheels of its project", chosen=False)


# The agents that work in this run and that nobody chose wheels for: neither the user (the step of the wheels) nor the leader (the swarm it proposed).
def withoutChoice(swarm):
    return [name for name, member in list(swarm.members.items()) if member["status"] not in ("done", "failed") and member.get("recipe") is not None
            and "wheels" not in (member["recipe"].get("answers") or {})]


# What the leader answered: {agent: [wheels]}, with agents it was asked about and wheels of the project.
def checkGiven(draft, names, project):
    try:
        given = json.loads(stripFences(draft))
    except ValueError:
        given = None
    if not isinstance(given, dict):
        return f"Reply only with JSON like {prompts.LEADER_WHEELS_EXAMPLE}"
    unknown = [name for name in given if name not in names]
    if unknown:
        return f"These are not agents you can give wheels to: {', '.join(unknown)}. Choose from: {', '.join(names)}."
    return " ".join(error for wheels in given.values() for error in checkWheels(wheels, project)[1])


# The leader gives the wheels, also in a swarm the user built: it is asked once which wheels the agents without a choice need for their tasks, and
# its answer is their choice from then on (an empty one too, so it is not asked again). If it cannot answer, nothing changes.
def askLeaderForWheels(swarm, names):
    project = swarm.project
    leader = swarm.getMember(swarm.getLeader())["agent"]
    agents = "\n".join(f"- {name} ({swarm.members[name]['role']}): {swarm.members[name]['task']}" for name in names if name in swarm.members)
    prompt = prompts.LEADER_WHEELS_PROMPT.format(mission=swarm.mission, wheels=describeWheelsFor(project, swarm.currentRequest()), agents=agents,
                                                 example=prompts.LEADER_WHEELS_EXAMPLE)
    try:
        draft = leader.reviewLoop(prompt, lambda draft: checkGiven(draft, names, project), ask=False, own=False)
    except Exception:
        draft = None
    if draft is None or not swarm.active or swarm.stopped:
        return {}
    given = json.loads(stripFences(draft))
    for name in names:
        member = swarm.members.get(name)
        if member is None:
            continue
        if given.get(name):
            giveWheels(swarm, name, given[name], "the leader chose them for its own task" if name == swarm.getLeader() else "its leader chose them for its task")
        else:
            member["recipe"].setdefault("answers", {})["wheels"] = []
    return given


# The wheels of the agents nobody chose wheels for, when a run starts. An agent alone gets the wheels of its project by itself. In a swarm that
# executes, a leader that is a model is asked in the background, so no agent waits for it: the agents get their wheels at their next step.
# A leader that is the user gives the wheels itself (the step of the wheels, the command wheels).
def giveStartingWheels(swarm):
    if len(swarm.members) == 1:
        giveLoneWheels(swarm)
        return None
    names = withoutChoice(swarm)
    if not names or swarm.mode != "execute" or swarm.leaderIsUser() or not catalogFor(swarm.project):
        return None
    thread = threading.Thread(target=askLeaderForWheels, args=(swarm, names), name=f"swarmup-wheels-{swarm.id}", daemon=True)
    thread.start()
    return thread


# Gives an agent of a running swarm its wheels (they replace the ones it had): it lists its tools again at its next step, and is told.
# chosen: the leader or the user chose them, so a saved swarm keeps this list.
def giveWheels(swarm, name, wheels, why="", chosen=True):
    member = swarm.members.get(name)
    if member is None or name == ARCHITECT_NAME:
        raise ValueError(f"There is no agent {name} in the swarm. Its agents: {', '.join(swarm.getAgents())}.")
    names, errors = checkWheels(wheels, swarm.project or projectOfFolder(member["agent"].folder))
    if errors:
        raise ValueError(" ".join(errors))
    agent = member["agent"]
    agent.wheels = list(names)
    recipe = member.get("recipe")
    if recipe is not None and chosen:
        recipe.setdefault("answers", {})["wheels"] = list(names)
    link = swarm.gateway.find(name) if swarm.gateway is not None else None
    if link is not None:
        link.stale = True
    if names:
        catalog = catalogFor(swarm.project)
        listed = "; ".join(f"wheels__{wheel}: {catalog[wheel].get('summary') or ''}" for wheel in names)
        if chosen and name != swarm.getLeader():
            agent.receive(swarm.getLeader(), f"Your leader gave you wheels for your task: {listed} Use a wheel instead of doing its steps by hand.")
        else:
            agent.receive("SwarmUP", f"You have {'these wheels for your task' if chosen else 'the wheels of your project'}: {listed} Use a wheel instead of doing its steps by hand.")
    if swarm.journal is not None:
        swarm.journal.add("wheel", swarm.getLeader(), action="given", to=name, wheels=list(names), why=str(why or "")[:500])
    swarm.emit("wheel", name, action="given", wheels=list(names), why=str(why or ""))
    return names
