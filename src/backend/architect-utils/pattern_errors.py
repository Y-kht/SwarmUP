# What went wrong, in a form that repeats: the miner of patterns (pattern_miner.py) groups the errors the agents meet by their family and code
# (a Python ModuleNotFoundError, a TypeScript TS2345, a failed pytest test, a port already in use, a refusal of SwarmUP...), with a template of
# the message in which what changes is masked. classifyError(text) gives {family, code, template, message}, or None for a text with no error.
import re

from pattern_shapes import collapse, maskText


TEMPLATE_CHARS = 160
TRACEBACK = re.compile(r"Traceback \(most recent call last\):")
EXCEPTION_LINE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure|Timeout|NotFound|Refused)|AssertionError|StopIteration|KeyboardInterrupt)(?::\s*(.*))?\s*$", re.MULTILINE)
FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([\w<>.]+)')
# SwarmUP's own refusals and limits: what the agents meet again and again because of how they work.
SWARMUP = [
    ("denied", re.compile(r"The user refused to let you (\w+(?: \w+)?)")),
    ("unknown-tool", re.compile(r"there is no tool (\S+) for you now")),
    ("schema", re.compile(r"Not done: \S+ (?:needs \w+|has no such argument|.* must be (?:a|an|one of) )")),
    ("owned", re.compile(r"was changed by (\S+) in this run, and \S+ is not done yet")),
    ("read-first", re.compile(r"already exists and you have not read it|changed since you last read it")),
    ("hidden", re.compile(r"is kept by SwarmUP itself and cannot be reached")),
    ("long-term", re.compile(r"Only the leader of the swarm writes the long-term memory")),
    ("memory-owner", re.compile(r"was written by \S+: only .* or the leader can change it")),
    ("time-limit", re.compile(r"took more than \d+ seconds and was stopped|ran for more than \d+ seconds and was stopped")),
    ("cancelled", re.compile(r"the swarm was stopped|The swarm was stopped")),
    ("cut", re.compile(r"your answer was cut because it was too long")),
    ("repeated", re.compile(r"you already made this same call")),
    ("old-text", re.compile(r"old_text is not in|old_text is found \d+ times|is found more than once")),
    ("bad-url", re.compile(r"is not a web address")),
    ("http", re.compile(r"the website answered with error (\d{3})|the page could not be read")),
    ("not-found", re.compile(r"There is no (?:file|folder|path) |is not a file|is not a folder|does not exist")),
    ("outside", re.compile(r"is not inside|outside your folder|not a file inside")),
    ("bad-call", re.compile(r"SwarmUP could not read|could not be read as JSON|is not valid JSON")),
    ("wheel", re.compile(r"The wheel (\S+) (?:failed|stopped|ran for more)")),
]
# Each rule: (family, pattern, how to build the code from the match). They are tried in this order, the first one that matches wins.
RULES = [
    ("compile", re.compile(r"error\[(E\d{4})\]:\s*(.+)"), lambda m: (f"rustc:{m.group(1)}", m.group(2))),
    ("compile", re.compile(r"error (TS\d{3,5}):\s*(.+)"), lambda m: (f"tsc:{m.group(1)}", m.group(2))),
    ("lint", re.compile(r"^\S+\.py:\d+:(?:\d+:)? error: (.+?)\s+\[([\w-]+)\]\s*$", re.MULTILINE), lambda m: (f"mypy:{m.group(2)}", m.group(1))),
    ("lint", re.compile(r"^\S+\.py:\d+:\d+: ([A-Z]{1,3}\d{2,4}) (.+)$", re.MULTILINE), lambda m: (f"ruff:{m.group(1)}", m.group(2))),
    ("lint", re.compile(r"^\S+\.py:\d+:\d+: ([CRWEF]\d{4}): (.+)$", re.MULTILINE), lambda m: (f"pylint:{m.group(1)}", m.group(2))),
    ("lint", re.compile(r"^\s*\d+:\d+\s+error\s+(.+?)\s{2,}([@\w/-]+)\s*$", re.MULTILINE), lambda m: (f"eslint:{m.group(2)}", m.group(1))),
    ("compile", re.compile(r"^\S+\.java:\d+: error: (.+)$", re.MULTILINE), lambda m: ("javac", m.group(1))),
    ("compile", re.compile(r"^\S+\.go:\d+:\d+: (.+)$", re.MULTILINE), lambda m: ("go", m.group(1))),
    ("compile", re.compile(r"^\S+\.swift:\d+:\d+: error: (.+)$", re.MULTILINE), lambda m: ("swiftc", m.group(1))),
    ("compile", re.compile(r"^\S+\.(?:c|cc|cpp|cxx|h|hpp|m|mm):\d+:\d+: (?:fatal )?error: (.+)$", re.MULTILINE), lambda m: ("cc", m.group(1))),
    ("test", re.compile(r"^FAILED (\S+?)::(\S+)(?: - (.+))?$", re.MULTILINE), lambda m: ("pytest", m.group(3) or "a test failed")),
    ("test", re.compile(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", re.MULTILINE), lambda m: ("unittest", f"{m.group(2).rsplit('.', 1)[-1]} failed")),
    ("test", re.compile(r"^\s*● (.+?) › (.+)$", re.MULTILINE), lambda m: ("jest", m.group(2))),
    ("test", re.compile(r"Tests?:\s+\d+ failed"), lambda m: ("jest", "tests failed")),
    ("test", re.compile(r"^--- FAIL: (\w+)", re.MULTILINE), lambda m: ("go-test", "a test failed")),
    ("test", re.compile(r"^test (\S+) \.\.\. FAILED", re.MULTILINE), lambda m: ("cargo-test", "a test failed")),
    ("test", re.compile(r"\*\* TEST FAILED \*\*|error: -\[\w+ \w+\] : (.+)"), lambda m: ("xcodebuild", m.group(1) if m.lastindex else "tests failed")),
    ("test", re.compile(r"=+ (\d+) failed"), lambda m: ("pytest", "tests failed")),
    ("test", re.compile(r"^FAILED \((?:failures|errors)=\d+", re.MULTILINE), lambda m: ("unittest", "tests failed")),
    ("package", re.compile(r"No matching distribution found for (\S+)|Could not find a version that satisfies the requirement (\S+)"), lambda m: ("pip:not-found", "no distribution for <pkg>")),
    ("package", re.compile(r"npm (?:ERR!|error) code (\S+)"), lambda m: (f"npm:{m.group(1)}", m.group(0))),
    ("package", re.compile(r"E: Unable to locate package (\S+)"), lambda m: ("apt:not-found", "unable to locate <pkg>")),
    ("package", re.compile(r'Error: No available formula with the name "?(\S+?)"?'), lambda m: ("brew:not-found", "no formula <pkg>")),
    ("package", re.compile(r"PackagesNotFoundError"), lambda m: ("conda:not-found", "packages not found")),
    ("package", re.compile(r"error: externally-managed-environment"), lambda m: ("pip:externally-managed", "externally managed environment")),
    ("missing", re.compile(r"Cannot find module '([^']+)'|Module not found: Error: Can't resolve '([^']+)'"), lambda m: ("node:module", "cannot find module <str>")),
    ("missing", re.compile(r"(?:^|\s)(\S+): (?:command )?not found\b|'(\S+)' is not recognized as an internal or external command"), lambda m: ("command", "<cmd>: command not found")),
    ("missing", re.compile(r"No such file or directory|ENOENT|cannot find the (?:file|path) specified", re.IGNORECASE), lambda m: ("file", "no such file or directory")),
    ("permission", re.compile(r"Permission denied|EACCES|Operation not permitted|EPERM", re.IGNORECASE), lambda m: ("os", "permission denied")),
    ("network", re.compile(r"Connection refused|ECONNREFUSED"), lambda m: ("refused", "connection refused")),
    ("network", re.compile(r"Could not resolve host|Name or service not known|getaddrinfo|ENOTFOUND|Temporary failure in name resolution"), lambda m: ("dns", "host not found")),
    ("network", re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLError|self.signed certificate", re.IGNORECASE), lambda m: ("ssl", "certificate problem")),
    ("network", re.compile(r"ETIMEDOUT|Read timed out|Connection timed out|timed out", re.IGNORECASE), lambda m: ("timeout", "timed out")),
    ("http", re.compile(r"HTTP Error (\d{3})|status(?: code)?[: ]+(\d{3})|\b(4\d\d|5\d\d) (?:Not Found|Forbidden|Unauthorized|Internal Server Error|Bad Gateway|Service Unavailable|Too Many Requests|Bad Request)"),
     lambda m: (f"http:{next(group for group in m.groups() if group)}", f"HTTP {next(group for group in m.groups() if group)}")),
    ("port", re.compile(r"Address already in use|EADDRINUSE|port \d+ is already (?:in use|allocated)", re.IGNORECASE), lambda m: ("in-use", "address already in use")),
    ("git", re.compile(r"fatal: not a git repository"), lambda m: ("not-repository", "not a git repository")),
    ("git", re.compile(r"CONFLICT \([\w/ ]+\): Merge conflict in"), lambda m: ("conflict", "merge conflict")),
    ("git", re.compile(r"error: failed to push some refs|\[rejected\]"), lambda m: ("push-rejected", "push rejected")),
    ("git", re.compile(r"Your local changes to the following files would be overwritten"), lambda m: ("dirty", "local changes would be overwritten")),
    ("git", re.compile(r"^fatal: (.+)$", re.MULTILINE), lambda m: ("fatal", m.group(1))),
    ("parse", re.compile(r"JSONDecodeError|Unexpected token .* in JSON|Unexpected end of JSON|is not valid JSON"), lambda m: ("json", "invalid JSON")),
    ("parse", re.compile(r"yaml\.(?:scanner|parser)\.\w+Error|could not find expected ':'|mapping values are not allowed"), lambda m: ("yaml", "invalid YAML")),
    ("parse", re.compile(r"TOMLDecodeError|tomllib"), lambda m: ("toml", "invalid TOML")),
    ("memory", re.compile(r"CUDA out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED", re.IGNORECASE), lambda m: ("gpu", "GPU out of memory")),
    ("memory", re.compile(r"\bMemoryError\b|\bKilled\b\s*$|std::bad_alloc", re.MULTILINE), lambda m: ("ram", "out of memory")),
    ("docker", re.compile(r"Cannot connect to the Docker daemon|docker: Error response from daemon: (.+)"), lambda m: ("daemon", m.group(1) if m.lastindex else "daemon not running")),
    ("compile", re.compile(r"^(?:\S+:\d+(?::\d+)?: )?error: (.+)$", re.MULTILINE), lambda m: ("error", m.group(1))),
]
EXIT_CODE = re.compile(r"Exit code (-?\d+)")


def template(text):
    return collapse(maskText(text), TEMPLATE_CHARS)


# The innermost frame of a Python traceback that is not in the library of Python: where the error came from, as a template.
def lastFrame(text):
    frames = FRAME.findall(text)
    own = [frame for frame in frames if "site-packages" not in frame[0] and "/lib/python" not in frame[0]] or frames
    if not own:
        return ""
    path, line, function = own[-1]
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{name}:{function}"


def pythonError(text):
    if not TRACEBACK.search(text) and not re.search(r"^\w+(?:Error|Exception): ", text, re.MULTILINE):
        return None
    matches = list(EXCEPTION_LINE.finditer(text[TRACEBACK.search(text).start():] if TRACEBACK.search(text) else text))
    if not matches:
        return None
    kind, message = matches[-1].group(1), matches[-1].group(2) or ""
    kind = kind.rsplit(".", 1)[-1]
    if kind == "ModuleNotFoundError":
        message = re.sub(r"'([\w.]+)'", lambda match: f"<{match.group(1).split('.')[0]}>", message)
    return {"family": "python", "code": f"py:{kind}", "template": template(message) if message else kind, "message": collapse(message, 300),
            "where": lastFrame(text)}


def swarmupError(text):
    for code, pattern in SWARMUP:
        match = pattern.search(text)
        if match:
            return {"family": "swarmup", "code": f"swarmup:{code}", "template": template(match.group(0)), "message": collapse(text, 300), "where": ""}
    return None


def classifyError(text, failed=True):
    text = str(text or "")
    if not text.strip():
        return None
    if text.lstrip().startswith("Not done:") or "The user refused" in text:
        found = swarmupError(text)
        if found:
            return found
    found = pythonError(text)
    if found:
        return found
    for family, pattern, build in RULES:
        match = pattern.search(text)
        if match:
            code, message = build(match)
            return {"family": family, "code": code, "template": template(message), "message": collapse(message, 300), "where": ""}
    found = swarmupError(text)
    if found:
        return found
    if not failed:
        return None
    code = EXIT_CODE.search(text)
    first = next((line for line in text.splitlines() if line.strip() and not line.startswith("Exit code")), text)
    return {"family": "exit" if code else "other", "code": f"exit:{code.group(1)}" if code else "other", "template": template(first), "message": collapse(first, 300), "where": ""}
