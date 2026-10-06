# A model client is any object with an input(prompt) method that returns the answer as text: it is the agent of a loop.
# ApiModel asks the API of a provider with its official library (anthropic for Claude, openai for GPT and for the OpenAI-compatible
# APIs of Gemini and DeepSeek). LocalModel runs a model of Hugging Face on the GPUs of the user with transformers.
# The libraries are only imported when a model is used, so a user who only needs one kind of model does not install the others.
# The Hugging Face cache is the folder of the HF_HOME environment variable, so the user chooses where the models are stored.
# The coding agents are in coding_agents.py and codex_agent.py, and what every client needs in model_support.py.
import gc
import re
import threading

from codex_agent import CodexModel
from coding_agents import ClaudeCodeModel
from harness_utils import isOnline
from model_support import API_LIBRARIES, ModelConnectionError, ModelError, getApiKey, importLibrary, isDownloaded, recordCall
from models_library import API_KEYS, isGated


BASE_URLS = {"claude": None, "gpt": None, "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/", "deepseek": "https://api.deepseek.com"}
API_RETRIES = 2
ACCEPTED_ENCODINGS = "gzip, deflate"
CLAUDE_MAX_TOKENS = 32000
LOCAL_MAX_TOKENS = 4096
THINKING_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


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


# The tokens of an answer of the OpenAI API, and of the APIs like it. The cached tokens are part of prompt_tokens (OpenAI gives them in
# prompt_tokens_details, DeepSeek in prompt_cache_hit_tokens). The thinking of a model is billed as output: it is in completion_tokens for
# OpenAI and DeepSeek, and Gemini only counts it in total_tokens, so the output is what the total has beyond the prompt.
def readOpenAiUsage(usage):
    if usage is None:
        return {"missing": True}
    prompt = usage.prompt_tokens or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
    cached = min(prompt, cached or getattr(usage, "prompt_cache_hit_tokens", None) or 0)
    output = max(usage.completion_tokens or 0, (usage.total_tokens or 0) - prompt)
    return {"input": prompt - cached, "cachedInput": cached, "output": output}


class ApiModel:
    def __init__(self, provider, name, apiKey):
        self.provider = provider
        self.name = name
        self.apiKey = apiKey
        self.company = API_KEYS[provider]["company"]
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.client = None
        self.lock = threading.Lock()

    # The answers are only asked compressed with gzip or deflate (ACCEPTED_ENCODINGS). Without it, the HTTP library of the providers asks for
    # Brotli when a brotli package is installed, and an old one (before 1.2, common in conda environments) makes every answer fail to decode.
    def connect(self):
        library = importLibrary(API_LIBRARIES[self.provider])
        if self.client is None:
            options = {"api_key": self.apiKey, "max_retries": API_RETRIES, "default_headers": {"Accept-Encoding": ACCEPTED_ENCODINGS}}
            if BASE_URLS[self.provider]:
                options["base_url"] = BASE_URLS[self.provider]
            self.client = (library.Anthropic if self.provider == "claude" else library.OpenAI)(**options)
        return library

    def addUsage(self, **tokens):
        with self.lock:
            recordCall(self.usage, **tokens)

    # Claude is asked with a stream and not with a single request, because a long answer would hit the time limit of a single request.
    # A refusal is a normal answer of the API (HTTP 200), so it is checked before the text is read. Thinking blocks are not part of the answer.
    def askClaude(self, prompt):
        with self.client.messages.stream(model=self.name, max_tokens=CLAUDE_MAX_TOKENS, messages=[{"role": "user", "content": prompt}]) as stream:
            message = stream.get_final_message()
        usage = message.usage
        self.addUsage(input=usage.input_tokens, cachedInput=getattr(usage, "cache_read_input_tokens", 0), cacheWrite=getattr(usage, "cache_creation_input_tokens", 0),
                      output=usage.output_tokens)
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise ModelError(f"{self.name} declined this request ({category or 'no reason given'}). Choose another model for this agent, or change what it is asked.")
        return "".join(block.text for block in message.content if block.type == "text")

    def askOpenAi(self, prompt):
        response = self.client.chat.completions.create(model=self.name, messages=[{"role": "user", "content": prompt}])
        self.addUsage(**readOpenAiUsage(response.usage))
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
# Claude Code takes the key of Anthropic, and Codex needs none: it is signed in with ChatGPT through Codex itself.
def createModel(info, apiKeys=None, token=None, report=None):
    if info.get("cli") == "codex":
        return CodexModel(info["name"], report)
    if info["local"]:
        return LocalModel(info["name"], info["bits"], token, report)
    key = (apiKeys or {}).get(info["provider"]) or getApiKey(info["provider"])
    if not key:
        raise ModelError(f"There is no API key for {API_KEYS[info['provider']]['company']}. Set {API_KEYS[info['provider']]['variable']} or give the key.")
    if info.get("cli") == "claude-code":
        return ClaudeCodeModel(info["name"], key, report)
    return ApiModel(info["provider"], info["name"], key)
