# The leader that builds the swarm and manages it while it works. The user chooses the leader (its model) and the folder of the mission,
# and tells it the mission. The leader proposes the whole swarm: the agents, their tasks and settings, their models, and who waits for whom.
# While the swarm works, it can propose to add an agent, to remove one, or to change the model of one that did not start yet.
#
# The leader never controls SwarmUP. It writes its proposals in blocks (LEADER_RULES in agent_prompts.py), and everything else is done here:
# 1. parseOutput finds the blocks in what the leader writes, even when they are not written exactly as asked: other names of tags, other
#    brackets, a missing closing tag, JSON in a code fence or alone in the text, JSON with comments, single quotes or trailing commas, or
#    "key: value" lines. A block that cannot be read goes back to the leader, with what is wrong, to be written again.
#    findSuggestions finds the sentences that suggest a change without a block. They are never acted upon: the leader is asked to confirm them
#    in a block, because a sentence can be read wrong and a block cannot. Words that only look like a change (a negation, a past event, a
#    possessive) are left alone, and the worst a wrong catch costs is one more question to the leader, never a change of the swarm.
# 2. LeaderCatalog tells the leader what it can choose from (the tasks and their settings, the models that are ready on this computer),
#    and checks every proposal: the task, the name, the model (it must fit in the GPUs with the others), who waits
#    for whom, and every setting, with the same checks as the forms of the user (readAnswers in tasks_library.py).
# 3. The user approves every proposal, with the reason the leader gave. Passwords, tokens and API keys are never taken from the leader: SwarmUP
#    asks the user for them, and for the settings the leader could not know (the address of the user...).
# 4. designSwarm builds the swarm with the leader, and LeaderManager follows the swarm while it runs: the leader is asked if a change is needed
#    each time an agent finishes or the user writes to it, and every change goes through the same checks and the approval of the user.
#    The program that runs the swarm (the user interface or the command line) makes the loops of the agents (see LeaderManager).
# This module has the building of the swarm. parseOutput and findSuggestions are in leader_parser.py, LeaderCatalog in leader_catalog.py
# (its checks in leader_checks.py), and LeaderManager in leader_manager.py.
import agent_prompts as prompts
from harness_utils import ConnectionLost
from leader_checks import describeAgent, describeProposal
from leader_parser import parseOutput
from messengers import MessagingError, findTelegramChats
from models_library import API_KEYS
from tasks_library import answerKey, getFields


REPAIR_ATTEMPTS = 3
MAX_REVISIONS = 8

ASK_ATTEMPTS = 3


# ==============
# What the user must give for the agents of the leader: the settings the leader could not know, the passwords and tokens, and the API keys.
# They are asked through askQuestions of the leader (a form in the window, questions in the console), checked like the forms of the user,
# and asked again with what is wrong. It returns True when everything was given.
# ==============
def askMissing(agents, catalog, leader):
    for attempt in range(ASK_ATTEMPTS):
        questions = []
        for agent in agents:
            for field in agent["needs"]:
                options = [{"label": option} for option in field.get("options") or []] if field["kind"] in ("choice", "choices") else []
                problem = f" ({field['problem']})" if field.get("problem") else ""
                questions.append({"id": f"{agent['name']}::{field['key']}", "header": agent["name"], "question": f"{field['ask']}{problem}", "options": options,
                                  "multiple": field["kind"] == "choices", "secret": field["kind"] == "secret"})
        for provider in dict.fromkeys(agent["key"] for agent in agents if agent.get("key") and not catalog.hasKey(agent["key"])):
            users = ", ".join(agent["name"] for agent in agents if agent.get("key") == provider)
            questions.append({"id": f"key::{provider}", "header": "API key", "question": f"API key of {API_KEYS[provider]['company']} for {users}. It stays in memory only, "
                              f"and is never saved. Create one at {API_KEYS[provider]['page']}", "options": [], "multiple": False, "secret": True})
        for name in dict.fromkeys(agent["token"] for agent in agents if agent.get("token") and attempt == 0):
            questions.append({"id": f"token::{name}", "header": "Hugging Face", "question": f"{name} needs a Hugging Face token, after you accepted its license on huggingface.co "
                              "(leave it empty if you logged in with huggingface-cli)", "options": [], "multiple": False, "secret": True})
        if not questions:
            return True
        answers = leader.askQuestions(questions)
        if not isinstance(answers, dict):
            return False
        given = {key: [str(item) for item in value] if isinstance(value, list) else [str(value)] for key, value in answers.items()}
        for question in questions:
            reply = [item.strip() for item in given.get(question["id"], []) if item.strip()]
            kind, _, key = question["id"].rpartition("::")
            if kind == "key":
                if reply:
                    catalog.keys[key] = reply[0]
            elif kind == "token":
                if reply:
                    catalog.tokens[key] = reply[0]
            else:
                agent = next(agent for agent in agents if agent["name"] == kind)
                field = next(field for field in agent["needs"] if field["key"] == key)
                agent["values"][key] = reply if field["kind"] == "choices" else (reply[0] if reply else "")
        for agent in agents:
            if not agent.get("task"):
                continue
            asked = {field["key"] for field in agent["needs"]}
            agent["answers"], missing, errors = catalog.readAnswers(agent["task"], agent["values"])
            problems = dict(errors)
            general = [problem for key, problem in errors if key not in asked]
            if general:
                raise ValueError(f"{agent['name']}: {' '.join(general)}")
            agent["needs"] = [{**field, "problem": problems.get(field["key"], "this is needed")} for field in getFields(agent["task"])
                              if field["key"] in {item["key"] for item in missing} | set(problems)]
            findChat(agent)
        for token in [agent["token"] for agent in agents if agent.get("token")]:
            for agent in agents:
                if agent.get("token") == token:
                    agent["token"] = None
    return not any(agent.get("needs") or (agent.get("key") and not catalog.hasKey(agent["key"])) for agent in agents)


# A news briefing sent with Telegram needs the chat of the user: it is found from the messages the user sent to the bot, like in the form.
def findChat(agent):
    chat = answerKey("Telegram", "chat")
    if agent["answers"].get("messenger") != "Telegram" or agent["answers"].get(chat) or any(field["key"] == answerKey("Telegram", "token") for field in agent["needs"]):
        return
    try:
        chats = findTelegramChats(agent["answers"][answerKey("Telegram", "token")])
    except (MessagingError, ConnectionLost) as error:
        chats, problem = [], str(error)
    else:
        problem = "" if len(chats) == 1 else "open your bot in Telegram, press Start and send it any message, then answer again" if not chats else \
            "several chats wrote to your bot: write the number of yours"
    if len(chats) == 1:
        agent["values"][chat] = agent["answers"][chat] = str(chats[0]["id"])
        agent["needs"] = [field for field in agent["needs"] if field["key"] != chat]
    elif not any(field["key"] == chat for field in agent["needs"]):
        field = next(field for field in getFields(agent["task"]) if field["key"] == chat)
        agent["needs"].append({**field, "problem": problem})


# ==============
# Building the swarm with the leader. leader is the LeaderLoop, connected to the user (askProposal, askQuestions, notifyUser).
# The leader sees every file of the folder of the mission, and can read them before it proposes (agent_storehouse.py).
# The leader writes its swarm, SwarmUP checks it and sends it back with what is wrong, then the user approves it, rejects it, or says
# what to change, and the leader writes it again. It returns the agents (with every answer the user gave), or None if the user rejected it.
# ==============
def designSwarm(leader, catalog):
    if catalog.folder and leader.folder is None:
        leader.setFolder(catalog.folder)
    base = catalog.buildPrompt()
    prompt = base
    for revision in range(MAX_REVISIONS):
        agents, problems, previous, note = None, [], "", ""
        for attempt in range(REPAIR_ATTEMPTS + 1):
            reply = leader.askAgent(prompt, own=False, tools=True)
            found = parseOutput(reply)
            builds = [block for block in found["blocks"] if block["action"] == "build"]
            previous, note = (builds[-1]["raw"] if builds else reply), found["text"]
            if builds:
                agents, problems = catalog.checkBuild(builds[-1]["data"])
            else:
                problems = [problem["error"] for problem in found["problems"]] or \
                    ["Your answer has no <swarmup_build> block. The swarm must be written in one <swarmup_build> block, in strict JSON."]
                problems += [f"Only the whole swarm can be proposed now, in one <swarmup_build> block: a <swarmup_{block['action']}> block is not possible yet."
                             for block in found["blocks"] if block["action"] != "build"]
            if not problems:
                break
            leader.notifyUser(f"The proposal of the leader cannot be used yet, so it writes it again: {' '.join(problems)[:600]}")
            prompt = f"{base}\n\n{prompts.LEADER_REPAIR_PROMPT.format(problems=chr(10).join('- ' + problem for problem in problems), previous=previous, blocks='one <swarmup_build> block')}"
        if problems:
            raise ValueError("The leader could not write a swarm that SwarmUP can use. Try again, give it more details, or choose a more capable model for it. "
                             f"The last problems: {' '.join(problems)[:900]}")
        shown = [describeAgent(agent) for agent in agents]
        text = f"A swarm of {len(agents)} {'agent' if len(agents) == 1 else 'agents'}: " + "; ".join(f"{agent['name']} ({agent['label']}, {agent['model']})" for agent in shown)
        proposal = {"action": "build", "agent": catalog.leader, "why": note, "text": text, "agents": shown}
        answer = leader.askProposal({**proposal, "summary": describeProposal(proposal)})
        if isinstance(answer, dict) and answer.get("decision") == "approve":
            if not askMissing(agents, catalog, leader):
                raise ValueError("Some agents still miss what only you can give (a password, an address, an API key...), so the swarm cannot be made.")
            for agent in agents:
                agent["answers"]["folder"] = str(catalog.folder) if catalog.folder else None
            return agents
        request = str(answer.get("message") or "").strip() if isinstance(answer, dict) else ""
        if not request:
            return None
        prompt = f"{base}\n\n{prompts.LEADER_REVISE_PROMPT.format(request=request, previous=previous)}"
    raise ValueError("The leader was asked to change its swarm too many times. Build the swarm yourself, or start again with a clearer mission.")
