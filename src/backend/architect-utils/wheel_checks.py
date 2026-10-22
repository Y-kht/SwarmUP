# What a wheel must be before it is saved (wheel_store.py), checked without running it: a wheel is code that a model wrote, and agents of
# every later swarm will run it. A script wheel is Python with a literal WHEEL and run(args, folder), nothing that runs when it is loaded,
# no eval or exec, no secret of the user, and no network unless it says so; a wheel of steps is a short list of calls of tools. Its summary
# is read by the models of every later swarm as the description of a tool, so it is screened like the tools of servers of other programs.
# And a wheel automates only what repeats: at least one of the patterns it covers repeats in the project, or across projects.
import ast
import json
import re
import types

import jsonschema

from mcp_external import screenTool


NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
FULL_NAME_CHARS = 64
SUMMARY_LIMITS = (20, 300)
MAX_INPUTS = 8
MAX_LINES = 600
MAX_STEPS = 12
MAX_TIMEOUT = 600
INPUT_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
MANIFEST_KEYS = {"name", "summary", "kind", "inputs", "required", "changes", "network", "timeout", "steps"}
FORBIDDEN_NAMES = {"eval", "exec", "__import__", "compile", "breakpoint", "globals", "locals", "vars"}
FORBIDDEN_MODULES = {"ctypes", "pickle", "marshal", "shelve", "cffi", "importlib"}
NETWORK_MODULES = {"socket", "ssl", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "smtplib", "imaplib", "poplib", "telnetlib", "urllib3",
                   "websocket", "websockets", "paramiko", "xmlrpc", "socketserver"}
CREDENTIALS = re.compile(r"(?i)(?:^|[/\\'\"~])(?:\.ssh|\.aws|\.gnupg|\.netrc|\.kube[/\\]config|id_rsa|id_ed25519|\.docker[/\\]config\.json|keychain|\.git-credentials|\.npmrc|\.pypirc)")
REPEATING = ("repeating", "cross")


def checkInputs(manifest):
    problems = []
    inputs, required = manifest.get("inputs") or {}, manifest.get("required") or []
    if not isinstance(inputs, dict):
        return ["inputs must be a dictionary {name: {type, description}}."]
    if len(inputs) > MAX_INPUTS:
        problems.append(f"A wheel has {MAX_INPUTS} inputs at most: one wheel does one job.")
    for key, spec in inputs.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", str(key)):
            problems.append(f"The input {key!r} must be a name in small letters, digits and _.")
        if not isinstance(spec, dict) or spec.get("type") not in INPUT_TYPES or not str(spec.get("description") or "").strip():
            problems.append(f"The input {key} needs a type ({', '.join(sorted(INPUT_TYPES))}) and a description.")
    if not isinstance(required, list) or any(name not in inputs for name in required):
        problems.append("required lists the names of inputs that the wheel cannot do without.")
    try:
        jsonschema.Draft202012Validator.check_schema({"type": "object", "properties": inputs, "required": list(required) if isinstance(required, list) else [],
                                                      "additionalProperties": False})
    except jsonschema.SchemaError as error:
        problems.append(f"The inputs are not a valid schema: {error.message}")
    return problems


def checkSummary(name, summary, inputs):
    problems = []
    summary = str(summary or "").strip()
    if not SUMMARY_LIMITS[0] <= len(summary) <= SUMMARY_LIMITS[1]:
        problems.append(f"The summary says the main objective of the wheel in one or two sentences, {SUMMARY_LIMITS[0]} to {SUMMARY_LIMITS[1]} characters.")
    findings = screenTool(types.SimpleNamespace(name=name, description=summary, inputSchema={"properties": inputs}))
    if findings:
        problems.append(f"The summary must only say what the wheel does, and it {'; '.join(findings)}.")
    return problems


def checkManifest(manifest, kind, name=None):
    problems = []
    unknown = set(manifest) - MANIFEST_KEYS
    if unknown:
        problems.append(f"WHEEL has keys it does not use: {', '.join(sorted(unknown))}. Its keys: {', '.join(sorted(MANIFEST_KEYS - {'steps'}))}.")
    wheelName = str(manifest.get("name") or "")
    if not NAME.fullmatch(wheelName) or len(f"wheels__{wheelName}") > FULL_NAME_CHARS:
        problems.append("The name of a wheel is a verb and a noun in small letters joined by _, like run_unit_tests, 55 characters at most.")
    if name and wheelName != name:
        problems.append(f"The name in the wheel ({wheelName}) is not the name it is saved under ({name}).")
    if manifest.get("kind", kind) != kind:
        problems.append(f"kind must be {kind}.")
    for flag in ("changes", "network"):
        if not isinstance(manifest.get(flag, False), bool):
            problems.append(f"{flag} is true or false.")
    timeout = manifest.get("timeout", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        problems.append(f"timeout is the most seconds the wheel may run, 1 to {MAX_TIMEOUT}.")
    problems += checkInputs(manifest)
    problems += checkSummary(wheelName, manifest.get("summary"), manifest.get("inputs") or {})
    return problems


def isLiteralAssignment(node):
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return False
    value = node.value
    if value is None:
        return True
    try:
        ast.literal_eval(value)
        return True
    except ValueError:
        return isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "compile" and \
            isinstance(value.func.value, ast.Name) and value.func.value.id == "re"


def isMainGuard(node):
    return isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"


def moduleNames(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def checkScript(text):
    problems = []
    if len(text.splitlines()) > MAX_LINES:
        problems.append(f"A wheel has {MAX_LINES} lines at most: split it into wheels that each do one job.")
    try:
        tree = ast.parse(text)
        compile(text, "<wheel>", "exec")
    except SyntaxError as error:
        return None, [f"The code does not compile: {error.msg}, line {error.lineno}."]
    manifest = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "WHEEL" for target in node.targets):
            try:
                manifest = ast.literal_eval(node.value)
            except ValueError:
                problems.append("WHEEL must be written out in full, with no variable or call in it.")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        elif not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not isLiteralAssignment(node) and not isMainGuard(node):
            problems.append(f"Line {node.lineno} runs when the wheel is loaded: only imports, functions, classes and constants may be at the top.")
    if not isinstance(manifest, dict):
        return None, problems + ["A script wheel starts with WHEEL = {...}, a dictionary written out in full."]
    run = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"), None)
    if run is None or [argument.arg for argument in run.args.args[:2]] != ["args", "folder"]:
        problems.append("A script wheel has def run(args, folder): args is the dictionary of its inputs, folder the folder it runs in.")
    network = bool(manifest.get("network"))
    for node in ast.walk(tree):
        for module in moduleNames(node):
            root = module.split(".")[0]
            if root in FORBIDDEN_MODULES:
                problems.append(f"A wheel may not use {root}.")
            if root in NETWORK_MODULES and not network:
                problems.append(f"The wheel uses {root}, which reaches the network: set \"network\": True in WHEEL, or do without it.")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            problems.append(f"A wheel may not call {node.func.id}() (line {node.lineno}).")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and CREDENTIALS.search(node.value):
            problems.append(f"A wheel may not reach the secrets of the user (line {node.lineno}).")
    return manifest, sorted(set(problems), key=problems.index)


def checkSteps(manifest):
    problems = []
    steps = manifest.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return [f"A wheel of steps has 1 to {MAX_STEPS} steps: [{{\"tool\": \"server__tool\", \"arguments\": {{...}}, \"save\": {{\"name\": \"regex\"}}}}]."]
    known = set(manifest.get("inputs") or {})
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not re.fullmatch(r"[a-z][a-z0-9_]*__[a-z][a-z0-9_]*", str(step.get("tool") or "")):
            problems.append(f"Step {number} needs a tool, as its full name like files__read_file.")
            continue
        if step["tool"].startswith(("wheels__", "architect__", "leader__", "user__")):
            problems.append(f"Step {number} calls {step['tool']}: a wheel calls the tools of the agent only, not wheels, the leader or the user.")
        names = set(re.findall(r"\{(\w+)\}", str(step.get("arguments") or {})))
        missing = names - known
        if missing:
            problems.append(f"Step {number} uses {', '.join(sorted(missing))}, which is no input and was not saved by a step before.")
        for name, pattern in (step.get("save") or {}).items():
            try:
                re.compile(str(pattern))
            except re.error:
                problems.append(f"Step {number} saves {name} with a regular expression that does not compile.")
            known.add(name)
    return problems


# The problems of a wheel before it is saved: [] when it can be saved. covers are the patterns (rows of the store) it automates, and liveCovers
# {fingerprint: wheel} what the live wheels already cover.
def checkWheel(text, kind, name, covers, scope, liveCovers, replaces=()):
    if kind == "steps":
        try:
            manifest = json.loads(text)
        except ValueError as error:
            return None, [f"A wheel of steps is JSON: {error}."]
        problems = [] if isinstance(manifest, dict) else ["A wheel of steps is a JSON object."]
        if not problems:
            problems += checkManifest(manifest, kind, name) + checkSteps(manifest)
    elif kind == "script":
        manifest, problems = checkScript(text)
        if manifest is not None:
            problems += checkManifest(manifest, kind, name)
    else:
        return None, ["kind is script (Python) or steps (calls of tools)."]
    if not covers:
        problems.append("A wheel automates patterns: give the ids of the patterns it covers (from architect__read_patterns).")
    elif not any(row["status"] in REPEATING or row["status"] == "covered" and liveCovers.get(row["fp"]) in (name, *replaces) for row in covers):
        problems.append("None of these patterns repeats yet (in this project or across projects). Write a note about it instead (architect__save_note).")
    taken = {liveCovers[row["fp"]] for row in covers if liveCovers.get(row["fp"]) not in (None, name, *replaces)}
    if taken:
        problems.append(f"The wheel {', '.join(sorted(taken))} already covers these patterns: update or merge it (give it in replaces) instead of writing another one.")
    if scope == "global" and not any(row["status"] == "cross" or (row.get("data") or {}).get("cross") or (row.get("data") or {}).get("note") for row in covers):
        problems.append("A wheel is for every project only when its pattern repeats across projects: keep this one for this project.")
    return manifest, problems


# How many wheels an agent can take: the most tools of the settings, less the tools it has without wheels.
def roomFor(toolCount, most):
    return max(0, most - toolCount)
