# We define local HF models of Qwen, Llama, Kimi, etc. (as many as possible).
# We define the API based models of OpenAI, Claude, Gemini, and DeepSeek.
# The API keys (if any) will be entered externally by the user. They will be
# guided how to do that.
# This will simply be a list. If the user doesn't find what they want
# in the list, they can still manually enter the full model name (e.g., from HF).
# The user will see next to the model's name the expected VRAM required to run the model locally.
# Their devices VRAM will be checked first. If it cannot host the model,
# they will be asked to choose a smaller model or change to an API hosted model.
# getModelInfo gives the VRAM of a chosen model, and harness_utils.py reads the GPUs (readGpus) and checks what they can host
# (checkVram). A Swarm adds up the VRAM of the models of its agents, so the swarm as a whole must fit in the GPUs.
# Every name below was checked on 3 October 2026: the local ones on huggingface.co (tests/live_checks.py checks them again)
# and the API ones in the documentation of their provider. Models are replaced quickly, so expect to update the lists.

VRAM_OVERHEAD = 1.2
BITS = (16, 8, 4)
# The Llama models and Gemma 3 are gated on Hugging Face: the user must accept their license on the page of the model
# and give a Hugging Face token (the HF_TOKEN environment variable) before they can be downloaded.
GATED_PREFIXES = ("meta-llama/", "google/gemma-3")

# Local models by family: the name on Hugging Face and its number of parameters in billions.
# Mixture-of-experts models (like Qwen3.5-35B-A3B) only use a part of their parameters at a time,
# but all of them must be loaded, so the total is what counts for the memory.
# The Llama models and Gemma 3 are gated: the user must first accept their license on the Hugging Face page of the model.
MODELS_LOCAL = {
    "qwen": {
        "Qwen/Qwen3.5-0.8B": 0.9,
        "Qwen/Qwen3.5-2B": 2.3,
        "Qwen/Qwen3.5-4B": 4.7,
        "Qwen/Qwen3.5-9B": 9.7,
        "Qwen/Qwen3.5-27B": 27.8,
        "Qwen/Qwen3.5-35B-A3B": 36.0,
        "Qwen/Qwen3.6-27B": 27.8,
        "Qwen/Qwen3.6-35B-A3B": 36.0,
        "Qwen/Qwen3.8-27B": 27.8,
        "Qwen/Qwen3.8-Flash-Next": 180.0,
        "Qwen/Qwen3.8-2.4T-A95B": 2446.2,
        "Qwen/Qwen3-8B": 8.2,
        "Qwen/Qwen3-14B": 14.8,
        "Qwen/Qwen3-32B": 32.8,
        "Qwen/Qwen3-Coder-30B-A3B-Instruct": 30.5,
        "Qwen/Qwen3-Coder-Next": 79.7,
        "Qwen/Qwen3-Next-80B-A3B-Instruct": 81.3,
    },
    "llama": {
        "meta-llama/Llama-3.2-1B-Instruct": 1.2,
        "meta-llama/Llama-3.2-3B-Instruct": 3.2,
        "meta-llama/Llama-3.1-8B-Instruct": 8.0,
        "meta-llama/Llama-3.3-70B-Instruct": 70.6,
        "meta-llama/Llama-4-Scout-17B-16E-Instruct": 108.6,
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct": 401.6,
    },
    "kimi": {
        "moonshotai/Moonlight-16B-A3B-Instruct": 16.0,
        "moonshotai/Kimi-Linear-48B-A3B-Instruct": 49.1,
        "moonshotai/Kimi-Dev-72B": 72.7,
        "moonshotai/Kimi-K2.6": 1026.9,
        "moonshotai/Kimi-K2.7-Code": 1026.9,
        "moonshotai/Kimi-K3": 2779.9,
    },
    "mistral": {
        "mistralai/Ministral-3-3B-Instruct-2512": 3.8,
        "mistralai/Ministral-3-8B-Instruct-2512": 8.9,
        "mistralai/Ministral-3-14B-Instruct-2512": 13.9,
        "mistralai/Ministral-3-14B-Reasoning-2512": 13.9,
        "mistralai/Devstral-Small-2-24B-Instruct-2512": 24.0,
        "mistralai/Mistral-Small-4-119B-2603": 119.4,
        "mistralai/Devstral-2-123B-Instruct-2512": 125.0,
        "mistralai/Mistral-Medium-3.5-128B": 127.7,
    },
    "deepseek": {
        "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B": 8.2,
        "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct": 15.7,
        "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B": 32.8,
        "deepseek-ai/DeepSeek-V4-Flash-DSpark": 165.3,
        "deepseek-ai/DeepSeek-V4.1-Flash": 763.2,
        "deepseek-ai/DeepSeek-Math-V2": 685.4,
        "deepseek-ai/DeepSeek-V4-Pro-0813": 1650.5,
    },
    "gemma": {
        "google/gemma-3-1b-it": 1.0,
        "google/gemma-4-E2B-it": 5.1,
        "google/gemma-4-E4B-it": 8.0,
        "google/gemma-4-12B-it": 12.0,
        "google/gemma-4-26B-A4B-it": 25.8,
        "google/gemma-4-31B-it": 31.3,
    },
    "phi": {
        "microsoft/Phi-4-mini-instruct": 3.8,
        "microsoft/Phi-4-mini-reasoning": 3.8,
        "microsoft/phi-4": 14.7,
        "microsoft/Phi-4-reasoning": 14.7,
        "microsoft/Phi-4-reasoning-plus": 14.7,
    },
    "gpt-oss": {
        "openai/gpt-oss-20b": 20.9,
        "openai/gpt-oss-120b": 116.8,
    },
    "glm": {
        "zai-org/GLM-4-9B-0414": 9.4,
        "zai-org/GLM-4.7-Flash": 31.2,
        "zai-org/GLM-5.3-Flash": 321.3,
        "zai-org/GLM-5.3": 753.3,
    },
    "granite": {
        "ibm-granite/granite-4.2-3b": 3.7,
        "ibm-granite/granite-4.2-8b": 8.8,
        "ibm-granite/granite-4.2-30b": 29.3,
    },
    "olmo": {
        "allenai/Olmo-3-7B-Instruct": 7.3,
        "allenai/Olmo-3-7B-Think": 7.3,
        "allenai/Olmo-3.1-32B-Instruct": 32.2,
        "allenai/Olmo-3.1-32B-Think": 32.2,
    },
    "nemotron": {
        "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16": 31.6,
        "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16": 123.6,
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16": 560.5,
    },
}

# Where to start for each kind of task, from the smallest model to the largest. These are suggestions based on what
# each model is built for (reasoning, coding, everyday writing, reading long texts, following a format exactly), not the result of a benchmark.
# All of them are in MODELS_LOCAL. The tasks are the ones of tasks_library.py: writing is the task of the writer (AuthorLoop) and code the one of the coder.
RECOMMENDED_LOCAL = {
    "math": ["allenai/Olmo-3-7B-Think", "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", "mistralai/Ministral-3-14B-Reasoning-2512",
             "microsoft/Phi-4-reasoning-plus", "Qwen/Qwen3.8-27B", "deepseek-ai/DeepSeek-Math-V2"],
    "code": ["deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct", "mistralai/Devstral-Small-2-24B-Instruct-2512", "Qwen/Qwen3-Coder-30B-A3B-Instruct",
             "Qwen/Qwen3-Coder-Next", "mistralai/Devstral-2-123B-Instruct-2512", "moonshotai/Kimi-K2.7-Code"],
    "writing": ["google/gemma-4-E4B-it", "mistralai/Ministral-3-8B-Instruct-2512", "google/gemma-4-12B-it", "google/gemma-4-31B-it",
                "meta-llama/Llama-3.3-70B-Instruct", "mistralai/Mistral-Small-4-119B-2603"],
    "email": ["google/gemma-4-E4B-it", "mistralai/Ministral-3-8B-Instruct-2512", "google/gemma-4-12B-it", "Qwen/Qwen3-14B",
              "google/gemma-4-31B-it", "meta-llama/Llama-3.3-70B-Instruct"],
    "calendar": ["Qwen/Qwen3.5-4B", "Qwen/Qwen3-8B", "microsoft/phi-4", "openai/gpt-oss-20b", "Qwen/Qwen3.5-27B"],
    "news": ["Qwen/Qwen3.5-4B", "meta-llama/Llama-3.1-8B-Instruct", "mistralai/Ministral-3-8B-Instruct-2512", "google/gemma-4-12B-it",
             "Qwen/Qwen3.5-27B", "google/gemma-4-31B-it"],
    "literature": ["Qwen/Qwen3-8B", "google/gemma-4-12B-it", "Qwen/Qwen3-14B", "openai/gpt-oss-20b", "Qwen/Qwen3.5-27B",
                   "Qwen/Qwen3-Next-80B-A3B-Instruct"],
    "formatting": ["microsoft/Phi-4-mini-instruct", "Qwen/Qwen3.5-4B", "Qwen/Qwen3-8B", "google/gemma-4-12B-it", "Qwen/Qwen3-14B", "Qwen/Qwen3.5-27B"],
    # The leader that builds the swarm itself must plan, follow a strict format, and judge the work of the others.
    "leading": ["Qwen/Qwen3-8B", "Qwen/Qwen3-14B", "openai/gpt-oss-20b", "Qwen/Qwen3.5-27B", "Qwen/Qwen3-Next-80B-A3B-Instruct", "openai/gpt-oss-120b"],
}

# API models by provider, from the most capable to the cheapest. Some are previews and some are only open to certain accounts.
MODELS_API = {
    "gpt": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "gpt-4.1-mini", "gpt-4o-mini"],
    "claude": ["claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
               "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "gemini": ["gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
               "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    "deepseek": ["deepseek-v4-pro", "deepseek-flash"],
}

# The same for the API models: the most capable ones for the tasks that need deep reasoning, the middle ones for writing and reading,
# and the cheapest ones for the light tasks (a calendar event, a briefing from headlines, a layout). One model of each provider, in the order
# claude, gpt, gemini, deepseek. These are suggestions from the tier of the model in MODELS_API, not the result of a benchmark.
_DEEP = ["claude-opus-5-5", "gpt-6-astra", "gemini-3.1-pro-preview", "deepseek-v4-pro"]
_MIDDLE = ["claude-sonnet-5-5", "gpt-6-luna", "gemini-3.8-flash", "deepseek-v4-pro"]
_LIGHT = ["claude-haiku-4-5", "gpt-4.1-mini", "gemini-3.5-flash-lite", "deepseek-flash"]
RECOMMENDED_API = {"math": _DEEP, "code": _DEEP, "writing": _MIDDLE, "email": _MIDDLE, "literature": _MIDDLE,
                   "calendar": _LIGHT, "news": _LIGHT, "formatting": _LIGHT, "leading": _DEEP}

# What the user needs to use the models of a provider: the environment variable the key is read from, and the page where the key is created.
# The key is only kept in memory while the program runs, and it is never written to a file.
API_KEYS = {
    "gpt": {"company": "OpenAI", "variable": "OPENAI_API_KEY", "page": "https://platform.openai.com/api-keys"},
    "claude": {"company": "Anthropic", "variable": "ANTHROPIC_API_KEY", "page": "https://platform.claude.com/settings/keys"},
    "gemini": {"company": "Google", "variable": "GEMINI_API_KEY", "page": "https://aistudio.google.com/apikey"},
    "deepseek": {"company": "DeepSeek", "variable": "DEEPSEEK_API_KEY", "page": "https://platform.deepseek.com/api_keys"},
}

# Coding agents that run on the computer of the user, linked to SwarmUP as a third kind of model: they think for an agent like a model,
# and they can also read files, run commands and search the web, each time with the approval of the user (model_clients.py).
# Claude Code is used with an Anthropic API key only: Anthropic does not allow third-party products to use a Claude subscription
# (https://code.claude.com/docs/en/agent-sdk/overview). Codex is used with the ChatGPT plan of the user, signed in through Codex itself.
# DEFAULT_CLI_MODEL lets the agent choose its own model. The models of Codex depend on the plan, so Codex lists them itself.
DEFAULT_CLI_MODEL = "default"
MODELS_CLI = {
    "claude-code": {"label": "Claude Code", "company": "Anthropic", "provider": "claude", "package": "claude-agent-sdk",
                    "page": "https://code.claude.com/docs/en/overview", "models": [DEFAULT_CLI_MODEL, *MODELS_API["claude"]]},
    "codex": {"label": "Codex", "company": "OpenAI", "provider": None, "package": None, "page": "https://developers.openai.com/codex/cli",
              "models": [DEFAULT_CLI_MODEL]},
}

# The costs are not written here because they change often. The info button of a model fetches them with getModelCost
# in harness_utils.py, and shows the official page of the provider for the details.
PRICING_PAGES = {
    "gpt": "https://developers.openai.com/api/docs/pricing",
    "claude": "https://platform.claude.com/docs/en/about-claude/pricing",
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing",
}

# What a single price cannot say. It is shown next to the price in the info button.
PRICE_NOTES = {
    "deepseek": "This is the price during the peak hours (weekdays 01:00-04:00 and 06:00-10:00 UTC). DeepSeek charges half of it at all other times.",
}


# The memory in GB that a local model needs. bits is 16 for the normal files and 4 for the 4-bit compressed ones.
# This is an estimate: the weights plus about 20% for the working memory. Very long texts need more.
def getVram(billionsOfParameters, bits=16):
    return round(billionsOfParameters * bits / 8 * VRAM_OVERHEAD, 1)


# The provider of an API model: the one that lists it, or the one its name starts with (claude-..., gpt-...). None if it is not an API model.
def getProvider(name):
    name = name.strip()
    listed = next((provider for provider, models in MODELS_API.items() if name in models), None)
    return listed or next((provider for provider in MODELS_API if name.startswith(f"{provider}-")), None)


def isGated(name):
    return name.strip().startswith(GATED_PREFIXES)


# The model a user chose for an agent, with the VRAM it needs. API models need none: they run on the servers of their provider.
# A Hugging Face name that is not in the list (owner/name) is a local model whose size must be given in billions of parameters.
# An API model that is not in the list can be entered by naming its provider (gpt, claude, gemini or deepseek), for the newest ones.
# cli is the coding agent of MODELS_CLI that runs the model (then name is its model, or DEFAULT_CLI_MODEL). Claude Code keeps the
# provider claude, because it is billed through the Anthropic API key of the user. Every info has cli, None for the other models.
def getModelInfo(name, bits=16, billions=None, provider=None, cli=None):
    name = name.strip()
    if cli:
        if cli not in MODELS_CLI:
            raise ValueError(f"{cli} is not one of the coding agents: {', '.join(MODELS_CLI)}.")
        return {"name": name or DEFAULT_CLI_MODEL, "local": False, "provider": MODELS_CLI[cli]["provider"], "billions": None, "bits": None, "vram": 0.0, "cli": cli}
    listed = next((family[name] for family in MODELS_LOCAL.values() if name in family), None)
    if "/" not in name and listed is None:
        provider = getProvider(name) or provider
        if provider not in MODELS_API or not name:
            raise ValueError(f"'{name}' is not a model of the list. A model from Hugging Face is written like owner/name.")
        return {"name": name, "local": False, "provider": provider, "billions": None, "bits": None, "vram": 0.0, "cli": None}
    size = billions or listed
    if not size or size <= 0:
        raise ValueError(f"The size of {name} is not known, so give its number of parameters in billions.")
    if bits not in BITS:
        raise ValueError(f"The models are used with {', '.join(str(option) for option in BITS)} bits, not {bits}.")
    return {"name": name, "local": True, "provider": None, "billions": size, "bits": bits, "vram": getVram(size, bits), "cli": None}
