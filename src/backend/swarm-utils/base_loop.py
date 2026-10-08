# The base of every loop: it writes a draft, checks it, asks the user, and remembers what it did.
import getpass
import os
import threading
from datetime import datetime
from pathlib import Path

import agent_prompts as prompts
from agent_conversation import canConverse, talk
from agent_storehouse import consultFolder, describeWorkplace, folderProblem, resultsFolder, safeName
from agent_tools import describeChanges, undoChanges
from harness_utils import ConnectionLost, USER_LOCK, asConnectionLost, checkLength, isNo, isOnline, isYes, loadRules


PLAN_LENGTH = 150


# The parent of all loops. A loop only has to define its tools, its task, and how to verify the draft.
class Loop:
    def __init__(self, agent, numberOfLoops=5, rulesFile=None):
        self.agent = agent
        self.numberOfLoops = numberOfLoops
        self.rules = loadRules(rulesFile)
        self.settings = {}
        self.inbox = []
        self.userMessages = []
        self.approvedPlan = ""
        # In a round after the first, the part of the new request of the user that this agent must do (swarm_rounds.py).
        self.roundRequest = ""
        self.planning = False
        self.reviewer = None
        self.name = type(self).__name__
        self.folder = None
        # What the agent did in its run, so a run that was interrupted goes on from there instead of starting again (see Swarm.resume).
        # progress is only read when resumed is True. A swarm sets onProgress, onConnectionLost and resumed.
        self.progress = {}
        self.actions = []
        self.resumed = False
        self.onProgress = None
        self.onConnectionLost = None
        # When the leader manages the swarm, every answer of its model goes through onAnswer (LeaderManager.readOutput), which acts on the
        # blocks it finds and gives back the text without them.
        self.onAnswer = None
        # The model answers one prompt at a time, even when two threads ask it (the leader that works, and the manager of the swarm).
        self.thinking = threading.RLock()
        # Called after every call of the model, even one that failed (it may have been billed), so the cost of the mission is shown as it grows.
        self.onUsage = None
        # The memory of the swarm session the agent works in (WorkSession in agent_storehouse.py), set by the swarm while it runs.
        self.session = None
        # The conversation with a model that can hold one (agent_conversation.py), the swarm the agent reaches with its tools (relay,
        # describeTeamFor, resultOf, shouldStop), what the user allowed it until the swarm runs again, and what it did lately with its tools.
        self.conversation = None
        self.team = None
        self.allowed = set()
        self.activity = []
        self.onActivity = None
        # What the loop says and asks is never mixed with what another loop says and asks to the same user. A window with several missions gives
        # each of them its own lock, so a question of one mission never waits for the answer to another.
        self.userLock = USER_LOCK

    # Messages sent by the other agents of a swarm. The agent reads them with every prompt.
    def receive(self, sender, message):
        self.inbox.append(f"{sender}: {message}")

    # Messages typed by the user while the agent works. The agent reads them with its next prompt,
    # and reviewLoop writes again a draft that was started before one of them arrived.
    def receiveFromUser(self, message):
        self.userMessages.append(message)

    # Everything an agent needs to go on after the program stopped. It holds no password: those are never saved.
    def getState(self):
        return {"progress": dict(self.progress), "actions": list(self.actions), "inbox": list(self.inbox), "userMessages": list(self.userMessages),
                "approvedPlan": self.approvedPlan, "roundRequest": self.roundRequest}

    def setState(self, state):
        self.progress = dict(state.get("progress", {}))
        self.actions = list(state.get("actions", []))
        self.inbox = list(state.get("inbox", []))
        self.userMessages = list(state.get("userMessages", []))
        self.approvedPlan = state.get("approvedPlan", "")
        self.roundRequest = state.get("roundRequest", "")

    def saveProgress(self):
        if self.onProgress:
            self.onProgress()

    def remember(self, key, value):
        self.progress[key] = value
        self.saveProgress()

    # What the agent changed outside of itself (an email sent, a file written...). The leader tells it to the user if the swarm is stopped.
    def logAction(self, text):
        self.actions.append({"time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "text": text})
        self.saveProgress()

    # The files the agent works on by itself. They must be inside its folder.
    def files(self):
        return []

    # The folder where the agent works: what it saves goes in it, and the files it works on must be in it. Without a folder nothing changes.
    def setFolder(self, folder):
        if not folder:
            self.folder = None
            return
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"There is no folder at {path}.")
        problem = folderProblem(path)
        if problem:
            raise ValueError(problem)
        for file in self.files():
            if not file.expanduser().resolve().is_relative_to(path):
                raise ValueError(f"{file.name} is not inside {path}. Choose a folder that contains it.")
        self.folder = path

    def describeFolder(self):
        return f" It works inside the folder {self.folder}." if self.folder else ""

    # Saves what the agent produced in the folder of the run (resultsFolder in agent_storehouse.py): swarmup-results/<run> inside its folder,
    # or agent-files/results/<run> without a folder. In a swarm the run is the one of the swarm. Alone, the run is named by the time of the
    # first save, and a run that is resumed keeps it, so the same file is written again instead of a new one.
    def saveResult(self, name, text, suffix=".md"):
        if self.session:
            runName = self.session.runName
        else:
            if not (self.resumed and self.progress.get("stamp")):
                self.progress["stamp"] = f"{datetime.now():%Y-%m-%d_%H-%M-%S}"
            runName = self.progress["stamp"]
        folder = resultsFolder(self.folder, runName)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safeName(self.name)}_{name}{suffix}"
        path.write_text(text, encoding="utf-8")
        self.logAction(f"Saved {path}.")
        return path

    # Puts back what an unfinished run changed in the files with its tools (see undoChanges in agent_tools.py). It gives how many were put back.
    def rollback(self):
        return undoChanges(self)

    # What the agent would leave half done if the swarm were stopped now.
    def describePending(self):
        return describeChanges(self)

    # What the agent does with its tools, for the user to follow it live.
    def recordActivity(self, text):
        self.activity = [*self.activity[-19:], {"time": f"{datetime.now():%H:%M:%S}", "text": text}]
        if self.onActivity:
            self.onActivity(text)
        else:
            self.notifyUser(f"[{self.name}] {text}")

    # Runs a step that needs the network. If the connection is lost inside a swarm, the agent waits here until the user decides:
    # to continue (the step is tried again) or to stop (SwarmStopped). Outside of a swarm the error goes up as it is.
    def keepTrying(self, step):
        while True:
            try:
                return step()
            except (ConnectionLost, OSError) as error:
                lost = asConnectionLost(error) if self.onConnectionLost else None
                if not lost:
                    raise
                self.onConnectionLost(lost)

    # Inside a swarm, a step that found nothing because the internet is gone is a lost connection, and not an empty result.
    def checkOnline(self, what):
        if self.onConnectionLost and not isOnline():
            raise ConnectionLost(f"The internet connection was lost while {what}.")

    # An email or a message cannot be taken back, so it must never go twice. If the program stopped while it was being sent, nobody knows
    # if it left, so the user decides. It returns True if it was sent now.
    def sendOnce(self, key, send, what):
        state = self.progress.get(key) if self.resumed else None
        if state == "sent":
            return False
        if state == "sending" and not isYes(self.askUser(f"The program stopped while {what}. It may or may not have gone out. Send it again? (yes/no)")):
            self.remember(key, "sent")
            return False
        self.remember(key, "sending")
        try:
            self.keepTrying(send)
        except BaseException:
            self.remember(key, "")
            raise
        self.remember(key, "sent")
        return True

    # own is False for the calls a leader makes to manage the swarm (summaries, corrections): the rules and the plan
    # of the task of the leader, like the rules of emails, do not apply to them.
    # With tools (by default for its own calls) the agent sees the files of its folder and can read them before it answers, and it keeps
    # notes in the memory of the session (see agent_storehouse.py). The calls that only check a draft go without them.
    # A model that can hold a conversation works in one, with all its tools (agent_conversation.py): its rules, its plan and the messages it
    # receives are part of the conversation. Another model is asked one prompt at a time, with them written before the prompt.
    def askAgent(self, prompt, own=True, tools=None):
        tools = own if tools is None else tools
        if tools and canConverse(self.agent):
            with self.thinking:
                answer = talk(self, prompt, own)
            return self.onAnswer(answer) if self.onAnswer else answer
        prompt = describeWorkplace(self, prompt, tools)
        if own and self.approvedPlan:
            prompt = prompts.APPROVED_PLAN_PROMPT.format(plan=self.approvedPlan) + f"\n\n{prompt}"
        if self.userMessages:
            prompt = prompts.USER_MESSAGES_PROMPT.format(messages="\n".join(f"- {message}" for message in self.userMessages)) + f"\n\n{prompt}"
        if self.inbox:
            prompt = "Messages from the other agents of your swarm:\n" + "\n".join(self.inbox) + f"\n\n{prompt}"
        if own and self.rules:
            prompt = f"Follow these rules strictly:\n{self.rules}\n\n{prompt}"
        with self.thinking:
            # A coding agent (Claude Code, Codex) works in the folder of the loop and asks the user through it before it acts.
            if hasattr(self.agent, "attach"):
                self.agent.attach(self)
            answer = consultFolder(self, prompt, lambda text: self.callModel(lambda: self.agent.input(text), own), tools)
        return self.onAnswer(answer) if self.onAnswer else answer

    # One call of the model (work). The calls of a leader that manages the swarm never wait for the user: they have a fallback of their own.
    def callModel(self, work, own):
        try:
            return self.keepTrying(work) if own else work()
        finally:
            if self.onUsage:
                self.onUsage()

    # The next three are the only places where the user is spoken to. A user interface can replace them.
    def notifyUser(self, message):
        with self.userLock:
            print(message)

    def askUser(self, question):
        with self.userLock:
            return input(f"{question} ")

    def askSecret(self, question):
        with self.userLock:
            return getpass.getpass(f"{question} ")

    # A coding agent wants to act: run a command, change files, read outside its folder, use the web... request is
    # {"action": what it wants to do, in words, "detail": the command, file or address, "folder": where, "reason": why (may be empty)}.
    # The answer is {"decision": "once", "run" (the same action is allowed until the swarm runs again) or "deny", "message": why it is denied}.
    def askPermission(self, request):
        with self.userLock:
            reason = f"\nWhy: {request['reason']}" if request.get("reason") else ""
            self.notifyUser(f"[{self.name}] wants to {request['action']}:\n{request['detail']}\nIn: {request.get('folder') or 'its folder'}{reason}")
            reply = self.askUser("Type yes to allow it this time, always to allow it until the swarm runs again, or no (followed by why, if you like):").strip()
        word, _, rest = reply.partition(" ")
        if word.lower() in ("always", "run"):
            return {"decision": "run", "message": ""}
        if isYes(word):
            return {"decision": "once", "message": ""}
        return {"decision": "deny", "message": (rest if isNo(word) else reply).strip()}

    # A coding agent asks the user questions. Each question is {"id", "header", "question", "options": [{"label", "description"}],
    # "multiple": several options can be chosen, "secret": the answer is hidden}. The answer is {id: [chosen labels, or the text typed]}.
    def askQuestions(self, questions):
        answers = {}
        with self.userLock:
            for question in questions:
                options = question.get("options") or []
                listed = "".join(f"\n  {number}. {option['label']}" + (f": {option['description']}" if option.get("description") else "") for number, option in enumerate(options, 1))
                self.notifyUser(f"[{self.name}] {question['question']}{listed}")
                ask = self.askSecret if question.get("secret") else self.askUser
                reply = ask("Your answer (numbers like 1,3, or your own words):" if options else "Your answer:").strip()
                picked = [options[int(part) - 1]["label"] for part in reply.split(",") if part.strip().isdigit() and 1 <= int(part) <= len(options)]
                answers[question["id"]] = picked or ([reply] if reply else [])
        return answers

    # The leader that builds the swarm proposes it, or proposes a change while it runs (leader_utils.py): proposal is {"action": build, add, remove
    # or model, "agent": the agent it is about, "why": the reason of the leader, "text": the change in a sentence, "summary": all of it in plain
    # text, and the details}. The answer is {"decision": "approve" or "reject", "message": what the user tells the leader to change}.
    def askProposal(self, proposal):
        with self.userLock:
            self.notifyUser(f"[{self.name}] {proposal.get('summary') or 'proposes: ' + proposal['text']}" + (f"\nWhy: {proposal['why']}" if proposal.get("why") else ""))
            reply = self.askUser("Type yes to approve it, or no (followed by what you want instead, if you like):").strip()
        word, _, rest = reply.partition(" ")
        if isYes(word):
            return {"decision": "approve", "message": ""}
        return {"decision": "reject", "message": (rest if isNo(word) else reply).strip()}

    # Passwords and addresses of servers come from the environment variables, otherwise the user is asked once.
    # They are only kept in memory while the loop exists, never saved to a file.
    def getSetting(self, name, secret=False):
        if name not in self.settings:
            question = f"Please enter {name}:"
            self.settings[name] = os.environ.get(name) or (self.askSecret(question) if secret else self.askUser(question))
        return self.settings[name]

    # Returns (username, password) of an account the user has on a website, or None if the user has none.
    def askLogin(self, host):
        with self.userLock:
            self.notifyUser(f"[{self.name}] {host} asks for an account. It is only used for this run and never saved. Leave the username empty to skip.")
            username = self.askUser(f"Username for {host}:").strip()
            return (username, self.askSecret(f"Password for {host}:")) if username else None

    def describe(self, draft):
        return draft

    # What the agent does, in a sentence. The swarm gives it to the agent as its task, and the user reads it.
    def describeTask(self):
        return ""

    # When the agent must start, as a datetime, if it has a time of its own. None means as soon as the agents it waits for are done.
    def startTime(self):
        return None

    def checkRules(self, draft):
        if not self.rules:
            return ""
        answer = self.askAgent("Check the draft below against the rules that apply to what the agent writes. "
                               f"Reply only OK if it follows all of them, otherwise list the rules it breaks.\n\n{draft}", tools=False)
        return "" if answer.strip().upper().startswith("OK") else answer

    def checkNewMessages(self, messagesBefore):
        return "The user sent new messages while this draft was being written. Make sure the draft follows them." if len(self.userMessages) > messagesBefore else ""

    def checkPlanText(self, draft):
        return checkLength(draft, PLAN_LENGTH) if draft.strip() else "Write the plan."

    # Where the user decides about a draft. The answer is yes, no, or what to change.
    # A swarm sets reviewer, so the user can answer through the summary of the leader or by clicking on the agent, instead of in the console.
    def askApproval(self, draft, problem):
        shown = draft if self.planning else self.describe(draft)
        if self.reviewer:
            return self.reviewer(shown, problem)
        with self.userLock:
            self.notifyUser(f"[{self.name}]\n{shown}")
            self.notifyUser(f"Warning, the automatic checks found a problem: {problem}" if problem else "The automatic checks passed.")
            return self.askUser("Is this good to go? Type yes, no, or what you want changed:")

    # Planning mode of an agent: it writes a plan for its task and does nothing else. The plan needs the approval of the user.
    # Once approved, the plan is part of every prompt of the agent, so it follows it when it executes.
    def makePlan(self, task):
        self.planning = True
        try:
            plan = self.reviewLoop(prompts.PLAN_PROMPT.format(task=task, maxWords=PLAN_LENGTH), self.checkPlanText)
        finally:
            # The conversation of the plan only looked: the work starts a new one, with the approved plan.
            self.planning, self.conversation = False, None
        self.approvedPlan = plan or ""
        return plan

    # Write a draft, verify it, show it to the user, and improve it until the user approves.
    # verify(draft) returns "" if the draft is fine, or a sentence describing the problem.
    # Without ask the user is not involved: the draft is returned when it passes the checks, otherwise None.
    # With a key the approved draft is remembered, and a run that is resumed gets it back instead of asking the user again.
    def reviewLoop(self, task, verify, ask=True, own=True, key=None):
        if key and self.resumed and key in self.progress:
            return self.progress[key]
        draft, feedback = "", ""
        for attempt in range(1, self.numberOfLoops + 1):
            prompt = task
            if feedback:
                # In a conversation the model still has the task and its draft, so it is only told what to change.
                conversing = own and self.conversation is not None and canConverse(self.agent)
                prompt = prompts.CONVERSATION_REVISION_PROMPT.format(feedback=feedback) if conversing else \
                    f"{task}\n\nYour previous draft:\n{draft}\n\nImprove it. What to change: {feedback}"
            messagesBefore = len(self.userMessages)
            draft = self.askAgent(prompt, own)
            problem = verify(draft) or self.checkNewMessages(messagesBefore)
            if problem and attempt < self.numberOfLoops:
                feedback = problem
                continue
            if not ask:
                return None if problem else draft
            reply = self.askApproval(draft, problem)
            if isYes(reply):
                if key:
                    self.remember(key, draft)
                return draft
            if isNo(reply):
                return None
            feedback = reply
        self.notifyUser(f"Stopped after {self.numberOfLoops} drafts.")
        return None
