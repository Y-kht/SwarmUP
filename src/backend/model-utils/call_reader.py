# The calls of tools a model wrote in its text (ADP-04). A model is asked for <tool_call>{"name": ..., "arguments": {...}}</tool_call>, and
# most models write that. But each family of models learned its own way (a list of calls, the whole answer as JSON, <function=name>{...},
# name(key=value), Action: name ...), and a small model often gets the JSON almost right. Whatever was written, one of three things happens:
# - it is a call that can be read: it is given as {"id", "name", "arguments"}, the arguments being an object;
# - it is a call that cannot be read: it is given with a "problem", which goes back to the model in words that say what to write instead;
# - it is not a call: it stays in the text of the answer.
# Nothing here raises, and nothing is run from what a model wrote. The gateway (mcp_gateway.py) checks the name and the arguments afterwards.
import ast
import json
import re
import uuid

from loose_json import FENCE, READ_ERRORS, decodeAt, literalOf, parseJson, parsePython, readLoose, readValues, unfence


TAG_WORDS = r"(?:tool[_ -]?calls?|function[_ -]?calls?|tool[_ -]?use)"
OPEN_TAGS = rf"<\|?\s*{TAG_WORDS}\s*\|?>|\[TOOL_CALLS\]"
CLOSE_TAGS = rf"<\|?\s*/\s*{TAG_WORDS}\s*\|?>|\[/TOOL_CALLS\]"
OPEN_TAG = re.compile(OPEN_TAGS, re.IGNORECASE)
CLOSE_TAG = re.compile(CLOSE_TAGS, re.IGNORECASE)
BODY_END = re.compile(rf"(?P<close>{CLOSE_TAGS})|(?P<open>{OPEN_TAGS})", re.IGNORECASE)
FENCE_START = re.compile(r"\s*```[ \t]*[\w+.-]*[ \t]*\r?\n?")
AFTER_VALUES = re.compile(r"[\s}\];,]*(?:```)?\s*")
THINKING = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.DOTALL | re.IGNORECASE)
FUNCTION_TAG = re.compile(r"<function\s*=\s*[\"']?([\w.:-]+)[\"']?\s*>(.*?)(?:</function>|\Z)", re.DOTALL | re.IGNORECASE)
INVOKE_TAG = re.compile(r"<invoke\s+name\s*=\s*[\"']?([\w.:-]+)[\"']?\s*>(.*?)(?:</invoke>|\Z)", re.DOTALL | re.IGNORECASE)
PARAMETER_START = re.compile(r"<parameter\s+name\s*=\s*[\"']?([\w.:-]+)[\"']?\s*>", re.IGNORECASE)
PARAMETER_END = re.compile(r"</parameter\s*>", re.IGNORECASE)
INVOKE_WRAP = re.compile(r"</?(?:function_calls|invoke)\s*>", re.IGNORECASE)
STARTED_CALL = re.compile(rf"\s*(?:{OPEN_TAGS}|<function\s*=|<invoke\s|(?:```(?:json)?\s*)?\[?\s*\{{\s*\"name\")", re.IGNORECASE)
CALL_NAME = re.compile(r"\"name\"\s*:\s*\"([^\"]+)\"")
NAME_FIRST = re.compile(r"\s*([A-Za-z_][\w.:-]*)\s*[:=(]?\s*(?=\{|$)")
LOOSE_START = re.compile(r"\s*\{\s*\"name\"\s*:\s*\"([^\"]+)\"\s*,\s*\"(?:arguments|parameters|args|input)\"\s*:\s*\{")
LOOSE_KEY = re.compile(r"\s*,?\s*\"([A-Za-z_]\w*)\"\s*:\s*")
LOOSE_TEXT_END = re.compile(r"(?<!\\)\"(?=\s*(?:,\s*\"[A-Za-z_]\w*\"\s*:|$))")
# A quote that nothing escapes (an even number of backslashes before it), and a backslash that starts no escape of JSON.
LOOSE_QUOTE = re.compile(r"(?<!\\)((?:\\\\)*)\"")
BAD_ESCAPE = re.compile(r"\\(?![\"\\/bfnrtu])")
# The words the models use for the name of the tool and for its arguments, the asked ones first.
NAME_KEYS = ("name", "tool", "tool_name", "toolName", "function", "function_name", "action")
ARGUMENT_KEYS = ("arguments", "parameters", "args", "input", "params", "kwargs", "tool_input", "action_input", "inputs")
INNER_KEYS = ("function", "function_call", "tool_call", "tool_use")
HOLDER_KEYS = ("tool_calls", "function_calls", "calls")
CALL_TYPES = ("function", "tool_call", "tool_use", "function_call")
NAME_CHARS = 2000
# An answer with this many calls that cannot be read is not read further: the model is told about these, and writes its calls again.
BAD_LIMIT = 8
# The places of an answer without tags where a call may start are tried this many times at most: each try reads the text from there.
TRIES = 200
BAD_JSON = "this tool call is not valid JSON ({error}). Write it again, with every quote and backslash of a text escaped."
NO_NAME = 'this tool call does not name a tool. Write it as {"name": "<the tool>", "arguments": {...}}.'
BAD_ARGUMENTS = 'the arguments of {name} must be a JSON object like {{"key": value}}, not {kind}. Write the call again.'
BAD_API_JSON = "its arguments were not valid JSON. Write them again."
KINDS = {str: "a text", list: "a list", int: "a number", float: "a number", bool: "true or false"}


def newCallId():
    return f"call_{uuid.uuid4().hex[:12]}"


def describeJsonError(error):
    return f"{error.msg} at character {error.pos}" if isinstance(error, json.JSONDecodeError) else str(error)


# The tools a model was given, as {name: the properties of its arguments}. None when nobody said which tools it has.
def knownTools(tools):
    if tools is None:
        return None
    return {str(tool.get("name")): ((tool.get("parameters") or {}).get("properties") or {}) for tool in tools if isinstance(tool, dict)}


# A call that cannot be run, with the name of its tool when it can be seen, so the model is told about the call it meant.
def badCall(body, problem, names=None, name=None):
    seen = [name] if name else [found for found in CALL_NAME.findall(str(body)[:NAME_CHARS]) if names is None or found in names]
    return {"id": newCallId(), "name": seen[0] if seen else "unknown", "arguments": {}, "text": str(body), "problem": problem}


# The arguments of a call when they are an object: as such, as JSON in a text (some models write them twice encoded), or nothing at all.
def argumentsOf(value):
    if value is None or isinstance(value, dict):
        return value or {}
    if not isinstance(value, str):
        return None
    if not value.strip():
        return {}
    try:
        read = readLoose(value)
    except ValueError:
        return None
    return read if isinstance(read, dict) else None


# (the name, the arguments) of a call written as an object, in the shapes the models use: {"name", "arguments"}, {"tool", "parameters"},
# {"function": {"name", "arguments"}}, {"<the tool>": {...}}, or the arguments beside the name. ValueError says what is wrong with it.
def shapeOf(data, names):
    if not isinstance(data, dict):
        raise ValueError(NO_NAME)
    data = next((data[key] for key in INNER_KEYS if isinstance(data.get(key), dict)), data)
    written = [key for key in NAME_KEYS if isinstance(data.get(key), str) and data[key].strip()]
    key = next((key for key in written if names and data[key].strip() in names), written[0] if written else None)
    if key is None:
        only = next(iter(data.items())) if len(data) == 1 else (None, None)
        if names and only[0] in names and argumentsOf(only[1]) is not None:
            return only[0], argumentsOf(only[1])
        raise ValueError(NO_NAME)
    name = data[key].strip()
    properties = (names or {}).get(name) or {}
    for other in ARGUMENT_KEYS:
        if other == key or other not in data or other in properties:
            continue
        arguments = argumentsOf(data[other])
        if arguments is not None:
            return name, arguments
        if other in ARGUMENT_KEYS[:2]:
            raise ValueError(BAD_ARGUMENTS.format(name=name, kind=KINDS.get(type(data[other]), "that")))
    return name, {other: value for other, value in data.items() if other != key and not (other == "type" and value in CALL_TYPES)}


def callOf(item, names, body):
    try:
        name, arguments = shapeOf(item, names)
    except ValueError as error:
        return badCall(body, str(error), names)
    return {"id": newCallId(), "name": name, "arguments": arguments}


# What a value that was read holds as calls: itself, its items, or the list an object keeps them in ({"tool_calls": [...]}).
def heldCalls(value):
    held = next((value[key] for key in HOLDER_KEYS if isinstance(value.get(key), list)), None) if isinstance(value, dict) else None
    return held if held is not None else value if isinstance(value, list) else [value]


def callsOf(value, names, body):
    items = heldCalls(value)
    calls = [callOf(item, names, body) for item in items] or [badCall(body, NO_NAME, names)]
    return calls[:1] if all(call.get("problem") for call in calls) else calls


# name(key=value, ...), as Python writes a call (Llama, Gemma), also inside print(...) and in a list. Only plain values are read, and nothing is run.
def pythonCalls(source, names):
    try:
        body = parsePython(source)
        nodes = body.elts if isinstance(body, (ast.List, ast.Tuple)) else [body]
        calls = []
        for node in nodes:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print" and len(node.args) == 1:
                node = node.args[0]
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)) or (names is not None and node.func.id not in names):
                return []
            arguments = literalOf(node.args[0]) if len(node.args) == 1 and isinstance(node.args[0], ast.Dict) and not node.keywords else None
            if arguments is None:
                if node.args or any(keyword.arg is None for keyword in node.keywords):
                    return []
                arguments = {keyword.arg: literalOf(keyword.value) for keyword in node.keywords}
            calls.append({"id": newCallId(), "name": node.func.id, "arguments": arguments})
        return calls
    except READ_ERRORS:
        return []


# A call as a small model writes it: a long text in an argument often has a quote or a backslash that is not escaped, and that is no reason
# to lose it. A text ends at a quote that is followed by the next key or by the end: any other quote inside it is part of the text.
def readLooseCall(body):
    start = LOOSE_START.match(body)
    if not start:
        raise ValueError("not a call")
    rest = body[start.end():].rstrip()
    for attempt in dict.fromkeys([re.sub(r"\}\s*\}$", "", rest), re.sub(r"[\s}\]`]*$", "", rest)]):
        try:
            return start.group(1), readLooseArguments(attempt)
        except ValueError:
            pass
    raise ValueError("not a call")


def readLooseArguments(text):
    arguments, position = {}, 0
    while position < len(text.rstrip()):
        key = LOOSE_KEY.match(text, position)
        if not key:
            raise ValueError("not a key")
        position = key.end()
        if text[position:position + 1] != '"':
            arguments[key.group(1)], position = decodeAt(text, position)
            continue
        end = LOOSE_TEXT_END.search(text, position + 1)
        if not end:
            raise ValueError("a text does not end")
        arguments[key.group(1)] = readLooseText(text[position + 1:end.start()])
        position = end.start() + 1
    return arguments


def readLooseText(raw):
    fixed = LOOSE_QUOTE.sub(r'\1\\"', raw)
    fixed = BAD_ESCAPE.sub(r"\\\\", fixed)
    return parseJson(f'"{fixed}"')


# What stands between the tags of a call when it is not plain JSON: the name before its arguments (name {...}, name({...}), name(key=value)),
# JSON that needs a repair, or a text with quotes that are not escaped. What cannot be read goes back to the model, with the first error.
def readLooseBody(body, names):
    inner = unfence(body)
    calls = readFunctionTags(inner, names)[0]
    if calls:
        return calls
    first = NAME_FIRST.match(inner)
    if first and (names is None or first.group(1) in names) and first.group(1) not in ("true", "false", "null"):
        arguments = argumentsOf(inner[first.end():].strip().rstrip(")").strip())
        if arguments is not None:
            return [{"id": newCallId(), "name": first.group(1), "arguments": arguments}]
    calls = pythonCalls(inner, names)
    if calls:
        return calls
    try:
        return callsOf(readLoose(inner), names, body)
    except ValueError as error:
        problem = BAD_JSON.format(error=describeJsonError(error))
    try:
        name, arguments = readLooseCall(inner)
    except ValueError:
        return [badCall(body, problem, names)]
    return [{"id": newCallId(), "name": name, "arguments": arguments}]


# One call tag, from the end of its opening tag: (its calls, where the text goes on). JSON is read where it stands, so a text of an argument
# that itself holds </tool_call> does not end the call. Otherwise the call ends at its closing tag, at the next call, or with the text.
def readBody(text, start, names):
    fence = FENCE_START.match(text, start)
    values, end = readValues(text, fence.end() if fence else start)
    if values:
        end = AFTER_VALUES.match(text, end).end()
        close = CLOSE_TAG.match(text, end)
        return [call for value in values for call in callsOf(value, names, text[start:end])], close.end() if close else end
    stop = BODY_END.search(text, start)
    body = text[start:stop.start()] if stop else text[start:]
    position = len(text) if stop is None else stop.end() if stop.group("close") else stop.start()
    if not body.strip():
        return ([] if stop is not None and stop.group("open") else [badCall(body, NO_NAME)]), position
    return readLooseBody(body, names), position


# What a model wrote while it thought is not what it asks for: a call inside <think>...</think> is not a call.
def outsideThinking(text, pattern):
    hidden = [match.span() for match in THINKING.finditer(text)]
    return [match for match in pattern.finditer(text) if not any(start <= match.start() < end for start, end in hidden)]


# <tool_call>...</tool_call>, and the tags like it (<tool_calls>, <function_call>, <|tool_call|>, [TOOL_CALLS]), closed or not.
def readTagged(text, names):
    calls, kept, position = [], [], 0
    for tag in outsideThinking(text, OPEN_TAG):
        if tag.start() < position:
            continue
        kept.append(text[position:tag.start()])
        found, position = readBody(text, tag.end(), names)
        calls += found
        if sum(1 for call in calls if call.get("problem")) >= BAD_LIMIT:
            position = len(text)
            break
    kept.append(text[position:])
    return (calls, "".join(kept)) if calls else ([], text)


# The value of a parameter written as text: the schema of the tool says if it is a text, or JSON (a number, a list...).
def parameterValue(raw, kind):
    raw = raw[1:] if raw.startswith("\n") else raw
    raw = raw[:-1] if raw.endswith("\n") else raw
    if kind == "string":
        return raw
    try:
        value = parseJson(raw)
    except ValueError:
        return raw
    return raw if isinstance(value, str) or (kind is None and isinstance(value, (int, float)) and not isinstance(value, bool)) else value


# [(the name of a parameter, its text)]: each one ends at its closing tag, and one that is not closed is not a parameter.
def parametersOf(body):
    found, position = [], 0
    while True:
        start = PARAMETER_START.search(body, position)
        end = PARAMETER_END.search(body, start.end()) if start else None
        if end is None:
            return found
        found.append((start.group(1), body[start.end():end.start()]))
        position = end.end()


# <function=name>{...}</function> (Llama, Functionary) and <invoke name="..."><parameter name="...">...</parameter></invoke>.
def readFunctionTags(text, names):
    calls, spans = [], []
    for match in outsideThinking(text, FUNCTION_TAG):
        name, body = match.group(1), match.group(2).strip()
        arguments = argumentsOf(unfence(body))
        problem = "" if arguments is not None else BAD_ARGUMENTS.format(name=name, kind="what you wrote")
        calls.append(badCall(body, problem, names, name) if problem else {"id": newCallId(), "name": name, "arguments": arguments})
        spans.append(match.span())
    for match in outsideThinking(text, INVOKE_TAG):
        name, properties = match.group(1), (names or {}).get(match.group(1)) or {}
        arguments = {key: parameterValue(raw, (properties.get(key) or {}).get("type")) for key, raw in parametersOf(match.group(2))}
        calls.append({"id": newCallId(), "name": name, "arguments": arguments})
        spans.append(match.span())
    return (calls, INVOKE_WRAP.sub("", cutSpans(text, spans)).strip()) if calls else ([], text)


def cutSpans(text, spans):
    kept, position = [], 0
    for start, end in sorted(spans):
        if start >= position:
            kept.append(text[position:start])
            position = end
    return "".join(kept + [text[position:]])


def isCall(item, names):
    try:
        name = shapeOf(item, names)[0]
    except ValueError:
        return False
    return name in names if names is not None else isinstance(item, dict) and "name" in item


# A text that is only calls, with no tag: JSON (one call, a list of them, several one after the other), or name(key=value) of its tools.
# The less strictly a text had to be read, the surer it must be that it names a tool the model has.
def bareCalls(inner, names):
    if inner[:1] in ("[", "{"):
        values, end = readValues(inner)
        if inner[end:].strip(" \t\r\n}]`"):
            values = []
        if not values and names:
            try:
                value = readLoose(inner)
                values = value if isinstance(value, list) else [value]
            except ValueError:
                values = []
        values = [item for value in values for item in heldCalls(value)]
        if values and all(isCall(item, names) for item in values):
            return [callOf(item, names, inner) for item in values]
    return pythonCalls(inner, names) if names else []


# Calls among the words of an answer ("I will read the file. {...}"), for a model that has tools: a code fence that holds only calls, JSON at
# the start of a line that names one of its tools, and one of its tools followed by its arguments (name {...}, Action: name / Action Input: {...}).
def readAmongWords(text, names):
    found = []
    for match in outsideThinking(text, FENCE)[:TRIES]:
        calls = bareCalls(match.group(1).strip(), names)
        found += [(match.start(), match.end(), calls)] if calls else []
    for match in outsideThinking(text, re.compile(r"^[ \t]*(?=[{\[])", re.MULTILINE))[:TRIES]:
        values, end = readValues(text, match.end())
        if values and all(isCall(item, names) for item in values):
            found.append((match.start(), end, [callOf(item, names, text[match.start():end]) for item in values]))
    alternation = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    named = re.compile(rf"^[ \t]*(?:Action[ \t]*:[ \t]*)?({alternation})[ \t]*(?:\r?\n[ \t]*(?:Action[ _]Input|Input|Arguments)[ \t]*:)?[ \t:=(]*(?=\{{)",
                       re.MULTILINE | re.IGNORECASE)
    for match in outsideThinking(text, named)[:TRIES]:
        try:
            value, end = decodeAt(text, match.end())
        except ValueError:
            continue
        name = next(name for name in names if name.lower() == match.group(1).lower())
        if isinstance(value, dict):
            found.append((match.start(), end + (text[end:end + 1] == ")"), [{"id": newCallId(), "name": name, "arguments": value}]))
    calls, spans, position = [], [], 0
    for start, end, group in sorted(found, key=lambda item: item[:2]):
        if start >= position:
            calls += group
            spans.append((start, end))
            position = end
    return calls, cutSpans(text, spans).strip() if calls else text


def readBare(text, names):
    whole = text.strip()
    fenced = FENCE.fullmatch(whole) if whole.startswith("```") else None
    calls = bareCalls(fenced.group(1).strip() if fenced else whole, names)
    if calls:
        return calls, ""
    return readAmongWords(text, names) if names else ([], text)


# The calls a model wrote in its text, and the text without them. tools: the tools it was given ([]: it has none now, so only a call written
# with tags counts, and it is not run; None: nobody said, and any JSON that names something is a call). A leader can also write a change of
# its swarm, or what to keep in the long-term memory, as a block (leader_parser.py, mission_memory.py): they are read as calls of its leader
# tools, when it has them.
def readToolCalls(text, tools=None):
    text, names = str(text or ""), knownTools(tools)
    for reader in (readTagged, readFunctionTags, readBare):
        calls, rest = reader(text, names)
        if calls:
            return calls, CLOSE_TAG.sub("", rest).strip()
    return readLeaderBlocks(text, set(names or ()))


# What a model wrote when its tokens ended. A call it was writing is the call it meant, which is not run (CUT_CALL tells it why): it never becomes
# the text of its answer, and it is not called invalid. Any other text is the start of an answer.
def readCutCalls(text, tools=None):
    text = str(text or "")
    calls, rest = readToolCalls(text, tools)
    if calls and not any(call.get("problem") for call in calls):
        return calls, rest
    if not calls and not STARTED_CALL.match(text):
        return calls, rest
    named = CALL_NAME.findall(text)
    good = [call for call in calls if not call.get("problem")]
    return [*good, {"id": newCallId(), "name": named[-1] if named else "unknown", "arguments": {}}], ""


def readLeaderBlocks(text, names):
    calls = []
    if any(name.startswith("leader__") and name != "leader__keep_long_term" for name in names):
        from leader_parser import readBlockCalls
        calls, text = readBlockCalls(text, newCallId)
    if "leader__keep_long_term" in names:
        from mission_memory import MEMORY_BLOCK
        calls += [{"id": newCallId(), "name": "leader__keep_long_term", "arguments": {"file": name.strip(), "text": body.strip(), "mode": (mode or "append").lower()}}
                  for name, mode, body in MEMORY_BLOCK.findall(text)]
        text = MEMORY_BLOCK.sub("", text).strip()
    return calls, text


# The call a provider gave through its API: the arguments are JSON in a text, or already an object (some APIs that work like OpenAI's).
def apiCall(callId, name, raw):
    call = {"id": callId, "name": name, "arguments": {}}
    if isinstance(raw, str) and raw.strip():
        try:
            raw = readLoose(raw)
        except ValueError:
            return {**call, "problem": BAD_API_JSON}
    if raw is None or raw == "" or isinstance(raw, dict):
        return {**call, "arguments": raw or {}}
    return {**call, "problem": BAD_ARGUMENTS.format(name=name, kind=KINDS.get(type(raw), "that"))}


# What an adapter gave as the calls of a step, whatever it gave: every call has an id, the name of a tool and its arguments as an object, or it
# says why it cannot be run. The core loop (agent_conversation.py) reads the calls of every kind of model through here.
def tidyCalls(calls):
    tidy = []
    for call in calls if isinstance(calls, (list, tuple)) else []:
        if not isinstance(call, dict):
            continue
        if not isinstance(call.get("id"), str):
            call["id"] = newCallId()
        named = isinstance(call.get("name"), str) and call["name"].strip()
        call["name"] = call["name"].strip() if named else "unknown"
        arguments = argumentsOf(call.get("arguments"))
        if not named:
            call.setdefault("problem", NO_NAME)
        elif arguments is None:
            call.setdefault("problem", BAD_ARGUMENTS.format(name=call["name"], kind=KINDS.get(type(call.get("arguments")), "that")))
        call["arguments"] = arguments or {}
        tidy.append(call)
    return tidy
