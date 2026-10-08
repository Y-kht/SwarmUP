# The step of the models in the command line: a local model on the GPUs, an API model with its price, or a coding agent, for each agent.
import os
import shutil

from cli_console import askUntilValid, askYesNo, chooseFrom, shorten
from cli_steps import describeAgent
from cli_view import describeModel
from codex_agent import CodexLogin, checkCodex, listCodexModels, readCodexAccount
from gpu_check import checkVram, readGpus
from internet_cache import describeCached, getModelCost
from mission_costs import formatDollars
from model_clients import createModel
from model_support import ModelError, findMissingPackages, getApiKey, getHubFolder, isDownloaded, lookupHuggingFace
from models_library import API_KEYS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, getProvider, isGated
from tasks_library import getTask, parseAnswer, parseChoices


SHOW_ALL, MANUAL, BACK = "Show all the models of the library", "Type the name of another model myself", "Go back"


CHANGE_KIND = "back"


# ==============
# The model of each agent: local on the GPUs, or paid through an API.
# ==============
def describeGpus(status):
    lines = ["GPU check:"] + [f"  {gpu['name']}: {gpu['total']} GB in total, {gpu['free']} GB free now" for gpu in status["gpus"]]
    lines.append(f"  All the GPUs: {status['total']} GB in total, {status['free']} GB free now ({status['used']} GB are used by other jobs)")
    lines.append(f"  VRAM the swarm is expected to need: {status['needed']} GB. Left in the GPUs: {status['left']} GB.")
    return "\n".join(lines)


def describeCost(cost):
    if "error" in cost:
        return f"  {cost['model']}: {cost['error']} Official prices: {cost['page']}"
    cached = f", cached input ${cost['cachedInput']}" if cost.get("cachedInput") is not None else ""
    context = f". Context window: {cost['context']:,} tokens" if cost.get("context") else ""
    note = f" {cost['note']}" if cost.get("note") else ""
    return f"  {cost['model']}: input ${cost['input']} and output ${cost['output']} per 1 million tokens{cached}{context}.{note} Official prices: {cost['page']}"


def labelLocal(swarm, name, replacing=None):
    info = getModelInfo(name)
    check = swarm.checkModel(info, replacing)
    mark = "" if not check["message"] else "  [too big for the GPUs]" if not check["allowed"] else "  [not free now]"
    return f"{name} ({info['vram']} GB{', gated' if isGated(name) else ''}){mark}"


def browseAll(console, groups, label):
    names = list(groups)
    labels = [f"{name} ({len(groups[name])} models)" for name in names]
    picked = chooseFrom(console, f"\n{label}:", labels + [BACK])
    return None if picked == BACK else names[labels.index(picked)]


def askManualLocal(console):
    name = askUntilValid(console, "Name of the model on Hugging Face (owner/name, like Qwen/Qwen3-8B):",
                         lambda text: (text.strip(), "") if text.strip().count("/") == 1 and " " not in text.strip() else (None, "It is written owner/name, like Qwen/Qwen3-8B."))
    billions = next((family[name] for family in MODELS_LOCAL.values() if name in family), None)
    if billions is None:
        try:
            found = lookupHuggingFace(name)
        except ModelError as error:
            console.say(str(error))
            return None
        billions = found["billions"]
        console.say(f"Found on Hugging Face. " + (f"It has {billions} billion parameters." if billions else "Its size is not published."))
        if not billions:
            billions = askUntilValid(console, "How many billion parameters does it have? (a number, like 7 or 8.2)",
                                     lambda text: (float(text), "") if text.replace(".", "", 1).isdigit() and float(text) > 0 else (None, "Write a number above 0, like 8.2."))
    return getModelInfo(name, billions=billions)


# The first list only has the models that fit in the VRAM the other agents leave. All of them are in the list of all the models.
def chooseLocalModel(console, swarm, task, replacing=None):
    listed = RECOMMENDED_LOCAL[getTask(task)["recommend"]]
    recommended = [name for name in listed if swarm.checkModel(getModelInfo(name), replacing)["allowed"]]
    while True:
        labels = [labelLocal(swarm, name, replacing) for name in recommended] + [SHOW_ALL, MANUAL]
        console.say("\nThe number in parentheses is the VRAM, in GB, that the model is expected to need.")
        if len(recommended) < len(listed):
            console.say(f"{len(listed) - len(recommended)} recommended models need more VRAM than the other agents leave, so they are not shown here. They are in the list of all the models.")
        picked = chooseFrom(console, "Models recommended for this task, from the smallest to the largest (type back to choose again where the model runs):", labels,
                            words={"back": CHANGE_KIND})
        if picked == CHANGE_KIND:
            return None
        if picked == SHOW_ALL:
            families = {family: list(models) for family, models in MODELS_LOCAL.items()}
            family = browseAll(console, families, "Families of local models")
            if not family:
                continue
            shown = [labelLocal(swarm, name, replacing) for name in families[family]]
            back = chooseFrom(console, f"\n{family}:", shown + [BACK])
            if back == BACK:
                continue
            info = getModelInfo(families[family][shown.index(back)])
        elif picked == MANUAL:
            info = askManualLocal(console)
            if info is None:
                continue
        else:
            info = getModelInfo(recommended[labels.index(picked)])
        check = swarm.checkModel(info, replacing)
        if not check["allowed"]:
            console.say(check["message"])
            continue
        return info


# From when the prices are: without internet, they are those of the last connection.
def describePriceDate():
    state = describeCached("model-prices")
    if not state["fetchedAt"]:
        return ""
    return f"No internet: the prices are those of the last connection, {state['fetchedAt']}." if state["offline"] else f"Prices of {state['fetchedAt']}."


def describePrice(costs, info):
    price = costs.priceOf(info)["reference"]
    return "price unknown" if price is None else f"{formatDollars(price)} per 1M tokens"


# What is left of the budget of the mission for the model of an agent (replacing is the agent whose model changes), or None without a budget.
def moneyLeft(swarm, replacing=None):
    return swarm.costs.left([member for member in swarm.describeTeam() if member["agent"] != replacing], wait=True)["left"]


def overBudget(swarm, info, money):
    price = swarm.costs.priceOf(info)["reference"]
    return money is not None and price is not None and price > money


# The first list only has the models whose price of 1 million tokens fits in what is left of the budget. All of them are in the list of all the models.
def chooseApiModel(console, swarm, task, replacing=None):
    money = moneyLeft(swarm, replacing)
    listed = RECOMMENDED_API[getTask(task)["recommend"]]
    recommended = [name for name in listed if not overBudget(swarm, getModelInfo(name), money)]
    if money is not None:
        console.say(f"\n{formatDollars(max(money, 0))} of the budget of the mission is left for this model.")
    if describePriceDate():
        console.say(describePriceDate())
    if len(recommended) < len(listed):
        console.say(f"{len(listed) - len(recommended)} recommended models cost more than that per 1 million tokens, so they are not shown here. They are in the list of all the models.")
    while True:
        labels = [f"{name} ({API_KEYS[getProvider(name)]['company']}, {describePrice(swarm.costs, getModelInfo(name))})" for name in recommended] + [SHOW_ALL, MANUAL]
        def showPrices(text):
            word, _, rest = text.partition(" ")
            if word.lower() not in ("p", "price", "prices"):
                return False
            chosen, error = parseChoices(labels[:len(recommended)], rest) if rest.strip() and rest.strip().lower() != "all" else (labels[:len(recommended)], "")
            console.say(error or "\n".join(describeCost(getModelCost(getProvider(recommended[labels.index(label)]), recommended[labels.index(label)])) for label in chosen))
            return True
        picked = chooseFrom(console, "\nModels recommended for this task. Type p 2 for the price of model 2, or p all for the prices of all of them (type back to choose again where the model runs):",
                            labels, extra=showPrices, words={"back": CHANGE_KIND})
        if picked == CHANGE_KIND:
            return None
        if picked == SHOW_ALL:
            family = browseAll(console, MODELS_API, "Providers of API models")
            if not family:
                continue
            def showFamilyPrices(text):
                word, _, rest = text.partition(" ")
                if word.lower() not in ("p", "price", "prices"):
                    return False
                chosen, error = parseChoices(MODELS_API[family], rest) if rest.strip() and rest.strip().lower() != "all" else (MODELS_API[family], "")
                console.say(error or "\n".join(describeCost(getModelCost(family, name)) for name in chosen))
                return True
            shown = [f"{name} ({describePrice(swarm.costs, getModelInfo(name))}){'  [over your budget]' if overBudget(swarm, getModelInfo(name), money) else ''}"
                     for name in MODELS_API[family]]
            name = chooseFrom(console, f"\n{family} (type p 2 for the price of model 2, or p all):", shown + [BACK], extra=showFamilyPrices)
            if name == BACK:
                continue
            info = getModelInfo(MODELS_API[family][shown.index(name)])
            if overBudget(swarm, info, money):
                console.say(f"Warning: {info['name']} costs more per 1 million tokens than the {formatDollars(max(money, 0))} left of the budget.")
            return info
        if picked == MANUAL:
            provider = chooseFrom(console, "\nWhich company provides the model?", [f"{API_KEYS[key]['company']} ({key}-...)" for key in MODELS_API] + [BACK])
            if provider == BACK:
                continue
            key = list(MODELS_API)[[f"{API_KEYS[key]['company']} ({key}-...)" for key in MODELS_API].index(provider)]
            name = askUntilValid(console, "Exact name of the model, as the company writes it in its API:", lambda text: (text.strip(), "") if text.strip() else (None, "This answer is needed."))
            return getModelInfo(name, provider=key)
        return getModelInfo(recommended[labels.index(picked)])


def chooseKind(console, swarm, number, replacing=None):
    status = checkVram(swarm.getNeededVram(replacing), readGpus())
    local = f"Local: runs on your GPUs ({status['total']} GB of VRAM, {status['free']} GB free now). Free to use and private, but the model must fit in the VRAM." if status["gpus"] \
        else "Local: runs on your GPUs. No supported GPU was found on this computer, so it is not possible."
    api = "API: runs on the servers of a company (OpenAI, Anthropic, Google, DeepSeek). You pay for every use, and you need an API key."
    cli = ("Coding agent: Claude Code (with your Anthropic API key) or Codex (with your ChatGPT plan), on this computer. It can also read files and run "
           "commands, each time with your approval.")
    while True:
        console.say(f"\nWhere must the model of agent {number} run?\n  {local}\n  {api}\n  {cli}")
        kind = chooseFrom(console, "", ["Local (on my GPUs)", "API (paid)", "Coding agent (Claude Code or Codex)"], hint="Your choice (1, 2 or 3):")
        if not kind.startswith("Local") or status["gpus"]:
            return "local" if kind.startswith("Local") else "api" if kind.startswith("API") else "cli"
        console.say(status["message"] or "No supported GPU was found, so local models cannot run. Choose API models.")


# Codex uses the ChatGPT plan of the user: if it is not signed in, the user signs in on the page of OpenAI (in the browser, or with a code).
# It returns True when Codex is ready.
def signInCodex(console):
    status = checkCodex()
    if status["problem"]:
        console.say(status["problem"])
        return False
    account = readCodexAccount()
    while not account["signedIn"]:
        options = ["Sign in with ChatGPT in the browser", "Sign in with a code (when the browser cannot come back to this computer)", BACK]
        picked = chooseFrom(console, "\nCodex is not signed in. It uses your ChatGPT plan: you sign in on the page of OpenAI, and Codex keeps the sign-in.", options)
        if picked == BACK:
            return False
        login = CodexLogin()
        target = login.start("code" if picked == options[1] else "browser")
        if target["code"]:
            console.say(f"Open {target['url']}, sign in with your ChatGPT account, and type this code: {target['code']}")
        else:
            console.say(f"Open this page in your browser and sign in with your ChatGPT account:\n{target['url']}")
        console.say("Waiting for the sign-in (up to 15 minutes)...")
        problem = login.wait(timeout=900)
        if problem:
            console.say(f"The sign-in did not work: {problem}")
        account = readCodexAccount()
    console.say(f"Codex is signed in{' as ' + account['email'] if account.get('email') else ''}.")
    return True


def chooseCodingAgent(console, swarm, replacing=None):
    while True:
        labels = [f"{agent['label']} ({'your Anthropic API key' if agent['provider'] else 'your ChatGPT plan'})" for agent in MODELS_CLI.values()] + [BACK]
        picked = chooseFrom(console, "\nWhich coding agent? (Anthropic does not allow other programs to use a Claude subscription, so Claude Code is used with an API key.)", labels)
        if picked == BACK:
            return None
        cli = list(MODELS_CLI)[labels.index(picked)]
        if cli == "codex":
            if not signInCodex(console):
                continue
            models = [DEFAULT_CLI_MODEL] + [model["id"] for model in listCodexModels()]
        else:
            money = moneyLeft(swarm, replacing)
            models = [name for name in MODELS_CLI[cli]["models"] if name == DEFAULT_CLI_MODEL or not overBudget(swarm, getModelInfo(name, cli=cli), money)]
            if len(models) < len(MODELS_CLI[cli]["models"]):
                console.say(f"{len(MODELS_CLI[cli]['models']) - len(models)} models cost more per 1 million tokens than the {formatDollars(max(money, 0))} left of the budget, so they are not shown.")
        shown = [f"Let {MODELS_CLI[cli]['label']} choose" if name == DEFAULT_CLI_MODEL else name if cli == "codex" else
                 f"{name} ({describePrice(swarm.costs, getModelInfo(name, cli=cli))})" for name in models]
        name = chooseFrom(console, f"\nWhich model must {MODELS_CLI[cli]['label']} use?", shown + [BACK])
        if name == BACK:
            continue
        return getModelInfo(models[shown.index(name)], cli=cli)


def prepareLocal(console, info, tokens):
    name = info["name"]
    if isDownloaded(name):
        console.say(f"{name} is already downloaded in {getHubFolder()}.")
    else:
        folder = getHubFolder()
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        console.say(f"{name} will be downloaded to {getHubFolder()}: about {round(info['billions'] * 2, 1)} GB, and {shutil.disk_usage(folder).free / 1e9:.0f} GB are free there. "
                    "Change the folder with the HF_HOME environment variable.")
    if isGated(name) and not os.environ.get("HF_TOKEN"):
        console.say(f"{name} is a gated model: accept its license at https://huggingface.co/{name} first. Then give a Hugging Face token (created at "
                    "https://huggingface.co/settings/tokens), or press Enter if you already logged in with huggingface-cli.")
        token = askUntilValid(console, "Hugging Face token (hidden, Enter to skip):", lambda text: (text.strip(), ""), secret=True)
        tokens[name] = token or None
    waitForPackages(console, info)


def waitForPackages(console, info):
    missing = findMissingPackages(info)
    while missing:
        console.say(f"This model needs the packages {', '.join(missing)}. Install them in another terminal with: pip install -U {' '.join(missing)}")
        if console.ask("Press Enter when they are installed, or type skip to go on without them:").strip().lower() == "skip":
            return
        missing = findMissingPackages(info)


def prepareApi(console, info, keys, price=True):
    provider, details = info["provider"], API_KEYS[info["provider"]]
    if provider not in keys:
        if getApiKey(provider):
            console.say(f"Using the API key found in {details['variable']}.")
        else:
            console.say(f"{details['company']} needs an API key. Create one at {details['page']}. It is only kept in memory while this program runs. "
                        f"(Next time you can set {details['variable']} instead of typing it.)")
            keys[provider] = askUntilValid(console, f"Your {details['company']} API key (hidden):", lambda text: parseAnswer({"key": "key", "ask": "", "kind": "secret", "required": True}, text), secret=True)
    waitForPackages(console, info)
    if price and askYesNo(console, f"Do you want the price of {info['name']}?", True):
        console.say(describeCost(getModelCost(provider, info["name"])))


def testConnection(console, model):
    if not askYesNo(console, "Test the connection now with a tiny request? (it costs a fraction of a cent)", True):
        return
    try:
        console.say(f"The model answered: {shorten(model.input('Reply with the single word OK.'), 60)}")
    except ModelError as error:
        console.say(f"It did not work: {error}")


def chooseModel(console, swarm, spec, number, total, keys, tokens, replacing=None):
    task = spec["task"]
    status = checkVram(swarm.getNeededVram(replacing), readGpus())
    console.say(f"\n--- Model of agent {number} of {total}: {spec['name']} ({getTask(task)['role']}) ---\n{describeAgent(spec)}")
    if task == "leader":
        console.say("This leader builds the swarm, follows it, and proposes changes to you: it must plan well and follow a strict format, so a capable model is worth it.")
    elif number == 1:
        console.say("This agent is the leader: besides its own task, its model writes the summaries you approve and decides which agents your corrections concern. "
                    "A capable model is worth it here.")
    if status["needed"]:
        console.say(f"VRAM of the swarm so far: {status['needed']} GB of {status['total']} GB ({status['free']} GB free now).")
    while True:
        kind = chooseKind(console, swarm, number, replacing)
        info = chooseLocalModel(console, swarm, task, replacing) if kind == "local" else chooseApiModel(console, swarm, task, replacing) if kind == "api" else \
            chooseCodingAgent(console, swarm, replacing)
        if info is None:
            continue
        if info.get("cli"):
            console.say(f"\nYou chose {describeModel(info)}. It reads the files of its folder freely, and asks you before anything else (a command, a change of a "
                        "file, a web page, a file outside its folder).")
            if info["cli"] == "claude-code":
                prepareApi(console, info, keys, price=False)
            else:
                waitForPackages(console, info)
            return info, createModel(info, keys, report=console.say)
        check = swarm.checkModel(info, replacing)
        if info["local"]:
            console.say(f"\nYou chose {info['name']}: it is expected to need {info['vram']} GB of VRAM.\n" + describeGpus(checkVram(check["needed"], readGpus())))
            if check["message"]:
                console.say(f"Warning: {check['message']}")
                if not askYesNo(console, "You can choose it, but the swarm cannot run until the memory is free. Keep this model?", False):
                    continue
            prepareLocal(console, info, tokens)
            model = createModel(info, token=tokens.get(info["name"]), report=console.say)
        else:
            console.say(f"\nYou chose {info['name']} ({API_KEYS[info['provider']]['company']}).")
            if check["message"]:
                console.say(check["message"])
            prepareApi(console, info, keys)
            model = createModel(info, keys)
            testConnection(console, model)
        return info, model
