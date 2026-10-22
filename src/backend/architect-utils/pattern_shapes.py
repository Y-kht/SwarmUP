# The shapes of what agents do, for the miner of patterns (pattern_miner.py). Two calls are the same step when their shapes are the same: the
# shape keeps what a step is (the tool, the program and its subcommand, the flags, the kind of file or page) and drops what changes from one
# time to the next (the numbers, the ids, the quoted texts, the exact names). Each shape has three levels:
# - L0, exact: the step as it was, made relative to the project and with its secrets and numbers masked ("python -m pytest tests/test_a.py -q");
# - L1, template: the same step on any target of the same kind ("python -m pytest <path>.py -q");
# - L2, family: what kind of step it is ("python -m pytest").
# fingerprint() gives a short, stable id of a shape: the same in every program and every project.
import hashlib
import json
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlsplit


UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
HEX = re.compile(r"\b(?:0x[0-9a-f]+|(?=[0-9a-f]*\d)[0-9a-f]{7,})\b", re.IGNORECASE)
REDIRECTION = re.compile(r"(?<![\w\"'=])(?:\d|&)?>{1,2}(?:&\d|\s*(?![-&|;])[^\s;&|]+)|(?<![\w\"'=])<\s*[^\s;&|<]+")
NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
QUOTED = re.compile(r"\"(?:[^\"\\\n]|\\.){1,200}\"|'(?:[^'\\\n]|\\.){1,200}'|`[^`\n]{1,200}`")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
URL = re.compile(r"\bhttps?://[^\s\"'<>)]+", re.IGNORECASE)
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>|]+")
POSIX_PATH = re.compile(r"(?<![\w.~:/-])(?:~|\.{1,2})?/(?:[\w.@+-]+/)*[\w.@+-]+")
SPACES = re.compile(r"\s+")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
# Programs that wrap another command: what they run is the step.
WRAPPERS = {"sudo", "env", "time", "nohup", "nice", "xvfb-run", "stdbuf", "caffeinate", "command", "exec", "builtin", "unbuffer", "chronic", "ionice"}
WRAPPERS_WITH_VALUE = {"timeout": 1, "nice": 0}
RUNNERS = {("conda", "run"), ("poetry", "run"), ("uv", "run"), ("pipenv", "run"), ("pdm", "run"), ("hatch", "run"), ("pnpm", "exec"), ("npm", "exec"),
           ("bundle", "exec"), ("dotnet", "run")}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"}
# How many words after the program say what it does (git commit, docker compose up, npm run build).
SUBCOMMANDS = {"git": 1, "pip": 1, "pip3": 1, "npm": 1, "yarn": 1, "pnpm": 1, "bun": 1, "cargo": 1, "go": 1, "docker": 1, "podman": 1, "kubectl": 1,
               "make": 1, "conda": 1, "mamba": 1, "poetry": 1, "uv": 1, "brew": 1, "apt": 1, "apt-get": 1, "dnf": 1, "yum": 1, "pacman": 1,
               "systemctl": 1, "gh": 2, "swift": 1, "dotnet": 1, "mvn": 1, "gradle": 1, "./gradlew": 1, "terraform": 1, "helm": 1, "rustup": 1,
               "bundle": 1, "rails": 1, "rake": 1, "composer": 1, "deno": 1, "flutter": 1, "dart": 1, "xcrun": 1, "aws": 2, "gcloud": 2, "az": 2,
               "hf": 1, "huggingface-cli": 1, "pytest": 0, "jest": 0, "vitest": 1, "tsc": 0, "ruff": 1, "black": 0, "mypy": 0, "eslint": 0,
               "prettier": 0, "unittest": 1, "venv": 0, "node": 0, "java": 0, "javac": 0, "gcc": 0, "clang": 0, "rustc": 0, "swiftc": 0, "xcodebuild": 1, "cmake": 0, "ninja": 0}
NESTED = {("docker", "compose"), ("npm", "run"), ("yarn", "run"), ("pnpm", "run"), ("bun", "run"), ("git", "stash"), ("git", "remote"), ("go", "mod"),
          ("conda", "env"), ("cargo", "add")}
CONNECTIVES = {"&&", "||", ";", "|", "&", "|&"}
CONTEXT_PROGRAMS = {"cd", "pushd", "popd", "source", ".", "export", "set", "unset", "alias"}
PATH_KEYS = ("path", "source", "target", "file", "folder", "cwd", "filename", "directory", "dir")
TEXT_KEYS = ("text", "pattern", "query", "content", "new_text", "old_text", "note", "message", "question", "prompt")
SOURCE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".m",
                   ".mm", ".scala", ".sh", ".sql", ".vue", ".svelte", ".lua", ".r", ".jl", ".dart", ".ex", ".exs", ".hs", ".ml", ".zig"}
DOC_SUFFIXES = {".md", ".txt", ".rst", ".tex", ".docx", ".pdf", ".html", ".adoc"}
DATA_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".xml", ".plist", ".env", ".lock"}


def fingerprint(*parts):
    return hashlib.blake2b("|".join(str(part) for part in parts).encode("utf-8", "replace"), digest_size=10).hexdigest()


def collapse(text, limit=400):
    text = SPACES.sub(" ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


# The masks of L1: what changes from one time to the next becomes a placeholder.
def maskText(text, keepPaths=False):
    text = str(text or "")
    text = URL.sub(lambda match: shapeUrl(match.group(0))[1], text)
    text = EMAIL.sub("<email>", text)
    if not keepPaths:
        text = WINDOWS_PATH.sub("<path>", text)
        text = POSIX_PATH.sub(lambda match: templatePath(match.group(0)), text)
    text = UUID.sub("<id>", text)
    text = DATE.sub("<date>", text)
    text = TIME.sub("<time>", text)
    text = HEX.sub("<hex>", text)
    text = QUOTED.sub("<str>", text)
    text = NUMBER.sub("#", text)
    return collapse(text)


def kindOfSuffix(suffix):
    suffix = suffix.lower()
    if suffix in SOURCE_SUFFIXES:
        return "source"
    if suffix in DOC_SUFFIXES:
        return "document"
    if suffix in DATA_SUFFIXES:
        return "data"
    return "file" if suffix else "folder"


# A path made relative to the root of the project when it is inside it, with / between its parts.
def relativePath(path, root=None):
    text = str(path or "").strip().replace("\\", "/")
    if root:
        base = str(root).replace("\\", "/").rstrip("/") + "/"
        if text.startswith(base):
            text = text[len(base):]
        elif text == base.rstrip("/"):
            text = "."
    return text[2:] if text.startswith("./") else text or "."


# L1 of a path: its folders are kept (with their numbers masked), and its name becomes <name> with its suffix.
def templatePath(path):
    text = str(path or "").replace("\\", "/")
    if text in ("", "."):
        return "."
    parts = [part for part in text.split("/") if part not in ("",)]
    if not parts:
        return "/"
    head = ["#" if part.isdigit() else HEX.sub("<hex>", UUID.sub("<id>", part)) for part in parts[:-1]][-3:]
    name = PurePosixPath(parts[-1])
    suffix = "".join(name.suffixes[-2:]) if len(name.suffixes) > 1 and name.suffixes[-1] in (".gz", ".bz2", ".xz") else name.suffix
    stem = name.name[:-len(suffix)] if suffix else name.name
    prefix = re.match(r"^(test_|tests?_|spec_|_)", stem)
    leaf = f"{prefix.group(1) if prefix else ''}<name>{suffix}" if stem and not stem.startswith("<") else name.name
    lead = ("/" if text.startswith("/") else "") + ("…/" if len(parts) > 4 else "")
    return lead + "/".join([*head, leaf])


def shapePath(path, root=None):
    relative = relativePath(path, root)
    suffix = PurePosixPath(relative).suffix
    return relative, templatePath(relative), f"{kindOfSuffix(suffix)}{suffix.lower() if suffix else ''}"


# (L0, L1, L2) of a web address: the host, then its path with the ids masked, and the names of its query.
def shapeUrl(url):
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return str(url), "<url>", "<url>"
    host = (parts.hostname or "").lower()
    segments = []
    for segment in [part for part in parts.path.split("/") if part]:
        if segment.isdigit() or UUID.fullmatch(segment) or HEX.fullmatch(segment) or len(segment) > 40:
            segments.append("<id>")
        elif re.search(r"\d", segment) and re.search(r"[a-z]", segment, re.IGNORECASE) and len(segment) > 12:
            segments.append("<slug>")
        else:
            segments.append(segment)
    keys = sorted({key for key, value in parse_qsl(parts.query, keep_blank_values=True)})
    query = f"?{'&'.join(keys)}" if keys else ""
    exact = f"{host}{parts.path}{('?' + parts.query) if parts.query else ''}"
    return exact, f"{host}/{'/'.join(segments)}{query}", host or "<url>"


def splitWords(command):
    command = REDIRECTION.sub(" ", command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


def programName(word):
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    name = re.sub(r"\.exe$", "", name)
    return "python" if re.fullmatch(r"python[\d.]*", name) else name


# Takes away what only wraps the real command: sudo, env, timeout 10, conda run -n x, poetry run, bash -lc "...".
def unwrap(words):
    words = list(words)
    while words:
        while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
            words.pop(0)
        if not words:
            break
        program = programName(words[0])
        if program in WRAPPERS:
            words.pop(0)
            if program == "env":
                while words and (words[0].startswith("-") or "=" in words[0]):
                    words.pop(0)
            while words and words[0].startswith("-") and program in ("sudo", "nice", "ionice", "stdbuf", "xvfb-run"):
                words.pop(0)
                if program == "nice" and words and words[0].lstrip("-").isdigit():
                    words.pop(0)
            continue
        if program in WRAPPERS_WITH_VALUE and len(words) > 1:
            words.pop(0)
            while words and words[0].startswith("-"):
                words.pop(0)
            if words and re.fullmatch(r"\d+[smhd]?", words[0]):
                words.pop(0)
            continue
        if len(words) > 1 and (program, words[1].lower()) in RUNNERS:
            words = words[2:]
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag in ("-n", "--name", "-p", "--prefix", "--project", "--directory", "--with") and words:
                    words.pop(0)
            continue
        if program in ("npx", "bunx", "pnpx", "uvx", "pipx") and len(words) > 1:
            words = words[1:]
            while words and words[0].startswith("-"):
                words.pop(0)
            if program == "pipx" and words and words[0] == "run":
                words.pop(0)
            continue
        if program in SHELLS and len(words) > 2 and any(flag in ("-c", "-lc", "-ic", "-cl", "/c", "-command", "-Command") for flag in words[1:3]):
            inner = words[words.index(next(flag for flag in words[1:3] if flag in ("-c", "-lc", "-ic", "-cl", "/c", "-command", "-Command"))) + 1:]
            return ("inner", " ".join(inner))
        break
    return ("words", words)


PACKAGE_SPEC = re.compile(r"^[A-Za-z0-9_.\-\[\],]+(?:==|>=|<=|~=|!=|>|<)\S*$")


def classify(word, root=None):
    if PACKAGE_SPEC.match(word):
        return maskText(word), "<arg>"
    if URL.fullmatch(word):
        exact, template, host = shapeUrl(word)
        return exact, template
    if NUMBER.fullmatch(word):
        return word, "#"
    if "/" in word or "\\" in word or re.search(r"\.[A-Za-z0-9]{1,6}$", word) and not word.startswith("-"):
        relative, template, kind = shapePath(word, root)
        return relative, template
    return maskText(word), "<arg>"


# The shape of one simple command (no connective): (L0, L1, L2, context), context being a cd or a source that prepares the next one.
def shapeSimple(words, root=None):
    kind, value = unwrap(words)
    if kind == "inner":
        return None, value
    words = value
    if not words:
        return None, None
    program = programName(words[0])
    if program in CONTEXT_PROGRAMS:
        target = classify(words[1], root)[1] if len(words) > 1 else ""
        return {"l0": " ".join([program, *[classify(word, root)[0] for word in words[1:2]]]), "l1": f"{program} {target}".strip(), "l2": program, "context": True}, None
    rest = words[1:]
    head = [program]
    depth = SUBCOMMANDS.get(program, 0)
    if program == "python" and rest and rest[0] == "-m" and len(rest) > 1:
        head, rest = ["python", "-m", rest[1]], rest[2:]
        depth = SUBCOMMANDS.get(head[2], 0)
    elif program == "python" and rest and rest[0] == "-c" and len(rest) > 1:
        return {"l0": f"python -c <code:{fingerprint(rest[1])[:8]}>", "l1": "python -c <code>", "l2": "python -c", "context": False}, None
    while depth and rest and not rest[0].startswith("-"):
        head.append(rest.pop(0).lower())
        depth -= 1
        if depth == 0 and (head[0], head[-1]) in NESTED and rest and not rest[0].startswith("-"):
            head.append(rest.pop(0).lower())
    flags, exact, template = [], [], []
    for word in rest:
        if word.startswith("-") and not NUMBER.fullmatch(word):
            name = word.split("=", 1)[0]
            flags.append(name)
            exact.append(word if "=" not in word else f"{name}={maskText(word.split('=', 1)[1])}")
            continue
        l0, l1 = classify(word, root)
        exact.append(l0)
        template.append(l1)
    family = " ".join(head[:4])
    flagText = " ".join(sorted(set(flags)))
    l1 = collapse(" ".join([family, flagText, *dedupe(template)]).strip(), 200)
    l0 = collapse(" ".join([family, *exact]).strip(), 300)
    return {"l0": maskText(l0, keepPaths=True), "l1": l1, "l2": family, "context": False}, None


def dedupe(items):
    kept = []
    for item in items:
        if not kept or kept[-1] != item:
            kept.append(item)
    return kept


# (L0, L1, L2, programs) of a command line: its simple commands joined by their connectives; a cd before a command is part of its L0 only.
def shapeCommand(command, root=None, depth=0):
    words = splitWords(str(command or ""))
    pieces, current, joins = [], [], []
    for word in words:
        if word in CONNECTIVES:
            pieces.append(current)
            joins.append(word)
            current = []
        else:
            current.append(word)
    pieces.append(current)
    shapes, contexts = [], []
    for piece in pieces:
        shape, inner = shapeSimple(piece, root)
        if inner is not None and depth < 3:
            nested = shapeCommand(inner, root, depth + 1)
            shapes.append({"l0": nested[0], "l1": nested[1], "l2": nested[2], "context": False})
            continue
        if shape is None:
            continue
        (contexts if shape["context"] else shapes).append(shape)
    if not shapes:
        shapes = contexts
        contexts = []
    l0 = " && ".join([*(context["l0"] for context in contexts), *(shape["l0"] for shape in shapes)])
    l1 = " | ".join(shape["l1"] for shape in shapes)
    l2 = " | ".join(dedupe([shape["l2"] for shape in shapes]))
    return collapse(l0, 400), collapse(l1, 300), collapse(l2, 120), [shape["l2"] for shape in shapes]


def shapeValue(key, value, root=None):
    if isinstance(value, bool) or value is None:
        return str(value).lower(), str(value).lower()
    if isinstance(value, (int, float)):
        return str(value), "#"
    if isinstance(value, dict) and "sha1" in value:
        return f"<{value.get('len')} chars:{value.get('sha1')}>", "<text>"
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return maskText(text)[:200], f"<{type(value).__name__}>"
    text = str(value)
    if key in PATH_KEYS:
        relative, template, kind = shapePath(text, root)
        return relative, template
    if URL.fullmatch(text.strip()):
        exact, template, host = shapeUrl(text.strip())
        return exact, template
    if key in TEXT_KEYS or len(text) > 60:
        masked = maskText(text)
        return masked[:200], f"<text:{len(text.split())}w>" if key in ("content", "new_text", "old_text") else masked[:80]
    return maskText(text)[:120], maskText(text)[:60]


# (L0, L1, L2) of a call of a tool: the tool with the shapes of its arguments. A command and a web page get their own shapes.
def shapeCall(server, tool, arguments, root=None):
    name = f"{server}__{tool}" if server else tool
    arguments = arguments if isinstance(arguments, dict) else {"value": arguments}
    if tool == "run_command" and isinstance(arguments.get("command"), str):
        l0, l1, l2, programs = shapeCommand(arguments["command"], root)
        flags = " background" if arguments.get("background") else ""
        return f"{name}: {l0}{flags}", f"{name}: {l1}{flags}", f"{name}: {l2}"
    if tool == "fetch_page" and isinstance(arguments.get("url"), str):
        exact, template, host = shapeUrl(arguments["url"])
        return f"{name}: {exact}", f"{name}: {template}", f"{name}: {host}"
    exact, template, family = [], [], []
    for key in sorted(arguments):
        if key in ("reason", "why"):
            continue
        l0, l1 = shapeValue(key, arguments[key], root)
        exact.append(f"{key}={l0}")
        template.append(f"{key}={l1}")
        family.append(key)
    if any(key in arguments for key in PATH_KEYS):
        target = next(arguments[key] for key in PATH_KEYS if key in arguments)
        kind = shapePath(str(target), root)[2] if isinstance(target, str) else "file"
        family = [*family, kind]
    return collapse(f"{name}({', '.join(exact)})", 400), collapse(f"{name}({', '.join(template)})", 300), collapse(f"{name}({', '.join(family)})", 160)


# The exact target of a call that reads (a file, a page, a search), to find the same thing read again and again.
def targetOf(server, tool, arguments, root=None):
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool == "fetch_page" and isinstance(arguments.get("url"), str):
        return shapeUrl(arguments["url"])[0]
    for key in PATH_KEYS:
        if isinstance(arguments.get(key), str):
            target = relativePath(arguments[key], root)
            if tool == "search_files":
                return f"{target} ? {maskText(arguments.get('text') or '')}"
            if tool == "read_file" and arguments.get("start_line"):
                return f"{target}:{arguments.get('start_line')}"
            return target
    if tool in ("search_files", "find_files"):
        return f"? {maskText(arguments.get('text') or arguments.get('pattern') or '')}"
    return ""


def numbersOf(arguments):
    return {key: value for key, value in (arguments or {}).items() if isinstance(value, (int, float)) and not isinstance(value, bool)} if isinstance(arguments, dict) else {}
