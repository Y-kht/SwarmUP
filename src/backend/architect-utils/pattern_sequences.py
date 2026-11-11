# The trail of one agent in one request, for the miner of patterns: the last steps it made (as L1 shapes, pattern_shapes.py), so the miner
# finds the sequences of steps that come back (read a file, run the tests, edit, run the tests again), the loops of a stuck agent (the same
# failing step three times, or two steps that alternate), and how an agent got out of an error (the error, the steps after it, then the same
# kind of step that now works: the fix). A trail is kept in the store between two polls of the miner, so a restart does not cut a sequence.
# A sequence counts only when its calls come from two model steps or more: calls the model made together in one step cost no step more, and a
# wheel that makes them at once would save nothing. A streak is the same kind of call in many model steps in a row (search a name, then the
# next name...): one call that takes the list would save the steps.
from pattern_shapes import fingerprint


WINDOW = 6
MIN_GRAM = 2
FIX_WINDOW = 8
RECIPE_STEPS = 3
LOOP_REPEATS = 3
STREAK_STEPS = 3
STREAK_EXAMPLES = 6


class AgentTrail:
    def __init__(self, data=None):
        data = data or {}
        self.shapes = [list(item) for item in data.get("shapes", [])]
        self.pending = list(data.get("pending", []))
        self.request = data.get("request", 0)
        self.steps = data.get("steps", 0)
        self.prompt = data.get("prompt", "")
        self.inLoop = data.get("inLoop", "")
        self.stepCalls = list(data.get("stepCalls", []))
        self.calls = data.get("calls", 0)
        self.streak = data.get("streak") or {"shape": "", "steps": [], "examples": []}

    def toData(self):
        return {"shapes": self.shapes, "pending": self.pending, "request": self.request, "steps": self.steps, "prompt": self.prompt, "inLoop": self.inLoop,
                "stepCalls": self.stepCalls, "calls": self.calls, "streak": self.streak}

    def newRequest(self, prompt=""):
        self.endStreak()
        self.request += 1
        self.shapes, self.pending, self.steps, self.prompt, self.inLoop, self.stepCalls = [], [], 0, prompt, "", []

    # A step of the model: the ids of the calls it made.
    def newStep(self, callIds):
        self.steps += 1
        self.stepCalls = [ident for ident in callIds if ident]

    # The model step a call came from: a call that is not in the last step (an old journal without steps) is a step of its own.
    def stepOf(self, callId):
        self.calls += 1
        return f"s{self.steps}" if callId and callId in self.stepCalls else f"c{self.calls}"

    # The same kind of call again in a new model step makes the streak longer; another kind ends it. It gives the streak that ended:
    # (shape, model steps, examples of the exact calls), or None.
    def follow(self, l1, step, l0=""):
        streak = self.streak
        if l1 == streak["shape"]:
            if step not in streak["steps"]:
                streak["steps"] = [*streak["steps"], step][-200:]
            if l0 and l0 not in streak["examples"] and len(streak["examples"]) < STREAK_EXAMPLES:
                streak["examples"].append(l0)
            return None
        ended = self.endStreak()
        self.streak = {"shape": l1, "steps": [step], "examples": [l0] if l0 else []}
        return ended

    def endStreak(self):
        streak, self.streak = self.streak, {"shape": "", "steps": [], "examples": []}
        return (streak["shape"], len(streak["steps"]), streak["examples"]) if streak["shape"] and len(streak["steps"]) >= STREAK_STEPS else None

    # A step of the agent: fp and l1 of its action, l2 its family, ok whether it worked, error the fingerprint of its error if it failed, step
    # the model step it came from, l0 its exact shape. It gives what the new step completes: {"grams": [(steps, fp, model steps it spans)],
    # "loop": (label, fp) or None, "fix": (error, recipe) or None, "streak": a streak that ended or None}.
    def add(self, l1, l2, ok, error=None, step=None, l0=""):
        found = {"grams": [], "loop": None, "fix": None, "streak": None}
        for pending in list(self.pending):
            if ok and pending["family"] == l2:
                found["fix"] = (pending["error"], [*pending["recipe"][-RECIPE_STEPS:], l1])
                self.pending.remove(pending)
                break
        for pending in self.pending:
            pending["recipe"].append(l1)
            pending["age"] += 1
        self.pending = [pending for pending in self.pending if pending["age"] <= FIX_WINDOW]
        if not ok and error:
            self.pending.append({"error": error, "family": l2, "recipe": [], "age": 0})
        if step is None:
            self.calls += 1
            step = f"c{self.calls}"
        self.shapes = [*self.shapes, [l1, ok, step, l0]][-WINDOW:]
        # A streak is of one family of calls (the tool, its arguments, the kind of file): an edit of 12 words and one of 9 are the same work.
        found["streak"] = self.follow(l2, step, l0) if ok else self.endStreak()
        steps = [item[0] for item in self.shapes]
        spans = [item[2] if len(item) > 2 else f"old{number}" for number, item in enumerate(self.shapes)]
        for size in range(MIN_GRAM, len(steps) + 1):
            gram, spanned = steps[-size:], len(set(spans[-size:]))
            if len(set(gram)) > 1 and spanned >= MIN_GRAM:
                found["grams"].append((gram, fingerprint("sequence", *gram), spanned))
        found["loop"] = self.loopOf()
        return found

    # A stuck agent: the same failing step LOOP_REPEATS times, or the same two calls that alternate twice. Each loop is told once.
    # Two steps of the same kind with other inputs each time are work, not a loop: an agent that edits a file, runs the tests, edits another
    # passage and runs them again goes forward (in a real run, every fix of a bug was counted as a stuck agent).
    def loopOf(self):
        steps = [item[0] for item in self.shapes]
        failed = [not item[1] for item in self.shapes]
        exact = [item[3] if len(item) > 3 and item[3] else item[0] for item in self.shapes]
        loop = None
        if len(steps) >= LOOP_REPEATS and len(set(steps[-LOOP_REPEATS:])) == 1 and any(failed[-LOOP_REPEATS:]):
            loop = (f"{steps[-1]} again and again, failing", fingerprint("loop", steps[-1]))
        elif len(steps) >= 4 and steps[-1] == steps[-3] and steps[-2] == steps[-4] and steps[-1] != steps[-2] and exact[-1] == exact[-3] and exact[-2] == exact[-4]:
            loop = (f"{steps[-2]} then {steps[-1]}, back and forth", fingerprint("loop", *sorted(steps[-2:])))
        if loop is None:
            self.inLoop = ""
            return None
        if loop[1] == self.inLoop:
            return None
        self.inLoop = loop[1]
        return loop
