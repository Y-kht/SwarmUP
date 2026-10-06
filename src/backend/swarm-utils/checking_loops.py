# The loops that check a work: the math checker, and the coder, which runs a command to check its code.
import re
import subprocess
import sys
from pathlib import Path

import agent_prompts as prompts
from base_loop import Loop
from harness_utils import isYes, stripFences


RUN_TIMEOUT = 60


# ==============
# Math checker harness. It referees a research text: its proofs, assumptions and logic. Arithmetic is not its job.
# ==============
FINDING_PATTERN = re.compile(r"^CLAIM:(.*?)^QUOTE:(.*?)^STATUS:(.*?)^WHY:(.*?)(?=^CLAIM:|^VERDICT:|\Z)", re.MULTILINE | re.DOTALL)
STATEMENT_PATTERN = re.compile(r"\\begin\{(?:theorem|lemma|proposition|corollary|claim)\}|"
                               r"^\W*(?:theorem|lemma|proposition|corollary|claim)\s+\d", re.MULTILINE | re.IGNORECASE)


class MathCheckLoop(Loop):
    def __init__(self, agent, filePath, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "MATH_CHECK_RULES.md")
        self.path = Path(filePath)
        self.original = self.path.read_text(encoding="utf-8")

    def describeTask(self):
        return f"Referee the mathematical text {self.path.name}: check its theorems, proofs and logic, and report every gap or error."

    def files(self):
        return [self.path]

    # Returns a list of (claim, quote, status, why), or an empty list if a finding is badly written.
    def parseFindings(self, report):
        findings = [(claim.strip(), quote.strip(), status.strip().upper(), why.strip())
                    for claim, quote, status, why in FINDING_PATTERN.findall(report)]
        return findings if len(findings) == len(re.findall(r"^CLAIM:", report, re.MULTILINE)) else []

    # A second agent call, which only sees one finding, tries to refute it. This removes false alarms.
    def challenge(self, quote, why):
        answer = self.askAgent("You are a second, skeptical referee. A reviewer says the passage below has a problem. "
                               "Check it carefully against the whole document. Reply CONFIRMED if it really has this problem, "
                               f"otherwise reply REJECTED and explain why.\nPassage: {quote}\nProblem: {why}\n\nDocument:\n{self.original}")
        return "" if answer.strip().upper().startswith("CONFIRMED") else answer

    def checkReport(self, report):
        verdict = re.search(r"^VERDICT:\s*(NO PROBLEMS FOUND|PROBLEMS FOUND)", report, re.MULTILINE | re.IGNORECASE)
        findings = self.parseFindings(report)
        if not findings or not verdict:
            return f"Write every finding as plain lines, exactly like this:\n{prompts.FINDING_FORMAT}\nThen finish with VERDICT: NO PROBLEMS FOUND or VERDICT: PROBLEMS FOUND."
        document = "".join(self.original.split())
        for claim, quote, status, why in findings:
            if status not in ("VALID", "GAP", "ERROR", "UNCLEAR") or not why:
                return f"The finding '{claim}' needs a STATUS (VALID, GAP, ERROR or UNCLEAR) and a WHY."
            # Whitespace is ignored, so a quote may be broken over several lines. An invented quote means an invented finding.
            if "".join(quote.strip('"“”`').split()) not in document:
                return f"The quote of '{claim}' is not in the document. Copy one passage exactly, character for character."
        statements = len(STATEMENT_PATTERN.findall(self.original))
        if len(findings) < statements:
            return f"The document states {statements} theorems, lemmas, propositions or claims, but you wrote only {len(findings)} findings. Review all of them."
        foundProblems = any(status != "VALID" for claim, quote, status, why in findings)
        saysProblems = verdict.group(1).upper() == "PROBLEMS FOUND"
        if foundProblems != saysProblems:
            return "Your VERDICT does not match your findings. It is NO PROBLEMS FOUND only if every STATUS is VALID."
        for claim, quote, status, why in findings:
            rejection = self.challenge(quote, why) if status in ("GAP", "ERROR") else ""
            if rejection:
                return f"A second referee rejected your finding '{claim}': {rejection}\nExamine the passage again. If you were wrong, change it to VALID."
        return ""

    def run(self):
        task = prompts.MATH_CHECK_PROMPT.format(findingFormat=prompts.FINDING_FORMAT, document=self.original)
        report = self.reviewLoop(task, self.checkReport, key="report")
        if report is None:
            self.notifyUser("The report was not saved.")
            return None
        output = self.path.with_name(f"{self.path.stem}_math_report.md")
        output.write_text(report, encoding="utf-8")
        self.logAction(f"Wrote the report {output}.")
        self.notifyUser(f"The report is saved as {output}.")
        return report


# ==============
# Coding harness. The code is only written and run after the user agrees.
# ==============
class CoderLoop(Loop):
    def __init__(self, agent, task, filePath, testCommand=None, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "CODER_RULES.md")
        self.task = task
        self.path = Path(filePath)
        self.testCommand = testCommand or [sys.executable, str(self.path.resolve())]

    def describeTask(self):
        return f"Write the code of {self.path.name} for this task: {self.task}. It is checked by running: {' '.join(self.testCommand)}"

    def files(self):
        return [self.path]

    # The file holds a draft from the first test on. The original is in the progress, which is saved before the first draft is written.
    def runTests(self, draft):
        if not self.progress.get("touched"):
            self.remember("touched", True)
        self.path.write_text(stripFences(draft), encoding="utf-8")
        try:
            result = subprocess.run(self.testCommand, capture_output=True, text=True, timeout=RUN_TIMEOUT, cwd=self.folder)
        except subprocess.TimeoutExpired:
            return f"The code ran for more than {RUN_TIMEOUT} seconds. Fix any endless loop or make it faster."
        if result.returncode == 0:
            return ""
        return f"Running {' '.join(self.testCommand)} failed with:\n{(result.stdout + result.stderr)[-2000:]}"

    def rollback(self):
        if not self.progress.get("touched") or self.progress.get("kept"):
            return
        original = self.progress["original"]
        if original is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(original, encoding="utf-8")
        self.remember("touched", False)

    def describePending(self):
        if self.progress.get("touched") and not self.progress.get("kept"):
            return f"{self.path} holds a draft of the code that the user did not approve yet. Stopping puts the original back."
        return ""

    def run(self):
        command = " ".join(self.testCommand)
        if not isYes(self.askUser(f"I will write code into {self.path} and run: {command}\nThis changes files on your computer. Continue? (yes/no)")):
            return None
        if not (self.resumed and "original" in self.progress):
            self.progress.update(touched=False, kept=False)
            self.remember("original", self.path.read_text(encoding="utf-8") if self.path.exists() else None)
        original = self.progress["original"]
        task = prompts.CODER_PROMPT.format(fileName=self.path.name, task=self.task, command=command,
                                           current=original if original is not None else "the file does not exist yet")
        try:
            code = self.reviewLoop(task, self.runTests, key="code")
        except BaseException:
            self.rollback()
            raise
        if code is None:
            self.rollback()
            self.notifyUser(f"No code was kept, {self.path} is back to how it was.")
            return None
        self.path.write_text(stripFences(code), encoding="utf-8")
        self.remember("kept", True)
        self.logAction(f"Wrote the code of {self.path}, checked by running: {command}")
        self.notifyUser(f"The code is saved in {self.path}.")
        return stripFences(code)
