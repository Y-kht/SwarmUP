# What every model client needs: the record of its calls, its errors, the packages it needs, its API key, and what Hugging Face says
# about a model that is not in the list of the library.
import importlib.util
import json
import os
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from harness_utils import ConnectionLost, describeError, fetchUrl
from internet_cache import remember
from models_library import API_KEYS, MODELS_CLI


API_LIBRARIES = {"claude": "anthropic", "gpt": "openai", "gemini": "openai", "deepseek": "openai"}


# One call of a model, for the cost of the mission (MissionCosts in mission_costs.py): the tokens it read (input, without the cache), read from
# the cache (cachedInput), wrote to the cache (cacheWrite) and wrote (output, thinking included). cost is the exact cost when the model bills
# itself (Claude Code), and missing says the provider did not tell the tokens (the call is then never counted as free). The totals are what
# the table of the tokens shows. The time is in UTC, because DeepSeek bills by the hour of the day.
def recordCall(usage, input=0, cachedInput=0, cacheWrite=0, output=0, cost=None, missing=False):
    input, cachedInput, cacheWrite, output = (max(0, int(value or 0)) for value in (input, cachedInput, cacheWrite, output))
    usage["calls"] += 1
    usage["input"] += input + cachedInput + cacheWrite
    usage["output"] += output
    usage.setdefault("records", []).append({"time": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S}", "input": input, "cachedInput": cachedInput,
                                            "cacheWrite": cacheWrite, "output": output, "cost": cost, "missing": missing})


# An error whose message is written for the user. The agent that meets it fails, and its message is shown to the user.
class ModelError(Exception):
    pass


# The model cannot be reached because the connection is lost. A swarm pauses the agent instead of failing it (see ConnectionLost).
class ModelConnectionError(ModelError, ConnectionLost):
    pass


def importLibrary(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        raise ModelError(f"The {name} package is needed for this model. Install it with: pip install -U {name}") from None


# The packages a chosen model needs and the computer does not have, for the user to know before the swarm runs.
# Codex is a program and not a package: checkCodex says if it is installed.
def findMissingPackages(info):
    if info.get("cli"):
        package = MODELS_CLI[info["cli"]]["package"]
        return [package] if package and importlib.util.find_spec(package.replace("-", "_")) is None else []
    if info["local"]:
        needed = ["torch", "transformers", "accelerate"] + (["bitsandbytes"] if info["bits"] < 16 else [])
    else:
        needed = [API_LIBRARIES[info["provider"]]]
    return [name for name in needed if importlib.util.find_spec(name) is None]


def getApiKey(provider):
    return os.environ.get(API_KEYS[provider]["variable"], "").strip()


def getHubFolder():
    return Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or "~/.cache/huggingface").expanduser() / "hub")


def isDownloaded(name):
    return (getHubFolder() / f"models--{name.replace('/', '--')}").exists()


# What Hugging Face says about a model that is not in the list of the library: its number of parameters in billions (None if it does not
# say) and whether it is gated. It raises a ModelError, with a message for the user, if the model cannot be found. The answer is kept a week
# (see remember in internet_cache.py), and given from the last connection without internet.
HUGGING_FACE_REFRESH_SECONDS = 7 * 24 * 3600


def readHuggingFace(name):
    data = json.loads(fetchUrl(f"https://huggingface.co/api/models/{name}"))
    if not isinstance(data, dict):
        raise ValueError("Hugging Face gave an answer that cannot be read")
    total = (data.get("safetensors") or {}).get("total")
    return {"billions": round(total / 1000000000, 1) if total else None, "gated": bool(data.get("gated"))}


def lookupHuggingFace(name):
    try:
        return remember(f"huggingface-{name.strip().lower()}", lambda: readHuggingFace(name), HUGGING_FACE_REFRESH_SECONDS, errors=(OSError, ValueError))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ModelError(f"There is no model called {name} on Hugging Face. Check how it is written.") from None
        raise ModelError(f"Hugging Face answered with error {error.code} for {name}. A private model needs a token.") from None
    except (OSError, ValueError) as error:
        raise ModelError(f"Hugging Face could not be asked: {describeError(error)}.") from None
