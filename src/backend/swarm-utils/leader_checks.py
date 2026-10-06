# The checks of what the leader proposes, and what the user is shown of it (see leader_utils.py).
import os
import re
import shlex
import subprocess
from pathlib import Path

from gpu_check import checkVram
from leader_parser import ACTION_KEYS, letters, normalizeKeys, squash
from mission_costs import formatDollars
from models_library import API_KEYS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, getModelInfo, isGated
from sources_library import ALL_NEWS_OUTLETS, PAPER_PUBLISHERS
from swarm_harness import Swarm
from tasks_library import SECRET_KINDS, TASKS, answerKey, buildLoop, checkAgentName, getFields, readAnswers


# Keys a leader may write next to an agent that are not settings: they are left out, unless the task has a setting of that name.
IGNORED_KEYS = ("provider", "company", "vendor", "api", "local", "vram", "cli", "note", "notes", "comment", "comments", "status")


# ==============
# What the leader can choose from, and the checks of what it chose.
# ==============
TASK_WORDS = {
    "email": ("email", "emailer", "emailwriter", "mail", "mailer", "emailsender", "sendemail"),
    "calendar": ("calendar", "calendarplanner", "planner", "event", "scheduler", "schedule", "booking"),
    "news": ("news", "newsbriefer", "briefer", "briefing", "newsbriefing", "headlines"),
    "author": ("author", "writer", "writing", "text", "essay", "article", "texts"),
    "literature": ("literature", "literaturereviewer", "literaturesurvey", "survey", "papers", "research", "researcher"),
    "format": ("format", "formatter", "formatting", "documentformatter", "layout"),
    "math": ("math", "mathchecker", "maths", "proof", "proofs", "proofchecker", "referee"),
    "coder": ("coder", "code", "coding", "programmer", "developer", "programming", "software"),
}
# Other names of the settings, by the key of the setting. Only the keys of the task of the agent are used.
SETTING_WORDS = {
    "filePath": ("file", "path", "filepath", "filename", "document", "documentpath", "codefile", "target", "input"),
    "maxWords": ("maxwords", "words", "wordlimit", "maximumwords", "length", "wordcount"),
    "length": ("length", "words", "maxwords", "wordcount", "wordlimit", "maximumwords"),
    "receiver": ("receiver", "to", "recipient", "recipientemail", "receiveremail", "toaddress"),
    "sender": ("sender", "from", "senderemail", "fromaddress"),
    "request": ("request", "content", "body", "message", "what", "instructions", "brief", "event"),
    "subject": ("subject", "topic", "theme", "title"),
    "topics": ("topics", "topic", "interests", "subjects"),
    "outlets": ("outlets", "sources", "feeds", "newsoutlets", "outlet"),
    "collectAt": ("collectat", "time", "at", "when", "schedule", "collecttime"),
    "testCommand": ("testcommand", "command", "test", "tests", "check", "checkcommand", "run"),
    "task": ("task", "goal", "what", "description", "spec", "specification", "instructions"),
    "style": ("style", "format", "formatstyle"),
    "searches": ("searches", "searchengines", "engines", "search", "databases"),
    "publishers": ("publishers", "publisher"),
    "language": ("language", "lang"),
    "numberOfLoops": ("numberofloops", "loops", "drafts", "maxdrafts", "attempts", "iterations"),
    "provider": ("provider", "emailprovider", "mailprovider", "service"),
    "messenger": ("messenger", "messagingapp", "app", "sendto", "deliverto"),
}


def modelLabel(info):
    if info.get("cli"):
        return f"{MODELS_CLI[info['cli']]['label']} ({'its default model' if info['name'] == DEFAULT_CLI_MODEL else info['name']})"
    if info["local"]:
        return f"{info['name']} on your GPUs ({info['vram']} GB{'' if info['bits'] == 16 else ', ' + str(info['bits']) + '-bit'})"
    return f"{info['name']} ({API_KEYS[info['provider']]['company']} API)"


def showValue(field, value):
    if field["kind"] == "command":
        return (subprocess.list2cmdline(value) if os.name == "nt" else shlex.join(value)) if value else "the file is run with Python"
    if isinstance(value, dict):
        return ", ".join(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def readNames(value):
    if value in (None, ""):
        return []
    parts = re.split(r",|;|\n|\band\b", value) if isinstance(value, str) else value if isinstance(value, list) else None
    if parts is None:
        return None
    return [str(part).strip().strip("\"'") for part in parts if str(part).strip() and squash(part) not in ("none", "nobody", "nothing", "noone")]


# A name as the leader may write it (fact checker, fact_checker) made into one word (FactChecker).
def cleanName(value):
    words = re.findall(r"[A-Za-z0-9]+", str(value or ""))
    name = words[0] if len(words) == 1 else "".join(word[:1].upper() + word[1:] for word in words)
    return f"Agent{name}" if name[:1].isdigit() else name


def matchOption(value, options):
    target = squash(value)
    exact = [option for option in options if squash(option) == target]
    close = [option for option in options if target and (target in squash(option) or squash(option) in target)]
    return exact[0] if exact else close[0] if len(close) == 1 else None


# Whether an agent can join the swarm now: it runs, and the leader did not start its final work.
def canJoin(swarm):
    stage = swarm.stage
    return bool(swarm.active and stage and stage["open"] and (stage["plan"] or swarm.getLeader() not in stage["names"]))


# An agent of the leader as the user sees it in a proposal. Passwords and tokens are never in it.
def describeAgent(agent):
    fields = {field["key"]: field for field in getFields(agent["task"])}
    settings = [{"label": fields[key]["ask"], "value": showValue(fields[key], agent["answers"].get(key, value))} for key, value in agent["values"].items()
                if key in fields and fields[key]["kind"] not in SECRET_KINDS]
    needs = [field["ask"] for field in agent["needs"]] + ([f"An API key of {API_KEYS[agent['key']]['company']}"] if agent.get("key") else []) + \
        ([f"A Hugging Face token for {agent['token']} (optional)"] if agent.get("token") else [])
    info = agent["model"]
    return {"name": agent["name"], "task": agent["task"], "label": TASKS[agent["task"]]["label"], "role": TASKS[agent["task"]]["role"], "model": modelLabel(info),
            "price": agent.get("price", ""),
            "modelKind": "cli" if info.get("cli") else "local" if info["local"] else "api", "waitsFor": agent["waitsFor"], "settings": settings, "why": agent["why"],
            "needs": needs, "description": agent["description"]}


def describeAgentText(shown, number=None):
    waits = f"It waits for {', '.join(shown['waitsFor'])}." if shown["waitsFor"] else "It starts right away."
    price = f", {shown['price']}" if shown.get("price") else ""
    lines = [f"{f'{number}. ' if number else ''}{shown['name']}: {shown['label']}, with {shown['model']}{price}. {waits}", f"   What it does: {shown['description']}"]
    lines += [f"   Why: {shown['why']}"] if shown["why"] else []
    lines += [f"   {item['label']}: {item['value']}" for item in shown["settings"]]
    lines += [f"   You will be asked for: {'; '.join(shown['needs'])}"] if shown["needs"] else []
    return "\n".join(lines)


def describeProposal(proposal):
    if proposal["action"] == "build":
        agents = "\n".join(describeAgentText(shown, number) for number, shown in enumerate(proposal["agents"], 1))
        return f"proposes a swarm of {len(proposal['agents'])} {'agent' if len(proposal['agents']) == 1 else 'agents'} for your mission:\n{agents}"
    if proposal["action"] == "add":
        return f"proposes to add an agent to the swarm:\n{describeAgentText(proposal['details'])}"
    details = proposal["details"]
    waited = f" {', '.join(details['waitedBy'])} wait for it, and go on without it." if details.get("waitedBy") and details.get("status") != "done" else ""
    return f"proposes: {proposal['text']}.{waited}"


# The checks of what the leader proposes (LeaderCatalog in leader_catalog.py): the task, the model, the settings, who waits for whom,
# the whole swarm, and the changes while it runs.
class LeaderChecks:
    def findTask(self, value):
        target = letters(value)
        if not target:
            return None
        for key, task in TASKS.items():
            if target in (key, letters(task["label"]), letters(task["role"]), letters(task["name"])) or target in TASK_WORDS.get(key, ()):
                return key
        return None

    # The model the leader wrote: (getModelInfo of it, "") or (None, what is wrong). It can be written in many ways: claude-sonnet-5-5,
    # Claude Sonnet 5.5, anthropic/claude-sonnet-5-5, qwen3-8b, Qwen/Qwen3-8B@4bit, claude code:claude-opus-5-5, codex (gpt-6-luna)...
    def findModel(self, value, bits=None):
        if isinstance(value, dict):
            keys = {letters(key): item for key, item in value.items()}
            bits = bits or (int(keys["bits"]) if str(keys.get("bits", "")).strip() in ("16", "8", "4") else None)
            value = keys.get("name") or keys.get("model") or keys.get("id") or ""
        text = str(value or "").strip().strip("\"'`")
        if not text:
            return None, "Every agent needs a model of the list of models."
        suffix = re.search(r"\s*[@:(_-]?\s*\b(16|8|4)[\s-]?bits?\)?$|@(16|8|4)$", text, re.I)
        if suffix:
            bits, text = bits or int(suffix.group(1) or suffix.group(2)), text[:suffix.start()].strip()
        cli = re.fullmatch(r"(claude[\s_-]?code|codex)(?:\s*[:/@(]\s*|\s+with\s+|\s+)?(.*?)\)?\s*", text, re.I)
        if cli:
            agent = "codex" if cli.group(1).lower() == "codex" else "claude-code"
            choices = [DEFAULT_CLI_MODEL, *(self.readStatus()["codex"]["models"] if agent == "codex" else MODELS_CLI[agent]["models"])]
            part = cli.group(2).strip()
            name = DEFAULT_CLI_MODEL if squash(part) in ("", "default", "defaultmodel", "itsdefaultmodel", "auto") else matchOption(part, choices) if squash(part) else None
            if name is None or (squash(name) != squash(part) and squash(part) not in ("", "default", "defaultmodel", "itsdefaultmodel", "auto")):
                return None, f"{MODELS_CLI[agent]['label']} cannot use the model {part}. It can use: {', '.join(choices)}."
            return getModelInfo(name, cli=agent), ""
        listed = {name: provider for provider, models in MODELS_API.items() for name in models}
        api = [name for name in listed if squash(name) in (squash(text), squash(text.split("/")[-1]))]
        if api:
            return getModelInfo(api[0], provider=listed[api[0]]), ""
        local = {name: size for family in MODELS_LOCAL.values() for name, size in family.items()}
        found = [name for name in local if name.lower() == text.lower()] or [name for name in local if squash(name) == squash(text)] or \
            [name for name in local if squash(name.split("/")[1]) == squash(text.split("/")[-1])]
        if len(found) == 1:
            if bits not in (None, 16, 8, 4):
                return None, f"A local model uses 16, 8 or 4 bits, not {bits}."
            return getModelInfo(found[0], bits=bits or 16), ""
        return None, f"'{value}' is not a model of the list of models. Write the name of a model exactly as the list gives it."

    # Why a model cannot be used now, or "". needs is filled with what the user must give first (an API key, a Hugging Face token).
    # money is what is left of the budget for new models (None without a budget).
    def checkUsable(self, info, taken, needs, money=None):
        status = self.readStatus()
        if not self.withinBudget(info, money):
            return (f"{info['name']} costs {formatDollars(self.reference(info))} per 1 million tokens, but only {formatDollars(max(money, 0))} is left of the budget of the mission "
                    "for new models. Choose a cheaper model, a local model, or Codex.")
        if info.get("cli") == "codex":
            return "" if status["codex"]["ready"] else f"Codex cannot be used now: {status['codex']['problem']} Choose another model."
        if info.get("cli") == "claude-code" and status["claudeMissing"]:
            return f"Claude Code cannot be used: the package {', '.join(status['claudeMissing'])} is not installed. Choose another model."
        if info["local"]:
            if not status["gpus"]:
                return "Local models cannot run on this computer: no supported GPU was found. Choose an API model or a coding agent."
            if status["localMissing"]:
                return f"Local models cannot run now: the packages {', '.join(status['localMissing'])} are not installed. Choose an API model."
            total = checkVram(0, status["gpus"])["total"]
            if taken + info["vram"] > total:
                return (f"{info['name']} needs {info['vram']} GB of VRAM at {info['bits']} bits, but only {max(round(total - taken, 1), 0)} GB are left of the {total} GB of the GPUs "
                        f"(the models already chosen take {taken} GB). Choose a smaller model, fewer bits, or an API model.")
            if isGated(info["name"]) and not os.environ.get("HF_TOKEN") and not self.tokens.get(info["name"]):
                needs["token"] = info["name"]
            return ""
        missing = status["apiMissing"].get(info["provider"]) if not info.get("cli") else []
        if missing:
            return f"{info['name']} needs the package {', '.join(missing)}, which is not installed. Choose another model."
        if info["provider"] and not self.hasKey(info["provider"]):
            needs["key"] = info["provider"]
        return ""

    # The settings of the leader as the values of a form of the task: their keys are found among the keys of the task, and their values are
    # turned into what the form expects. It returns (values, errors). Passwords, tokens and accounts written by the leader are dropped.
    def readSettings(self, task, settings):
        fields = {field["key"]: field for field in getFields(task)}
        values, errors = {}, []
        for key, value in settings.items():
            target = squash(key)
            name = next((field for field in fields if squash(field) == target), None) or next((field for field in fields if target in (squash(word) for word in SETTING_WORDS.get(field, ()))), None)
            if name is None:
                errors.append(f"{key} is not a setting of the task {task}. Its settings are: {', '.join(fields)}.")
                continue
            field = fields[name]
            if field["kind"] in SECRET_KINDS or value in (None, "", [], {}):
                continue
            converted, error = self.formValue(field, value)
            if error:
                errors.append(f"{name}: {error}")
            else:
                values[name] = converted
        return values, errors

    def formValue(self, field, value):
        kind = field["kind"]
        if isinstance(value, bool):
            return None, "write a value, not true or false."
        if kind == "choice":
            option = matchOption(value, field["options"])
            return (option, "") if option else (None, f"'{value}' is not one of: {', '.join(field['options'])}.")
        if kind in ("choices", "outlets", "publishers"):
            items = list(value) if isinstance(value, (list, dict)) else readNames(str(value)) or []
            if kind == "choices":
                chosen = [matchOption(item, field["options"]) for item in items]
                wrong = [str(item) for item, option in zip(items, chosen) if option is None]
                return (chosen, "") if not wrong else (None, f"{', '.join(wrong)} not among: {', '.join(field['options'])}.")
            if kind == "outlets":
                chosen = [str(item).strip() if str(item).strip().startswith(("http://", "https://")) else matchOption(item, ALL_NEWS_OUTLETS) for item in items]
                wrong = [str(item) for item, outlet in zip(items, chosen) if outlet is None]
                return (chosen, "") if not wrong else (None, f"{', '.join(wrong)} not among the news outlets of the list, and not the address of a feed.")
            numbers = value if isinstance(value, dict) else {}
            chosen = {}
            for item in items:
                publisher = matchOption(item, PAPER_PUBLISHERS)
                if publisher:
                    chosen[publisher] = str(PAPER_PUBLISHERS[publisher])
                elif str(numbers.get(item, "")).isdigit():
                    chosen[str(item)] = str(numbers[item])
                else:
                    return None, f"{item} is not among the publishers of the list."
            return chosen, ""
        if kind == "number":
            number = value if isinstance(value, (int, float)) else str(value).strip()
            if isinstance(number, float) and number.is_integer():
                number = int(number)
            return str(number), ""
        if kind == "command":
            return (shlex.join(value) if os.name != "nt" else subprocess.list2cmdline(value)) if isinstance(value, list) else str(value), ""
        if kind in ("file", "path", "folder"):
            path = Path(str(value).strip()).expanduser()
            return str(path if path.is_absolute() or self.folder is None else self.folder / path), ""
        if isinstance(value, (list, dict)):
            return ", ".join(str(item) for item in value), ""
        return str(value), ""

    # Checks one agent of the leader. names are the names already taken (the leader included), waitable the agents it may wait for, and taken
    # the VRAM in GB of the models already chosen. It returns the agent ready to be made, with what the user must still give (needs), and the
    # errors to tell the leader.
    def checkAgent(self, raw, names, waitable, taken, money=None):
        keys, extra = normalizeKeys(raw)
        errors, needs = [], {}
        task = self.findTask(keys.get("task")) or (self.findTask(keys.get("role")) if not keys.get("task") else None)
        if task is None:
            errors.append(f"'{keys.get('task') or ''}' is not a task of the list. Write the key of a task: {', '.join(TASKS)}.")
        name = cleanName(keys.get("name")) or (TASKS[task]["name"] if task else "")
        problem = checkAgentName(name, names) if name else "Every agent needs a name."
        if name and name.lower() == str(self.leader).lower():
            problem = f"{name} is your own name: give the agent another one."
        if problem:
            errors.append(problem)
        bits = keys.get("bits")
        if bits not in (None, "") and str(bits).strip() not in ("16", "8", "4"):
            errors.append(f"bits is 16, 8 or 4, not {bits}.")
            bits = None
        info, problem = self.findModel(keys.get("model"), int(bits) if bits not in (None, "") else None)
        if problem:
            errors.append(problem)
        elif info:
            problem = self.checkUsable(info, taken, needs, money)
            if problem:
                errors.append(problem)
        waits = []
        listed = readNames(keys.get("waits_for"))
        if listed is None:
            errors.append("waits_for is a list of names of agents.")
        for other in listed or []:
            match = next((agent for agent in waitable if squash(agent) == squash(other)), None)
            if squash(other) == squash(self.leader):
                errors.append(f"{name} cannot wait for you, {self.leader}: you work last and receive every result.")
            elif match is None or squash(other) == squash(name):
                errors.append(f"{name} cannot wait for {other}: it is not another agent of the swarm. It can wait for: {', '.join(agent for agent in waitable if agent != name) or 'nobody'}.")
            elif match not in waits:
                waits.append(match)
        settings = keys.get("settings") if isinstance(keys.get("settings"), dict) else {}
        if keys.get("settings") not in (None, "", {}) and not isinstance(keys.get("settings"), dict):
            errors.append("settings is an object: {\"key of the setting\": value}.")
        answers, missing, description = {}, [], ""
        if task:
            fields = {squash(field["key"]) for field in getFields(task)}
            extra = {key: value for key, value in extra.items() if letters(key) not in ACTION_KEYS and (letters(key) not in IGNORED_KEYS or squash(key) in fields)}
            values, problems = self.readSettings(task, {**extra, **settings})
            errors += problems
            answers, missing, problems = self.readAnswers(task, values)
            errors += [f"{key}: {problem}" if key else problem for key, problem in problems]
            description = self.describe(task, answers, missing)
        else:
            values = {}
        return {"name": name, "task": task, "model": info, "waitsFor": waits, "values": values, "answers": answers, "needs": missing, "key": needs.get("key"),
                "token": needs.get("token"), "why": str(keys.get("why") or "").strip(), "description": description, "errors": errors,
                "price": self.describePrice(info) if info else ""}

    # The answers of the task from the values. The settings the leader did not give, and that are needed (a password, the address of the user),
    # are what the user must give: the fields of missing. A value the leader gave that is wrong is an error for the leader.
    def readAnswers(self, task, values):
        answers, problems = readAnswers(task, values)
        fields = {field["key"]: field for field in getFields(task)}
        missing, errors = [], []
        for key, problem in problems.items():
            field = fields.get(key)
            if field and values.get(key) in (None, "", [], {}) and (field["kind"] in SECRET_KINDS or field.get("required")):
                missing.append({**field, "problem": ""})
            else:
                errors.append((key, problem))
        if answers.get("messenger") == "Telegram" and not answers.get(answerKey("Telegram", "chat")) and not any(item["key"] == answerKey("Telegram", "token") for item in missing):
            chat = next(field for field in getFields(task) if field["key"] == answerKey("Telegram", "chat"))
            missing.append({**chat, "problem": ""})
        if not missing and not errors:
            try:
                buildLoop(task, None, {**answers, "folder": self.folder})
            except (ValueError, OSError) as error:
                errors.append(("", str(error)))
        return answers, missing, errors

    # What the agent will do, in the words of its loop. Every agent works in the folder of the mission, so the folder is not repeated.
    def describe(self, task, answers, missing):
        try:
            return buildLoop(task, None, {**answers, "folder": self.folder} if not missing else {**answers, "folder": None}).describeTask()
        except Exception:
            return TASKS[task]["info"].split(". ")[0] + "."

    # The whole swarm of the leader: (agents, errors). The agents may wait for each other in any order, but never in a circle.
    def checkBuild(self, data):
        raws = data.get("agents") or []
        if len(raws) > self.maxAgents:
            return [], [f"The swarm can have {self.maxAgents} agents at most, and you proposed {len(raws)}. Keep only the agents the mission needs."]
        planned = [cleanName(normalizeKeys(raw)[0].get("name")) for raw in raws]
        agents, errors, names = [], [], [self.leader]
        taken = self.leaderModel["vram"] if self.leaderModel else 0.0
        money = self.moneyLeft(self.leaderTeam())
        for number, raw in enumerate(raws, 1):
            waitable = [name for name in planned if name]
            agent = self.checkAgent(raw, names, waitable, taken, money)
            errors += [f"Agent {number} ({agent['name'] or 'without a name'}): {error}" for error in agent["errors"]]
            names.append(agent["name"])
            taken = round(taken + (agent["model"]["vram"] if agent["model"] else 0.0), 1)
            if money is not None and agent["model"]:
                money -= self.reference(agent["model"]) or 0.0
            agents.append(agent)
        if not raws:
            errors.append("The swarm needs at least one agent.")
        if not errors:
            try:
                Swarm("").findStages({agent["name"]: agent["waitsFor"] for agent in agents})
            except ValueError as error:
                errors.append(f"{error}. Change who waits for whom.")
        return agents, errors

    # A change the leader proposes while the swarm works: (the change, errors). The change has the proposal the user sees.
    # The user decides with the reason of the leader, so a change without one goes back to the leader.
    def checkChange(self, action, data, swarm):
        keys = normalizeKeys(data)[0]
        if not str(keys.get("why") or "").strip():
            return None, [f"Every change needs \"why\": one or two sentences for the user that say how this change helps the mission."]
        if action == "add":
            return self.checkAdd(data, swarm)
        name = next((agent for agent in swarm.getAgents() if squash(agent) == squash(keys.get("name"))), None)
        why = str(keys.get("why") or "").strip()
        if name is None:
            others = [agent for agent in swarm.getAgents() if agent != swarm.getLeader()]
            return None, [f"There is no agent called {keys.get('name')} in the swarm. Its agents are: {', '.join(others) or 'none besides you'}."]
        if name == swarm.getLeader():
            return None, ["You cannot remove yourself nor change your own model: you speak to the user for the swarm."]
        member = swarm.getMember(name)
        details = {"role": member["role"], "status": member["status"], "model": modelLabel(member["model"]) if member["model"] else "",
                   "waitedBy": [other for other in swarm.getAgents() if name in swarm.getMember(other)["waitsFor"]]}
        if action == "remove":
            text = f"Remove {name} ({member['role']}, {member['status']})"
            proposal = {"action": "remove", "agent": name, "why": why, "text": text, "details": details}
            return {"action": "remove", "name": name, "why": why, "signature": ("remove", name.lower()), "text": text, "proposal": {**proposal, "summary": describeProposal(proposal)}}, []
        if member["started"] or member["status"] != "waiting":
            return None, [f"{name} already started, so its model cannot change. Propose to remove it and to add a new agent instead, if it is really needed."]
        needs = {}
        info, problem = self.findModel(keys.get("model"), int(keys["bits"]) if str(keys.get("bits", "")).strip() in ("16", "8", "4") else None)
        money = self.moneyLeft(swarm.describeTeam())
        if money is not None and member["model"]:
            money += self.reference(member["model"]) or 0.0
        problem = problem or self.checkUsable(info, swarm.getNeededVram(replacing=name), needs, money)
        if problem:
            return None, [problem]
        if member["model"] and all(member["model"].get(key) == info.get(key) for key in ("name", "bits", "cli", "provider")):
            return None, [f"{name} already uses {modelLabel(info)}."]
        text = f"Change the model of {name} from {details['model'] or 'none'} to {modelLabel(info)}"
        proposal = {"action": "model", "agent": name, "why": why, "text": text, "details": {**details, "from": details["model"], "to": modelLabel(info)}}
        return {"action": "model", "name": name, "model": info, "why": why, "key": needs.get("key"), "token": needs.get("token"),
                "signature": ("model", name.lower(), info["name"], info.get("cli")), "text": text, "proposal": {**proposal, "summary": describeProposal(proposal)}}, []

    def checkAdd(self, data, swarm):
        if not canJoin(swarm):
            return None, ["No agent can join the swarm now: you already started your final work, or the swarm is not running."]
        others = [name for name in swarm.getAgents() if name != swarm.getLeader()]
        if len(others) >= self.maxAgents:
            return None, [f"The swarm already has {self.maxAgents} agents besides you, the most the user allows. Propose to remove one first, if it is not needed."]
        names = [*swarm.getAgents(), *swarm.departed]
        agent = self.checkAgent(data, names, others, swarm.getNeededVram(), self.moneyLeft(swarm.describeTeam()))
        if agent["errors"]:
            return None, agent["errors"]
        details = describeAgent(agent)
        text = f"Add {agent['name']} ({TASKS[agent['task']]['label']}) with {modelLabel(agent['model'])}" + (f", waiting for {', '.join(agent['waitsFor'])}" if agent["waitsFor"] else "")
        proposal = {"action": "add", "agent": agent["name"], "why": agent["why"], "text": text, "details": details}
        return {"action": "add", "agent": agent, "name": agent["name"], "why": agent["why"], "signature": ("add", agent["name"].lower(), agent["task"]), "text": text,
                "proposal": {**proposal, "summary": describeProposal(proposal)}}, []
