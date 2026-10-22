# The loop of the architect of a swarm (architect_manager.py runs it in the background). It is not a member of the team: it has no task, it
# never waits for the user and never asks the user anything, and it reaches only its own tools (server_architect.py) and the reading tools of
# the files of the project. Its desk (ArchitectDesk) is where its tools find the project, the store of the patterns, its pass and its manager,
# and it stands for the team where SwarmUP looks for one: a stop of the swarm or the end of its budget stops the pass at its next step.
import threading

from architect_prompts import ARCHITECT_RULES_HEADING, ARCHITECT_SYSTEM_PROMPT
from base_loop import Loop
from harness_utils import ARCHITECT_NAME, loadRules
from models_library import DEFAULT_CLI_MODEL, MODELS_API, getModelInfo


RULES_FILE = "ARCHITECT.md"
MAX_STEPS = 14
READING_TOOLS = {"list_files", "find_files", "search_files", "read_file"}


class ArchitectDesk:
    # manager: the ArchitectManager of the swarm. The pass is the work of the architect between two waits.
    def __init__(self, manager):
        self.manager = manager
        self.leader, self.members, self.allowedAll = None, {}, set()
        self.stopped = threading.Event()
        self.passId, self.since = "", None

    @property
    def project(self):
        return self.manager.project

    def store(self):
        return self.manager.store()

    def shouldStop(self, name):
        return self.stopped.is_set() or self.manager.overBudget()

    def describeTeamFor(self, name):
        return "The architect works alone."

    def relay(self, sender, receiver, message):
        raise ValueError("The architect does not write to the agents.")


class ArchitectLoop(Loop):
    isArchitect = True

    def __init__(self, model):
        super().__init__(model, numberOfLoops=1, rulesFile=RULES_FILE)
        self.name = ARCHITECT_NAME
        self.maxSteps = MAX_STEPS
        self.toolScope = {"architect": "*", "files": READING_TOOLS}
        self.desk = None
        self.say = None

    def describeTask(self):
        return "find what the agents of the project repeat, and turn it into wheels."

    def systemPrompt(self):
        manager = self.desk.manager if self.desk else None
        rules = f"{ARCHITECT_RULES_HEADING}{self.rules}" if self.rules else ""
        return ARCHITECT_SYSTEM_PROMPT.format(project=getattr(manager, "projectName", "") or "of this swarm", folder=self.folder or "no folder",
                                              rules=rules).strip()

    # The architect never asks: a permission is refused, a question has no answer, and what it would say goes to its own window.
    def notifyUser(self, message):
        if self.say is not None:
            self.say(message)

    def askUser(self, question):
        return ""

    def askUntil(self, question, stale):
        return ""

    def askSecret(self, question):
        return ""

    def askPermission(self, request):
        return {"decision": "deny", "message": "The architect only reads; it never acts outside SwarmUP."}

    def askQuestions(self, questions):
        return {}

    def askProposal(self, proposal):
        return {"decision": "reject", "message": "The architect proposes nothing."}

    def askLogin(self, host):
        return None


# The model the architect runs on, from what the user chose at the setup of the swarm. choice is {"same": name of an agent} or the info
# of a model (models_library.py). It gives (info, how): how is "share" for the instance of a local model of an agent, or "own" for a client
# of its own (an API model, a model the user picked), or (None, "") when no model can run it (a swarm of the user and of Codex only).
def pickArchitect(swarm, choice):
    if not choice:
        return None, ""
    if "same" in choice:
        name = choice["same"]
        member = swarm.members.get(name)
        info = (member or {}).get("model") or {}
        if not info or info.get("user"):
            return None, ""
        if info.get("cli") == "codex":
            return None, ""
        if info.get("cli") == "claude-code":
            name = info["name"] if info["name"] != DEFAULT_CLI_MODEL else next(iter(MODELS_API["claude"]))
            return getModelInfo(name, provider="claude"), "own"
        return info, "share" if info.get("local") else "own"
    if choice.get("user") or choice.get("cli") == "codex":
        return None, ""
    return choice, "own"
