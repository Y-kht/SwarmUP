# What an agent sees of the memory (mission_memory.py) with its prompt, as Claude Code does with its memory: the indexes always, the files that
# matter most in full or in part, and the others by name only, to read when the agent needs them. Each file is ranked by what is known of its use:
# how often the agents of the mission read and wrote it, how lately, and whether it is the agent's own. The text is written in a stable order
# (the rules, the long-term memory, then the mission), so it stays the same from one request to the next and the providers keep it in their cache.
import time

import agent_prompts as prompts
from mission_memory import (LONG_TERM_INDEX, MISSION_INDEX, OUTPUTS_FOLDER, PLANS, PROGRESS, STATE_FOLDER, USER_PROMPTS, agentFileName, folderLock,
                            memoryFiles, readJson, readText, writeJson)


MEMORY_CHARS = 24000
LONG_TERM_CHARS = 8000
FILE_CHARS = 4000
PINNED_CHARS = 3000
DAY_SECONDS = 24 * 3600


# ---------- What is known of the use of each file. ----------
def statsPath(folder):
    return folder / STATE_FOLDER / "cache.json"


def noteUse(folder, relative, agent, kind="read"):
    with folderLock(folder):
        stats = readJson(statsPath(folder), {})
        entry = stats.setdefault(relative, {"read": 0, "write": 0, "last": 0, "by": {}})
        entry[kind] = entry.get(kind, 0) + 1
        entry["last"] = time.time()
        entry["by"][agent] = entry["by"].get(agent, 0) + 1
        writeJson(statsPath(folder), stats)


# Higher is more useful to this agent: its own files, the files used often, and lately.
def score(relative, path, stats, agent, owners):
    entry = stats.get(relative, {})
    uses = entry.get("read", 0) + 2 * entry.get("write", 0) + 3 * entry.get("by", {}).get(agent, 0)
    last = max(entry.get("last", 0), path.stat().st_mtime)
    freshness = 1 / (1 + (time.time() - last) / DAY_SECONDS)
    return (5 if owners.get(relative) == agent else 0) + uses + 10 * freshness


def tail(text, limit, where):
    return text if len(text) <= limit else f"[... the start is left out: read {where} for all of it]\n" + text[-limit:]


def head(text, limit, where):
    return text if len(text) <= limit else text[:limit] + f"\n[... the rest is left out: read {where} for all of it]"


# ---------- The text for the prompt. ----------
# The long-term memory in full while it is short (it is chosen with care by the leaders), otherwise its index and the start of each file.
def describeLongTerm(memory):
    files = memoryFiles(memory.longTerm, LONG_TERM_INDEX)
    if not files:
        return "Nothing yet."
    parts, used = [], 0
    for relative, path in files:
        text = head(readText(path).strip(), FILE_CHARS, f"@long-term/{relative}")
        if used + len(text) > LONG_TERM_CHARS:
            parts.append(f"(More files, by name only: {', '.join(name for name, _ in files[len(parts):])}. Read them with read_file @long-term/<name>.)")
            break
        parts.append(f"--- @long-term/{relative} ---\n{text}")
        used += len(text)
    return "\n\n".join(parts)


# The files of the mission that are shown in part to every agent: where the mission stands, what the user asked lately, and its own work.
def pinnedFiles(memory, agent):
    return [PROGRESS, USER_PROMPTS, memory.notesName(agent), f"{OUTPUTS_FOLDER}/{agentFileName(agent, 'OUTPUTS')}"]


def describeMission(memory, agent, budget):
    files = dict(memoryFiles(memory.folder, MISSION_INDEX))
    parts, used = [], 0
    for relative in pinnedFiles(memory, agent):
        if relative in files:
            text = tail(readText(files.pop(relative)).strip(), PINNED_CHARS, f"@memory/{relative}")
            parts.append(f"--- @memory/{relative} (the latest part) ---\n{text}")
            used += len(text)
    stats, owners = readJson(statsPath(memory.folder), {}), memory.owners()
    ranked = sorted(files.items(), key=lambda item: score(item[0], item[1], stats, agent, owners), reverse=True)
    left = []
    for relative, path in ranked:
        text = tail(readText(path).strip(), FILE_CHARS, f"@memory/{relative}")
        if used + len(text) > budget:
            left.append(relative)
            continue
        parts.append(f"--- @memory/{relative} ---\n{text}")
        used += len(text)
    if left:
        parts.append("Not shown here (read them with read_file when you need them): " + ", ".join(f"@memory/{name}" for name in sorted(left)))
    return "\n\n".join(parts) or "Nothing yet."


# Everything an agent knows of the memory and of the temp folder at the start of its conversation. how says how it reaches them: with its tools
# (by default), with requests in its answer for a model asked one prompt at a time (agent_storehouse.py), or as a coding agent.
def describeMemory(memory, agent, budget=MEMORY_CHARS, how=None):
    leader = agent == memory.leader
    rules = prompts.MEMORY_PROMPT.format(temp=memory.temp, memory=memory.folder, longTerm=memory.longTerm,
                                         role=prompts.MEMORY_LEADER_RIGHTS if leader else prompts.MEMORY_AGENT_RIGHTS,
                                         how=how or prompts.MEMORY_TOOLS_HOW)
    longTerm = describeLongTerm(memory)
    mission = describeMission(memory, agent, max(budget - len(rules) - len(longTerm), PINNED_CHARS))
    return (f"{rules}\n\nTHE LONG-TERM MEMORY (for every swarm of the user)\n{longTerm}\n\n"
            f"THE MEMORY OF THIS MISSION\n{memory.index().strip()}\n\n{mission}")
