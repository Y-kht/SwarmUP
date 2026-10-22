# The trail of one agent in one request, for the miner of patterns: the last steps it made (as L1 shapes, pattern_shapes.py), so the miner
# finds the sequences of steps that come back (read a file, run the tests, edit, run the tests again), the loops of a stuck agent (the same
# failing step three times, or two steps that alternate), and how an agent got out of an error (the error, the steps after it, then the same
# kind of step that now works: the fix). A trail is kept in the store between two polls of the miner, so a restart does not cut a sequence.
from pattern_shapes import fingerprint


WINDOW = 6
MIN_GRAM = 2
FIX_WINDOW = 8
RECIPE_STEPS = 3
LOOP_REPEATS = 3


class AgentTrail:
    def __init__(self, data=None):
        data = data or {}
        self.shapes = [list(item) for item in data.get("shapes", [])]
        self.pending = list(data.get("pending", []))
        self.request = data.get("request", 0)
        self.steps = data.get("steps", 0)
        self.prompt = data.get("prompt", "")
        self.inLoop = data.get("inLoop", "")

    def toData(self):
        return {"shapes": self.shapes, "pending": self.pending, "request": self.request, "steps": self.steps, "prompt": self.prompt, "inLoop": self.inLoop}

    def newRequest(self, prompt=""):
        self.request += 1
        self.shapes, self.pending, self.steps, self.prompt, self.inLoop = [], [], 0, prompt, ""

    # A step of the agent: fp and l1 of its action, l2 its family, ok whether it worked, error the fingerprint of its error if it failed.
    # It gives what the new step completes: {"grams": [(steps, fp)], "loop": (label, fp) or None, "fix": (error, recipe) or None}.
    def add(self, l1, l2, ok, error=None):
        found = {"grams": [], "loop": None, "fix": None}
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
        self.shapes = [*self.shapes, [l1, ok]][-WINDOW:]
        steps = [shape for shape, worked in self.shapes]
        for size in range(MIN_GRAM, len(steps) + 1):
            gram = steps[-size:]
            if len(set(gram)) > 1:
                found["grams"].append((gram, fingerprint("sequence", *gram)))
        found["loop"] = self.loopOf()
        return found

    # A stuck agent: the same failing step LOOP_REPEATS times, or two steps that alternate twice. Each loop is told once.
    def loopOf(self):
        steps = [shape for shape, worked in self.shapes]
        failed = [not worked for shape, worked in self.shapes]
        loop = None
        if len(steps) >= LOOP_REPEATS and len(set(steps[-LOOP_REPEATS:])) == 1 and any(failed[-LOOP_REPEATS:]):
            loop = (f"{steps[-1]} again and again, failing", fingerprint("loop", steps[-1]))
        elif len(steps) >= 4 and steps[-1] == steps[-3] and steps[-2] == steps[-4] and steps[-1] != steps[-2]:
            loop = (f"{steps[-2]} then {steps[-1]}, back and forth", fingerprint("loop", *sorted(steps[-2:])))
        if loop is None:
            self.inLoop = ""
            return None
        if loop[1] == self.inLoop:
            return None
        self.inLoop = loop[1]
        return loop
