# From the journal to patterns: each entry of a journal (agent_journal.py) gives the patterns it is an occurrence of. The kinds:
# - action / command: a call of a tool with the same shape (a command has its own shape: the program, its subcommand, its flags);
# - sequence: steps that come in the same order; loop: a stuck agent; reread: the same file or page read again (context spent again);
# - error: a problem met again, by family and code; fix: how agents got out of an error;
# - reasoning: text written again and again before an action, near the same (code and answers are left out); decision: the same reasoning
#   followed by the same action (where the button is, then a click at the same place); intent: what agents keep saying they set out to do;
#   hotspot: a step or a request that cost much;
# - correction: what the user, the leader or the checks keep asking to change; permission: what the user keeps being asked; message: what
#   agents keep writing to each other; badcall: calls a model keeps writing wrong;
# - wheel: the runs of a wheel; wheelfail: a wheel that fails; redo: the work of a wheel done by hand (ignored: the agent had the wheel;
#   missed: nobody gave it the wheel).
# Detectors only take note (Batch): the miner writes the batch in the store, in one transaction.
import time

from pattern_errors import classifyError
from pattern_sequences import AgentTrail
from pattern_shapes import collapse, fingerprint, maskText, numbersOf, shapeCall, shapeCommand, targetOf
from pattern_text import anchorsOf, chunksOf, intentsOf, labelOf, similarity, sketchOf


JACCARD = 0.6
REREAD_SIZE = 2000
LONG_STEP_TOKENS = 2000
LONG_STEP_CHARS = 8000
LONG_REQUEST_STEPS = 15
LONG_CALL_SECONDS = 60
READING_TOOLS = ("read_file", "fetch_page", "search_files")
# A sequence of these steps only reads: it is how agents find their way, worth less than a sequence that does something.
LOOKING = ("files__read_file", "files__list_files", "files__find_files", "files__search_files", "team__read_team", "team__read_result")
EXAMPLE_CHARS = 600


class Batch:
    def __init__(self):
        self.hits, self.links, self.agents = [], [], {}

    def hit(self, kind, key, shape, label, entry, ref, level=1, tokens=0, seconds=0, size=0, error=False, data=None, example="", request=None):
        fp = fingerprint(kind, level, *key) if isinstance(key, (list, tuple)) else fingerprint(kind, level, key)
        self.hits.append({"fp": fp, "kind": kind, "level": level, "shape": collapse(shape, 400), "label": collapse(label, 300), "mission": entry.get("mission") or "",
                          "agent": entry.get("agent") or "", "request": request, "t": (entry.get("t") or time.time() * 1000) / 1000, "tokens": tokens or 0,
                          "seconds": seconds or 0, "size": size or 0, "error": bool(error), "data": data or {}, "example": collapse(example or label, EXAMPLE_CHARS),
                          "source": ref[0], "offset": ref[1]})
        return fp

    def link(self, first, second, kind):
        if first and second and first != second:
            self.links.append((first, second, kind))


class Detectors:
    # covers: {fingerprint: wheel} of the live wheels of the project; root: the folder of the project, to make paths relative.
    def __init__(self, store, project, root=None):
        self.store, self.project, self.root = store, project, root
        self.trails, self.agents, self.covers = {}, {}, {}

    def trailOf(self, entry):
        key = f"{entry.get('mission')}:{entry.get('agent')}"
        if key not in self.trails:
            self.trails[key] = AgentTrail(self.store.getState(f"trail:{key}"))
        return self.trails[key]

    def requestKey(self, entry, trail):
        return f"{entry.get('mission')}:{entry.get('agent')}:{trail.request}"

    def saveTrails(self, db):
        for key, trail in self.trails.items():
            self.store.setState(db, f"trail:{key}", trail.toData())

    # A text near one seen before is in the same cluster: its fingerprint is the cluster's. A short text is its own masked template.
    def cluster(self, db, kind, text):
        sketch = sketchOf(text)
        if not sketch:
            return fingerprint(kind, maskText(text)[:160]), maskText(text)[:160]
        anchors = anchorsOf(sketch)
        near, cluster = self.store.nearest(db, kind, sketch, anchors, similarity)
        if near < JACCARD:
            cluster = fingerprint(kind, maskText(text)[:300])
        if near < 0.9:
            self.store.addSketch(db, kind, cluster, sketch, anchors)
        return cluster, maskText(text)[:200]

    def process(self, db, entry, ref, batch):
        handler = getattr(self, f"on_{entry.get('kind')}", None)
        if handler is not None:
            handler(db, entry, ref, batch)

    def on_run(self, db, entry, ref, batch):
        for agent in entry.get("agents") or []:
            self.agents[f"{entry.get('mission')}:{agent.get('name')}"] = agent
            batch.agents[f"{entry.get('mission')}:{agent.get('name')}"] = agent

    def on_request(self, db, entry, ref, batch):
        trail = self.trailOf(entry)
        trail.newRequest(" ".join(maskText(entry.get("prompt") or "").split()[:12]))

    def on_step(self, db, entry, ref, batch):
        trail = self.trailOf(entry)
        trail.steps += 1
        request = self.requestKey(entry, trail)
        text, calls = entry.get("text") or "", entry.get("calls") or []
        tokens = ((entry.get("tokens") or {}).get("out") or 0) or (entry.get("length") or len(text)) / 4
        if trail.steps == LONG_REQUEST_STEPS:
            batch.hit("hotspot", ("request", trail.prompt), f"a request of {LONG_REQUEST_STEPS}+ steps: {trail.prompt}",
                      f"requests that take {LONG_REQUEST_STEPS} steps or more: \"{trail.prompt}\"", entry, ref, request=request)
        # A step with no call is the answer of the agent: what it made, not reasoning that a wheel could save.
        if not calls:
            return
        first = calls[0]
        firstShape = shapeCall(*splitFull(first.get("name")), first.get("args"), self.root)
        chunks = chunksOf(text)
        main = None
        for chunk in chunks:
            cluster, shape = self.cluster(db, "reasoning", chunk)
            share = tokens * len(chunk) / max(len(text), 1)
            fp = batch.hit("reasoning", cluster, shape, labelOf(chunk), entry, ref, tokens=share, request=request, example=chunk)
            if main is None or len(chunk) > main[1]:
                main = (fp, len(chunk), cluster, chunk)
        if main:
            numbers = numbersOf(first.get("args"))
            fp = batch.hit("decision", (main[2], firstShape[1]), f"{labelOf(main[3], 16)} -> {firstShape[1]}", f"reasoning \"{labelOf(main[3], 12)}\" then {firstShape[1]}",
                           entry, ref, tokens=tokens, request=request, data={"numbers": numbers, "exact": firstShape[0], **({"toolOnly": True} if numbers else {})},
                           example=f"{labelOf(main[3], 40)}\n-> {firstShape[0]}")
            batch.link(main[0], fp, "decides")
        for intent in intentsOf(text):
            batch.hit("intent", intent, intent, intent, entry, ref, request=request)
        if tokens >= LONG_STEP_TOKENS or len(text) >= LONG_STEP_CHARS:
            batch.hit("hotspot", ("step", firstShape[2]), f"a long step before {firstShape[2]}", f"long reasoning ({int(tokens):,} tokens) before {firstShape[2]}", entry, ref,
                      tokens=tokens, seconds=entry.get("seconds") or 0, request=request, example=labelOf(text, 60))

    def on_call(self, db, entry, ref, batch):
        origin = str(entry.get("origin") or "")
        if origin.startswith("wheel:"):
            return
        trail = self.trailOf(entry)
        request = self.requestKey(entry, trail)
        server, tool, args = entry.get("server") or "", entry.get("tool") or "", entry.get("args")
        l0, l1, l2 = shapeCall(server, tool, args, self.root)
        ok = entry.get("outcome") == "success"
        seconds, size = entry.get("seconds") or 0, entry.get("size") or 0
        result = entry.get("result") or ""
        example = f"{l0}\n{'ok' if ok else entry.get('outcome')}: {result[:300]}"
        errorFp = None
        if server != "wheels":
            kind = "command" if tool == "run_command" else "action"
            data = {"exact": l0, **({"local": True} if tool == "run_command" else {}), **({"toolOnly": True} if server not in ("files", "processes", "transfer") else {})}
            actionFp = batch.hit(kind, l1, l1, l1, entry, ref, seconds=seconds, size=size, error=not ok, data=data, example=example, request=request)
            self.checkRedo(entry, ref, batch, actionFp, request)
            if ok and tool in READING_TOOLS and size >= REREAD_SIZE:
                target = targetOf(server, tool, args, self.root)
                if target:
                    batch.hit("reread", (tool, target), f"{tool} {target}", f"{tool} of {target} again", entry, ref, size=size, request=request, example=example)
        if not ok:
            found = classifyError(result) or {"family": "other", "code": entry.get("outcome"), "template": maskText(result)[:120]}
            if entry.get("outcome") in ("timeout", "cancelled", "refused") and found["family"] != "swarmup":
                found = {"family": "swarmup", "code": f"swarmup:{entry.get('outcome')}", "template": maskText(result)[:120]}
            errorFp = batch.hit("error", (found["code"], found["template"], l2), f"{found['code']}: {found['template']} | {l2}", f"{found['code']}: {found['template']} (after {l2})",
                                entry, ref, error=True, seconds=seconds, data={"family": found["family"], "where": found.get("where") or ""}, example=example, request=request)
        if seconds >= LONG_CALL_SECONDS:
            batch.hit("hotspot", ("call", l1), f"a call of {LONG_CALL_SECONDS}+ s: {l1}", f"slow call ({int(seconds)} s): {l1}", entry, ref, seconds=seconds, request=request)
        found = trail.add(l1, l2, ok, errorFp)
        for gram, _ in found["grams"]:
            reading = all(step.startswith(LOOKING) for step in gram)
            fp = batch.hit("sequence", gram, " -> ".join(gram), " -> ".join(gram), entry, ref, level=len(gram), seconds=seconds, request=request,
                           data={"steps": gram, **({"reading": True} if reading else {})})
            self.checkRedo(entry, ref, batch, fp, request)
        if found["loop"]:
            label, fp = found["loop"]
            batch.hit("loop", fp, label, label, entry, ref, error=True, request=request, example=example)
        if found["fix"]:
            error, recipe = found["fix"]
            fix = batch.hit("fix", (error, *recipe), " -> ".join(recipe), f"fixed by {' -> '.join(recipe)}", entry, ref, request=request, data={"recipes": [" -> ".join(recipe)]})
            batch.link(error, fix, "fixedBy")

    # The work of a wheel done by hand: the step or sequence a live wheel covers, made by an agent that has it (ignored) or not (missed).
    def checkRedo(self, entry, ref, batch, fp, request):
        wheel = self.covers.get(fp)
        if not wheel:
            return
        agent = self.agents.get(f"{entry.get('mission')}:{entry.get('agent')}") or {}
        how = "ignored" if wheel in (agent.get("wheels") or []) else "missed"
        batch.hit("redo", (wheel, how), f"{wheel} {how}", f"the work of the wheel {wheel} done by hand ({'the agent had it' if how == 'ignored' else 'nobody gave it'})",
                  entry, ref, request=request, data={"wheel": wheel, "how": how})

    # A command a coding agent ran by itself (Codex): the same patterns as a command through the gateway.
    def on_cmd(self, db, entry, ref, batch):
        failed = entry.get("exit") not in (0, None)
        call = {**entry, "kind": "call", "origin": "codex", "server": "processes", "tool": "run_command", "args": {"command": entry.get("command") or ""},
                "outcome": "error" if failed else "success", "result": (f"Exit code {entry.get('exit')}.\n" if failed else "") + (entry.get("output") or ""),
                "size": len(entry.get("output") or "")}
        self.on_call(db, call, ref, batch)

    def on_bad(self, db, entry, ref, batch):
        trail = self.trailOf(entry)
        name, why = entry.get("name") or "?", entry.get("why") or "parse"
        batch.hit("badcall", (name, why), f"{name}: {why}", f"calls of {name} that could not run ({why})", entry, ref, error=True, request=self.requestKey(entry, trail),
                  example=entry.get("problem") or "")

    def on_permission(self, db, entry, ref, batch):
        if entry.get("how") not in ("asked", "plan"):
            return
        key = str(entry.get("key") or "")
        kind, _, detail = key.partition(":")
        if kind == "run":
            shape = f"run:{shapeCommand(detail, self.root)[1]}"
        elif kind == "wheel":
            shape = f"wheel:{detail.partition('@')[0]}"
        else:
            shape = key[:120]
        decision = entry.get("decision") or ""
        batch.hit("permission", (shape, decision), f"{shape} -> {decision}", f"the user was asked to {entry.get('action') or 'allow'} ({shape}) and said {decision}",
                  entry, ref, error=decision == "deny", example=entry.get("detail") or "")

    def correction(self, db, entry, ref, batch, source, text):
        if not text or not str(text).strip():
            return
        cluster, shape = self.cluster(db, f"correction:{source}", text)
        batch.hit("correction", (source, cluster), f"{source}: {shape}", f"{source} asked: {labelOf(text, 20)}", entry, ref, error=True, example=text, data={"source": source})

    def on_draft(self, db, entry, ref, batch):
        self.correction(db, entry, ref, batch, "check", entry.get("problem"))

    def on_review(self, db, entry, ref, batch):
        if entry.get("reply") == "correction":
            self.correction(db, entry, ref, batch, "user", entry.get("text"))

    def on_user(self, db, entry, ref, batch):
        if entry.get("whileReady"):
            self.correction(db, entry, ref, batch, "user", entry.get("text"))

    def on_route(self, db, entry, ref, batch):
        self.correction(db, entry, ref, batch, "leader", entry.get("comment"))

    def on_message(self, db, entry, ref, batch):
        if not entry.get("direct") or not entry.get("text"):
            return
        cluster, shape = self.cluster(db, "message", entry["text"])
        batch.hit("message", cluster, shape, f"{entry.get('from')} to {entry.get('to')}: {labelOf(entry['text'], 20)}", entry, ref, example=entry["text"])

    def on_status(self, db, entry, ref, batch):
        if entry.get("status") != "failed" or not entry.get("error"):
            return
        found = classifyError(entry["error"]) or {"code": "agent:failed", "template": maskText(entry["error"])[:120], "family": "agent"}
        batch.hit("error", (found["code"], found["template"], "agent"), f"{found['code']}: {found['template']} | agent", f"an agent did not finish: {found['template']}",
                  entry, ref, error=True, data={"family": found["family"]}, example=entry["error"])

    def on_wheel(self, db, entry, ref, batch):
        if entry.get("action") != "run":
            return
        name, ok = entry.get("name") or "?", bool(entry.get("ok"))
        batch.hit("wheel", name, name, f"runs of the wheel {name}", entry, ref, seconds=entry.get("seconds") or 0, error=not ok, data={"pin": entry.get("pin")})
        if not ok:
            error = entry.get("error") or {}
            batch.hit("wheelfail", (name, error.get("type") or "?"), f"{name}: {error.get('type')}", f"the wheel {name} failed: {error.get('type')}: {error.get('message') or ''}",
                      entry, ref, error=True, data={"wheel": name, "pin": entry.get("pin"), "trace": (entry.get("trace") or "")[-1500:]},
                      example=f"{error.get('type')}: {error.get('message')}\n{(entry.get('trace') or '')[-800:]}")


def splitFull(name):
    server, separator, tool = str(name or "").partition("__")
    return (server, tool) if separator else ("", str(name or ""))
