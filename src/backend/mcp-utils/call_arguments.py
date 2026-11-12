# The name and the arguments of a call, as the gateway receives them (mcp_gateway.py, GTW-03). What a model writes is often right in meaning and
# wrong in form: the name of the tool with another separator or without its server (files.read_file, read_file), a number or a yes/no as
# text ("12", "true"), a text as a number, a list as JSON in a text, one value where a list is asked, null for an argument it leaves out, a
# key in another case (filePath for file_path). Where the tools of the agent and the schema of the tool say which one is meant, the call is
# read as it was meant. Nothing is guessed: when two things could be meant, or none, the call goes back to the model with what to change.
import re

import jsonschema

from loose_json import readLoose
from mcp_kit import splitName


NAME_PREFIX = re.compile(r"^(?:functions?|tools?|mcp)[._/:]+", re.IGNORECASE)
WHOLE_NUMBER = re.compile(r"[-+]?\d+(?:\.0+)?")
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
YES_NO = {"true": True, "false": False, "yes": True, "no": False}
FITS = {"string": lambda value: isinstance(value, str), "boolean": lambda value: isinstance(value, bool), "array": lambda value: isinstance(value, list),
        "object": lambda value: isinstance(value, dict), "null": lambda value: value is None,
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)}


def squashName(name):
    text = NAME_PREFIX.sub("", str(name).strip().strip("`'\"").removesuffix("()"))
    return re.sub(r"[^a-z0-9]", "", text.lower())


# The tool a model meant when it did not write its name exactly: the same letters with another case or other separators, behind a prefix some
# models add (functions.files__read_file), or without its server when one server only has such a tool. None when no tool, or more than one, could be meant.
def nearName(name, names):
    if not isinstance(name, str):
        return None
    if name in names:
        return name
    wanted = squashName(name)
    if not wanted:
        return None
    same = [known for known in names if squashName(known) == wanted] or [known for known in names if squashName(splitName(known)[1]) == wanted]
    return same[0] if len(same) == 1 else None


def describeSchemaError(name, error):
    where = ".".join(str(part) for part in error.absolute_path)
    if error.validator == "required":
        return f"{name} needs {error.message.split(' is a required')[0].strip(chr(39))}."
    if error.validator == "additionalProperties":
        return f"{name} has no such argument: {', '.join(re.findall(r"'([^']*)'", error.message))}. Its arguments: {', '.join(error.schema.get('properties') or {})}."
    if error.validator == "type":
        return f"{where or 'the arguments'} must be {'an' if str(error.validator_value)[0] in 'aeiou' else 'a'} {error.validator_value}."
    if error.validator == "enum":
        return f"{where} must be one of {', '.join(str(value) for value in error.validator_value)}."
    return f"{where + ': ' if where else ''}{error.message}."


# The kinds of value a schema takes ("string", "integer"...), also when it gives several of them (type as a list, anyOf, oneOf).
def kindsOf(rule):
    kinds = rule.get("type")
    kinds = [kinds] if isinstance(kinds, str) else list(kinds) if isinstance(kinds, list) else []
    for word in ("anyOf", "oneOf"):
        for option in rule.get(word) if isinstance(rule.get(word), list) else []:
            kinds += kindsOf(option) if isinstance(option, dict) else []
    return [kind for kind in kinds if isinstance(kind, str)]


def ruleOf(schema, key):
    rule = schema.get(key)
    return rule if isinstance(rule, dict) else {}


# A text where the schema asks for something else: exactly a number, a yes/no, or JSON of the kind that is asked.
def readText(text, kinds):
    text = text.strip()
    if "integer" in kinds and WHOLE_NUMBER.fullmatch(text):
        return int(float(text)) if "." in text else int(text)
    if "number" in kinds and NUMBER.fullmatch(text):
        return float(text)
    if "boolean" in kinds and text.lower() in YES_NO:
        return YES_NO[text.lower()]
    if ("array" in kinds or "object" in kinds) and text[:1] in ("[", "{"):
        try:
            value = readLoose(text)
        except ValueError:
            return None
        return value if any(FITS[kind](value) for kind in kinds if kind in ("array", "object")) else None
    return None


# One argument in the form its schema asks for, when what was written has that form in one way only. Anything else stays as it was written.
def coerceValue(rule, value):
    kinds = [kind for kind in kindsOf(rule) if kind in FITS]
    if isinstance(value, dict) and "object" in kinds:
        return coerceArguments(rule, value)
    if isinstance(value, list) and "array" in kinds:
        return [coerceValue(ruleOf(rule, "items"), item) for item in value]
    options = rule.get("enum") if isinstance(rule.get("enum"), list) else []
    near = [option for option in options if isinstance(option, str) and isinstance(value, str) and option.strip().lower() == value.strip().lower()]
    if len(near) == 1 and value not in options:
        return near[0]
    if not kinds or any(FITS[kind](value) for kind in kinds):
        return value
    read = readText(value, kinds) if isinstance(value, str) else None
    if read is not None:
        return coerceValue(rule, read)
    if isinstance(value, float) and value.is_integer() and "integer" in kinds:
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "string" in kinds:
        return str(value)
    if "array" in kinds and value is not None and (not isinstance(value, str) or value.strip()[:1] not in ("", "[", "{")):
        item = coerceValue(ruleOf(rule, "items"), value)
        wanted = [kind for kind in kindsOf(ruleOf(rule, "items")) if kind in FITS]
        return [item] if not wanted or any(FITS[kind](item) for kind in wanted) else value
    return value


# The key of the schema a model meant: the same letters in another case or with other separators (filePath, file-path), when one key only is such.
def nearKey(key, properties, arguments):
    if key in properties or not properties:
        return key
    squashed = re.sub(r"[^a-z0-9]", "", key.lower())
    near = [name for name in properties if name not in arguments and re.sub(r"[^a-z0-9]", "", str(name).lower()) == squashed]
    return near[0] if len(near) == 1 else key


# The arguments of a call in the form the schema of its tool asks for (see the top of this file). An argument written as null that the tool
# does not need (or as an empty text where a number is asked) is an argument the model left out.
def coerceArguments(schema, arguments):
    if not isinstance(arguments, dict) or not isinstance(schema, dict):
        return arguments
    properties, required = ruleOf(schema, "properties"), schema.get("required") if isinstance(schema.get("required"), list) else []
    fixed = {}
    for key, value in arguments.items():
        key = nearKey(str(key), properties, arguments)
        rule = ruleOf(properties, key)
        kinds = kindsOf(rule)
        if rule and key not in required and (value is None and "null" not in kinds or value == "" and kinds and "string" not in kinds):
            continue
        fixed[key] = coerceValue(rule, value)
    return fixed


# GTW-03: the arguments must match the schema of the tool, or the model is told what to change, and the server never sees the call. A schema
# that cannot be used (a server outside SwarmUP can give anything) checks nothing: its server checks the call itself.
def checkArguments(name, schema, arguments):
    if not isinstance(arguments, dict):
        return f"The arguments of {name} must be a JSON object."
    try:
        errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(arguments), key=lambda error: [str(part) for part in error.absolute_path])
        return " ".join(describeSchemaError(name, error) for error in errors[:3])
    except Exception:
        return ""
