# An entry of a journal as the miner and the architect read it. SwarmUP writes the journal (agent_journal.py), but what its entries hold comes
# from the models (the calls they wrote, their arguments, their texts), from programs outside SwarmUP (a coding agent, an MCP server), from older
# versions of SwarmUP, and from lines that were cut to fit. So an entry is read like anything else that comes from outside: each field the miner
# uses has its kind here. A field of another kind is read as its kind when nothing is lost (12 for "12"), and is otherwise left out, like a
# field that was never written: one odd field never costs the rest of its entry, and never stops the miner.
import json
import math


TEXTS = ("kind", "agent", "mission", "run", "project", "origin", "server", "tool", "outcome", "result", "text", "prompt", "name", "why", "problem", "how", "key",
         "decision", "action", "detail", "reply", "message", "comment", "from", "to", "status", "command", "output", "trace", "pin", "id", "news", "request")
AMOUNTS = ("seconds", "size", "length")
NUMBERS = ("steps", "n", "exit", "round", "seq")
# The time of an entry, in milliseconds: between the years 2000 and 2100, or it is not a time.
FIRST_TIME, LAST_TIME = 946684800000, 4102444800000


def textOf(value):
    if isinstance(value, str):
        return value
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def numberOf(value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def namesOf(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def keep(entry, key, value):
    if value is None:
        entry.pop(key, None)
    else:
        entry[key] = value


# What went wrong with a wheel is {"type", "message"}; what went wrong with anything else is a text.
def errorOf(kind, value):
    if kind != "wheel":
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str) if value else None
    if isinstance(value, dict):
        return {**value, "type": textOf(value.get("type")) or "?", "message": textOf(value.get("message")) or ""}
    return {"type": "?", "message": textOf(value) or ""} if value else None


# The entry with every field the miner uses in its kind, or None for what is not an entry at all.
def cleanEntry(entry):
    if not isinstance(entry, dict):
        return None
    entry = dict(entry)
    for key in TEXTS:
        if key in entry:
            keep(entry, key, textOf(entry[key]))
    for key in (*AMOUNTS, *NUMBERS, "t"):
        if key in entry:
            keep(entry, key, numberOf(entry[key]))
    if any(entry.get(key, 0) < 0 for key in AMOUNTS):
        entry = {key: value for key, value in entry.items() if key not in AMOUNTS or value >= 0}
    if not FIRST_TIME <= entry.get("t", FIRST_TIME) <= LAST_TIME:
        del entry["t"]
    if "tokens" in entry:
        tokens = entry["tokens"]
        keep(entry, "tokens", {key: max(numberOf(tokens.get(key)) or 0, 0) for key in ("in", "out")} if isinstance(tokens, dict) else None)
    if "calls" in entry:
        calls = entry["calls"] if isinstance(entry["calls"], list) else []
        entry["calls"] = [{"id": textOf(call.get("id")), "name": textOf(call.get("name")) or "", "args": call.get("args")} for call in calls if isinstance(call, dict)]
    if "agents" in entry:
        agents = entry["agents"] if isinstance(entry["agents"], list) else []
        entry["agents"] = [{**agent, "name": textOf(agent.get("name")) or "", "wheels": namesOf(agent.get("wheels"))} for agent in agents if isinstance(agent, dict)]
    if "wheels" in entry:
        entry["wheels"] = namesOf(entry["wheels"])
    if "error" in entry:
        keep(entry, "error", errorOf(entry.get("kind"), entry["error"]))
    return entry


# One line of a journal as an entry, or None for a line that is not one (half written, cut, or not JSON).
def readEntry(line):
    try:
        return cleanEntry(json.loads(line))
    except (ValueError, RecursionError):
        return None
