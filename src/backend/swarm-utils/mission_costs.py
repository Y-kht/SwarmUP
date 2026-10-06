import math
import threading
from datetime import datetime

from harness_utils import PRICE_REFRESH_SECONDS, describeError
from internet_cache import LONG_PRICE_FIELDS, getModelCost
from models_library import DEFAULT_CLI_MODEL


# ==============
# The cost of a mission. Every model client records each of its calls (recordCall in model_support.py): the tokens it read, read from the
# cache, wrote to the cache and wrote, or the exact cost when the model bills itself (Claude Code reports it). callCost prices a call the
# way the providers bill it: the tokens of the cache at their own price, a prompt over LONG_PROMPT tokens at the long price when the model
# has one, and DeepSeek at half its price outside its peak hours (see PRICE_NOTES). Local models are free, and Codex is paid by the ChatGPT
# plan of the user. A call of a model whose price is not published is counted in tokens, and never as free.
# The user can give the mission a budget, in US dollars. Each model of the team sets aside the price of 1 million of its tokens (the higher
# of reading and writing), the way a local model takes its VRAM. An agent that spent more counts with what it spent, and an agent that
# finished counts with what it really spent. What is left for more models is the budget, minus what the whole mission spent (the agents that
# left and the models that were replaced too), minus what the agents still at work set aside (MissionCosts.left).
# ==============
LONG_PROMPT = 200000
TOKENS_PRICED = 1000000
DEEPSEEK_PEAK_HOURS = ((1, 4), (6, 10))
BUDGET_WARNINGS = (0.8, 1.0)
PRICE_RETRY_SECONDS = 300
PRICE_FIELDS = ("input", "output", "cachedInput", *LONG_PRICE_FIELDS)


# Whether a call made at moment (UTC, written YYYY-MM-DDTHH:MM:SS) was in the peak hours of DeepSeek: weekdays, at the hours of PRICE_NOTES.
def isDeepseekPeak(moment):
    when = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%S")
    return when.weekday() < 5 and any(start <= when.hour < end for start, end in DEEPSEEK_PEAK_HOURS)


# What a model costs: kind is free (on the GPUs), plan (Codex with a ChatGPT plan), priced, or unknown (no published price, or not found).
# reference is the price of 1 million tokens, the higher of reading and writing, in US dollars (None if unknown). client tells how Codex
# is signed in: with a ChatGPT plan, or with an API key, which OpenAI bills per use.
def priceOfModel(info, client=None):
    if info["local"]:
        return {"kind": "free", "reference": 0.0}
    provider, name = info["provider"], info["name"]
    if info.get("cli") == "codex":
        if getattr(client, "accountType", "chatgpt") == "chatgpt":
            if "usage_based" in (getattr(client, "planType", "") or ""):
                return {"kind": "unknown", "reference": None, "error": "Your ChatGPT plan bills the use of Codex: its cost is on the bill of your plan."}
            return {"kind": "plan", "reference": 0.0}
        provider = "gpt"
    if name == DEFAULT_CLI_MODEL:
        return {"kind": "unknown", "reference": None, "error": "It chooses its own model, so its price is only known once it ran."}
    cost = getModelCost(provider, name)
    if "error" in cost:
        return {"kind": "unknown", "reference": None, "error": cost["error"]}
    return {"kind": "priced", "provider": provider, **{field: cost.get(field) for field in PRICE_FIELDS}, "reference": max(cost["input"], cost["output"])}


# The cost of one call in US dollars, or None if it cannot be known.
def callCost(record, price):
    if record.get("cost") is not None:
        return record["cost"]
    if price["kind"] in ("free", "plan"):
        return 0.0
    if price["kind"] != "priced" or record.get("missing"):
        return None
    long = record["input"] + record["cachedInput"] + record["cacheWrite"] > LONG_PROMPT
    def rate(field):
        special = price.get(f"{field}Long") if long else None
        return special if special is not None else price.get(field)
    reading, writing = rate("input"), rate("output")
    cached = rate("cachedInput") if rate("cachedInput") is not None else reading
    written = rate("cacheWrite") if rate("cacheWrite") is not None else reading
    dollars = (record["input"] * reading + record["cachedInput"] * cached + record["cacheWrite"] * written + record["output"] * writing) / TOKENS_PRICED
    return dollars / 2 if price.get("provider") == "deepseek" and not isDeepseekPeak(record["time"]) else dollars


def formatDollars(amount):
    if amount is None:
        return "unknown"
    return f"${amount:,.4f}" if 0 < amount < 0.01 else f"${amount:,.2f}"


def checkBudget(value):
    if value in (None, ""):
        return None
    try:
        budget = float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise ValueError("Write the budget as a number of US dollars, like 5 or 12.50.") from None
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("The budget is a number of US dollars above 0.")
    return round(budget, 2)


# What a saved mission had spent with one client before the program stopped: its calls, priced again with the prices of now.
class SpentBefore:
    def __init__(self, usage, accountType=None):
        self.usage = usage
        if accountType:
            self.accountType = accountType


# The spending of one mission: the clients of every model it used (track). Its state is saved with the swarm (state and restore): every
# call of every client, without anything secret, so a mission that goes on after a stop knows exactly what it spent before.
class MissionCosts:
    def __init__(self, budget=None):
        self.budget = checkBudget(budget)
        self.entries = []
        self.prices = {}
        self.fetching = set()
        self.warned = set()
        self.lock = threading.RLock()

    def setBudget(self, budget):
        with self.lock:
            self.budget = checkBudget(budget)
            self.warned = set()

    # A client the mission uses for the agent called agent. The same client is only counted once. Its price is fetched when a cost is asked for.
    def track(self, agent, info, client):
        if client is None or not info:
            return
        with self.lock:
            entry = next((entry for entry in self.entries if entry["client"] is client), None)
            if entry is None:
                self.entries.append({"agent": agent, "model": info, "client": client})
            else:
                entry.update(agent=agent, model=info)

    def rename(self, old, new):
        with self.lock:
            for entry in self.entries:
                if entry["agent"] == old:
                    entry["agent"] = new

    def priceKey(self, info, client):
        codex = (getattr(client, "accountType", "chatgpt"), getattr(client, "planType", "")) if info.get("cli") == "codex" else None
        return (info.get("provider"), info["name"], info.get("cli"), info["local"], codex)

    # The price of a model, kept for a while. Without wait, a price that is not known yet is fetched in another thread and None is returned.
    def priceOf(self, info, client=None, wait=True):
        key = self.priceKey(info, client)
        now = datetime.now().timestamp()
        with self.lock:
            known = self.prices.get(key)
            if known and now - known[1] < (PRICE_REFRESH_SECONDS if known[0]["kind"] != "unknown" else PRICE_RETRY_SECONDS):
                return known[0]
            if not wait:
                if key not in self.fetching:
                    self.fetching.add(key)
                    threading.Thread(target=self.fetchPrice, args=(key, info, client), daemon=True).start()
                return known[0] if known else None
        return self.fetchPrice(key, info, client)

    def fetchPrice(self, key, info, client):
        try:
            price = priceOfModel(info, client)
        except Exception as error:
            price = {"kind": "unknown", "reference": None, "error": describeError(error)}
        with self.lock:
            self.prices[key] = (price, datetime.now().timestamp())
            self.fetching.discard(key)
        return price

    # What the clients spent, agent by agent: {agent: {"calls", "input", "output", "cost", "unpriced" (tokens of the calls whose cost is not
    # known), "pending" (a price is being fetched), "kinds", "models"}}. Without wait the prices that are not known yet are fetched in the
    # background (for a window that shows the cost), and with it they are known first (for a decision about the budget).
    def spending(self, wait=False):
        rows = {}
        def row(agent):
            return rows.setdefault(agent, {"agent": agent, "calls": 0, "input": 0, "output": 0, "cost": 0.0, "unpriced": 0, "pending": False, "kinds": set(), "models": []})
        with self.lock:
            entries = list(self.entries)
        for entry in entries:
            usage = getattr(entry["client"], "usage", None) or {}
            current = row(entry["agent"])
            current["calls"] += usage.get("calls", 0)
            current["input"] += usage.get("input", 0)
            current["output"] += usage.get("output", 0)
            if entry["model"]["name"] not in current["models"]:
                current["models"].append(entry["model"]["name"])
            price = self.priceOf(entry["model"], entry["client"], wait=wait)
            current["kinds"].add(price["kind"] if price else "pending")
            for record in list(usage.get("records") or []):
                cost = record.get("billed")
                if cost is None:
                    cost = callCost(record, price) if price else record.get("cost")
                    # A call keeps the price it was billed at, even when the prices change later.
                    if price and cost is not None:
                        record["billed"] = cost
                if cost is None:
                    current["unpriced"] += record["input"] + record["cachedInput"] + record["cacheWrite"] + record["output"]
                    current["pending"] = current["pending"] or price is None
                else:
                    current["cost"] += cost
            if not usage.get("records") and price and price["kind"] not in ("free", "plan") and usage.get("calls"):
                current["unpriced"] += usage.get("input", 0) + usage.get("output", 0)
        return rows

    # team is [{"agent", "model", "finished"}]: the agents of the mission now. Without a budget, left is None.
    def left(self, team, rows=None, wait=False):
        rows = self.spending(wait) if rows is None else rows
        spent = sum(row["cost"] for row in rows.values())
        setAside, unknown = 0.0, []
        for member in team:
            if member["finished"] or not member["model"]:
                continue
            price = self.priceOf(member["model"], member.get("client"), wait=wait)
            if price is None or price["reference"] is None:
                unknown.append(member["agent"])
                continue
            setAside += max(0.0, price["reference"] - (rows[member["agent"]]["cost"] if member["agent"] in rows else 0.0))
        left = None if self.budget is None else round(self.budget - spent - setAside, 6)
        return {"budget": self.budget, "spent": round(spent, 6), "setAside": round(setAside, 6), "left": left, "unknown": unknown}

    def report(self, team=(), wait=False):
        rows = self.spending(wait)
        summary = self.left(team, rows, wait)
        agents = [{**row, "kinds": sorted(row["kinds"]), "cost": round(row["cost"], 6)} for row in rows.values() if row["calls"] or row["cost"]]
        return {**summary, "unpriced": sum(row["unpriced"] for row in rows.values()), "pending": any(row["pending"] for row in rows.values()), "agents": agents,
                "share": round(summary["spent"] / self.budget, 4) if self.budget else None}

    # The warnings the user has not had yet: when the mission spent 80% of its budget, and when it spent all of it.
    def warnings(self):
        if self.budget is None:
            return []
        spent = sum(row["cost"] for row in self.spending(wait=True).values())
        found = []
        with self.lock:
            for level in BUDGET_WARNINGS:
                if spent >= self.budget * level and level not in self.warned:
                    self.warned.add(level)
                    found.append(f"The mission spent {formatDollars(spent)}, {'all' if level >= 1 else f'{round(level * 100)}%'} of its budget of {formatDollars(self.budget)}. "
                                 + ("Nothing is stopped by itself: remove agents, or stop the swarm, if it must not spend more." if level >= 1 else
                                    "The leader and you can remove agents that are not needed anymore."))
        return found

    def state(self):
        with self.lock:
            entries = list(self.entries)
        clients = []
        for entry in entries:
            usage = getattr(entry["client"], "usage", None) or {}
            if usage.get("calls"):
                clients.append({"agent": entry["agent"], "model": entry["model"], "accountType": getattr(entry["client"], "accountType", None),
                                "usage": {"calls": usage["calls"], "input": usage.get("input", 0), "output": usage.get("output", 0), "records": list(usage.get("records") or [])}})
        return {"budget": self.budget, "clients": clients}

    def restore(self, saved):
        with self.lock:
            self.budget = saved.get("budget")
            for item in saved.get("clients") or []:
                self.entries.append({"agent": item["agent"], "model": item["model"], "client": SpentBefore(dict(item["usage"]), item.get("accountType"))})
