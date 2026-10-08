# The steps that build a swarm in the command line: the mission, the budget, the task of each agent with its questions, and its folder.
from cli_console import askUntilValid, askYesNo, chooseFrom
from datetime import datetime
from harness_utils import ConnectionLost, FETCH_ERRORS, describeError
from message_loops import checkEmailLogin, nextOccurrence
from messengers import MessagingError, checkMessenger, findTelegramChats
from mission_costs import checkBudget
from sources_library import ALL_NEWS_OUTLETS, MESSAGING_APPS, NEWS_OUTLETS, PAPER_PUBLISHERS
from tasks_library import ADVANCED_FIELDS, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getHelp, getTask, isAsked, messengerSettings, parseAnswer, parseChoices, suggestFolder, suggestName
from writing_loops import findPublishers


def askAgentCount(console):
    console.say("A swarm is a team of agents. Each agent has one task (write an email, check a proof...) and a model, the AI that does the thinking. "
                "The first agent is the leader: it summarises the work of the others for you, and passes your corrections on to the agents they concern. "
                "One agent is enough to start.")
    return askUntilValid(console, "How many agents do you want in your swarm?", lambda text: parseAnswer({"key": "count", "ask": "", "kind": "number", "required": True}, text))


# The budget of the whole mission, for its API models: each one chosen sets aside the price of 1 million of its tokens (see MissionCosts).
def askBudget(console, costs):
    console.say("You can give the mission a budget, in US dollars, for its API models (the leader included). Each API model you choose sets aside the price of "
                "1 million of its tokens, and the lists only show the models that fit in what is left. Models on your GPUs and Codex cost nothing from it. "
                "What the mission spends is counted, and shown while the swarm runs (type cost).")
    def parse(text):
        try:
            return checkBudget(text), ""
        except ValueError as error:
            return None, str(error)
    costs.setBudget(askUntilValid(console, "Budget of the mission in US dollars (Enter for no budget):", parse))


def askMission(console):
    console.say("The agents read the mission to understand what they work for.")
    return askUntilValid(console, "What is the mission of the swarm? (one or two sentences)", lambda text: parseAnswer({"key": "mission", "ask": "", "kind": "text", "required": True}, text))


def chooseTask(console, number, total):
    keys = list(TASKS)
    labels = [TASKS[key]["label"] for key in keys]
    def showInfo(text):
        if not text.lower().startswith(("info", "?")):
            return False
        chosen, error = parseChoices(labels, text.lstrip("?").strip() if text.startswith("?") else text[4:], one=True)
        console.say(TASKS[keys[labels.index(chosen[0])]]["info"] if not error else "Write info and the number of a task, like: info 3")
        return True
    label = chooseFrom(console, f"\nAgent {number} of {total}: what must this agent do? (type info 2 to read about task 2)", labels, extra=showInfo)
    return keys[labels.index(label)]


def askName(console, key, taken):
    suggestion = suggestName(key, taken)
    def parse(text):
        name = text.strip() or suggestion
        error = checkAgentName(name, taken)
        return (None, error) if error else (name, "")
    return askUntilValid(console, f"Name of this agent [{suggestion}]:", parse)


def chooseOutlets(console):
    console.say("Pick outlets from the lists, group by group. You can also give the address of any news feed (RSS or Atom), or of a news website that has one.")
    chosen = []
    while True:
        groups = list(NEWS_OUTLETS)
        labels = [f"{group} ({len(NEWS_OUTLETS[group])} outlets)" for group in groups] + ["Search an outlet by name", "Add the address of a feed myself", f"Done ({len(chosen)} chosen)"]
        index = labels.index(chooseFrom(console, f"\nOutlets chosen so far: {', '.join(chosen) or 'none'}", labels))
        if index == len(labels) - 1:
            if chosen:
                return chosen
            console.say("Choose at least one outlet.")
        elif index == len(labels) - 2:
            address = askUntilValid(console, "Address of the feed (starting with http:// or https://):",
                                    lambda text: (text.strip(), "") if text.strip().startswith(("http://", "https://")) else (None, "The address must start with http:// or https://."))
            chosen.append(address)
        elif index == len(labels) - 3:
            text = console.ask("Part of the name of the outlet:").strip().lower()
            found = [name for name in ALL_NEWS_OUTLETS if text and text in name.lower()]
            if not found:
                console.say("No outlet has this in its name.")
            chosen += [name for name in (chooseFrom(console, "Which ones?", found, one=False, hint="Your choices (numbers like 1,3-5; empty = none):", default=[]) if found else []) if name not in chosen]
        else:
            names = list(NEWS_OUTLETS[groups[index]])
            picked = chooseFrom(console, f"\n{groups[index]}:", names, one=False, hint="Your choices (numbers like 1,3-5; empty = none):", default=[])
            chosen += [name for name in picked if name not in chosen]


def choosePublishers(console):
    console.say("A survey can be restricted to some publishers: then only their papers are searched (with the number Crossref gives to each publisher). "
                "Choose none to search only in the search engines.")
    names = list(PAPER_PUBLISHERS)
    picked = chooseFrom(console, "Publishers:", names, one=False, hint="Your choices (numbers like 1,3-5; empty = none):", default=[])
    publishers = {name: PAPER_PUBLISHERS[name] for name in picked}
    while askYesNo(console, "Add a publisher that is not in the list?", False):
        text = askUntilValid(console, "Name of the publisher:", lambda value: parseAnswer({"key": "publisher", "ask": "", "kind": "text", "required": True}, value))
        try:
            found = findPublishers(text)
        except FETCH_ERRORS as error:
            console.say(f"Crossref could not be asked: {describeError(error)}.")
            continue
        labels = [f"{publisher['name']} ({publisher['papers']:,} works)" for publisher in found]
        if not found:
            console.say("Crossref does not know a publisher with this name.")
            continue
        label = chooseFrom(console, "Which one?", labels + ["None of them"])
        if label != "None of them":
            publishers[found[labels.index(label)]["name"]] = found[labels.index(label)]["id"]
    return publishers


def askAccounts(console):
    accounts = {}
    while askYesNo(console, "Add an account for a website?", False):
        host = askUntilValid(console, "The website (like ieeexplore.ieee.org):", lambda text: (text.strip().lower(), "") if text.strip() and "/" not in text else (None, "Write only the name of the website, without https:// or a path."))
        user = askUntilValid(console, f"Username for {host}:", lambda text: parseAnswer({"key": "user", "ask": "", "kind": "text", "required": True}, text))
        accounts[host] = (user, askUntilValid(console, f"Password for {host} (hidden):", lambda text: parseAnswer({"key": "password", "ask": "", "kind": "secret", "required": True}, text), secret=True))
    return accounts


def askField(console, field, answers):
    kind, help = field["kind"], getHelp(field, answers)
    if help:
        console.say(f"  ({help})")
    if kind == "choice":
        default = getDefault(field, answers)
        return chooseFrom(console, field["ask"], field["options"], default=default, hint=f"Your choice (a number, Enter = {default}):" if default else "")
    if kind == "choices":
        picked = chooseFrom(console, field["ask"], field["options"], one=False, default=field["default"], none=True,
                            hint="Your choices (numbers like 1,3-4; Enter = all; none = none of them):")
        return picked
    if kind == "outlets":
        return chooseOutlets(console)
    if kind == "publishers":
        return choosePublishers(console)
    if kind == "accounts":
        console.say(field["ask"])
        return askAccounts(console)
    default = getDefault(field, answers)
    shown = f" [{' '.join(default) if isinstance(default, list) else default}]" if default not in (None, "") and kind != "secret" else ""
    return askUntilValid(console, f"{field['ask']}{shown}:", lambda text: parseAnswer(field, text, answers), secret=kind == "secret")


def explainSchedule(console, clock):
    if not clock:
        console.say("The feeds are collected as soon as the agent starts.")
        return
    moment = nextOccurrence(clock)
    minutes = int((moment - datetime.now()).total_seconds() // 60)
    console.say(f"The feeds will be collected on {moment:%A %Y-%m-%d at %H:%M}, in {minutes // 60} h {minutes % 60} min. This agent waits until then, the other agents "
                "do not. While the swarm runs you can start it earlier with: start <agent>")


def verifyEmail(console, answers):
    password = next(field for field in TASKS["email"]["fields"] if field["key"] == "password")
    while askYesNo(console, "Test the login now? Nothing is sent: the program only logs in to check your password.", True):
        console.say("Testing the login...")
        problem = checkEmailLogin(answers["sender"], answers["password"], answers["smtp"], answers["imap"])
        if not problem:
            console.say("The login works.")
            return
        console.say(problem)
        if not askYesNo(console, "Type the password again?", True):
            return
        answers["password"] = askField(console, password, answers)


# What the agent will do, in the words of the loop itself, built without a model.
def describeAgent(spec):
    return describeLoop(buildLoop(spec["task"], None, spec["answers"]))


# The chat of the user in Telegram is found from the messages they sent to their bot.
def lookUpTelegramChat(console, token):
    console.say("Open your bot in Telegram, press Start and send it any message. Then come back here.")
    while True:
        if console.ask("Press Enter when you sent a message to your bot (or type skip to write the chat number yourself):").strip().lower() == "skip":
            return ""
        try:
            chats = findTelegramChats(token)
        except (MessagingError, ConnectionLost) as error:
            console.say(str(error))
            chats = []
        if chats:
            labels = [f"{chat['name']} (chat number {chat['id']})" for chat in chats]
            return str(chats[labels.index(chooseFrom(console, "Which chat is yours?", labels))]["id"])
        console.say("I did not see any message yet.")
        if not askYesNo(console, "Try again?", True):
            return ""


def verifyMessenger(console, answers):
    app = answers.get("messenger")
    if app not in MESSAGING_APPS:
        return
    chat = answerKey(app, "chat")
    while app == "Telegram" and not answers[chat]:
        answers[chat] = lookUpTelegramChat(console, answers[answerKey(app, "token")]) or askField(console, {"ask": "Your chat number", "kind": "text", "required": True}, answers)
    while askYesNo(console, f"Test the connection to {app} now? Nothing is sent: only the information is checked.", True):
        console.say(f"Testing {app}...")
        problem = checkMessenger(app, messengerSettings(answers))
        if not problem:
            console.say(f"{app} accepted the information.")
            return
        console.say(problem)
        if not askYesNo(console, "Enter the information again?", True):
            return
        for field in MESSAGING_APPS[app]["fields"]:
            answers[answerKey(app, field["key"])] = askField(console, field, answers)


def fillTask(console, key, taken):
    task, answers = TASKS[key], {}
    console.say(f"\n--- {task['label']} ---\n{task['info']}")
    for field in task["fields"]:
        if not isAsked(field, answers):
            continue
        answers[field["key"]] = askField(console, field, answers)
        if field["key"] == "collectAt":
            explainSchedule(console, answers["collectAt"])
        if field["key"] == "messenger" and answers["messenger"] in MESSAGING_APPS:
            console.say(MESSAGING_APPS[answers["messenger"]]["info"])
    while key == "literature" and not (answers["searches"] or answers["publishers"]):
        console.say("The survey needs a place to search: choose at least a search engine or a publisher.")
        answers["searches"], answers["publishers"] = (askField(console, field, answers) for field in task["fields"] if field["key"] in ("searches", "publishers"))
    if key == "email":
        verifyEmail(console, answers)
    if key == "news":
        verifyMessenger(console, answers)
    if askYesNo(console, "Change the advanced settings of this agent?", False):
        for field in ADVANCED_FIELDS:
            answers[field["key"]] = askField(console, field, answers)
    spec = {"task": key, "answers": answers, "name": askName(console, key, taken)}
    console.say(f"{spec['name']} will do this: {describeAgent(spec)}")
    return spec


# ==============
# The folder of each agent: where it saves what it makes, and where the files it works on must be.
# ==============
def askFolder(console, spec, number, total):
    task = getTask(spec["task"])
    suggestion = suggestFolder(spec["answers"])
    console.say(f"\n--- Folder of agent {number} of {total}: {spec['name']} ({task['role']}) ---\n{describeAgent(spec)}\n{task['folder']}")
    def parse(text):
        if text.strip().lower() == "none" or not (text.strip() or suggestion):
            return None, ""
        value, error = parseAnswer({"key": "folder", "ask": "", "kind": "folder"}, text.strip() or suggestion)
        if error:
            return None, error
        try:
            buildLoop(spec["task"], None, {**spec["answers"], "folder": value})
        except ValueError as problem:
            return None, str(problem)
        return value, ""
    shown = f" [{suggestion}]" if suggestion else ""
    return askUntilValid(console, f"Folder of {spec['name']}{shown} (a path, Enter = {'the one in brackets' if suggestion else 'no folder'}, none = no folder):", parse)


def chooseFolders(console, specs):
    console.say("An agent can work inside a folder of your computer, where it saves what it makes. This is optional, and every agent can have its own folder.")
    for number, spec in enumerate(specs, 1):
        spec["answers"]["folder"] = askFolder(console, spec, number, len(specs))
