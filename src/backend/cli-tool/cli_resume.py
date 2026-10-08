# The missions of the history (mission_history.py) in the command line. A swarm that was interrupted is continued where it stopped, or cancelled
# after a summary. A mission whose round is over is followed up, as in a chat: the user gives a new request and the same swarm works on it,
# right after the round, or later from the history (swarmup_cli.py open).
from base_loop import Loop
from cli_console import askUntilValid, chooseFrom, heading, shorten
from cli_models import prepareApi, prepareLocal, signInCodex
from cli_program import ConsoleMaker, addLive, chooseMode, connect, startSwarm, unloadModels
from cli_steps import askAccounts, askField
from cli_view import renderTree, watchCosts
from codex_agent import readCodexAccount
from leader_catalog import LeaderCatalog
from leader_manager import LeaderManager
from mission_history import loadMission
from model_clients import createModel
from model_support import ModelError, getApiKey
from models_library import API_KEYS
from saved_swarms import UNFINISHED_STATES, findUnfinishedSwarms
from swarm_harness import Swarm
from tasks_library import buildLoop, getTask, restoreAnswers, secretFields


def describeInterrupted(saved):
    reason = "The connection was lost" if saved["state"] == "paused" else "The program or the computer stopped"
    agents = [f"  - {name} ({member['role']}): {member['status']}" for name, member in saved["members"].items()]
    return "\n".join([f"The swarm \"{shorten(saved['mission'], 70)}\" was interrupted at {saved['savedAt']}. {reason}, and your work is saved.", *agents])


# The model of an agent of a swarm that is brought back. The API keys and the Hugging Face tokens are never saved, so they are asked again.
def rebuildModel(console, info, keys, tokens):
    if info.get("cli") == "codex":
        if not signInCodex(console):
            raise ModelError("Codex is not signed in, so this agent cannot continue.")
        return createModel(info, keys, report=console.say)
    if info["local"]:
        prepareLocal(console, info, tokens)
        return createModel(info, token=tokens.get(info["name"]), report=console.say)
    prepareApi(console, info, keys, price=False)
    return createModel(info, keys)


def rebuildAgent(console, name, data, keys, tokens, models, specs=None):
    recipe = data["recipe"]
    task, secrets = recipe["task"], {}
    console.say(f"\n--- {name} ({getTask(task)['role']}) ---")
    for field in secretFields(task, recipe["answers"]):
        secrets[field["key"]] = askField(console, field, recipe["answers"])
    if task == "literature":
        secrets["accounts"] = askAccounts(console)
    models[name] = rebuildModel(console, data["model"], keys, tokens) if data["model"] else None
    answers = restoreAnswers(task, recipe["answers"], secrets)
    loop = buildLoop(task, models[name], answers)
    connect(loop, console)
    if specs is not None:
        specs.append({"task": task, "name": name, "answers": answers})
    return loop


# The swarm of a saved mission, built again with its agents (their secrets and keys are asked again), or None if it cannot be.
def restoreSwarm(console, saved, models):
    keys, tokens, specs = {}, {}, []
    if not all(member.get("recipe") for member in saved["members"].values()):
        console.say("This swarm was made by another program, so it cannot be continued here.")
        return None
    try:
        swarm = Swarm.restore(saved, lambda name, data: rebuildAgent(console, name, data, keys, tokens, models, specs))
    except (ValueError, ModelError) as error:
        console.say(f"The swarm cannot be continued yet: {error} It stays saved, so you can try again.")
        unloadModels(models)
        return None
    console.say("\n" + renderTree(swarm, console.color))
    for name in swarm.getAgents():
        watchCosts(console, swarm, swarm.getMember(name)["agent"])
    leader = saved["members"][saved["leader"]]
    if saved.get("managed") and leader["recipe"]["task"] == "leader":
        catalog = LeaderCatalog(saved["mission"], leader["recipe"]["answers"].get("folder"), saved["leader"], leader["model"], keys, tokens, swarm.costs)
        LeaderManager(swarm, catalog, ConsoleMaker(console, swarm, specs, keys, tokens, models))
    console.newAgent = lambda swarm: addLive(console, swarm, specs, keys, tokens, models)
    return swarm


def resumeSaved(console, saved):
    models = {}
    heading(console, "Continuing your swarm")
    swarm = restoreSwarm(console, saved, models)
    if swarm is None:
        return False
    startSwarm(console, swarm, swarm.getMode(), models, resume=True)
    followUps(console, swarm, models)
    unloadModels(models)
    return True


# ==============
# Following up, as in a chat. After a round, the session asks for the next request (a session that is not interactive, like a script, ends).
# ==============
def askRequest(console):
    return askUntilValid(console, "Your new request for the swarm (the leader gives each agent its part):", lambda text: (text.strip(), "" if text.strip() else "Write a request."))


def followUps(console, swarm, models):
    while console.interactive:
        options = ["Follow up: give the swarm a new request", "End here (the mission stays in the history: swarmup_cli.py open continues it)"]
        if chooseFrom(console, f"\nRound {swarm.round} is over. What now?", options) != options[0]:
            return
        swarm.followUp(askRequest(console))
        startSwarm(console, swarm, chooseMode(console), models)


def describeLastRound(saved):
    requests = saved.get("requests") or [{"round": 1, "text": saved["mission"]}]
    report = saved["members"][saved["leader"]].get("result")
    lines = [f"The mission: {saved['mission']}"] + [f"Round {item['round']}: {shorten(item['text'], 200)}" for item in requests[1:]]
    return "\n".join(lines + ([f"\nThe last report of {saved['leader']}:\n{report}"] if report else []))


def followUpSaved(console, saved):
    models = {}
    heading(console, "Following up on your mission")
    console.say(describeLastRound(saved))
    swarm = restoreSwarm(console, saved, models)
    if swarm is None:
        return False
    swarm.followUp(askRequest(console))
    startSwarm(console, swarm, chooseMode(console), models)
    followUps(console, swarm, models)
    unloadModels(models)
    return True


# A mission chosen from the history (swarmup_cli.py open): continued where it stopped, or followed up.
def openMission(console, missionId):
    try:
        saved = loadMission(missionId)
    except ValueError as error:
        console.say(str(error))
        return False
    return resumeSaved(console, saved) if saved["state"] in UNFINISHED_STATES else followUpSaved(console, saved)


# The leader writes the summary with its model if that can be made again (an API key is asked for it, and can be skipped).
def makeLeaderModel(console, info, keys, tokens):
    if not info:
        return None
    if info.get("cli") == "codex":
        try:
            if not readCodexAccount()["signedIn"]:
                return None
        except ModelError:
            return None
    if not info["local"] and info["provider"] and not (keys.get(info["provider"]) or getApiKey(info["provider"])):
        console.say(f"The leader writes the summary with {info['name']}, which needs its API key again. Press Enter to skip it and get a plain list instead.")
        key = askUntilValid(console, f"Your {API_KEYS[info['provider']]['company']} API key (hidden, Enter to skip):", lambda text: (text.strip(), ""), secret=True)
        if not key:
            return None
        keys[info["provider"]] = key
    try:
        return rebuildModel(console, info, keys, tokens)
    except ModelError as error:
        console.say(str(error))
        return None


# An agent of a swarm that is cancelled: it only needs to remember what it did and to undo what it left half done, so it needs no model.
def rebuildForCancel(console, name, data):
    recipe = data.get("recipe") or {}
    try:
        loop = buildLoop(recipe["task"], None, {**restoreAnswers(recipe["task"], recipe["answers"]), "folder": None})
    except (KeyError, ValueError, OSError):
        loop = Loop(None)
    connect(loop, console)
    return loop


# It returns True if the swarm was continued after all.
def cancelSaved(console, saved):
    keys, tokens = {}, {}
    swarm = Swarm.restore(saved, lambda name, data: rebuildForCancel(console, name, data))
    leader = swarm.getMember(swarm.getLeader())["agent"]
    leader.agent = makeLeaderModel(console, saved["members"][saved["leader"]]["model"], keys, tokens)
    console.say(f"\n[{swarm.getLeader()}] Summary of what the swarm did so far:\n{swarm.summarizeChanges()}")
    console.say("If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task.")
    if chooseFrom(console, "\nWhat now?", ["Stop here", "Continue it until the end"]) == "Stop here":
        swarm.abandon()
        console.say("The swarm is stopped. What the agents that did not finish had changed in your files was put back.")
        return False
    return resumeSaved(console, saved)


# It returns True if a swarm was continued, which is all the program has to do then.
def offerUnfinished(console):
    for saved in findUnfinishedSwarms():
        if saved["running"]:
            console.say(f"\nThe swarm \"{shorten(saved['mission'], 70)}\" seems to be working in another window of the program, so it is left alone. "
                        "If that window was closed a few seconds ago, start this program again in half a minute.")
            continue
        heading(console, "A swarm was interrupted")
        console.say(describeInterrupted(saved))
        options = ["Continue it where it stopped", "Cancel it (you will see what it did, and confirm)", "Leave it for later and go on"]
        picked = chooseFrom(console, "\nWhat do you want to do?", options)
        if (picked == options[0] and resumeSaved(console, saved)) or (picked == options[1] and cancelSaved(console, saved)):
            return True
    return False
