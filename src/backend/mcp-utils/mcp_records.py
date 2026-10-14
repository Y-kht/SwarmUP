# The records of the calls of a mission (docs/MCP_DESIGN_BOOKLET.md, OBS-01 to OBS-04): every call that goes through the gateway is written
# down, with its time, its agent, its server and tool, its arguments, how long it took, how large its result was and how it ended. They are in
# the memory of the mission (.swarmup/calls.jsonl, so deleting the mission deletes them), the leader reads them as a resource, and the
# window and the command line show them. No secret is ever written: every secret the gateway knows is replaced by ***.
import json
import threading
from datetime import datetime


CALLS_FILE = "calls.jsonl"
OUTCOMES = ("success", "error", "refused", "cancelled", "timeout")
MASK = "***"
SHOWN_CHARS = 300
KEPT_IN_MEMORY = 500


def maskText(text, secrets):
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, MASK)
    return text


def maskValue(value, secrets):
    if isinstance(value, str):
        return maskText(value, secrets)
    if isinstance(value, dict):
        return {key: maskValue(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [maskValue(item, secrets) for item in value]
    return value


def shorten(value):
    if isinstance(value, str) and len(value) > SHOWN_CHARS:
        return value[:SHOWN_CHARS] + f"... ({len(value):,} characters)"
    if isinstance(value, dict):
        return {key: shorten(item) for key, item in value.items()}
    return value


class CallRecords:
    # folder: where the records of the mission are kept (None for an agent that works alone: they are only kept in memory).
    def __init__(self, folder=None):
        self.folder = folder
        self.lock = threading.Lock()
        self.recent = []
        self.counts = {}

    @property
    def path(self):
        return self.folder / CALLS_FILE if self.folder else None

    def add(self, agent, server, tool, arguments, outcome, seconds, size, round=1, cost=0.0, secrets=(), note=""):
        entry = {"time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "round": round, "agent": agent, "server": server, "tool": tool,
                 "arguments": shorten(maskValue(arguments or {}, secrets)), "outcome": outcome, "seconds": round_(seconds), "size": size,
                 "cost": cost, "note": maskText(note, secrets)[:SHOWN_CHARS]}
        with self.lock:
            self.recent = [*self.recent[-(KEPT_IN_MEMORY - 1):], entry]
            count = self.counts.setdefault(agent, {"calls": 0, "errors": 0, "refused": 0, "cancelled": 0, "timeout": 0, "seconds": 0.0, "cost": 0.0})
            count["calls"] += 1
            if outcome != "success":
                count["errors" if outcome == "error" else outcome] += 1
            count["seconds"] = round_(count["seconds"] + seconds)
            count["cost"] += cost
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as file:
                    file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def read(self, agent=None, server=None, limit=200):
        with self.lock:
            entries = self.load()
        chosen = [entry for entry in entries if (not agent or entry["agent"] == agent) and (not server or entry["server"] == server)]
        return chosen[-limit:]

    def load(self):
        if not (self.path and self.path.exists()):
            return list(self.recent)
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def countsOf(self):
        if self.counts or not (self.path and self.path.exists()):
            return {agent: dict(count) for agent, count in self.counts.items()}
        counts = {}
        for entry in self.load():
            count = counts.setdefault(entry["agent"], {"calls": 0, "errors": 0, "refused": 0, "cancelled": 0, "timeout": 0, "seconds": 0.0, "cost": 0.0})
            count["calls"] += 1
            if entry["outcome"] != "success":
                count["errors" if entry["outcome"] == "error" else entry["outcome"]] += 1
            count["seconds"] = round_(count["seconds"] + entry.get("seconds", 0))
        return counts


def round_(value):
    return round(float(value or 0), 3)


def describeRecords(entries):
    if not entries:
        return "No call yet."
    lines = []
    for entry in entries:
        arguments = ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)[:80]}" for key, value in (entry.get("arguments") or {}).items())
        mark = "" if entry["outcome"] == "success" else f" [{entry['outcome']}]"
        lines.append(f"{entry['time'][11:]} {entry['agent']}: {entry['server']}__{entry['tool']}({arguments}) {entry['seconds']}s{mark}" + (f" {entry['note']}" if entry.get("note") and mark else ""))
    return "\n".join(lines)
