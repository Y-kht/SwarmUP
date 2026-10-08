# The general agent: it does what the user (or the leader) writes in its instructions, instead of one task of the list. Its harness is the one of
# every loop (base_loop.py): it works in its conversation with its tools and its folder, follows its rules, and the user approves its result.
# The rules are the general rules of agent-rules (AGENT_RULES.md) and the rules files the user or the leader chose for it (listRules).
# Before the user sees a draft, a strict review checks it against the instructions and the rules (it can read the files to verify it). A real
# problem sends the draft back to the agent with what to fix, CHECK_ROUNDS times at most, so the user gets the best draft the agent can write.
# If the user does not approve the result, what the agent changed in the files is put back.
import agent_prompts as prompts
from base_loop import Loop
from harness_utils import GENERAL_RULES, rulesFileOf


CHECK_ROUNDS = 2


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
        verdict = self.askAgent(prompts.GENERAL_CHECK_PROMPT.format(prompt=self.instructions(), rules=self.rules or "None.", draft=draft), own=False, tools=True).strip()
        return "" if verdict.upper().startswith("GOOD") else verdict

    def run(self):
        if not self.prompt:
            raise ValueError("This agent has no instructions. Write what it must do.")
        self.checks = 0
        task = prompts.FOLLOW_UP_TASK_PROMPT.format(request=self.team.currentRequest(), part=self.roundRequest, prompt=self.prompt) if self.roundRequest and self.team else \
            prompts.GENERAL_TASK_PROMPT.format(prompt=self.instructions())
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
