# JSON as the models write it. A model that is asked for JSON (a call of a tool, a route, a plan) often writes it almost right: a comma after
# the last item, a bracket too many or one missing, single quotes, True and None, curly quotes, comments, words around it. Nothing here guesses
# what a text means: each repair only changes what stands outside the strings, so the texts a model wrote arrive as it wrote them. What cannot
# be read raises ValueError, with the first error of the JSON as the model wrote it.
import ast
import json
import re
import warnings


JSON_LIMIT = 60000
JSON_TAIL = 200
READ_ERRORS = (ValueError, SyntaxError, TypeError, MemoryError, RecursionError)
SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "″": '"', "‘": "'", "’": "'", "′": "'"})
FENCE = re.compile(r"```[ \t]*[\w+.-]*[ \t]*\r?\n?(.*?)(?:```|\Z)", re.DOTALL)
UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][\w-]*)(\s*:)")
WORDS = {"true": True, "false": False, "null": None, "True": True, "False": False, "None": None}
SEPARATORS = " \t\r\n;,"
MODEL_TEXT = "<text of a model>"


# JSON nested very deep ends the stack of Python (RecursionError), which is not a ValueError: for who reads, it is JSON that cannot be read.
def parseJson(text):
    try:
        return json.loads(text, strict=False)
    except RecursionError:
        raise ValueError("the JSON is nested too deep") from None


def decodeAt(text, position):
    try:
        return json.JSONDecoder(strict=False).raw_decode(text, position)
    except RecursionError:
        raise ValueError("the JSON is nested too deep") from None


# The text inside the first code fence (``` or ```json), closed or not. A text without a fence is given as it is.
def unfence(text):
    text = str(text).strip()
    match = FENCE.search(text)
    return match.group(1).strip() if match else text


# Comments (//, # and /* */) out of JSON, except inside strings, where // is often part of an address.
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


# The brackets a model forgot to close are closed, those it closed twice are dropped, and so is a comma before a closing bracket. Only what
# is outside the strings counts. None when the text ends inside a string, or after a comma or a colon: more was to come, and nobody knows what.
def closeBrackets(text):
    stack, kept, inString, escaped = [], [], False, False
    for char in text:
        if inString:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                inString = False
        elif char == '"':
            inString = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                continue
            stack.pop()
            dropComma(kept)
        kept.append(char)
    if inString or (stack and "".join(kept[-JSON_TAIL:]).rstrip()[-1:] in (",", ":")):
        return None
    return "".join(kept) + "".join(reversed(stack))


def dropComma(kept):
    index = len(kept) - 1
    while index >= 0 and kept[index] in " \t\r\n":
        index -= 1
    if index >= 0 and kept[index] == ",":
        del kept[index]


# What Python reads as a value without running anything: a model often writes a dictionary of Python ('single quotes', True, None) for JSON.
def literalOf(node):
    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, (str, int, float, bool))):
        return node.value
    if isinstance(node, ast.Dict):
        return {literalOf(key): literalOf(value) for key, value in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [literalOf(item) for item in node.elts]
    if isinstance(node, ast.Name) and node.id in WORDS:
        return WORDS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float):
        return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
    raise ValueError("not a plain value")


# Python warns about what it reads (a backslash that escapes nothing): what a model wrote is not the code of SwarmUP, and the user is not told.
def parsePython(text):
    warnings.filterwarnings("ignore", category=SyntaxWarning, module=re.escape(MODEL_TEXT))
    return ast.parse(text.strip(), filename=MODEL_TEXT, mode="eval").body


def readPython(text):
    try:
        value = literalOf(parsePython(text))
    except READ_ERRORS:
        raise ValueError("not a value of Python") from None
    if not isinstance(value, (dict, list)):
        raise ValueError("not an object or a list")
    return value


# The same text with what a model often gets wrong put right, the mildest repair first.
def repairsOf(text):
    closed = closeBrackets(text)
    plain = removeComments(text.translate(SMART_QUOTES)).strip()
    plainClosed = closeBrackets(plain)
    attempts = [closed, plain, plainClosed, UNQUOTED_KEY.sub(r'\1"\2"\3', plainClosed or plain)]
    return [attempt for attempt in attempts if attempt and attempt != text]


# The value of a text that should be JSON. The first error is the one that is told when nothing works.
def readLoose(text):
    text = str(text).strip()
    try:
        return parseJson(text)
    except ValueError as error:
        first = error
    attempts = repairsOf(text)
    for attempt in attempts:
        try:
            return parseJson(attempt)
        except ValueError:
            pass
    for attempt in [text, *attempts]:
        try:
            return readPython(attempt)
        except ValueError:
            pass
    raise first


# JSON values written one after the other from a position of a text ({...} {...}, {...}; {...}, or a list of them): (the values, where they end).
# A list gives its items. It stops at what is not JSON, and gives nothing when the first value cannot be read.
def readValues(text, position=0):
    values = []
    while True:
        while position < len(text) and text[position] in SEPARATORS:
            position += 1
        if text[position:position + 1] not in ("{", "["):
            return values, position
        try:
            value, position = decodeAt(text, position)
        except ValueError:
            return values, position
        values += value if isinstance(value, list) else [value]


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


# The JSON object of a reply that was asked to be only JSON: the reply itself, what is in its code fence, or the first object among its words
# ("Here is the route: {...}"). ValueError when the reply has no object that can be read.
def readReply(text):
    text = str(text or "").strip()
    candidates = [text, unfence(text)]
    start = text.find("{")
    end = findBalanced(text, start) if start >= 0 else None
    candidates += [text[start:end]] if end else [text[start:]] if start >= 0 else []
    first = None
    for candidate in candidates:
        try:
            value = readLoose(candidate)
        except ValueError as error:
            first = first or error
            continue
        if isinstance(value, dict):
            return value
    raise first or ValueError("the reply has no JSON object")
