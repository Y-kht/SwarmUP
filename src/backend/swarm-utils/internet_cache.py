# The information SwarmUP takes from the internet to show it (the prices of the models first), kept for when the connection is lost.
import json
import os
import re
import threading
import traceback
from datetime import datetime

from harness_utils import AGENT_FILES, FETCH_ERRORS, PRICE_REFRESH_SECONDS, describeError, fetchUrl
from models_library import PRICE_NOTES, PRICING_PAGES


MODEL_PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICE_PROVIDERS = {"gpt": "openai", "claude": "anthropic", "gemini": "gemini", "deepseek": "deepseek"}

PRICE_FILE_LIMIT = 20000000


# ==============
# What SwarmUP takes from the internet to show it (the prices of the models, what Hugging Face says about a model, the models of Codex, the
# publishers of Crossref) is kept in agent-files/internet-cache, with the time it was fetched. remember fetches it again once it is older than
# its age limit, and keeps what it had when the internet does not answer: SwarmUP works offline with the information of the last connection,
# and describeCached says from when it is. A failed fetch is only tried again after CACHE_RETRY_SECONDS, so an offline program does not wait for
# the internet at every step. keepFresh refreshes the prices in the background while the program runs, so they are always up to date.
# What the agents fetch for their work (news, papers, messages) is never kept here: it must be new, and a lost connection pauses the agent.
# ==============
CACHE_FOLDER = "internet-cache"
CACHE_RETRY_SECONDS = 60
REFRESH_CHECK_SECONDS = 600
internetCache = {}
CACHE_LOCK = threading.Lock()
refresher = {"thread": None}


def cachePath(name):
    return AGENT_FILES / CACHE_FOLDER / f"{re.sub(r'[^A-Za-z0-9._-]', '_', name)}.json"


def readCacheFile(name):
    try:
        data = json.loads(cachePath(name).read_text(encoding="utf-8"))
        return {"value": data["value"], "fetchedAt": float(data["fetchedAt"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


# The file is written under another name and then renamed, so a stop in the middle never leaves a broken file. A cache that cannot be written
# only means the information is fetched again at the next start.
def writeCacheFile(name, value, fetchedAt):
    path = cachePath(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps({"fetchedAt": fetchedAt, "value": value}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def cacheEntry(name):
    entry = internetCache.get(name)
    if entry is None:
        entry = internetCache[name] = readCacheFile(name) or {}
    return entry


# The information called name: kept if it is younger than maxAge seconds, otherwise fetched again with fetch(). If fetch fails with one of
# errors, the last information is given; without any, the error goes up.
def remember(name, fetch, maxAge, errors=FETCH_ERRORS):
    now = datetime.now().timestamp()
    with CACHE_LOCK:
        entry = cacheEntry(name)
        if "value" in entry and (now - entry["fetchedAt"] < maxAge or now - entry.get("failedAt", 0) < CACHE_RETRY_SECONDS):
            return entry["value"]
    try:
        value = fetch()
    except errors as error:
        with CACHE_LOCK:
            entry.update(failedAt=now, error=describeError(error))
            if "value" in entry:
                return entry["value"]
        raise
    with CACHE_LOCK:
        entry.update(value=value, fetchedAt=now, failedAt=0, error="")
    writeCacheFile(name, value, now)
    return value


# From when the information called name is, for the user: {"fetchedAt": "YYYY-MM-DD HH:MM" or None, "offline": the last try to renew it
# failed, so this is the information of the last connection, "error": why}.
def describeCached(name):
    with CACHE_LOCK:
        entry = cacheEntry(name)
    fetched = entry.get("fetchedAt")
    offline = bool(entry.get("failedAt") and (not fetched or entry["failedAt"] > fetched))
    return {"fetchedAt": f"{datetime.fromtimestamp(fetched):%Y-%m-%d %H:%M}" if fetched else None, "offline": offline, "error": entry.get("error", "") if offline else ""}


# Started once by the program (the window or the command line): the price list is renewed as soon as it is an hour old, while the program runs,
# until stop is set.
def keepFresh(stop=None):
    if refresher["thread"] is not None:
        return
    stop = stop or threading.Event()
    def refresh():
        while True:
            try:
                loadModelPrices()
            except FETCH_ERRORS:
                pass
            except Exception:
                traceback.print_exc()
            if stop.wait(REFRESH_CHECK_SECONDS):
                return
    refresher["thread"] = threading.Thread(target=refresh, daemon=True, name="keep-fresh")
    refresher["thread"].start()


# The other prices a provider bills: writing to the cache, and the prompts over LONG_PROMPT tokens, for the models that price them apart.
LONG_PRICE_FIELDS = {"cacheWrite": "cache_creation_input_token_cost", "inputLong": "input_cost_per_token_above_200k_tokens",
                     "outputLong": "output_cost_per_token_above_200k_tokens", "cachedInputLong": "cache_read_input_token_cost_above_200k_tokens",
                     "cacheWriteLong": "cache_creation_input_token_cost_above_200k_tokens"}


def perMillion(cost):
    return round(cost * 1000000, 4) if cost is not None else None


# The prices of all the API models, in US dollars per 1 million tokens, from a list kept up to date by the community (LiteLLM).
# For the models checked it agrees with the pages of the providers. The list is downloaded again once it is an hour old (see remember),
# and without internet the list of the last connection is used, even after the program started again.
def fetchModelPrices():
    entries = json.loads(fetchUrl(MODEL_PRICES_URL, limit=PRICE_FILE_LIMIT))
    if not isinstance(entries, dict):
        raise ValueError("the price list has an unexpected format")
    prices = {}
    for name, entry in entries.items():
        if isinstance(entry, dict) and all(isinstance(entry.get(field), (int, float)) for field in ("input_cost_per_token", "output_cost_per_token")):
            prices[name] = {"input": perMillion(entry["input_cost_per_token"]), "output": perMillion(entry["output_cost_per_token"]),
                            "cachedInput": perMillion(entry.get("cache_read_input_token_cost")), "context": entry.get("max_input_tokens"),
                            **{key: perMillion(entry.get(field)) if isinstance(entry.get(field), (int, float)) else None for key, field in LONG_PRICE_FIELDS.items()}}
    return prices


def loadModelPrices():
    return remember("model-prices", fetchModelPrices, PRICE_REFRESH_SECONDS)


# What the info button of a model shows. It never fails: if the price is not known, it says why and still gives the official page.
def getModelCost(provider, model):
    page = PRICING_PAGES.get(provider, "")
    try:
        prices = loadModelPrices()
    except FETCH_ERRORS as error:
        return {"model": model, "error": f"The price could not be fetched: {describeError(error)}.", "page": page}
    price = prices.get(model) or prices.get(f"{PRICE_PROVIDERS.get(provider, provider)}/{model}")
    if not price:
        return {"model": model, "error": "No price is published for this model yet.", "page": page}
    return {"model": model, **price, "unit": "US dollars per 1 million tokens", "note": PRICE_NOTES.get(provider, ""), "page": page}
