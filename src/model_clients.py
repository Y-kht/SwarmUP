import gc
import importlib
import importlib.util
import json
import os
import re
import threading
import urllib.error
from pathlib import Path

from harness_utils import ConnectionLost, describeError, fetchUrl, isOnline
from models_library import API_KEYS, isGated

# A model client is any object with an input(prompt) method that returns the answer as text: it is the agent of a loop.
# ApiModel asks the API of a provider with its official library (anthropic for Claude, openai for GPT and for the OpenAI-compatible
# APIs of Gemini and DeepSeek). LocalModel runs a model of Hugging Face on the GPUs of the user with transformers.
# The libraries are only imported when a model is used, so a user who only needs one kind of model does not install the others.
# The Hugging Face cache is the folder of the HF_HOME environment variable, so the user chooses where the models are stored.
API_LIBRARIES = {"claude": "anthropic", "gpt": "openai", "gemini": "openai", "deepseek": "openai"}
BASE_URLS = {"claude": None, "gpt": None, "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/", "deepseek": "https://api.deepseek.com"}
API_RETRIES = 2
CLAUDE_MAX_TOKENS = 32000
LOCAL_MAX_TOKENS = 4096
THINKING_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


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
def findMissingPackages(info):
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
# say) and whether it is gated. It raises a ModelError, with a message for the user, if the model cannot be found.
def lookupHuggingFace(name):
    try:
        data = json.loads(fetchUrl(f"https://huggingface.co/api/models/{name}"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ModelError(f"There is no model called {name} on Hugging Face. Check how it is written.") from None
        raise ModelError(f"Hugging Face answered with error {error.code} for {name}. A private model needs a token.") from None
    except (OSError, ValueError) as error:
        raise ModelError(f"Hugging Face could not be asked: {describeError(error)}.") from None
    total = (data.get("safetensors") or {}).get("total")
    return {"billions": round(total / 1000000000, 1) if total else None, "gated": bool(data.get("gated"))}


# What went wrong with a call to an API, in words a user understands. The libraries of Anthropic and OpenAI have the same error classes.
def explainApiError(library, error, company, name):
    if isinstance(error, library.APITimeoutError):
        return f"{company} took too long to answer."
    if isinstance(error, library.APIConnectionError):
        return f"{company} could not be reached. Check the internet connection."
    if isinstance(error, library.AuthenticationError):
        return f"{company} refused the API key. Check that it is correct and still active."
    if isinstance(error, library.PermissionDeniedError):
        return f"{company} does not allow this API key to use {name} (permissions, region, or an access request is missing)."
    if isinstance(error, library.NotFoundError):
        return f"{company} does not know a model called {name}, or this account cannot use it."
    if isinstance(error, library.RateLimitError):
        return f"{company} says there are too many requests, or that the account has no credit left. Try again later or check the billing page."
    if isinstance(error, library.APIStatusError):
        return f"{company} answered with error {error.status_code}: {getattr(error, 'message', error)}"
    return f"{company} failed: {error}"


class ApiModel:
    def __init__(self, provider, name, apiKey):
        self.provider = provider
        self.name = name
        self.apiKey = apiKey
        self.company = API_KEYS[provider]["company"]
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.client = None
        self.lock = threading.Lock()

    def connect(self):
        library = importLibrary(API_LIBRARIES[self.provider])
        if self.client is None:
            options = {"api_key": self.apiKey, "max_retries": API_RETRIES}
            if BASE_URLS[self.provider]:
                options["base_url"] = BASE_URLS[self.provider]
            self.client = (library.Anthropic if self.provider == "claude" else library.OpenAI)(**options)
        return library

    def addUsage(self, inputTokens, outputTokens):
        with self.lock:
            self.usage["calls"] += 1
            self.usage["input"] += inputTokens or 0
            self.usage["output"] += outputTokens or 0

    # Claude is asked with a stream and not with a single request, because a long answer would hit the time limit of a single request.
    # A refusal is a normal answer of the API (HTTP 200), so it is checked before the text is read. Thinking blocks are not part of the answer.
    def askClaude(self, prompt):
        with self.client.messages.stream(model=self.name, max_tokens=CLAUDE_MAX_TOKENS, messages=[{"role": "user", "content": prompt}]) as stream:
            message = stream.get_final_message()
        self.addUsage(message.usage.input_tokens, message.usage.output_tokens)
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise ModelError(f"{self.name} declined this request ({category or 'no reason given'}). Choose another model for this agent, or change what it is asked.")
        return "".join(block.text for block in message.content if block.type == "text")

    def askOpenAi(self, prompt):
        response = self.client.chat.completions.create(model=self.name, messages=[{"role": "user", "content": prompt}])
        if response.usage:
            self.addUsage(response.usage.prompt_tokens, response.usage.completion_tokens)
        choice = response.choices[0]
        if choice.message.refusal and not choice.message.content:
            raise ModelError(f"{self.name} declined this request: {choice.message.refusal}")
        return choice.message.content or ""

    def input(self, prompt):
        library = self.connect()
        try:
            return self.askClaude(prompt) if self.provider == "claude" else self.askOpenAi(prompt)
        except library.APIError as error:
            lost = isinstance(error, library.APIConnectionError)
            raise (ModelConnectionError if lost else ModelError)(explainApiError(library, error, self.company, self.name)) from error


# A Hugging Face model on the GPUs. It is loaded when it is first asked something, one model at a time (a swarm has many),
# spread over all the GPUs. bits below 16 load it compressed (it needs bitsandbytes), and token is the Hugging Face token of gated models.
# report is a function that tells the user what takes time (the download and the loading of a big model take minutes).
class LocalModel:
    loading = threading.Lock()

    def __init__(self, name, bits=16, token=None, report=None, maxNewTokens=LOCAL_MAX_TOKENS):
        self.name = name
        self.bits = bits
        self.token = token
        self.report = report or (lambda message: None)
        self.maxNewTokens = maxNewTokens
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.model = None
        self.tokenizer = None
        self.torch = None
        self.lock = threading.Lock()

    def load(self):
        torch, transformers = importLibrary("torch"), importLibrary("transformers")
        importLibrary("accelerate")
        if not torch.cuda.is_available():
            raise ModelError("PyTorch does not see any GPU, so the model would run on the processor and be extremely slow. Install the CUDA version of PyTorch.")
        options = {"device_map": "auto", "token": self.token}
        if self.bits < 16:
            importLibrary("bitsandbytes")
            options["quantization_config"] = transformers.BitsAndBytesConfig(load_in_8bit=self.bits == 8, load_in_4bit=self.bits == 4,
                                                                             bnb_4bit_compute_dtype=torch.bfloat16)
        else:
            options["dtype"] = "auto"
        self.report(f"Loading {self.name} on the GPUs." + ("" if isDownloaded(self.name) else " The first time it is downloaded, which can take a long time."))
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(self.name, token=self.token)
        try:
            self.model = transformers.AutoModelForCausalLM.from_pretrained(self.name, **options)
        except ValueError:
            multimodal = getattr(transformers, "AutoModelForImageTextToText", None)
            if multimodal is None:
                raise ModelError(f"{self.name} needs a newer version of transformers. Update it with: pip install -U transformers") from None
            self.model = multimodal.from_pretrained(self.name, **options)
        self.torch = torch
        self.report(f"{self.name} is ready.")

    def answer(self, prompt):
        with self.lock:
            if self.model is None:
                with LocalModel.loading:
                    self.load()
            messages = [{"role": "user", "content": prompt}]
            inputs = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt", return_dict=True, enable_thinking=False)
            inputs = inputs.to(self.model.device)
            with self.torch.no_grad():
                output = self.model.generate(**inputs, max_new_tokens=self.maxNewTokens)
            start = inputs["input_ids"].shape[-1]
            self.usage["calls"] += 1
            self.usage["input"] += start
            self.usage["output"] += output.shape[-1] - start
            text = self.tokenizer.decode(output[0][start:], skip_special_tokens=True)
            return THINKING_PATTERN.sub("", text).split("<think>")[0].strip()

    # The two things that go wrong most with a local model are told in words a user understands.
    def input(self, prompt):
        try:
            return self.answer(prompt)
        except OSError as error:
            if not isOnline():
                raise ModelConnectionError(f"{self.name} could not be loaded because it needs the internet and the connection is lost.") from error
            gated = f" It is a gated model: accept its license on https://huggingface.co/{self.name} and give a Hugging Face token (HF_TOKEN)." if isGated(self.name) else ""
            raise ModelError(f"{self.name} could not be downloaded or opened ({str(error).splitlines()[0] if str(error) else type(error).__name__}).{gated}") from error
        except RuntimeError as error:
            if "out of memory" not in str(error).lower():
                raise
            raise ModelError(f"The GPUs ran out of memory with {self.name}. Other jobs may be using them. Free some memory or choose a smaller model.") from error

    # Frees the memory of the GPUs.
    def unload(self):
        with self.lock:
            self.model = self.tokenizer = None
            gc.collect()
            if self.torch is not None:
                self.torch.cuda.empty_cache()


# The client of a model chosen with getModelInfo (models_library.py). apiKeys is {provider: key}, and the environment variable of the provider is the other source.
def createModel(info, apiKeys=None, token=None, report=None):
    if info["local"]:
        return LocalModel(info["name"], info["bits"], token, report)
    key = (apiKeys or {}).get(info["provider"]) or getApiKey(info["provider"])
    if not key:
        raise ModelError(f"There is no API key for {API_KEYS[info['provider']]['company']}. Set {API_KEYS[info['provider']]['variable']} or give the key.")
    return ApiModel(info["provider"], info["name"], key)
