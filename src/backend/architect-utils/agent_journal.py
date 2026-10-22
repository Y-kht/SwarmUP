# The journal of a mission: one line of JSON for everything its agents do, in the order it happens, in .swarmup/journal.jsonl. Every model
# step (what the model wrote and the calls it made), every call through the gateway (from every kind of agent, coding agents included), the
# calls that never ran, the permissions, the drafts and their checks, the corrections of the user and of the leader, the messages and the
# changes of state. The miner of patterns (pattern_miner.py) reads it while the swarm works, so the architect learns what repeats.
# A line is written whole and at once (O_APPEND), several threads and programs can write the same journal, and a secret is never written.
# The journal is only a record: a problem with it never stops an agent, and it is deleted with the mission.
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from mcp_records import maskValue


JOURNAL_FILE = "journal.jsonl"
VERSION = 1
ENTRY_CHARS = 16000
LITE_BYTES = 256 * 1024 * 1024
STOP_BYTES = 512 * 1024 * 1024
LITE_CHARS = 300
ARGUMENT_CHARS = 2000
CONTENT_CHARS = 300
# Arguments that carry the content of a file: the journal keeps their size, a hash and their beginning.
CONTENT_KEYS = ("content", "new_text", "old_text", "text", "code", "body")
SECRET_PATTERN = re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[\w-]{10,}|"
                            r"hf_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})")
SECRET_ASSIGNMENT = re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}")
# The events of the swarm that the journal keeps, by the kind they get in it.
EVENT_KINDS = {"round": "round", "stopped": "stop", "resumed": "resume", "status": "status", "message": "message", "summary": "summary",
               "connectionLost": "lost", "joined": "join", "removed": "leave", "retired": "leave", "model": "model"}


def clip(text, head, tail=0):
    text = "" if text is None else str(text)
    if len(text) <= head + tail:
        return text
    return text[:head] + f" [... {len(text) - head - tail:,} characters ...] " + (text[-tail:] if tail else "")


def hashOf(text):
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:12]


def maskSecrets(value, secrets=()):
    if isinstance(value, str):
        value = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}***", SECRET_PATTERN.sub("***", value))
    return maskValue(value, secrets) if secrets else value


def maskAll(value, secrets):
    if isinstance(value, str):
        return maskSecrets(value, secrets)
    if isinstance(value, dict):
        return {key: maskAll(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [maskAll(item, secrets) for item in value]
    return value


# The arguments of a call as the journal keeps them: the content of a file is replaced by its size, hash and beginning.
def shapeArguments(arguments):
    if not isinstance(arguments, dict):
        return clip(arguments, ARGUMENT_CHARS)
    kept = {}
    for key, value in arguments.items():
        if isinstance(value, str) and key in CONTENT_KEYS and len(value) > CONTENT_CHARS:
            kept[key] = {"len": len(value), "sha1": hashOf(value), "head": value[:CONTENT_CHARS]}
        elif isinstance(value, str):
            kept[key] = clip(value, ARGUMENT_CHARS)
        elif isinstance(value, (list, dict)):
            dumped = json.dumps(value, ensure_ascii=False, default=str)
            kept[key] = value if len(dumped) <= ARGUMENT_CHARS else clip(dumped, ARGUMENT_CHARS)
        else:
            kept[key] = value
    return kept


# The longest texts of an entry are cut until its line fits ENTRY_CHARS.
def fit(entry):
    line = json.dumps(entry, ensure_ascii=False, default=str)
    while len(line) > ENTRY_CHARS:
        texts = [(len(value), key) for key, value in entry.items() if isinstance(value, str)]
        if not texts:
            entry = {key: value for key, value in entry.items() if not isinstance(value, (dict, list))} | {"cut": True}
            return json.dumps(entry, ensure_ascii=False, default=str)
        size, key = max(texts)
        entry[key] = clip(entry[key], max(100, size // 2 - 50))
        line = json.dumps(entry, ensure_ascii=False, default=str)
    return line


def secretsOf(loop):
    return {str(value) for value in (getattr(loop, "settings", None) or {}).values() if value and len(str(value)) >= 4}


class Journal:
    # folder: the .swarmup folder of the mission. onProblem(text) tells the user once that the journal cannot be written.
    def __init__(self, folder, mission, project="", run="", onProblem=None):
        self.path = Path(folder) / JOURNAL_FILE
        self.mission, self.project, self.run, self.round = mission, project, run, 1
        self.onProblem, self.swarm = onProblem, None
        self.lock = threading.Lock()
        self.seq, self.problems, self.written = 0, 0, 0
        self.lite = self.stopped = False
        self.size = self.path.stat().st_size if self.path.exists() else 0

    def add(self, kind, agent="", loop=None, secrets=(), **fields):
        if self.stopped:
            return
        try:
            session = getattr(loop, "session", None)
            with self.lock:
                self.seq += 1
                entry = {"v": VERSION, "t": int(time.time() * 1000), "seq": self.seq, "pid": os.getpid(), "mission": self.mission, "run": self.run,
                         "round": getattr(session, "round", None) or self.round, "project": self.project, "agent": agent, "kind": kind}
            known = set(secrets) | (secretsOf(loop) if loop is not None else set())
            for key, value in fields.items():
                entry[key] = maskAll(value, known)
                if self.lite and isinstance(entry[key], str):
                    entry[key] = clip(entry[key], LITE_CHARS)
            self.write(fit(entry) + "\n")
        except Exception as error:
            self.problem(error)

    def write(self, line):
        data = line.encode("utf-8", "replace")
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(handle, data)
            finally:
                os.close(handle)
            self.size += len(data)
            self.written += 1
            if self.size > STOP_BYTES:
                self.stopped = True
            elif self.size > LITE_BYTES:
                self.lite = True
        if self.stopped:
            self.tell(f"The journal of this mission passed {STOP_BYTES // (1024 * 1024)} MB, so it stops growing. The architect works with what it has.")

    def problem(self, error):
        self.problems += 1
        if self.problems == 1:
            self.tell(f"The journal of this mission cannot be written ({error}), so the architect learns less from this run. The work goes on.")

    def tell(self, text):
        if self.onProblem is not None:
            try:
                self.onProblem(text)
            except Exception:
                pass

    # One call through the gateway, from any agent: what the model asked, how it ended, and the beginning and end of what it got back.
    def call(self, loop, callId, origin, server, tool, arguments, outcome, seconds, text, trusted=True, secrets=()):
        result = clip(text, 600, 400) if outcome == "success" else clip(text, 800, 2400)
        self.add("call", loop.name, loop=loop, secrets=secrets, id=callId, origin=origin, server=server, tool=tool, args=shapeArguments(arguments),
                 outcome=outcome, seconds=round(seconds, 3), size=len(text or ""), result=result, trusted=trusted)

    # A listener of the swarm (Swarm.addListener): the changes of state, the messages, the rounds and the stops.
    def watch(self, swarm):
        self.swarm = swarm
        swarm.addListener(self.onEvent)

    def onEvent(self, event):
        kind = EVENT_KINDS.get(event.get("kind"))
        if kind is None or self.swarm is not None and self.swarm.journal is not self:
            return
        agent = event.get("agent", "")
        details = {key: value for key, value in event.items() if key not in ("kind", "agent", "time")}
        if kind == "round":
            self.round = event.get("round") or self.round
            details["request"] = clip(details.get("request"), 1500, 500)
        elif kind == "status" and event.get("status") == "failed" and self.swarm is not None:
            details["error"] = clip((self.swarm.members.get(agent) or {}).get("error"), 1500)
        elif kind == "message":
            limit = 1500 if event.get("direct") else 300
            details = {"from": event.get("sender"), "to": event.get("receiver"), "text": clip(event.get("message"), limit), "direct": bool(event.get("direct")),
                       "toLeader": bool(event.get("toLeader"))}
        elif kind == "summary":
            details["text"] = clip(details.get("text"), 1500)
        self.add(kind, agent, **details)
