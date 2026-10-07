import os
import re
import shlex
from pathlib import Path

from checking_loops import CoderLoop, MathCheckLoop
from harness_utils import PHONE_PATTERN, USER_NAME
from message_loops import CalendarLoop, EMAIL_PATTERN, EmailLoop, NewsLoop, nextOccurrence
from sources_library import EMAIL_PROVIDERS, MESSAGING_APPS
from writing_loops import AuthorLoop, DocumentFormatLoop, LeaderLoop, LiteratureSurveyLoop, PAPER_SEARCHES

# The tasks a user can give to an agent, each one a loop of message_loops.py, writing_loops.py or checking_loops.py. For every task: what the user is told (info),
# the questions to ask (fields), how to build the loop once the model is chosen (build), and which recommendations of
# models_library.py to show (recommend). The command line test uses this now, and the graphical interface will use it later.
#
# A field is a dictionary with its key, its question (ask), its kind, and optionally: help (a text, or a function of the answers so far),
# default (a value, or a function of the answers so far), required, and when (a function of the answers so far: the question is only asked
# if it gives True, like the questions of the messaging app that was chosen). The kinds that parseAnswer understands are text, email, secret,
# number, file (an existing file), path (a file that may not exist yet), folder (an existing folder), phone (with its country code), time
# (HH:MM, or nothing), and command. The other kinds are lists or choices that the interface shows in its own way: choice (one of options),
# choices (several of options), outlets (the news outlets), publishers (the publishers of the literature survey) and accounts (the websites
# where the user has an account).
#
# Every task also says what its agent does with a folder (folder). The folder is not one of the fields: the user chooses it for each agent
# after all the tasks are chosen, and before the models. It becomes the answer "folder" (buildLoop gives it to the loop).
# The passwords, tokens and accounts (the kinds secret and accounts) are never saved: publicAnswers leaves them out.
OTHER_PROVIDER = "Other (I enter the servers)"
NO_MESSENGER = "No, I will read it here"
SECRET_KINDS = ("secret", "accounts")
DEFAULT_LOOPS = 5
AGENT_NAME = re.compile(r"[A-Za-z][\w.-]*")


def loops(answers):
    return answers.get("numberOfLoops", DEFAULT_LOOPS)


def providerDetails(answers, position):
    return EMAIL_PROVIDERS.get(answers.get("provider"), ("", "", ""))[position]


def buildEmail(model, answers):
    loop = EmailLoop(model, answers["sender"], answers["receiver"], answers["subject"], answers["request"], answers["language"], loops(answers))
    loop.settings.update(EMAIL_PASSWORD=answers["password"], EMAIL_SMTP_SERVER=answers["smtp"])
    if answers["imap"]:
        loop.settings["EMAIL_IMAP_SERVER"] = answers["imap"]
    return loop


def answerKey(app, key):
    return f"{app.lower()}{key[0].upper()}{key[1:]}"


# What a messaging app needs ({key: value}, as the sender of messengers.py wants it), from the answers.
def messengerSettings(answers):
    app = answers.get("messenger")
    return {field["key"]: answers.get(answerKey(app, field["key"])) or "" for field in MESSAGING_APPS[app]["fields"]} if app in MESSAGING_APPS else {}


def buildNews(model, answers):
    app = answers.get("messenger")
    return NewsLoop(model, answers["topics"], tuple(answers["outlets"]), answers["maxWords"], answers["language"], loops(answers), answers["collectAt"],
                    app if app in MESSAGING_APPS else None, messengerSettings(answers))


def buildLiterature(model, answers):
    loop = LiteratureSurveyLoop(model, answers["subject"], answers["length"], tuple(answers["searches"]), loops(answers), answers["publishers"])
    loop.logins.update(answers["accounts"])
    return loop


# The questions about the messaging apps: which one (or none), then only what the chosen one needs.
MESSENGER_FIELDS = [{"key": "messenger", "ask": "Do you also want to receive the briefing in a messaging app?", "kind": "choice", "options": [NO_MESSENGER, *MESSAGING_APPS],
                     "default": NO_MESSENGER, "help": "It is sent after you approve the briefing, exactly as you approved it."}]
MESSENGER_FIELDS += [{**field, "key": answerKey(app, field["key"]), "when": lambda answers, app=app: answers.get("messenger") == app}
                     for app, details in MESSAGING_APPS.items() for field in details["fields"]]

TASKS = {
    "email": {
        "label": "Email writer and sender", "name": "Emailer", "role": "email writer", "recommend": "email", "build": buildEmail,
        "folder": "It can read every file of this folder, and a copy of every email it sends is saved in its swarmup-results folder.",
        "info": "This agent writes an email and sends it from your account. If you give an IMAP server, it reads the latest email of the receiver with the same subject, "
                "and it reads your earlier emails to them, to keep the same tone. It never sends anything before you approve the exact text. It needs your login, "
                "which is only kept in memory while the program runs.",
        "fields": [
            {"key": "provider", "ask": "Which email provider do you use?", "kind": "choice", "options": [*EMAIL_PROVIDERS, OTHER_PROVIDER], "required": True,
             "help": "The provider tells the program which mail servers to use."},
            {"key": "sender", "ask": "Your email address (the one the email is sent from)", "kind": "email", "required": True},
            {"key": "password", "ask": "Password of that account", "kind": "secret", "required": True,
             "help": lambda answers: providerDetails(answers, 2) or "Use an app password if your provider has them: it can be revoked without changing your real password."},
            {"key": "smtp", "ask": "SMTP server to send the email", "kind": "text", "required": True, "default": lambda answers: providerDetails(answers, 0) or None,
             "help": "Emails are sent with STARTTLS on port 587."},
            {"key": "imap", "ask": "IMAP server to read the replies (leave empty to write a new email without reading your inbox)", "kind": "text",
             "default": lambda answers: providerDetails(answers, 1) or ""},
            {"key": "receiver", "ask": "Who receives the email? (address)", "kind": "email", "required": True},
            {"key": "subject", "ask": "Subject of the email", "kind": "text", "required": True},
            {"key": "request", "ask": "What must the email say? (explain it in your own words)", "kind": "text", "required": True},
            {"key": "language", "ask": "Language of the email", "kind": "text", "default": "English"},
        ],
    },
    "calendar": {
        "label": "Calendar planner", "name": "Planner", "role": "calendar planner", "recommend": "calendar", "build": lambda model, answers: CalendarLoop(model, answers["request"], loops(answers)),
        "folder": "It can read every file of this folder, and the calendar file calendar_events.ics is also written in it.",
        "info": "This agent books an event in a calendar file. It avoids the events already booked, tells you if the new one overlaps, and writes calendar_events.ics, "
                "which you can import into Google Calendar, Outlook or Apple Calendar. The event is only booked after you approve it.",
        "fields": [{"key": "request", "ask": "Which event must be planned? (for example: dentist next Monday at 10:30 for one hour)", "kind": "text", "required": True}],
    },
    "news": {
        "label": "News briefer", "name": "NewsBriefer", "role": "news briefer", "recommend": "news", "build": buildNews,
        "folder": "It can read every file of this folder, and every approved briefing is saved in its swarmup-results folder.",
        "info": "This agent reads the news feeds you choose, and writes a short briefing from their headlines, with the link of every story. It only uses facts "
                "of the headlines. You choose the outlets, the topics you care about, and the time of the day when the feeds are collected. The briefing is saved "
                "only after you approve it, and if you like it is also sent to your phone in Telegram or WhatsApp.",
        "fields": [
            {"key": "outlets", "ask": "Which news outlets do you want?", "kind": "outlets", "required": True},
            {"key": "topics", "ask": "Which topics do you care about? (leave empty for anything important)", "kind": "text", "default": ""},
            {"key": "collectAt", "ask": "At what time of the day must the feeds be collected? (HH:MM on a 24-hour clock, like 07:30. Leave empty to collect them right away)",
             "kind": "time", "default": None,
             "help": "The agent waits for that time. The other agents of the swarm do not wait for it, and you can start it at once at any moment."},
            {"key": "maxWords", "ask": "Maximum number of words of the briefing", "kind": "number", "default": 250},
            {"key": "language", "ask": "Language of the briefing", "kind": "text", "default": "English"},
            *MESSENGER_FIELDS,
        ],
    },
    "author": {
        "label": "Writer (texts, essays, articles)", "name": "Writer", "role": "writer", "recommend": "writing",
        "build": lambda model, answers: AuthorLoop(model, answers["subject"], answers["length"], loops(answers)),
        "folder": "It can read every file of this folder (your notes, your earlier texts...), and every approved text is saved in its swarmup-results folder.",
        "info": "This agent writes a text on a subject you give, with a maximum length. It reads your earlier texts to match your writing style, and saves the "
                "text only after you approve it.",
        "fields": [{"key": "subject", "ask": "What must the text be about?", "kind": "text", "required": True},
                   {"key": "length", "ask": "Maximum number of words", "kind": "number", "default": 300}],
    },
    "literature": {
        "label": "Literature reviewer", "name": "Reviewer", "role": "literature reviewer", "recommend": "literature", "build": buildLiterature,
        "folder": "It can read every file of this folder, and every approved survey is saved in its swarmup-results folder.",
        "info": "This agent searches papers on a subject, reads their abstracts, and writes a survey that cites each paper with its exact link. The links are checked, "
                "so a paper that was not found cannot be invented. You choose where to search (search engines, and publishers) and you can add publishers by their name.",
        "fields": [
            {"key": "subject", "ask": "On which subject must the survey be?", "kind": "text", "required": True},
            {"key": "length", "ask": "Maximum number of words of the survey", "kind": "number", "default": 500},
            {"key": "searches", "ask": "In which search engines must the papers be searched?", "kind": "choices", "options": list(PAPER_SEARCHES), "default": list(PAPER_SEARCHES),
             "help": "Google Scholar sometimes asks for a captcha. A search that fails is skipped and the others continue."},
            {"key": "publishers", "ask": "Do you want to restrict the survey to some publishers, or add some?", "kind": "publishers", "default": {}},
            {"key": "accounts", "ask": "Do you have an account on a publisher's website that the agent should use?", "kind": "accounts", "default": {},
             "help": "It is only needed for websites that ask for a username and a password (HTTP authentication), and it stays in memory."},
        ],
    },
    "format": {
        "label": "Document formatter", "name": "Formatter", "role": "document formatter", "recommend": "formatting",
        "build": lambda model, answers: DocumentFormatLoop(model, answers["filePath"], answers["style"], loops(answers)),
        "folder": "The document must be inside this folder, and the formatted copy is saved next to it. It can also read the other files of the folder.",
        "info": "This agent changes only the layout of a document (headings, lists, punctuation) in the style you ask. It checks that no word was lost or changed. "
                "Your file is never modified: the result is saved next to it with _formatted in its name, after you approve it.",
        "fields": [{"key": "filePath", "ask": "Path of the document to format", "kind": "file", "required": True},
                   {"key": "style", "ask": "In which style? (for example: markdown with numbered headings, or IEEE)", "kind": "text", "required": True}],
    },
    "math": {
        "label": "Math checker", "name": "MathChecker", "role": "math checker", "recommend": "math",
        "build": lambda model, answers: MathCheckLoop(model, answers["filePath"], loops(answers)),
        "folder": "The text to check must be inside this folder, and the report is saved next to it. It can also read the other files of the folder (the sections, the references...).",
        "info": "This agent referees a research text like a reviewer: it goes through every theorem, lemma and claim, and through the key steps of their proofs. "
                "Each finding quotes the text, and a second check tries to refute the findings that report an error. It checks logic and proofs, not arithmetic. "
                "The report is saved next to your file after you approve it.",
        "fields": [{"key": "filePath", "ask": "Path of the text to check (a text file, like .tex or .md)", "kind": "file", "required": True}],
    },
    "coder": {
        "label": "Coder", "name": "Coder", "role": "coder", "recommend": "code",
        "build": lambda model, answers: CoderLoop(model, answers["task"], answers["filePath"], answers["testCommand"], loops(answers)),
        "folder": "The code file must be inside this folder, and the command that checks the code starts in it. It can read every other file of the folder.",
        "info": "This agent writes the code of one file and checks it by running a command. It writes and RUNS code on your computer, so it asks your permission "
                "before it starts, and again for every draft. If you do not give a command, the file is run with Python.",
        "fields": [{"key": "task", "ask": "What must the code do?", "kind": "text", "required": True},
                   {"key": "filePath", "ask": "In which file must the code be written? (the file is created if it does not exist)", "kind": "path", "required": True},
                   {"key": "testCommand", "ask": "Command that checks the code (leave empty to run the file with Python; example: python -m pytest tests)", "kind": "command", "default": None}],
    },
}

# The task of the leader that builds the swarm itself (leader_utils.py). It is not one of TASKS: nobody chooses it for an agent, the user
# chooses it by letting the leader build the swarm. The mission is the one the user gave, and the folder is the folder of the mission.
LEADER_TASK = {
    "label": "Leader that builds the swarm", "name": "Leader", "role": "leader", "recommend": "leading",
    "build": lambda model, answers: LeaderLoop(model, answers["mission"], loops(answers)),
    "folder": "The folder of the mission: every agent can read all its files, at any depth, and what they make (the final report too) is saved in its swarmup-results folder.",
    "info": "This agent builds the swarm for your mission: it chooses the agents, their tasks and their models, and you approve its proposal. While the swarm works "
            "it can propose to add or remove agents, always with a reason, and nothing changes before you approve. At the end it writes the final report.",
    "fields": [{"key": "mission", "ask": "What must the swarm achieve?", "kind": "text", "required": True}],
}
HIDDEN_TASKS = {"leader": LEADER_TASK}

# The questions every agent has, asked at the end as an option.
ADVANCED_FIELDS = [{"key": "numberOfLoops", "ask": "How many drafts may the agent write at most before it stops?", "kind": "number", "default": DEFAULT_LOOPS,
                    "help": "Every time you ask for a change, or an automatic check finds a problem, the agent writes a new draft."}]


def getTask(key):
    return TASKS[key] if key in TASKS else HIDDEN_TASKS[key]


def getDefault(field, answers):
    default = field.get("default")
    return default(answers) if callable(default) else default


def getHelp(field, answers):
    text = field.get("help", "")
    return text(answers) if callable(text) else text


def isAsked(field, answers):
    condition = field.get("when")
    return condition(answers) if condition else True


def getFields(key):
    return [*getTask(key)["fields"], *ADVANCED_FIELDS]


# The secrets (passwords, tokens) that the user must give again for a task that was saved, only for the questions that were asked.
def secretFields(key, answers):
    return [field for field in getFields(key) if field["kind"] == "secret" and isAsked(field, answers)]


# The answers of a task that can be written to a file: the passwords, tokens and accounts stay in memory.
def publicAnswers(key, answers):
    secrets = {field["key"] for field in getFields(key) if field["kind"] in SECRET_KINDS}
    return {name: value for name, value in answers.items() if name not in secrets}


# The answers of a task that was saved, with the secrets the user gave again. The ones that are missing are empty, so the loop can still be built.
def restoreAnswers(key, saved, secrets=None):
    answers = dict(saved)
    for field in getFields(key):
        if field["kind"] in SECRET_KINDS:
            answers[field["key"]] = (secrets or {}).get(field["key"], {} if field["kind"] == "accounts" else "")
    return answers


# The loop of a task, working inside the folder of the answers (the folder is chosen after the task). It refuses a folder that is not right.
def buildLoop(key, model, answers):
    loop = getTask(key)["build"](model, answers)
    loop.setFolder(answers.get("folder"))
    return loop


def describeLoop(loop):
    return loop.describeTask() + loop.describeFolder()


# The folder to suggest for an agent that works on a file: the folder of the file.
def suggestFolder(answers):
    return str(Path(answers["filePath"]).expanduser().resolve().parent) if answers.get("filePath") else None


# Turns the text typed for a field into its value. It returns (value, error): the error is "" if the text is right, otherwise a sentence for the user.
# An empty text gives the default of the field, and is an error if the field is required and has none.
def parseAnswer(field, text, answers=None):
    answers, text = answers or {}, text.strip() if field["kind"] != "secret" else text
    if not text:
        default = getDefault(field, answers)
        if default is None and field.get("required"):
            return None, "This answer is needed."
        return ("" if default is None and field["kind"] in ("text", "secret", "email") else default), ""
    kind = field["kind"]
    if kind == "email" and not re.fullmatch(EMAIL_PATTERN, text):
        return None, "This is not an email address. It looks like name@example.com."
    if kind == "number":
        if not text.isdigit() or int(text) < 1:
            return None, "Write a whole number above 0."
        return int(text), ""
    if kind == "file":
        path = Path(text).expanduser()
        return (str(path), "") if path.is_file() else (None, f"There is no file at {path}.")
    if kind == "path":
        path = Path(text).expanduser()
        if path.is_dir():
            return None, f"{path} is a folder. Write the path of a file."
        return (str(path), "") if path.resolve().parent.is_dir() else (None, f"The folder {path.resolve().parent} does not exist.")
    if kind == "folder":
        path = Path(text).expanduser()
        return (str(path.resolve()), "") if path.is_dir() else (None, f"There is no folder at {path}.")
    if kind == "phone":
        number = re.sub(r"[\s().-]", "", text)
        number = f"+{number[2:]}" if number.startswith("00") else number
        return (number, "") if re.fullmatch(PHONE_PATTERN, number) else (None, "Write the number with its country code, like +4915112345678.")
    if kind == "time":
        try:
            nextOccurrence(text)
        except ValueError as error:
            return None, str(error)
        return f"{int(text.split(':')[0]):02d}:{int(text.split(':')[1]):02d}", ""
    if kind == "command":
        try:
            return shlex.split(text, posix=os.name != "nt"), ""
        except ValueError as error:
            return None, f"This command cannot be read: {error}"
    return text, ""


# Turns the values of a whole form into the answers of a task, field after field, like the command line asks them. The values are what a form
# sends: a text for the kinds of parseAnswer, an option for choice, a list for choices and outlets, {name: number} for publishers, and rows
# {host, user, password} for accounts. It returns (answers, errors), the errors being {key of the field: what is wrong}.
# A secret left empty keeps the one of previous (the answers given before), so a password never has to be shown again to edit an agent.
# The user interface reads its forms with it, and the leader its proposals (leader_utils.py), so both are checked the same way.
def readAnswers(task, values, previous=None):
    answers, errors = {}, {}
    for field in getFields(task):
        if not isAsked(field, answers):
            continue
        key, kind, raw = field["key"], field["kind"], values.get(field["key"])
        value, error = None, ""
        if kind == "choice":
            value = raw or getDefault(field, answers)
            if value not in field["options"]:
                value, error = None, "Choose one of the options."
        elif kind == "choices":
            value = [option for option in field["options"] if option in (raw if raw is not None else getDefault(field, answers) or [])]
        elif kind == "outlets":
            value = [str(outlet).strip() for outlet in raw or [] if str(outlet).strip()]
            wrong = [outlet for outlet in value if "://" in outlet and not outlet.startswith(("http://", "https://"))]
            error = "Choose at least one outlet." if field.get("required") and not value else f"{wrong[0]} must start with http:// or https://." if wrong else ""
        elif kind == "publishers":
            value = {str(name): str(number) for name, number in (raw or {}).items()}
        elif kind == "accounts":
            value, old = {}, (previous or {}).get(key) or {}
            for row in raw or []:
                host, user, password = (str(row.get(part, "")).strip() for part in ("host", "user", "password"))
                if not host and not user:
                    continue
                password = password or (old.get(host, ("", ""))[1] if old.get(host, ("", ""))[0] == user else "")
                if not host or "/" in host or not user or not password:
                    error = "Every account needs the website (like ieeexplore.ieee.org, without https://), a username and a password."
                value[host] = (user, password)
        else:
            text = "" if raw is None else str(raw)
            if kind == "secret" and not text and previous and previous.get(key):
                value = previous[key]
            else:
                value, error = parseAnswer(field, text, answers)
        if error:
            errors[key] = error
        answers[key] = value
    if task == "literature" and not (answers.get("searches") or answers.get("publishers")) and "searches" not in errors:
        errors["searches"] = "The survey needs a place to search: choose at least a search engine or a publisher."
    return answers, errors


# Which of the options the text chooses: numbers (2), several numbers and ranges (1,3-5), or the label itself.
# It returns (chosen options, error). With one=True a single option is expected.
def parseChoices(options, text, one=False):
    chosen = []
    for part in (piece.strip() for piece in text.split(",") if piece.strip()):
        low, _, high = part.partition("-")
        if low.isdigit() and (not high or high.isdigit()):
            numbers = range(int(low), int(high or low) + 1)
            if not numbers or any(not 1 <= number <= len(options) for number in numbers):
                return [], f"Choose numbers between 1 and {len(options)}."
            chosen += [options[number - 1] for number in numbers]
            continue
        matches = [option for option in options if option.lower() == part.lower()]
        if not matches:
            return [], f"'{part}' is not one of the choices."
        chosen += matches
    chosen = list(dict.fromkeys(chosen))
    if not chosen:
        return [], "Choose at least one."
    if one and len(chosen) > 1:
        return [], "Choose only one."
    return chosen, ""


def checkAgentName(name, taken=()):
    if not AGENT_NAME.fullmatch(name):
        return "A name starts with a letter and has only letters, digits, dots, hyphens and underscores."
    if name.lower() == USER_NAME.lower():
        return f"{USER_NAME} is the name of the user, choose another name."
    if name.lower() in (other.lower() for other in taken):
        return f"There is already an agent called {name}."
    return ""


# A name for a new agent of a task that is not taken: Writer, then Writer2, Writer3...
def suggestName(task, taken=()):
    base, number = getTask(task)["name"], 1
    while checkAgentName(base if number == 1 else f"{base}{number}", taken):
        number += 1
    return base if number == 1 else f"{base}{number}"
