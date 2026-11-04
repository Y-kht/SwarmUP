# What the model clients send and read (model_clients.py): the conversation of SwarmUP (agent_conversation.py) in the words of each provider,
# the calls of tools that a local model writes in its text, the tokens of an answer, and what went wrong with an API in words a user understands.
import json
import re
import uuid

import agent_prompts as prompts
from harness_utils import stripFences


TOOL_CALL_PATTERN = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)
STARTED_CALL = re.compile(r"\s*(?:<tool_call>|(?:```(?:json)?\s*)?\[?\s*\{\s*\"name\")")
CALL_NAME = re.compile(r"\"name\"\s*:\s*\"([^\"]+)\"")
LOOSE_CALL = re.compile(r"\{\s*\"name\"\s*:\s*\"([^\"]+)\"\s*,\s*\"(?:arguments|parameters)\"\s*:\s*\{(.*)\}\s*\}", re.DOTALL)
LOOSE_KEY = re.compile(r"\s*,?\s*\"([A-Za-z_]\w*)\"\s*:\s*")
LOOSE_TEXT_END = re.compile(r"(?<!\\)\"(?=\s*(?:,\s*\"[A-Za-z_]\w*\"\s*:|$))")
# A quote that nothing escapes (an even number of backslashes before it), and a backslash that starts no escape of JSON.
LOOSE_QUOTE = re.compile(r"(?<!\\)((?:\\\\)*)\"")
BAD_ESCAPE = re.compile(r"\\(?![\"\\/bfnrtu])")


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
# OpenAI (the GPT models): the Responses API, the only one where a GPT model that reasons can call tools. What it answered is given back
# exactly as it came (its reasoning included, encrypted, since nothing is stored at OpenAI), and each result goes with the id of its call.
def responsesInput(messages):
    items = []
    for message in messages:
        if message["role"] == "user":
            items.append({"role": "user", "content": message["content"]})
        elif message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["id"], "output": message["content"]})
        elif (message.get("raw") or {}).get("responses"):
            items += message["raw"]["responses"]
        else:
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            items += [{"type": "function_call", "call_id": call["id"], "name": call["name"], "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False)}
                      for call in message.get("calls") or []]
    return items


def readResponsesUsage(usage):
    if usage is None:
        return {"missing": True}
    details = getattr(usage, "input_tokens_details", None)
    cached = min(usage.input_tokens or 0, (getattr(details, "cached_tokens", None) or 0) if details is not None else 0)
    return {"input": (usage.input_tokens or 0) - cached, "cachedInput": cached, "output": usage.output_tokens or 0}


# Some chat templates (Llama) take one call in each answer of the model: an answer with several calls is given as one call, its result, the
# next call, its result... which is what happened, one call after the other.
def oneCallEach(messages):
    ordered, results = [], {message["id"]: message for message in messages if message["role"] == "tool"}
    placed = set()
    for message in messages:
        calls = message.get("calls") or []
        if message["role"] == "assistant" and len(calls) > 1:
            for number, call in enumerate(calls):
                ordered.append({**message, "content": message.get("content") if number == 0 else "", "calls": [call]})
                if call["id"] in results:
                    ordered.append(results[call["id"]])
                    placed.add(call["id"])
        elif message["role"] != "tool" or message["id"] not in placed:
            ordered.append(message)
    return ordered


def localMessages(system, messages, tools, native):
    chat = [{"role": "system", "content": system if native else f"{system}\n\n" + prompts.TEXT_TOOLS_PROMPT.format(tools=json.dumps(tools, ensure_ascii=False))}]
    for message in oneCallEach(messages) if native else messages:
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
        data = readLooseCall(body)
        arguments = data.get("arguments", data.get("parameters", {}))
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        return {"id": f"call_{uuid.uuid4().hex[:12]}", "name": str(data["name"]), "arguments": arguments}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return {"id": f"call_{uuid.uuid4().hex[:12]}", "name": "unknown", "arguments": {}, "text": body,
                "problem": f"this tool call is not valid JSON ({describeJsonError(error)}). Write it again, with every quote and backslash of a text escaped."}


def describeJsonError(error):
    return f"{error.msg} at character {error.pos}" if isinstance(error, json.JSONDecodeError) else str(error)


# A call as JSON, or as a small model writes it: a long text in an argument often has a quote or a backslash that is not escaped, and that is no reason
# to lose it. The first error is the one that is told if nothing works.
def readLooseCall(body):
    try:
        return json.loads(body, strict=False)
    except ValueError as error:
        loose = LOOSE_CALL.fullmatch(body.strip())
        if not loose:
            raise error
        try:
            return {"name": loose.group(1), "arguments": readLooseArguments(loose.group(2))}
        except ValueError:
            raise error from None


# The arguments of a call, where a text ends at a quote that is followed by the next key or by the end: any other quote inside it is part of the text.
def readLooseArguments(text):
    arguments, position, decoder = {}, 0, json.JSONDecoder(strict=False)
    while position < len(text.rstrip()):
        key = LOOSE_KEY.match(text, position)
        if not key:
            raise ValueError("not a key")
        position = key.end()
        if text[position:position + 1] != '"':
            arguments[key.group(1)], position = decoder.raw_decode(text, position)
            continue
        end = LOOSE_TEXT_END.search(text, position + 1)
        if not end:
            raise ValueError("a text does not end")
        arguments[key.group(1)] = readLooseText(text[position + 1:end.start()])
        position = end.start() + 1
    return arguments


def readLooseText(raw):
    fixed = LOOSE_QUOTE.sub(r'\1\\"', raw)
    fixed = BAD_ESCAPE.sub(r"\\\\", fixed)
    return json.loads(f'"{fixed}"', strict=False)


def newCallId():
    return f"call_{uuid.uuid4().hex[:12]}"


# The calls a model wrote in its text (ADP-04): <tool_call>{json}</tool_call> (Qwen, Hermes and most models), or its whole answer as JSON
# (Llama, Mistral) when it names one of its tools. A leader can also write a change of its swarm, or what to keep in the long-term memory, as a
# block (leader_parser.py, mission_memory.py): they are read as calls of its leader tools, when it has them. tools: the tools it was given.
def readToolCalls(text, tools=None):
    names = {tool["name"] for tool in tools or []}
    calls = [readCall(body) for body in TOOL_CALL_PATTERN.findall(text)]
    if calls:
        return calls, TOOL_CALL_PATTERN.sub("", text).strip()
    # A small model often writes its call as JSON in a code fence (```json ... ```).
    stripped = stripFences(text.strip()) if text.strip().startswith("```") else text.strip()
    if stripped[:1] in ("[", "{"):
        items = readJsonSequence(stripped)
        if items and all(isinstance(item, dict) and "name" in item for item in items) and (not names or all(item["name"] in names for item in items)):
            return [readCall(json.dumps(item)) for item in items], ""
    return readLeaderBlocks(text, names)


# What a model wrote when its tokens ended. A call it was writing is the call it meant, which is not run (CUT_CALL tells it why): it never becomes
# the text of its answer, and it is not called invalid. Any other text is the start of an answer.
def readCutCalls(text, tools=None):
    calls, rest = readToolCalls(text, tools)
    if calls and not any(call.get("problem") for call in calls):
        return calls, rest
    if not calls and not STARTED_CALL.match(text):
        return calls, rest
    named = CALL_NAME.findall(text)
    good = [call for call in calls if not call.get("problem")]
    return [*good, {"id": newCallId(), "name": named[-1] if named else "unknown", "arguments": {}}], ""


# One JSON value, or several written one after the other (Llama writes {...}; {...}). A list gives its items. None if it is not only JSON.
def readJsonSequence(text):
    decoder, items, position = json.JSONDecoder(), [], 0
    while position < len(text):
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError:
            return None
        items += value if isinstance(value, list) else [value]
        while position < len(text) and text[position] in " \t\r\n;,":
            position += 1
    return items


def readLeaderBlocks(text, names):
    calls = []
    if any(name.startswith("leader__") and name != "leader__keep_long_term" for name in names):
        from leader_parser import readBlockCalls
        calls, text = readBlockCalls(text, newCallId)
    if "leader__keep_long_term" in names:
        from mission_memory import MEMORY_BLOCK
        calls += [{"id": newCallId(), "name": "leader__keep_long_term", "arguments": {"file": name.strip(), "text": body.strip(), "mode": (mode or "append").lower()}}
                  for name, mode, body in MEMORY_BLOCK.findall(text)]
        text = MEMORY_BLOCK.sub("", text).strip()
    return calls, text
