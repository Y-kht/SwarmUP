# The permission policy of SwarmUP, in one place for every agent kind (docs/MCP_DESIGN_BOOKLET.md, PRM-01 and PRM-02). A server that wants
# to act asks through MCP elicitation, a coding agent through its own approval request, and both arrive here:
# - looking needs nothing, and in plan mode nothing else is allowed;
# - the temp folder, the memory (with its own rules, mission_memory.py) and the workspace of SwarmUP itself (an agent without a folder) are
#   free (the request says free);
# - everything else is shown to the user as a card (Loop.askPermission): once, until the swarm runs again (the key of the request is
#   remembered in loop.allowed, so the same kind of action is not asked again), or no.
# The other questions of a server go to the user as a form (Loop.askQuestions), and a server that needs a model asks the model of its agent.
import json

from mcp import types


PLAN_REFUSAL = "SwarmUP is in plan mode: the agents only look until the plan is approved."


def decision(value, message=""):
    return {"decision": value, "message": message}


# request: {action, detail, folder, reason, key, place, free, readOnly}. The answer: {"decision": once|run|deny, "message"}.
def decidePermission(loop, request):
    key = request.get("key") or "change"
    if loop.planning and not request.get("readOnly"):
        return decision("deny", PLAN_REFUSAL)
    if request.get("free") or (key == "change" and request.get("place", "") == "" and not loop.folder):
        return decision("once")
    if key in loop.allowed:
        return decision("once")
    answer = loop.askPermission({"action": request.get("action") or "act", "detail": request.get("detail") or "", "folder": request.get("folder") or "",
                                 "reason": request.get("reason") or ""})
    chosen = answer.get("decision") if isinstance(answer, dict) else "deny"
    if chosen == "run":
        loop.allowed.add(key)
        return decision("run")
    if chosen == "once":
        return decision("once")
    return decision("deny", str(answer.get("message") or "") if isinstance(answer, dict) else "")


# A form of an external server (its requested schema) as questions of SwarmUP, and the answers as the content the server asked for.
def schemaQuestions(name, message, schema):
    questions = []
    for key, field in (schema.get("properties") or {}).items():
        options = [{"label": str(value)} for value in field.get("enum") or []]
        if field.get("type") == "boolean":
            options = [{"label": "yes"}, {"label": "no"}]
        questions.append({"id": key, "header": name, "question": f"{message}\n{field.get('title') or key}: {field.get('description') or ''}".strip(),
                          "options": options, "multiple": field.get("type") == "array", "secret": False})
    return questions


def schemaContent(schema, answers):
    content = {}
    for key, field in (schema.get("properties") or {}).items():
        values = [str(value) for value in answers.get(key) or []]
        if not values:
            continue
        kind = field.get("type")
        try:
            content[key] = values if kind == "array" else values[0].strip().lower() in ("yes", "true", "1") if kind == "boolean" else \
                int(values[0]) if kind == "integer" else float(values[0]) if kind == "number" else values[0]
        except ValueError:
            continue
    return content


# Every elicitation of a server for the agent of the loop. Only a server of SwarmUP (trusted) can say that an action is free or ask its forms.
def elicit(loop, serverName, params, trusted=True):
    meta = params.meta.model_dump().get("swarmup") if params.meta and trusted else None
    if isinstance(meta, dict) and "permission" in meta:
        return types.ElicitResult(action="accept", content=decidePermission(loop, dict(meta["permission"])))
    if isinstance(meta, dict) and "proposal" in meta:
        answer = loop.askProposal(meta["proposal"])
        approved = isinstance(answer, dict) and answer.get("decision") == "approve"
        return types.ElicitResult(action="accept", content={"decision": "approve" if approved else "reject",
                                                            "message": "" if approved or not isinstance(answer, dict) else str(answer.get("message") or "")})
    if isinstance(meta, dict) and "questions" in meta:
        answers = loop.askQuestions(meta["questions"])
        return types.ElicitResult(action="accept", content={"answers": json.dumps(answers if isinstance(answers, dict) else {}, ensure_ascii=False)})
    schema = getattr(params, "requestedSchema", None) or {}
    questions = schemaQuestions(serverName, params.message, schema)
    if not questions:
        return types.ElicitResult(action="decline")
    answers = loop.askQuestions(questions)
    content = schemaContent(schema, answers if isinstance(answers, dict) else {})
    return types.ElicitResult(action="accept", content=content) if content else types.ElicitResult(action="decline")


# A server asks the model of the agent for a completion (MCP sampling). The model answers without tools, as for a check.
def sample(loop, params):
    parts = [params.systemPrompt] if params.systemPrompt else []
    for message in params.messages:
        content = message.content
        text = content.text if isinstance(content, types.TextContent) else " ".join(getattr(item, "text", "") for item in content) if isinstance(content, list) else ""
        parts.append(f"[{message.role}] {text}")
    answer = loop.askAgent("\n\n".join(parts), own=False, tools=False)
    return types.CreateMessageResult(role="assistant", content=types.TextContent(type="text", text=answer or ""), model=getattr(loop.agent, "name", "") or "model")
