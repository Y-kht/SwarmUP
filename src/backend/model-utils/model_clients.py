# A model client is any object with an input(prompt) method that returns the answer as text: it is the agent of a loop.
# ApiModel asks the API of a provider with its official library (anthropic for Claude, openai for GPT and for the OpenAI-compatible
# APIs of Gemini and DeepSeek). LocalModel runs a model of Hugging Face on the GPUs of the user with transformers.
# The libraries are only imported when a model is used, so a user who only needs one kind of model does not install the others.
# The Hugging Face cache is the folder of the HF_HOME environment variable, so the user chooses where the models are stored.
# The coding agents are in coding_agents.py and codex_agent.py, and what every client needs in model_support.py.
# Besides input (one prompt, one answer), ApiModel and LocalModel hold a conversation with tools (converse, see agent_conversation.py): they
# translate the messages and the tools of SwarmUP into those of their provider, and its answer back into text and calls of tools.
import gc
import json
import re
import threading
import uuid

import agent_prompts as prompts
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
TOOL_CALL_PATTERN = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
STREAM_ATTEMPTS = 2


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


# ==============
# The conversation of SwarmUP (agent_conversation.py) in the words of each provider.
# ==============
# Claude: the system prompt and the end of the conversation are kept in the cache of Anthropic, so each step only pays the new part in full.
# What Claude answered is given back exactly as it came (its thinking included), because Claude needs the conversation unchanged.
def claudeBlocks(message):
    if message["role"] == "user":
        return [{"type": "text", "text": message["content"]}] if message["content"] else []
    if message["role"] == "tool":
        return [{"type": "tool_result", "tool_use_id": message["id"], "content": message["content"] or "(empty)", **({"is_error": True} if message.get("error") else {})}]
    raw = (message.get("raw") or {}).get("claude")
    if raw:
        return raw
    text = [{"type": "text", "text": message["content"]}] if message["content"] else []
    return text + [{"type": "tool_use", "id": call["id"], "name": call["name"], "input": call.get("arguments") or {}} for call in message.get("calls") or []]


# The results of the tools and the messages that arrived meanwhile go together in one message of the user, as Claude wants them.
def claudeMessages(messages):
    result = []
    for message in messages:
        role, blocks = "assistant" if message["role"] == "assistant" else "user", claudeBlocks(message)
        if not blocks:
            continue
        if result and result[-1]["role"] == role:
            result[-1]["content"] = result[-1]["content"] + blocks
        else:
            result.append({"role": role, "content": list(blocks)})
    return result


def claudeBlock(block):
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return block.model_dump(mode="json", exclude_none=True)


# The API of OpenAI, and those of Gemini and DeepSeek that work like it. What the model answered is given back as it came, with what some
# providers add to it (the thinking of DeepSeek, the signatures of Gemini).
def openAiMessages(system, messages):
    result = [{"role": "system", "content": system}]
    for message in messages:
        if message["role"] == "user":
            result.append({"role": "user", "content": message["content"]})
        elif message["role"] == "tool":
            result.append({"role": "tool", "tool_call_id": message["id"], "content": message["content"] or "(empty)"})
        elif (message.get("raw") or {}).get("openai"):
            result.append(message["raw"]["openai"])
        else:
            entry = {"role": "assistant", "content": message["content"] or ""}
            if message.get("calls"):
                entry["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments") or {})}}
                                       for call in message["calls"]]
            result.append(entry)
    return result


# A local model whose chat template knows tools gets them as such. Another one gets them in its system prompt (TEXT_TOOLS_PROMPT), writes its
# calls in its text, and reads their results as messages of the user. Such templates often want the user and the model to speak in turn.
def localMessages(system, messages, tools, native):
    chat = [{"role": "system", "content": system if native else f"{system}\n\n" + prompts.TEXT_TOOLS_PROMPT.format(tools=json.dumps(tools, ensure_ascii=False))}]
    for message in messages:
        calls = message.get("calls") or []
        if message["role"] == "user":
            entry = {"role": "user", "content": message["content"]}
        elif message["role"] == "tool":
            entry = {"role": "tool", "name": message["name"], "tool_call_id": message["id"], "content": message["content"]} if native else \
                {"role": "user", "content": f"[Result of {message['name']}]\n{message['content']}"}
        elif native:
            entry = {"role": "assistant", "content": message["content"] or ""}
            if calls:
                entry["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call.get("arguments") or {}}} for call in calls]
        else:
            written = [f"<tool_call>{json.dumps({'name': call['name'], 'arguments': call.get('arguments') or {}}, ensure_ascii=False)}</tool_call>" for call in calls]
            entry = {"role": "assistant", "content": "\n".join([message["content"] or "", *written]).strip()}
        if not native and chat[-1]["role"] == entry["role"] == "user":
            chat[-1]["content"] += f"\n\n{entry['content']}"
        else:
            chat.append(entry)
    return chat


def readCall(body):
    try:
        data = json.loads(body)
        arguments = data.get("arguments", data.get("parameters", {}))
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        return {"id": f"call_{uuid.uuid4().hex[:12]}", "name": str(data["name"]), "arguments": arguments}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"id": f"call_{uuid.uuid4().hex[:12]}", "name": "unknown", "arguments": {}, "problem": "this tool call is not valid JSON. Write it again."}


# The calls a local model wrote: <tool_call>{json}</tool_call> (Qwen, Hermes and most models), or its whole answer as JSON (Llama, Mistral).
def readToolCalls(text):
    calls = [readCall(body) for body in TOOL_CALL_PATTERN.findall(text)]
    if calls:
        return calls, TOOL_CALL_PATTERN.sub("", text).strip()
    stripped = text.strip()
    if stripped[:1] in ("[", "{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            return [], text
        items = data if isinstance(data, list) else [data]
        if items and all(isinstance(item, dict) and "name" in item for item in items):
            return [readCall(json.dumps(item)) for item in items], ""
    return [], text


class ApiModel:
    def __init__(self, provider, name, apiKey):
        self.provider = provider
        self.name = name
        self.apiKey = apiKey
        self.company = API_KEYS[provider]["company"]
        self.usage = {"calls": 0, "input": 0, "output": 0}
        self.client = None
        self.lock = threading.Lock()
        # Claude needs the conversation to stay exactly as it was: its old messages are never shortened (see Conversation.shorten).
        self.appendOnly = provider == "claude"

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
        message = self.streamClaude(messages=[{"role": "user", "content": prompt}])
        return "".join(block.text for block in message.content if block.type == "text")

    # A streamed answer can carry a call whose arguments cannot be read (they are streamed as they are written): it is asked once more.
    def streamClaude(self, **options):
        for attempt in range(STREAM_ATTEMPTS):
            try:
                with self.client.messages.stream(model=self.name, max_tokens=CLAUDE_MAX_TOKENS, **options) as stream:
                    message = stream.get_final_message()
                break
            except ValueError:
                if attempt + 1 == STREAM_ATTEMPTS:
                    raise ModelError(f"{self.name} wrote a call of a tool that could not be read, twice. Try again, or choose another model.") from None
        usage = message.usage
        self.addUsage(input=usage.input_tokens, cachedInput=getattr(usage, "cache_read_input_tokens", 0), cacheWrite=getattr(usage, "cache_creation_input_tokens", 0),
                      output=usage.output_tokens)
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None)
            raise ModelError(f"{self.name} declined this request ({category or 'no reason given'}). Choose another model for this agent, or change what it is asked.")
        return message

    def converseClaude(self, system, messages, tools):
        options = {"system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}], "messages": claudeMessages(messages), "cache_control": {"type": "ephemeral"}}
        if tools:
            options["tools"] = [{"name": item["name"], "description": item["description"], "input_schema": item["parameters"], "eager_input_streaming": True} for item in tools]
        message = self.streamClaude(**options)
        calls = [{"id": block.id, "name": block.name, "arguments": block.input} for block in message.content if block.type == "tool_use"]
        return {"text": "".join(block.text for block in message.content if block.type == "text"), "calls": calls, "raw": {"claude": [claudeBlock(block) for block in message.content]},
                "cut": message.stop_reason == "max_tokens" and bool(calls)}

    def converseOpenAi(self, system, messages, tools):
        options = {"model": self.name, "messages": openAiMessages(system, messages)}
        if tools:
            options["tools"] = [{"type": "function", "function": {"name": item["name"], "description": item["description"], "parameters": item["parameters"]}} for item in tools]
        response = self.client.chat.completions.create(**options)
        self.addUsage(**readOpenAiUsage(response.usage))
        choice = response.choices[0]
        message = choice.message
        if message.refusal and not message.content and not message.tool_calls:
            raise ModelError(f"{self.name} declined this request: {message.refusal}")
        calls, written = [], [item for item in message.tool_calls or [] if getattr(item, "function", None)]
        for item in written:
            try:
                calls.append({"id": item.id, "name": item.function.name, "arguments": json.loads(item.function.arguments or "{}")})
            except ValueError:
                calls.append({"id": item.id, "name": item.function.name, "arguments": {}, "problem": "its arguments were not valid JSON. Write them again."})
        raw = {"role": "assistant", "content": message.content or ""}
        if written:
            raw["tool_calls"] = [item.model_dump(mode="json", exclude_none=True) for item in written]
        if getattr(message, "reasoning_content", None):
            raw["reasoning_content"] = message.reasoning_content
        return {"text": message.content or "", "calls": calls, "raw": {"openai": raw}, "cut": choice.finish_reason == "length" and bool(calls)}

    def askOpenAi(self, prompt):
        response = self.client.chat.completions.create(model=self.name, messages=[{"role": "user", "content": prompt}])
        self.addUsage(**readOpenAiUsage(response.usage))
        choice = response.choices[0]
        if choice.message.refusal and not choice.message.content:
            raise ModelError(f"{self.name} declined this request: {choice.message.refusal}")
        return choice.message.content or ""

    def ask(self, work):
        library = self.connect()
        try:
            return work()
        except library.APIError as error:
            lost = isinstance(error, library.APIConnectionError)
            raise (ModelConnectionError if lost else ModelError)(explainApiError(library, error, self.company, self.name)) from error

    def input(self, prompt):
        return self.ask(lambda: self.askClaude(prompt) if self.provider == "claude" else self.askOpenAi(prompt))

    # One step of a conversation (agent_conversation.py): {"text", "calls", "raw", "cut"}.
    def converse(self, system, messages, tools):
        return self.ask(lambda: (self.converseClaude if self.provider == "claude" else self.converseOpenAi)(system, messages, tools))


# A Hugging Face model on the GPUs. It is loaded when it is first asked something, one model at a time (a swarm has many),
# spread over all the GPUs. bits below 16 load it compressed (it needs bitsandbytes), and token is the Hugging Face token of gated models.
# report is a function that tells the user what takes time (the download and the loading of a big model take minutes).
class LocalModel:
    loading = threading.Lock()
    # A model on the GPUs of this computer: a swarm lets its memory go when its agent has finished (Swarm.release), and it loads again if needed.
    local = True

    # vram is the memory the model is expected to need (getModelInfo), so it can be put on one GPU that has room for it.
    def __init__(self, name, bits=16, token=None, report=None, maxNewTokens=LOCAL_MAX_TOKENS, vram=None):
        self.name = name
        self.bits = bits
        self.vram = vram
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
        options = {"device_map": self.placement(torch), "token": self.token}
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
        self.checkPlacement()
        self.checkSpread()
        self.report(f"{self.name} is ready.")

    # A model is put whole on the GPU with the most free memory when it fits there: it runs faster than spread over several GPUs, and it does
    # not depend on the GPUs passing data to each other. Otherwise the loader spreads it.
    def placement(self, torch):
        if not self.vram or torch.cuda.device_count() < 2:
            return "auto"
        free, index = max((torch.cuda.mem_get_info(index)[0] / 1000000000, index) for index in range(torch.cuda.device_count()))
        return {"": index} if free >= self.vram else "auto"

    # On some computers the GPUs cannot pass data to each other (a problem of the driver, often with the IOMMU): a model spread over them then
    # computes only zeros. One short pass finds it before the model writes nonsense or breaks the GPU.
    def checkSpread(self):
        places = {str(place) for place in (getattr(self.model, "hf_device_map", None) or {}).values()}
        if len(places) < 2:
            return
        inputs = self.tokenizer("Hello", return_tensors="pt").to(self.model.device)
        with self.torch.no_grad():
            logits = self.model(**inputs).logits.float()
        if bool(self.torch.isfinite(logits).all()) and float(logits.abs().max()) > 0:
            return
        self.model = self.tokenizer = None
        gc.collect()
        self.torch.cuda.empty_cache()
        raise ModelError(f"{self.name} had to be spread over several GPUs, and the GPUs of this computer do not pass data to each other correctly (a "
                         "problem of the driver, often of the IOMMU): it would only write nonsense. Choose a model that fits in one GPU, free one GPU for it, "
                         "or use a compressed model (4 or 8 bits).")

    # When other jobs fill the GPUs, the loader puts the part that does not fit on the processor (or the disk) without saying it. Such a model
    # is extremely slow, and its answers can break the GPU (an error of CUDA that only a restart clears), so it is refused at once.
    def checkPlacement(self):
        places = {str(place) for place in (getattr(self.model, "hf_device_map", None) or {}).values()}
        if places & {"cpu", "disk", "meta"}:
            self.model = self.tokenizer = None
            gc.collect()
            self.torch.cuda.empty_cache()
            raise ModelError(f"{self.name} does not fit in the memory of the GPUs that is free now: a part of it would run on the processor, which is "
                             "too slow and unstable. Other jobs may be using the GPUs. Wait until they free some memory, or choose a smaller model "
                             "(or a compressed one, in 4 or 8 bits).")

    def answer(self, prompt):
        with self.lock:
            return self.generate([{"role": "user", "content": prompt}])

    # A step of a conversation. The chat template of the model says if it knows tools; otherwise they are written in its system prompt.
    def dialogue(self, system, messages, tools):
        with self.lock:
            self.ensureLoaded()
            native = bool(tools) and "tools" in str(self.tokenizer.chat_template or "")
            text = self.generate(localMessages(system, messages, tools, native), [{"type": "function", "function": item} for item in tools] if native else None)
        calls, text = readToolCalls(text)
        return {"text": text, "calls": calls, "raw": None, "cut": False}

    def ensureLoaded(self):
        if self.model is None:
            with LocalModel.loading:
                self.load()

    # Must be called with self.lock held.
    def generate(self, messages, tools=None):
        self.ensureLoaded()
        inputs = self.tokenizer.apply_chat_template(messages, tools=tools, add_generation_prompt=True, return_tensors="pt", return_dict=True, enable_thinking=False)
        inputs = inputs.to(self.model.device)
        with self.torch.no_grad():
            output = self.model.generate(**inputs, max_new_tokens=self.maxNewTokens)
        start = inputs["input_ids"].shape[-1]
        self.usage["calls"] += 1
        self.usage["input"] += start
        self.usage["output"] += output.shape[-1] - start
        text = self.tokenizer.decode(output[0][start:], skip_special_tokens=True)
        return THINKING_PATTERN.sub("", text).split("<think>")[0].strip()

    def input(self, prompt):
        return self.guard(lambda: self.answer(prompt))

    def converse(self, system, messages, tools):
        return self.guard(lambda: self.dialogue(system, messages, tools))

    # The two things that go wrong most with a local model are told in words a user understands.
    def guard(self, work):
        try:
            return work()
        except OSError as error:
            if not isOnline():
                raise ModelConnectionError(f"{self.name} could not be loaded because it needs the internet and the connection is lost.") from error
            gated = f" It is a gated model: accept its license on https://huggingface.co/{self.name} and give a Hugging Face token (HF_TOKEN)." if isGated(self.name) else ""
            raise ModelError(f"{self.name} could not be downloaded or opened ({str(error).splitlines()[0] if str(error) else type(error).__name__}).{gated}") from error
        except RuntimeError as error:
            if "device-side assert" in str(error) or "cuda error" in str(error).lower():
                raise ModelError(f"{self.name} broke on the GPU ({str(error).splitlines()[0]}). The GPUs of this program cannot be used again before it "
                                 "restarts: close it, and start it again to continue the swarm.") from error
            if "out of memory" not in str(error).lower():
                raise
            raise ModelError(f"The GPUs ran out of memory with {self.name}. Other jobs may be using them. Free some memory or choose a smaller model.") from error

    # Frees the memory of the GPUs. After an error of CUDA the GPU cannot be cleared anymore, and that must not stop the program.
    def unload(self):
        with self.lock:
            self.model = self.tokenizer = None
            gc.collect()
            if self.torch is not None:
                try:
                    self.torch.cuda.empty_cache()
                except RuntimeError:
                    pass


# The client of a model chosen with getModelInfo (models_library.py). apiKeys is {provider: key}, and the environment variable of the provider is the other source.
# Claude Code takes the key of Anthropic, and Codex needs none: it is signed in with ChatGPT through Codex itself.
def createModel(info, apiKeys=None, token=None, report=None):
    if info.get("cli") == "codex":
        return CodexModel(info["name"], report)
    if info["local"]:
        return LocalModel(info["name"], info["bits"], token, report, vram=info.get("vram"))
    key = (apiKeys or {}).get(info["provider"]) or getApiKey(info["provider"])
    if not key:
        raise ModelError(f"There is no API key for {API_KEYS[info['provider']]['company']}. Set {API_KEYS[info['provider']]['variable']} or give the key.")
    if info.get("cli") == "claude-code":
        return ClaudeCodeModel(info["name"], key, report)
    return ApiModel(info["provider"], info["name"], key)
