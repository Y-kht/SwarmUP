# What the leader is told: the tasks with their settings, the models it can choose on this computer and the cost of the mission
# (see leader_utils.py). Its checks are in leader_checks.py. The files of the folder are shown to the leader like to every agent (agent_storehouse.py).
import time
from pathlib import Path

import agent_prompts as prompts
from codex_agent import checkCodex, listCodexModels, readCodexAccount
from gpu_check import checkVram, readGpus
from harness_utils import USER_NAME
from internet_cache import describeCached
from leader_checks import LeaderChecks, canJoin, showValue
from mission_costs import MissionCosts, formatDollars
from model_support import ModelError, findMissingPackages, getApiKey
from models_library import API_KEYS, MODELS_API, MODELS_LOCAL, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, getVram
from sources_library import NEWS_OUTLETS, PAPER_PUBLISHERS
from tasks_library import DEFAULT_LOOPS, SECRET_KINDS, TASKS, isAsked
from user_settings import loadSettings


STATUS_SECONDS = 60

KIND_WORDS = {"text": "text", "email": "an email address", "number": "a whole number", "file": "the path of a file that exists in the folder",
              "path": "the path of a file in the folder, created if it does not exist", "folder": "a folder", "phone": "a phone number with its country code, like +4915112345678",
              "time": "a time of the day HH:MM", "command": "a command line, run in the folder", "outlets": "a list of news outlets: names of the list of outlets below, or addresses of RSS feeds",
              "publishers": "a list of publishers: names of the list of publishers below", "secret": "a secret: never write it, the user gives it",
              "accounts": "accounts of the user: never write them, the user gives them"}


def describeCondition(field, fields):
    if not field.get("when"):
        return ""
    for other in fields:
        if other["kind"] == "choice" and other is not field:
            enabling = [option for option in other["options"] if isAsked(field, {other["key"]: option})]
            if enabling and len(enabling) < len(other["options"]):
                return f"only when {other['key']} is {' or '.join(enabling)}"
    return "only in some cases"


def describeKind(field, fields):
    kind = field["kind"]
    parts = [KIND_WORDS.get(kind) or (f"one of: {' | '.join(field['options'])}" if kind == "choice" else f"a list of some of: {', '.join(field['options'])}")]
    condition = describeCondition(field, fields)
    if condition:
        parts.append(condition)
    default = field.get("default")
    if kind in SECRET_KINDS:
        parts.append("required" if field.get("required") else "optional")
    elif callable(default):
        parts.append("default: set from the settings before it" if not field.get("required") else "required, but its default is set from the settings before it")
    elif field.get("required") and default is None:
        parts.append("required")
    elif default in (None, "", [], {}):
        parts.append("optional")
    else:
        parts.append(f"default: {showValue(field, default)}")
    return "; ".join(parts)


def describeTasks():
    lines = []
    for key, task in TASKS.items():
        lines.append(f"- {key}: {task['label']}. {task['info']} Its folder: {task['folder']}")
        lines += [f"    {field['key']} ({describeKind(field, task['fields'])}): {field['ask']}" for field in task["fields"]]
    lines.append(f"Every task also has numberOfLoops (a whole number; default: {DEFAULT_LOOPS}): how many drafts the agent may write at most before it stops.")
    lines.append("The news outlets, by group: " + "; ".join(f"{group}: {', '.join(names)}" for group, names in NEWS_OUTLETS.items()) + ".")
    lines.append("The publishers: " + ", ".join(PAPER_PUBLISHERS) + ".")
    return "\n".join(lines)


# Everything the leader is told and checked against. keys and tokens are those of the program that runs the swarm (the API keys and
# Hugging Face tokens the user gave): the ones the user gives for an agent of the leader go in them too, and stay in memory only.
# costs is what the mission spent, with its budget (MissionCosts in mission_costs.py), and maxAgents the most agents the leader may put in
# the swarm (the settings of the user). The local models that do not fit in the VRAM left, and the API models whose price of 1 million tokens
# is more than the budget left, are not offered to the leader, and refused if it chooses them anyway.
class LeaderCatalog(LeaderChecks):
    def __init__(self, mission, folder, leader, leaderModel, keys=None, tokens=None, costs=None, maxAgents=None):
        self.mission, self.leader, self.leaderModel = mission, leader, leaderModel
        self.costs = costs if costs is not None else MissionCosts()
        self.maxAgents = maxAgents or loadSettings()["maxAgents"]
        self.folder = Path(folder).expanduser().resolve() if folder else None
        self.keys = keys if keys is not None else {}
        self.tokens = tokens if tokens is not None else {}
        self.status, self.statusTime = None, 0.0

    def hasKey(self, provider):
        return bool(self.keys.get(provider) or getApiKey(provider))

    # What can be used on this computer now: Codex (installed and signed in), Claude Code and the local models (their packages). It is read
    # again after a minute, because asking Codex takes a few seconds.
    def readStatus(self):
        if self.status is not None and time.monotonic() - self.statusTime < STATUS_SECONDS:
            return self.status
        codex = {"ready": False, "problem": "", "models": []}
        found = checkCodex()
        if found["problem"]:
            codex["problem"] = found["problem"]
        else:
            try:
                account = readCodexAccount()
                codex.update(ready=account["signedIn"], problem="" if account["signedIn"] else "Codex is not signed in to a ChatGPT account.")
                codex["models"] = [model["id"] for model in listCodexModels()] if account["signedIn"] else []
            except (ModelError, OSError) as error:
                codex["problem"] = str(error)
        self.status = {"codex": codex, "claudeMissing": findMissingPackages(getModelInfo("", cli="claude-code")),
                       "localMissing": findMissingPackages(getModelInfo("Qwen/Qwen3-8B")), "gpus": readGpus(),
                       "apiMissing": {provider: findMissingPackages(getModelInfo(models[0], provider=provider)) for provider, models in MODELS_API.items()}}
        self.statusTime = time.monotonic()
        return self.status

    def rules(self):
        return prompts.LEADER_RULES.format(leader=self.leader, user=USER_NAME)

    # The price of 1 million tokens of a model (the higher of reading and writing) in US dollars, 0 for a free model, None if it is not known.
    def reference(self, info):
        return self.costs.priceOf(info)["reference"]

    # The team before the swarm exists: only the leader, which sets aside the price of its model too.
    def leaderTeam(self):
        return [{"agent": self.leader, "model": self.leaderModel, "finished": False}] if self.leaderModel else []

    def describePrice(self, info):
        price = self.reference(info)
        return "price unknown" if price is None else "free" if price == 0 else f"{formatDollars(price)} per 1M tokens"

    # What the mission spent and what is left of its budget, for the prompts of the leader.
    def describeCosts(self, team):
        report = self.costs.report(team, wait=True)
        unpriced = f", and {report['unpriced']:,} tokens of models whose price is not known" if report["unpriced"] else ""
        lines = [f"The mission spent {formatDollars(report['spent'])} so far{unpriced}. You and every agent cost money with each call of an API model."]
        if report["budget"] is None:
            lines.append("The user set no budget, but keep the cost low: prefer the cheaper model when it fits the task as well.")
        else:
            lines.append(f"The budget of the mission is {formatDollars(report['budget'])}. The agents that did not finish set aside {formatDollars(report['setAside'])} "
                         f"(the price of 1 million tokens of their model, less what they spent), so {formatDollars(max(report['left'], 0))} is left for new models."
                         + (" The budget is spent: propose no new API model, and remove the agents that are not needed." if report["left"] <= 0 else ""))
        lines += [f"- {row['agent']}: {formatDollars(row['cost'])} ({row['calls']} calls, {row['input']:,} tokens read, {row['output']:,} written)" for row in report["agents"]]
        return "\n".join(lines)

    def describeFolder(self):
        return str(self.folder) if self.folder else "No folder: the agents save nothing on their own, and no file can be used."

    # taken is the VRAM in GB the models already chosen take (the leader, and the other agents), and money what is left of the budget for new
    # models in US dollars (None without a budget).
    def describeModels(self, taken=0.0, money=None):
        status = self.readStatus()
        gpus = status["gpus"]
        gpu = checkVram(taken, gpus)
        left = round(gpu["total"] - taken, 1)
        lines = ["LOCAL MODELS run on the GPUs of the user: free of charge, and private."]
        if not gpus:
            lines.append("None can be used: no supported GPU was found on this computer.")
        elif status["localMissing"]:
            lines.append(f"None can be used now: they need the packages {', '.join(status['localMissing'])}, which are not installed.")
        else:
            lines.append(f"The GPUs have {gpu['total']} GB of VRAM in total, and {gpu['free']} GB are free now. The models already chosen take {taken} GB, so all the next "
                         f"ones together can take {max(left, 0)} GB. After each model, the VRAM it needs at 16 bits: with \"bits\": 8 it needs half, with \"bits\": 4 a quarter. "
                         "Only the models that can fit are listed.")
            for family, models in MODELS_LOCAL.items():
                fitting = [f"{name} ({getVram(size)} GB)" for name, size in models.items() if getVram(size, 4) <= left]
                if fitting:
                    lines.append(f"- {family}: {', '.join(fitting)}")
        lines.append("API MODELS run on the servers of their company, and the user pays for what they use. After each model, the price of 1 million of its tokens "
                     "(the higher of reading and writing), in US dollars.")
        prices = describeCached("model-prices")
        if prices["fetchedAt"]:
            lines.append(f"These prices are those of {prices['fetchedAt']}" + (", the last time there was an internet connection: they may have changed since." if prices["offline"] else "."))
        if money is not None:
            lines.append(f"{formatDollars(max(money, 0))} of the budget of the mission is left for new models. Each new API model sets aside the price of 1 million of its "
                         "tokens, so only the models whose price fits in what is left are listed.")
        hidden = 0
        for provider, models in MODELS_API.items():
            missing = status["apiMissing"].get(provider)
            ready = f"cannot be used: the package {', '.join(missing)} is not installed" if missing else \
                "ready, the API key is set" if self.hasKey(provider) else "not ready: the user must give an API key first"
            shown = [name for name in models if self.withinBudget(getModelInfo(name, provider=provider), money)]
            hidden += len(models) - len(shown)
            if shown:
                lines.append(f"- {API_KEYS[provider]['company']} ({ready}): " + ", ".join(f"{name} ({self.describePrice(getModelInfo(name, provider=provider))})" for name in shown))
        if hidden:
            lines.append(f"({hidden} other API models cost more than what is left of the budget: they cannot be chosen.)")
        lines.append("CODING AGENTS are programs on the computer of the user that think like a model and can also read files and run commands, each time with "
                     "the permission of the user. They fit the coder task best.")
        claude = f"cannot be used: the package {', '.join(status['claudeMissing'])} is not installed" if status["claudeMissing"] else \
            "ready" if self.hasKey("claude") else "not ready: the user must give an Anthropic API key first"
        lines.append(f"- claude-code: Claude Code, billed to the Anthropic API key of the user ({claude}). Write \"claude-code\" for its default model "
                     "(its price is only known once it ran), or \"claude-code:<model>\" with one of the Anthropic models listed above, at the same price.")
        codex = status["codex"]
        models = f", or \"codex:<model>\" with one of: {', '.join(codex['models'])}" if codex["models"] else ""
        lines.append(f"- codex: Codex, with the ChatGPT plan of the user ({'ready' if codex['ready'] else 'cannot be used: ' + codex['problem'].rstrip('.')}). "
                     f"Its use is paid by the plan, not by the budget. Write \"codex\" for its default model{models}.")
        lines.append("RECOMMENDED for each task, the lighter first:")
        local = bool(gpus) and not status["localMissing"]
        for key, task in TASKS.items():
            names = [name for name in RECOMMENDED_LOCAL[task["recommend"]] if local and getVram(getModelInfo(name)["billions"], 4) <= left][:3] + \
                [name for name in RECOMMENDED_API[task["recommend"]] if self.withinBudget(getModelInfo(name), money)]
            lines.append(f"- {key}: {', '.join(names) or 'no model of the list fits what is left'}")
        return "\n".join(lines)

    # Whether a model can be chosen with the money left of the budget (always without a budget, and for a model whose price is not known).
    def withinBudget(self, info, money):
        price = self.reference(info) if money is not None else None
        return price is None or price <= money

    def moneyLeft(self, team):
        return self.costs.left(team, wait=True)["left"]

    def buildPrompt(self):
        taken = self.leaderModel["vram"] if self.leaderModel else 0.0
        return prompts.LEADER_BUILD_PROMPT.format(rules=self.rules(), mission=self.mission, folder=self.describeFolder(),
                                                  tasks=describeTasks(), models=self.describeModels(taken, self.moneyLeft(self.leaderTeam())),
                                                  costs=self.describeCosts(self.leaderTeam()), most=self.maxAgents)

    def repairPrompt(self, problems, previous, blocks):
        return f"{self.rules()}\n\n" + prompts.LEADER_REPAIR_PROMPT.format(problems="\n".join(f"- {problem}" for problem in problems), previous=previous, blocks=blocks)

    def describePossible(self, swarm):
        leader, members = swarm.getLeader(), list(swarm.members.items())
        others = [name for name, member in members if name != leader]
        changeable = [name for name, member in members if name != leader and not member["started"] and member["status"] == "waiting"]
        lines = [("- Add: yes, a new agent joins the agents at work now. It can wait for: " + (", ".join(others) or "nobody") + "." if canJoin(swarm) else
                  "- Add: no, no agent can join now."), f"- The swarm has {len(others)} agents besides you, and can have {self.maxAgents} at most.",
                 f"- Remove: {', '.join(others)}." if others else "- Remove: nobody, you are alone.",
                 f"- Change the model: {', '.join(changeable)} (they did not start yet)." if changeable else "- Change the model: nobody, every agent already started."]
        return "\n".join(lines)

    def supervisePrompt(self, swarm, events, decisions):
        team = swarm.describeTeam()
        return prompts.LEADER_SUPERVISE_PROMPT.format(rules=self.rules(), mission=self.mission, folder=self.describeFolder(), progress=swarm.describeProgress(),
                                                      events="\n".join(f"- {event}" for event in events), possible=self.describePossible(swarm), tasks=describeTasks(),
                                                      models=self.describeModels(swarm.getNeededVram(), self.moneyLeft(team)), decisions=decisions,
                                                      costs=self.describeCosts(team))

    def confirmPrompt(self, swarm, sentences):
        team = swarm.describeTeam()
        return prompts.LEADER_CONFIRM_PROMPT.format(rules=self.rules(), mission=self.mission, progress=swarm.describeProgress(), possible=self.describePossible(swarm),
                                                    tasks=describeTasks(), models=self.describeModels(swarm.getNeededVram(), self.moneyLeft(team)),
                                                    sentences="\n".join(f'"{sentence}"' for sentence in sentences), costs=self.describeCosts(team))
