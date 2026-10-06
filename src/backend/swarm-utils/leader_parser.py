# Reading what the leader writes: the blocks of its proposals, even when they are not written as asked, and the sentences
# that suggest a change (see leader_utils.py).
import ast
import json
import re

from harness_utils import stripFences
from models_library import MODELS_API, MODELS_LOCAL


MAX_SUGGESTIONS = 3

JSON_LIMIT = 60000


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
