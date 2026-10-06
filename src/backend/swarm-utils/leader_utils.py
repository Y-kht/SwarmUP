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
# 2. LeaderCatalog tells the leader what it can choose from (the tasks and their settings, the models that are ready on this computer,
#    the files of the folder), and checks every proposal: the task, the name, the model (it must fit in the GPUs with the others), who waits
#    for whom, and every setting, with the same checks as the forms of the user (readAnswers in tasks_library.py).
# 3. The user approves every proposal, with the reason the leader gave. Passwords, tokens and API keys are never taken from the leader: SwarmUP
#    asks the user for them, and for the settings the leader could not know (the address of the user...).
# 4. designSwarm builds the swarm with the leader, and LeaderManager follows the swarm while it runs: the leader is asked if a change is needed
#    each time an agent finishes or the user writes to it, and every change goes through the same checks and the approval of the user.
#    The program that runs the swarm (the user interface or the command line) makes the loops of the agents (see LeaderManager).
import ast
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import traceback
from pathlib import Path

import agent_prompts as prompts
from harness_utils import (USER_NAME, ConnectionLost, MessagingError, MissionCosts, Swarm, checkVram, describeCached, findTelegramChats, formatDollars, loadSettings,
                           readGpus, stripFences)
from model_clients import ModelError, checkCodex, findMissingPackages, getApiKey, listCodexModels, readCodexAccount
from models_library import API_KEYS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, getVram, isGated
from sources_library import ALL_NEWS_OUTLETS, NEWS_OUTLETS, PAPER_PUBLISHERS
from tasks_library import DEFAULT_LOOPS, SECRET_KINDS, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getFields, isAsked, publicAnswers, readAnswers

REPAIR_ATTEMPTS = 3
MAX_REVISIONS = 8
MAX_SUPERVISIONS = 12
MAX_SUGGESTIONS = 3
ASK_ATTEMPTS = 3
DEBOUNCE_SECONDS = 1.5
STATUS_SECONDS = 60
FILES_LIMIT = 120
FILES_DEPTH = 3
JSON_LIMIT = 60000
SKIPPED_FOLDERS = {"__pycache__", "node_modules", "venv", "env", "site-packages", "dist", "build"}
SENDER = "SwarmUP"
# Keys a leader may write next to an agent that are not settings: they are left out, unless the task has a setting of that name.
IGNORED_KEYS = ("provider", "company", "vendor", "api", "local", "vram", "cli", "note", "notes", "comment", "comments", "status")


def squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def letters(text):
    return re.sub(r"[^a-z]", "", str(text).lower())


# ==============
# Reading the blocks in what the leader writes.
# ==============
ACTIONS = ("build", "add", "remove", "model")
# The names a block can have, in small letters only: <swarmup_add>, <SwarmUP-Add-Agent>, [add_agent], ```swarmup add... all give swarmupadd or addagent.
# A name that starts with swarmup can only be a block, so one that cannot be read goes back to the leader. The other names (<add>, <model>...)
# can be words of a text, so they only count when what follows them is a block that can be read.
BLOCK_NAMES = {
    "build": ("swarmupbuild", "swarmupbuildswarm", "swarmupswarm", "swarmupteam", "swarmupcreate", "buildswarm", "swarmbuild", "createswarm", "buildteam", "swarm", "build", "team"),
    "add": ("swarmupadd", "swarmupaddagent", "swarmupnewagent", "swarmupjoin", "addagent", "newagent", "createagent", "spawnagent", "add", "join"),
    "remove": ("swarmupremove", "swarmupremoveagent", "swarmupdelete", "swarmupdeleteagent", "swarmupdrop", "removeagent", "deleteagent", "dropagent", "retireagent",
               "remove", "delete", "drop"),
    "model": ("swarmupmodel", "swarmupchangemodel", "swarmupsetmodel", "swarmupswitchmodel", "changemodel", "setmodel", "switchmodel", "replacemodel", "updatemodel",
              "model"),
}
TAG_ACTIONS = {name: action for action, names in BLOCK_NAMES.items() for name in names}
# The keys of a block, and the other words a leader uses for them (in small letters only).
KEY_ALIASES = {
    "name": ("name", "agent", "agentname", "who", "target", "id"),
    "task": ("task", "tasktype", "taskkey", "taskname", "job", "skill"),
    "model": ("model", "modelname", "llm", "engine", "brain"),
    "bits": ("bits", "quantization", "quantisation", "precision"),
    "waits_for": ("waitsfor", "waits", "waitfor", "waitson", "waiton", "dependson", "depends", "dependencies", "after", "needs", "requires", "inputsfrom"),
    "settings": ("settings", "answers", "parameters", "params", "config", "configuration", "fields", "options", "arguments", "args", "inputs"),
    "why": ("why", "reason", "justification", "rationale", "because", "motivation", "explanation", "purpose"),
    "agents": ("agents", "team", "members", "swarm", "workers"),
    "role": ("role", "description", "responsibility", "type", "kind"),
}
KEY_NAMES = {alias: key for key, aliases in KEY_ALIASES.items() for alias in aliases}
# The keys that say which change an object is, when it is not inside a tag: {"action": "remove_agent", "name": ...}.
ACTION_KEYS = ("action", "do", "command", "operation", "op", "change", "type", "kind")
SHAPES = {"build": 'an object with "agents": the list of the agents', "add": 'one object with "name", "task", "model", "waits_for", "settings" and "why"',
          "remove": 'one object with "name" and "why"', "model": 'one object with "name", "model" and "why"'}
TAG_PATTERN = re.compile(r"(?:<<|\[\[|<|\[)[ \t]*(/)?[ \t]*([A-Za-z][A-Za-z0-9 _:.-]{0,40}?)[ \t]*/?(?:>>|\]\]|>|\])")
FENCE_PATTERN = re.compile(r"```[ \t]*([A-Za-z][\w :.-]*)?[ \t]*\r?\n(.*?)(?:\r?\n[ \t]*```|\Z)", re.S)
PAIR_PATTERN = re.compile(r"[ \t]*(?:[-*•][ \t]*)?[\"']?([A-Za-z_][\w \t-]{0,30}?)[\"']?[ \t]*[:=][ \t]*(.*)")
NO_CHANGE_PATTERN = re.compile(r"^\W*no[\s_-]*changes?\b", re.I)
SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "″": '"', "‘": "'", "’": "'", "′": "'"})


def actionOf(word):
    name = letters(word)
    return TAG_ACTIONS.get(name) or TAG_ACTIONS.get(name.removeprefix("swarmup"))


def normalizeKeys(data):
    found, extra = {}, {}
    for key, value in data.items():
        name = KEY_NAMES.get(letters(key))
        if name and name not in found:
            found[name] = value
        else:
            extra[str(key)] = value
    return found, extra


# Where the bracket that starts at start closes, or None. Brackets inside strings do not count.
def findBalanced(text, start):
    stack, quote, escaped = [], None, False
    for index in range(start, min(len(text), start + JSON_LIMIT)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'" and not (char == "'" and index > start and text[index - 1].isalnum()):
            quote = char
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return index + 1
    return None


# Comments (// and /* */) out of JSON, except inside strings, where // is often part of an address.
def removeComments(text):
    result, index, quote = [], 0, None
    while index < len(text):
        char = text[index]
        if quote:
            result.append(char)
            if char == "\\" and index + 1 < len(text):
                result.append(text[index + 1])
                index += 1
            elif char == quote:
                quote = None
        elif char == '"' or (char == "'" and not (index and text[index - 1].isalnum())):
            quote = char
            result.append(char)
        elif text.startswith("//", index) or text.startswith("#", index):
            index = text.find("\n", index)
            index = len(text) if index < 0 else index
            continue
        elif text.startswith("/*", index):
            index = text.find("*/", index + 2)
            index = len(text) if index < 0 else index + 2
            continue
        else:
            result.append(char)
        index += 1
    return "".join(result)


# JSON as a model writes it: with comments, curly quotes, trailing commas, keys without quotes, single quotes, True and None, or without
# its outer braces. It returns the value, or None if nothing can be read.
def loadJson(text):
    text = stripFences(str(text))[:JSON_LIMIT].strip()
    if not text:
        return None
    fixed = removeComments(text.translate(SMART_QUOTES)).strip()
    noCommas = re.sub(r",\s*([}\]])", r"\1", fixed)
    attempts = [text, fixed, noCommas, re.sub(r"([{,]\s*)([A-Za-z_][\w-]*)(\s*:)", r'\1"\2"\3', noCommas)]
    if noCommas[:1] not in ("{", "[") and re.match(r"[\"']?[A-Za-z_][\w -]*[\"']?\s*:", noCommas):
        attempts.append("{" + noCommas + "}")
    for attempt in attempts:
        try:
            return json.loads(attempt)
        except ValueError:
            pass
    for attempt in attempts:
        python = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", attempt)))
        try:
            value = ast.literal_eval(python)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        if isinstance(value, (dict, list)):
            return value
    return None


def readValue(text):
    text = text.strip().rstrip(",").strip()
    if text[:1] in ("[", "{") or text in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", text):
        value = loadJson(text)
        if value is not None:
            return value
    return text[1:-1] if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'" else text


# "key: value" lines, one after the other, until an empty line or a line that is not one. A key that comes again starts another object.
# It returns (an object, or a list of them, or None), and how much of the text was read.
def readPairs(text):
    items, current, used = [], {}, 0
    for line in text.splitlines(keepends=True):
        if not line.strip():
            if current:
                break
            used += len(line)
            continue
        found = PAIR_PATTERN.fullmatch(line.rstrip("\r\n"))
        if not found or letters(found.group(1)) in ("http", "https"):
            break
        key = found.group(1).strip()
        if key in current:
            items.append(current)
            current = {}
        current[key] = readValue(found.group(2))
        used += len(line)
    if current:
        items.append(current)
    return (items[0] if len(items) == 1 else items) if items else None, used


# What follows an opening tag: JSON (maybe in a code fence) or "key: value" lines. It returns (the value, how much of the text it took), or (None, None).
def readContent(content):
    opening = re.match(r"\s*(?:```[^\n]*\n)?", content)
    start, fenced = opening.end(), "```" in opening.group(0)
    body = content[start:]
    closing = re.search(r"\n?[ \t]*```", body) if fenced else None
    if closing:
        body = body[:closing.start()]
    after = start + closing.end() if closing else None
    stripped = body.lstrip()
    lead = len(body) - len(stripped)
    if stripped[:1] in ("{", "["):
        end = findBalanced(stripped, 0)
        value = loadJson(stripped[:end] if end else stripped)
        return (value, after or start + lead + (end or len(stripped))) if value is not None else (None, None)
    if stripped[:1] in ("\"", "'"):
        paragraph = re.split(r"\n\s*\n", stripped, maxsplit=1)[0]
        value = loadJson(paragraph)
        if isinstance(value, dict):
            return value, after or start + lead + len(paragraph)
    pairs, used = readPairs(stripped)
    return (pairs, after or start + lead + used) if pairs else (None, None)


# The changes an object holds for an action: a list of objects, or None if it does not have the shape of that action.
def readShape(action, data):
    if action == "build":
        agents = data if isinstance(data, list) else normalizeKeys(data)[0].get("agents") if isinstance(data, dict) else None
        if isinstance(agents, dict):
            agents = [{"name": name, **agent} if isinstance(agent, dict) else None for name, agent in agents.items()]
        return [{"agents": agents}] if isinstance(agents, list) and agents and all(isinstance(agent, dict) for agent in agents) else None
    if isinstance(data, list):
        items = [readShape(action, item) for item in data]
        return [one for item in items for one in item] if items and all(items) else None
    if not isinstance(data, dict):
        return None
    keys = normalizeKeys(data)[0]
    if action == "add":
        if isinstance(keys.get("agents"), list):
            return readShape("add", keys["agents"])
        return [data] if any(key in keys for key in ("task", "model", "role")) else None
    names = keys.get("name")
    if action == "remove":
        if isinstance(names, list) and names and all(isinstance(name, str) for name in names):
            return [{"name": name, "why": keys.get("why", "")} for name in names]
        return [data] if isinstance(names, str) and names.strip() else None
    return [data] if isinstance(names, str) and names.strip() and keys.get("model") else None


# Which change a value is: (action, [objects]). hint is the action of the tag around it. An action written in the object wins.
def interpret(value, hint=None):
    if isinstance(value, dict):
        for key, word in value.items():
            if letters(key) in ACTION_KEYS and isinstance(word, str) and actionOf(word):
                action = actionOf(word)
                return action, readShape(action, {other: item for other, item in value.items() if other != key})
    if hint:
        return hint, readShape(hint, value)
    if isinstance(value, dict) and "agents" in normalizeKeys(value)[0]:
        return "build", readShape("build", value)
    if isinstance(value, list) and value and all(isinstance(item, dict) and "task" in normalizeKeys(item)[0] for item in value):
        return "build", readShape("build", value)
    return None, None


def findTags(text):
    tags = []
    for found in TAG_PATTERN.finditer(text):
        name = letters(found.group(2))
        closing = bool(found.group(1))
        if not closing and name.startswith("end") and name[3:] in TAG_ACTIONS:
            name, closing = name[3:], True
        if name in TAG_ACTIONS or (closing and name == "swarmup"):
            tags.append({"start": found.start(), "end": found.end(), "name": name, "closing": closing})
    return tags


def overlaps(spans, start, end):
    return any(start < spanEnd and spanStart < end for spanStart, spanEnd in spans)


# Everything the leader wrote, read: {"blocks": [{"action", "data", "raw"}], "problems": [{"action", "raw", "error"}], "text": the text
# without the blocks, for the user}. The blocks are found in tags first, then in code fences, then as JSON alone in the text.
def parseOutput(text):
    text = str(text or "")
    blocks, problems, spans = [], [], []
    tags = findTags(text)
    for index, tag in enumerate(tags):
        if tag["closing"] or overlaps(spans, tag["start"], tag["end"]):
            continue
        following = tags[index + 1] if index + 1 < len(tags) else None
        closed = following is not None and following["closing"]
        content = text[tag["end"]:following["start"] if following else len(text)]
        value, used = readContent(content)
        action, items = interpret(value, TAG_ACTIONS[tag["name"]]) if value is not None else (TAG_ACTIONS[tag["name"]], None)
        strict = tag["name"].startswith("swarmup")
        if not items and not strict:
            continue
        end = following["end"] if closed else tag["end"] + (used or (len(content) if not items else 0))
        if not closed and not items:
            blank = re.search(r"\n\s*\n", content)
            end = tag["end"] + (blank.start() if blank else len(content))
        raw = text[tag["start"]:end].strip()
        spans.append((tag["start"], end))
        if items:
            blocks += [{"action": action, "data": item, "raw": raw} for item in items]
        else:
            error = f"This <swarmup_{action}> block does not hold {SHAPES[action]}." if value is not None else \
                f"What is inside this <swarmup_{action}> block cannot be read as JSON. Write {SHAPES[action]}, in strict JSON."
            problems.append({"action": action, "raw": raw, "error": error})
    for found in FENCE_PATTERN.finditer(text):
        if overlaps(spans, found.start(), found.end()):
            continue
        language = letters(found.group(1) or "")
        hint = actionOf(language) if language not in ("json", "javascript", "js", "yaml", "text", "python") else None
        value = loadJson(found.group(2))
        if value is None and hint:
            value = readPairs(found.group(2).strip())[0]
        action, items = interpret(value, hint) if value is not None else (hint, None)
        if items:
            blocks += [{"action": action, "data": item, "raw": found.group(0)} for item in items]
            spans.append((found.start(), found.end()))
        elif hint and language.startswith("swarmup"):
            problems.append({"action": hint, "raw": found.group(0), "error": f"This <swarmup_{hint}> block does not hold {SHAPES[hint]}."})
            spans.append((found.start(), found.end()))
    index = 0
    while True:
        starts = [position for position in (text.find("{", index), text.find("[", index)) if position >= 0]
        if not starts:
            break
        start = min(starts)
        end = None if overlaps(spans, start, start + 1) else findBalanced(text, start)
        value = loadJson(text[start:end]) if end else None
        action, items = interpret(value) if value is not None else (None, None)
        if items:
            blocks += [{"action": action, "data": item, "raw": text[start:end]} for item in items]
            spans.append((start, end))
            index = end
        else:
            index = start + 1
    cleaned, last = "", 0
    for start, end in sorted(spans) + [(len(text), len(text))]:
        piece = text[last:start] if start > last else ""
        cleaned = f"{cleaned.rstrip(' ')} {piece.lstrip(' ')}" if cleaned.endswith(" ") and piece.startswith(" ") else cleaned + piece
        last = max(last, end)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return {"blocks": blocks, "problems": problems, "text": cleaned}


def isNoChange(text):
    return bool(NO_CHANGE_PATTERN.match(str(text or "").strip()))


# ==============
# Sentences that suggest a change without a block.
# ==============
PROPOSAL_PATTERN = re.compile(
    r"\b(?:i(?: would|'d)? (?:propose|suggest|recommend|advise)|(?:we|you) (?:should|could|might want to|may want to|need to|must|ought to)|let'?s|let us|"
    r"i(?: will|'ll| want to| plan to| intend to| am going to|'m going to| would like to|'d like to| can)|it (?:would|might|could) (?:be (?:better|wise|good|useful|best)|help)|"
    r"(?:should|could|can|must|may|might) (?:now |then )?be (?:removed|deleted|dropped|retired|added|replaced|switched|changed|upgraded|downgraded|let go)|"
    r"(?:is|are|seems|looks) (?:now )?(?:no longer|not) (?:needed|necessary|required|useful)|(?:is|are) (?:now )?redundant|"
    r"(?:consider|recommend|suggest|propose)(?:s|ed|ing)? (?:adding|removing|deleting|dropping|replacing|switching|changing|upgrading|downgrading|retiring)|"
    r"would benefit from|needs? (?:an?|another|one more) (?:new |extra |additional |second )?(?:agent|helper)|"
    r"i(?: have| just)? (?:added|removed|deleted|dropped|replaced|switched|changed|retired))\b", re.I)
NEGATION_PATTERN = re.compile(r"\b(?:not|no need|never|don'?t|doesn'?t|shouldn'?t|won'?t|wouldn'?t|cannot|can'?t|no reason|without|instead of|rather than|nor)\b", re.I)
REMOVE_VERBS = r"(?:remov|delet|drop|retir|dismiss|unload|discard|eliminat|kick(?:ing|ed)? out|tak(?:e|ing) out|let(?:ting)? go of|get(?:ting)? rid of|shut(?:ting)? down)\w*"
MODEL_VERBS = r"(?:switch|chang|replac|swap|mov|upgrad|downgrad|us|giv|assign|run)\w*"
ADD_VERBS = r"(?:add|adding|create|creating|bring(?:ing)? in|recruit\w*|hire|hiring|introduc\w*|spawn\w*|include|including|set(?:ting)? up|onboard\w*)"
AGENT_NOUNS = (r"(?:agents?|helpers?|assistants?|specialists?|workers?|writers?|translators?|editors?|reviewers?|checkers?|fact-?checkers?|coders?|programmers?|developers?|"
               r"formatters?|planners?|briefers?|emailers?|mailers?|researchers?|summari[sz]ers?|analysts?|testers?|proofreaders?)")
SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+|\n+")


# Whether a negation comes in the few words before position (like "do not remove", "no need to add").
def negatedBefore(sentence, position):
    return bool(NEGATION_PATTERN.search(" ".join(sentence[:position].split()[-4:])))


def findSuggestions(text, names, leader=None):
    text = str(text or "")
    if not text.strip() or isNoChange(text) or loadJson(stripFences(text)) is not None:
        return []
    others = [name for name in names if name != leader]
    known = [model.lower() for models in MODELS_API.values() for model in models] + [name.lower() for family in MODELS_LOCAL.values() for name in family] + ["claude-code", "codex"]
    found = []
    for sentence in (part.strip() for part in SENTENCE_PATTERN.split(text)):
        if len(sentence) < 8 or not PROPOSAL_PATTERN.search(sentence):
            continue
        low = sentence.lower()
        suggestion = None
        for name in others:
            word = re.escape(name)
            removing = re.search(rf"\b{REMOVE_VERBS}\b(?:\W+\w+){{0,4}}?\W+{word}\b(?!['’]s)", sentence, re.I)
            state = re.search(rf"\b{word}\b(?:\W+\w+){{0,3}}?\W+(?:is|are|seems|looks)\W+(?:now\W+)?(?:(?:no longer|not)\W+(?:needed|necessary|required|useful)|redundant)|"
                              rf"\b{word}\b\W+(?:can|could|should|may)\W+(?:now\W+)?(?:go|leave|be (?:removed|deleted|dropped|retired|let go))\b", sentence, re.I)
            if (removing and not negatedBefore(sentence, removing.start())) or (state and not negatedBefore(sentence, state.start())):
                suggestion = {"action": "remove", "agent": name, "sentence": sentence}
                break
            changing = re.search(rf"\b{MODEL_VERBS}\b", sentence, re.I)
            if re.search(rf"\b{word}\b", sentence, re.I) and changing and not negatedBefore(sentence, changing.start()) and \
                    (re.search(r"\bmodels?\b", low) or any(model in low for model in known)):
                suggestion = {"action": "model", "agent": name, "sentence": sentence}
                break
        adding = re.search(rf"\b{ADD_VERBS}\b(?:\W+\w+){{0,4}}?\W+{AGENT_NOUNS}\b(?!['’]s)", sentence, re.I) or \
            re.search(rf"\b(?:would benefit from|needs?|is missing|lacks|could use|requires)\W+(?:an?|another|one more|a new|an extra|a second)\W+(?:[\w-]+\W+){{0,2}}?{AGENT_NOUNS}\b", sentence, re.I)
        if suggestion is None and adding and not negatedBefore(sentence, adding.start()):
            suggestion = {"action": "add", "agent": None, "sentence": sentence}
        if suggestion and all(item["sentence"] != sentence for item in found):
            found.append(suggestion)
    return found[:MAX_SUGGESTIONS]


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
KIND_WORDS = {"text": "text", "email": "an email address", "number": "a whole number", "file": "the path of a file that exists in the folder",
              "path": "the path of a file in the folder, created if it does not exist", "folder": "a folder", "phone": "a phone number with its country code, like +4915112345678",
              "time": "a time of the day HH:MM", "command": "a command line, run in the folder", "outlets": "a list of news outlets: names of the list of outlets below, or addresses of RSS feeds",
              "publishers": "a list of publishers: names of the list of publishers below", "secret": "a secret: never write it, the user gives it",
              "accounts": "accounts of the user: never write them, the user gives them"}


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


def describeFiles(folder):
    if folder is None:
        return "No folder."
    lines, base = [], len(folder.parts)
    for root, folders, files in os.walk(folder):
        depth = len(Path(root).parts) - base
        folders[:] = sorted(name for name in folders if not name.startswith(".") and name not in SKIPPED_FOLDERS and depth + 1 < FILES_DEPTH)
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            lines.append(f"- {path.relative_to(folder).as_posix()} ({size:,} bytes)")
            if len(lines) >= FILES_LIMIT:
                return "\n".join(lines + ["- ... (more files are not listed)"])
    return "\n".join(lines) or "The folder is empty."


# Everything the leader is told and checked against. keys and tokens are those of the program that runs the swarm (the API keys and
# Hugging Face tokens the user gave): the ones the user gives for an agent of the leader go in them too, and stay in memory only.
# costs is what the mission spent, with its budget (MissionCosts in harness_utils.py), and maxAgents the most agents the leader may put in
# the swarm (the settings of the user). The local models that do not fit in the VRAM left, and the API models whose price of 1 million tokens
# is more than the budget left, are not offered to the leader, and refused if it chooses them anyway.
class LeaderCatalog:
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
        return prompts.LEADER_BUILD_PROMPT.format(rules=self.rules(), mission=self.mission, folder=self.describeFolder(), files=describeFiles(self.folder),
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
# The leader writes its swarm, SwarmUP checks it and sends it back with what is wrong, then the user approves it, rejects it, or says
# what to change, and the leader writes it again. It returns the agents (with every answer the user gave), or None if the user rejected it.
# ==============
def designSwarm(leader, catalog):
    base = catalog.buildPrompt()
    prompt = base
    for revision in range(MAX_REVISIONS):
        agents, problems, previous, note = None, [], "", ""
        for attempt in range(REPAIR_ATTEMPTS + 1):
            reply = leader.askAgent(prompt, own=False)
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


# ==============
# The leader manages the swarm while it runs. The manager is the listener of the swarm, and it reads every answer of the leader (readOutput,
# called by the loop of the leader): the blocks it finds are proposed to the user, the sentences that suggest a change are sent back to the
# leader to confirm, and the leader is asked if a change is needed when an agent finishes or the user writes to it.
# The work is done in a thread of its own, so the swarm never waits for the manager.
# maker is the program that runs the swarm (the user interface or the command line). It makes the loops of the agents:
# makeLoop(agent) gives the loop of a new agent, from agent["name"], ["task"], ["answers"] and ["model"], with the keys of the catalog;
# joined(agent, loop) is told once that agent is in the swarm; remakeLoop(name, model) gives the loop of an agent with another model,
# and remade(name, model, loop) is told once the swarm uses it.
# ==============
class LeaderManager:
    def __init__(self, swarm, catalog, maker):
        self.swarm, self.catalog, self.maker = swarm, catalog, maker
        self.queue = queue.Queue()
        self.thread = None
        self.rounds = 0
        self.rejected = {}
        self.decisions = []
        swarm.manager = self
        swarm.addListener(self.onEvent)

    # Called by the swarm, maybe with its lock held: it only takes note.
    def onEvent(self, event):
        kind, agent = event["kind"], event["agent"]
        if kind == "run":
            self.start()
        elif kind == "finished":
            self.queue.put(("end", None))
        elif kind == "status" and event["status"] in ("done", "failed") and agent != self.swarm.getLeader() and self.othersAtWork(agent):
            self.queue.put(("event", f"{agent} {'is done' if event['status'] == 'done' else 'did not finish'}."))
        elif kind == "message" and event["sender"] == USER_NAME and event["receiver"] == self.swarm.getLeader():
            self.queue.put(("event", f"The user wrote to you: {event['message']}"))

    # When the last agent finishes, the leader starts its own work at once: nothing is left to change, so the leader is not asked (it costs tokens).
    def othersAtWork(self, agent):
        return any(member["status"] not in ("done", "failed") for name, member in list(self.swarm.members.items()) if name not in (agent, self.swarm.getLeader()))

    def start(self):
        self.queue, self.rounds = queue.Queue(), 0
        self.thread = threading.Thread(target=self.work, args=(self.queue,), daemon=True, name="leader-manager")
        self.thread.start()

    # Every answer of the leader comes here (Loop.onAnswer). The blocks are taken out of the text, which goes on to the user without them.
    # What the manager asks the leader itself is read by the manager, so it is given back as it is.
    def readOutput(self, text):
        if threading.current_thread() is self.thread:
            return text
        found = parseOutput(text)
        suggestions = [] if found["blocks"] or found["problems"] else findSuggestions(found["text"], self.swarm.getAgents(), self.swarm.getLeader())
        if found["blocks"] or found["problems"] or suggestions:
            self.queue.put(("output", {**found, "suggestions": suggestions}))
        return found["text"]

    def work(self, items):
        while True:
            kind, data = items.get()
            batch = [(kind, data)]
            if kind != "end":
                time.sleep(DEBOUNCE_SECONDS)
                while not items.empty():
                    batch.append(items.get_nowait())
            ending = any(kind == "end" for kind, _ in batch)
            try:
                self.handle(batch, ending)
            except Exception:
                traceback.print_exc()
            if ending:
                return

    def isOpen(self):
        return self.swarm.active and not self.swarm.stopped

    def handle(self, batch, ending):
        for kind, data in batch:
            if kind == "output" and self.isOpen():
                self.act(data)
        events = [data for kind, data in batch if kind == "event"]
        if events and not ending:
            self.supervise(events)

    def leaderLoop(self):
        return self.swarm.getMember(self.swarm.getLeader())["agent"]

    def tell(self, text):
        self.leaderLoop().receive(SENDER, text)

    def ask(self, prompt):
        try:
            return self.leaderLoop().askAgent(prompt, own=False)
        except Exception as error:
            self.leaderLoop().notifyUser(f"The leader could not be asked about the swarm: {error}")
            return None

    # The blocks of an answer of the leader are proposed one after the other. What cannot be used is sent back to the leader once; a sentence
    # that suggests a change, without any block, is sent back to be confirmed.
    def act(self, found, again=True):
        problems = [problem["error"] for problem in found["problems"]]
        for block in found["blocks"]:
            problems += [f"{error} (in: {block['raw'][:300]})" for error in self.propose(block)]
        if problems and again:
            reply = self.ask(self.catalog.repairPrompt(problems, "\n".join(block["raw"] for block in found["blocks"] + found["problems"]) or found["text"], "the right blocks"))
            if reply is not None:
                self.act(parseOutput(reply), again=False)
        elif problems:
            self.tell("SwarmUP could not use your proposal, so the user was not asked: " + " ".join(problems))
        elif not found["blocks"] and found.get("suggestions") and again:
            reply = self.ask(self.catalog.confirmPrompt(self.swarm, [item["sentence"] for item in found["suggestions"]]))
            if reply is not None and not isNoChange(reply):
                self.act({**parseOutput(reply), "suggestions": []}, again=False)

    def supervise(self, events):
        stage = self.swarm.stage
        leaderWorks = bool(stage and not stage["plan"] and self.swarm.getLeader() in stage["names"])
        if self.rounds >= MAX_SUPERVISIONS or not self.isOpen() or self.swarm.interruption or leaderWorks:
            return
        self.rounds += 1
        reply = self.ask(self.catalog.supervisePrompt(self.swarm, events, "\n".join(f"- {decision}" for decision in self.decisions[-12:]) or "Nothing yet."))
        if reply is None:
            return
        found = parseOutput(reply)
        if found["blocks"] or found["problems"]:
            self.act({**found, "suggestions": []})
        elif not isNoChange(found["text"]):
            self.act({**found, "suggestions": findSuggestions(found["text"], self.swarm.getAgents(), self.swarm.getLeader())})

    # One change of the leader: checked, shown to the user with the reason of the leader, and made if the user approves it. It returns what
    # the leader must correct (the change was not shown to the user then), or [].
    def propose(self, block):
        if block["action"] == "build":
            known = {squash(name) for name in [*self.swarm.getAgents(), *(item["name"] for item in self.swarm.removed)]}
            if all(squash(normalizeKeys(agent)[0].get("name")) in known for agent in block["data"]["agents"]):
                return []
            return ["The swarm is already built: propose each new agent in its own <swarmup_add> block, and each agent to take out in a <swarmup_remove> block."]
        change, errors = self.catalog.checkChange(block["action"], block["data"], self.swarm)
        if errors:
            return errors
        if change["signature"] in self.rejected:
            self.tell(f"You proposed again what the user already rejected, so it was not shown to the user: {change['text']}.")
            return []
        leader = self.leaderLoop()
        answer = leader.askProposal(change["proposal"])
        if not self.isOpen():
            return []
        if not isinstance(answer, dict) or answer.get("decision") != "approve":
            message = str(answer.get("message") or "").strip() if isinstance(answer, dict) else ""
            self.rejected[change["signature"]] = message
            self.decisions.append(f"Rejected: {change['text']}." + (f" The user said: {message}" if message else ""))
            self.tell(f"The user rejected your proposal: {change['text']}." + (f" The user said: {message}" if message else ""))
            return []
        try:
            self.apply(change, leader)
        except (ValueError, ModelError, OSError) as error:
            leader.notifyUser(f"The change you approved could not be made: {error}")
            self.decisions.append(f"Approved, but it could not be made: {change['text']}. Why: {error}")
            self.tell(f"The user approved {change['text']}, but it could not be made: {error}")
            return []
        self.decisions.append(f"Approved and done: {change['text']}.")
        self.tell(f"The user approved your proposal, and it is done: {change['text']}.")
        return []

    def apply(self, change, leader):
        action = change["action"]
        if action == "remove":
            self.swarm.removeAgent(change["name"], change["why"] or f"Proposed by {self.swarm.getLeader()}.")
            return
        if action == "model":
            if not askMissing([{"name": change["name"], "needs": [], "key": change["key"], "token": change["token"], "task": None}], self.catalog, leader):
                raise ValueError(f"the API key for {change['name']} was not given")
            loop = self.maker.remakeLoop(change["name"], change["model"])
            try:
                self.swarm.setModel(change["name"], change["model"], loop)
            except ValueError:
                if hasattr(loop.agent, "unload"):
                    loop.agent.unload()
                raise
            self.maker.remade(change["name"], change["model"], loop)
            return
        agent = change["agent"]
        if not askMissing([agent], self.catalog, leader):
            raise ValueError(f"{agent['name']} still misses what only you can give")
        agent["answers"]["folder"] = str(self.catalog.folder) if self.catalog.folder else None
        loop = self.maker.makeLoop(agent)
        recipe = {"task": agent["task"], "answers": publicAnswers(agent["task"], agent["answers"])}
        try:
            self.swarm.addAgent(agent["name"], loop, TASKS[agent["task"]]["role"], describeLoop(loop), waitsFor=agent["waitsFor"], model=agent["model"], recipe=recipe)
        except ValueError:
            if hasattr(loop.agent, "unload"):
                loop.agent.unload()
            raise
        self.maker.joined(agent, loop)
