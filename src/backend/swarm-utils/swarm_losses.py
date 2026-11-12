# When a swarm (Swarm in swarm_harness.py) loses an agent. An agent that failed for good leaves the swarm, and its model frees its memory. An agent
# that waited for the result of an agent that failed or left does not fail with it: the leader decides if it can still do its task. It goes on
# without that result, or it leaves the swarm too, and then the agents that wait for it are decided about in the same way.
# No approval is asked for this: an agent that waits did nothing yet, and the user still approves everything it does if it goes on.
import json

import agent_prompts as prompts
from agent_journal import clip
from harness_utils import isYes
from loose_json import readReply


KEEP, REMOVE = "keep", "remove"


# What the leader decided about an agent that lost what it waited for: (keep or remove, why), or None when its reply does not say.
def readDecision(reply):
    try:
        decision = readReply(reply)
    except ValueError:
        return None
    word = str(decision.get("decision") or "").strip().strip(".").lower()
    return (word, str(decision.get("why") or "").strip()) if word in (KEEP, REMOVE) else None


class SwarmLosses:
    # An agent failed for good when its work ended without a result because of what happened to it. A draft the user rejected and a swarm the user
    # stopped are choices of the user, not failures: the next run can still use the agent.
    def failedForGood(self, member):
        return member["result"] is None and not self.stopped and not member.get("removed") and member["review"] != "rejected"

    # The leader speaks to the user for the swarm, so it never leaves: it only lets its memory go.
    def dropIfFailed(self, name, member):
        if not self.failedForGood(member):
            return
        if name == self.leader:
            self.letGo(member)
        else:
            self.removeAgent(name, f"It failed: {member['error']}")

    def checkDecision(self, draft):
        return "" if readDecision(draft) else f"Reply only with JSON like {prompts.LOST_AGENT_EXAMPLE}: the decision is one word, {KEEP} or {REMOVE}."

    # The answer of the leader: (keep or remove, why). A user who leads the swarm is asked. A leader that cannot answer lets the agent try, since nothing
    # it does happens without the approval of the user.
    def askAboutWaiter(self, name, member, lost):
        leader = self.getMember(self.leader)["agent"]
        missing = "\n".join(f"- {item['name']}: {item['why']}" for item in lost)
        if self.leaderIsUser():
            reply = leader.askUser(prompts.LOST_AGENT_QUESTION.format(name=name, task=member["task"], missing=missing))
            return (KEEP if isYes(reply) else REMOVE), "you decided it"
        named = [item["name"] for item in lost if item["name"].lower() in str(member["task"]).lower()]
        prompt = prompts.LOST_AGENT_PROMPT.format(mission=self.mission, name=name, role=member["role"], task=member["task"], missing=missing, example=prompts.LOST_AGENT_EXAMPLE,
                                                  named=f"This task names {', '.join(named)}, so it probably works on what they would have given." if named else "This task does not name them, so it probably works from something else.")
        try:
            reply = leader.reviewLoop(prompt, self.checkDecision, ask=False, own=False)
        except Exception:
            reply = None
        if reply is None:
            return KEEP, "the leader could not decide, so it tries"
        decision, why = readDecision(reply)
        return decision, why or "the leader gave no reason"

    # An agent waited for agents that will never give it their result (lost: [{"name", "why"}]). It says if the agent goes on without them.
    def keepsWorking(self, name, member, lost):
        decision, why = self.askAboutWaiter(name, member, lost)
        member["lost"] = []
        if self.stopped or member.get("removed"):
            return True
        names, leader = ", ".join(item["name"] for item in lost), self.getMember(self.leader)["agent"]
        if self.journal is not None:
            self.journal.add("waiter", self.leader, about=name, lost=clip(json.dumps(lost, ensure_ascii=False), 1500), decision=decision, why=clip(why, 800))
        self.record("addProgress", self.round, f"What the leader decided about {name}", f"It waited for {names}, which did not finish: {decision}. {why}")
        if decision == REMOVE:
            leader.notifyUser(f"[{self.leader}] {name} waited for {names}, which did not finish, and it cannot work without {'it' if len(lost) == 1 else 'them'}, so it leaves the swarm: {why}")
            self.removeAgent(name, f"It waited for {names}, which did not finish, and it cannot work without {'it' if len(lost) == 1 else 'them'}: {why}")
            return False
        leader.notifyUser(f"[{self.leader}] {name} waited for {names}, which did not finish. It goes on without {'it' if len(lost) == 1 else 'them'}: {why}")
        member["agent"].receive("swarm", f"You waited for {names}, but {'it' if len(lost) == 1 else 'they'} will not give a result:\n"
                                + "\n".join(f"- {item['name']}: {item['why']}" for item in lost) + f"\nThe leader decided that you go on without it: {why}")
        return True
