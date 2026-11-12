# The general agent: it does what the user (or the leader) writes in its instructions, instead of one task of the list. Its harness is the one of
# every loop (base_loop.py): it works in its conversation with its tools and its folder, follows its rules, and the user approves its result.
# The rules are the general rules of agent-rules (AGENT_RULES.md) and the rules files the user or the leader chose for it (listRules).
# Before the user sees a draft, a strict review checks it against the instructions and the rules (it can read the files to verify it). A real
# problem sends the draft back to the agent with what to fix, CHECK_ROUNDS times at most, so the user gets the best draft the agent can write.
# If the user does not approve the result, what the agent changed in the files is put back.
from pathlib import Path

import agent_prompts as prompts
from base_loop import Loop
from harness_utils import GENERAL_RULES, rulesFileOf


CHECK_ROUNDS = 2
ECHO_CHARS = 120
RECORD_FILES = 15
RECORD_CALLS = 12
RECORD_CHARS = 240
# The tools that only look: what the review must know is what the agent did, not what it read.
LOOKING_TOOLS = ("files__list_files", "files__find_files", "files__search_files", "files__read_file", "team__read", "transfer__list_clips", "processes__check_process")


# What the agent really did with its tools, for the review of its draft: the files it changed (those SwarmUP keeps to put back) and its last calls
# that did something, with how they ended. In a real run, a small model wrote "fixed, and all the tests pass" without changing a file, after a run of the
# tests that failed, and its review could not tell: with the records it can.
def describeWork(loop):
    changed = [Path(change["path"]).name for change in loop.progress.get("changes") or []]
    lines = [f"- The files it changed: {', '.join(changed[:RECORD_FILES]) if changed else 'none'}."]
    messages = loop.conversation.messages if loop.conversation is not None else []
    acts = [message for message in messages if message["role"] == "tool" and not str(message.get("name") or "").startswith(LOOKING_TOOLS)]
    for message in acts[-RECORD_CALLS:]:
        result = " ".join(str(message.get("content") or "").split())
        lines.append(f"- {message['name']}: {'it failed. ' if message.get('error') else ''}{result[:RECORD_CHARS]}")
    if not acts:
        lines.append("- It only looked: it ran no command and no wheel, and changed nothing with its tools.")
    return "\n".join(lines)


# A verdict that starts with GOOD, or that ends with it after the model thought aloud.
def isGood(verdict):
    lines = [line.strip(" .*`\"'") for line in verdict.strip().splitlines() if line.strip()]
    return verdict.strip().upper().startswith("GOOD") or bool(lines) and lines[-1].upper() == "GOOD"


# Whether a verdict only says the draft again: one of them starts like a part of the other.
def repeats(verdict, draft):
    said, written = " ".join(verdict.split()), " ".join(draft.split())
    return len(said) >= ECHO_CHARS and len(written) >= ECHO_CHARS and (said[:ECHO_CHARS] in written or written[:ECHO_CHARS] in said)


class GeneralLoop(Loop):
    # rules are names of listRules (or their files).
    def __init__(self, agent, prompt, rules=(), numberOfLoops=5):
        self.ruleNames = [str(name) for name in rules or ()]
        files = [rulesFileOf(name) for name in self.ruleNames]
        unknown = [name for name, file in zip(self.ruleNames, files) if file is None]
        if unknown:
            raise ValueError(f"There are no rules called {', '.join(unknown)} in agent-rules.")
        super().__init__(agent, numberOfLoops, [GENERAL_RULES, *dict.fromkeys(files)])
        self.prompt = str(prompt or "").strip()
        self.checks = 0

    # The instructions, with the part of the new request of the user in a round after the first.
    def instructions(self):
        return f"{self.prompt}\n\nIn this round, the user also asks: {self.roundRequest}" if self.roundRequest else self.prompt

    def describeTask(self):
        ended = not self.ruleNames or self.prompt.endswith((".", "!", "?"))
        return (self.prompt if ended else f"{self.prompt}.") + self.describeRules()

    def describeRules(self):
        return f" It follows the rules: {', '.join(self.ruleNames)}." if self.ruleNames else ""

    # The review of a draft: "" when it is good, otherwise what the agent must fix. A draft is reviewed CHECK_ROUNDS times at most in a run, so
    # the agent is never sent back again and again for small things.
    def checkResult(self, draft):
        if not draft.strip():
            return "Your answer is empty. Do the work and give its result."
        if self.checks >= CHECK_ROUNDS:
            return ""
        self.checks += 1
        prompt = prompts.GENERAL_CHECK_PROMPT.format(prompt=self.instructions(), rules=self.rules or "None.", draft=draft, work=describeWork(self))
        # A capable model verifies the draft in the files. A small local model loses its way there (in real runs it searched for 34 steps, then gave
        # the result back instead of a verdict, and the agent was told to change its result into itself): it judges from the records, which are in
        # the prompt, in one answer. So does a model whose review with tools gave no verdict. A result given back twice is no verdict: the user reads the draft.
        local = bool(getattr(self.agent, "local", False))
        verdict = "" if local else self.askAgent(prompt, own=False, tools=True).strip()
        if local or repeats(verdict, draft):
            verdict = self.askAgent(prompt, own=False, tools=False, alone=True).strip()
        return "" if isGood(verdict) or repeats(verdict, draft) else verdict

    def followUpTask(self):
        request = self.team.currentRequest()
        part = prompts.FOLLOW_UP_PART.format(part=self.roundRequest) if self.roundRequest.strip() != request.strip() else ""
        return prompts.FOLLOW_UP_TASK_PROMPT.format(request=request, part=part, prompt=self.prompt)

    def run(self):
        if not self.prompt:
            raise ValueError("This agent has no instructions. Write what it must do.")
        self.checks = 0
        task = self.followUpTask() if self.roundRequest and self.team else prompts.GENERAL_TASK_PROMPT.format(prompt=self.instructions())
        result = self.reviewLoop(task, self.checkResult, key="result")
        if result is None:
            if self.rollback():
                self.notifyUser("The result was not approved, so every file the agent changed is back to how it was.")
            else:
                self.notifyUser("The result was not kept.")
            return None
        path = self.saveResult("result", result)
        self.notifyUser(f"The result is saved in {path}.")
        return result
