# Reading the blocks a leader writes for a change of its swarm, even when they are not written as asked: the adapter of a model that writes
# its calls as text (model_messages.py) reads them as calls of the leader tools (server_leader.py).
import ast
import json
import re

from harness_utils import stripFences


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
# The calls of tools a model writes as blocks (docs/MCP_DESIGN_BOOKLET.md, ADP-04). A model that cannot call tools natively, or a small one,
# often writes the change of the swarm as a block instead of a call: the adapter of the model reads it here as the call of the leader tool it
# means, with its arguments, and a block that cannot be read is a call with its problem. Nothing outside the adapters reads such blocks.
# ==============
BLOCK_TOOLS = {"build": "leader__propose_swarm", "add": "leader__add_agent", "remove": "leader__remove_agent", "model": "leader__change_model"}
AGENT_KEYS = ("name", "task", "model", "bits", "waits_for", "settings", "why", "role")


def cleanAgent(data):
    keys = normalizeKeys(data)[0] if isinstance(data, dict) else {}
    agent = {key: keys[key] for key in AGENT_KEYS if key in keys and keys[key] is not None}
    if str(agent.get("bits", "")).strip().isdigit():
        agent["bits"] = int(str(agent["bits"]).strip())
    if isinstance(agent.get("waits_for"), str):
        agent["waits_for"] = [name.strip() for name in agent["waits_for"].split(",") if name.strip()]
    return agent


def readBlockCalls(text, newId):
    found = parseOutput(text)
    calls = []
    for block in found["blocks"]:
        data = block["data"]
        arguments = {"agents": [cleanAgent(agent) for agent in data.get("agents") or []]} if block["action"] == "build" else cleanAgent(data)
        # The words around a swarm are the reason the leader gives for it.
        if block["action"] == "build" and found["text"].strip():
            arguments["why"] = found["text"].strip()
        calls.append({"id": newId(), "name": BLOCK_TOOLS[block["action"]], "arguments": arguments})
    calls += [{"id": newId(), "name": BLOCK_TOOLS[problem["action"]], "arguments": {}, "problem": problem["error"]} for problem in found["problems"]]
    return calls, found["text"]
