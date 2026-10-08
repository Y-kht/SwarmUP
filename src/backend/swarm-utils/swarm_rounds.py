import json
from datetime import datetime

import agent_prompts as prompts
from agent_conversation import Conversation
from harness_utils import stripFences


# The rounds of a mission (Swarm in swarm_harness.py), as in a chat: when a round is over, the user follows up with a new request, and the same
# swarm works on it, the same day or after a restart (the swarm is brought back from the history of the missions). The agents keep their
# conversations, so they still know what they did. The leader sends each part of the request to the agents it concerns, and the others sit
# the round out: their last result stays as it is, for the agents that need it. The leader always works, and reports on the round.
class SwarmRounds:
    # Starts the next round with the request of the user. The swarm runs it with run() (or startInBackground), in plan or in execute mode.
    def followUp(self, request, mode=None):
        request = str(request or "").strip()
        if not request:
            raise ValueError("Write what the swarm must do now.")
        if self.isRunning():
            raise ValueError("The swarm is still working on this round. Wait until it ends, or send a message to an agent instead.")
        self.round += 1
        self.requests.append({"round": self.round, "text": request, "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}"})
        self.roundParts = None
        if mode:
            self.setMode(mode)
        self.emit("round", round=self.round, request=request)

    def currentRequest(self):
        return next((item["text"] for item in reversed(self.requests) if item["round"] == self.round), self.mission)

    # The leader decides which agents the request concerns, and what each of them must do. If it cannot, every agent gets the whole request.
    def routeFollowUp(self, request):
        names = [name for name in self.members if name != self.leader]
        if not names:
            return {}
        agents = "\n".join(f"- {name} ({self.members[name]['role']}): {self.members[name]['task']}" for name in names)
        prompt = prompts.FOLLOW_UP_ROUTE_PROMPT.format(mission=self.mission, request=request, agents=agents, example=prompts.CORRECTION_ROUTE_EXAMPLE,
                                                       report=str(self.members[self.leader]["result"] or "none")[:4000])
        try:
            route = self.getMember(self.leader)["agent"].reviewLoop(prompt, lambda draft: self.checkRoute(draft, names), ask=False, own=False)
        except Exception:
            route = None
        return json.loads(stripFences(route)) if route is not None else {name: request for name in names}

    # A run of a round after the first, before its agents start (prepareRun): the leader routes the request once, the agents concerned get
    # their part (and the leader the whole request), and the others keep their last result. previous holds the results before this run.
    def startRound(self, previous):
        request = self.currentRequest()
        first = self.roundParts is None
        if first:
            self.roundParts = self.routeFollowUp(request)
            self.record("addUserPrompt", self.round, request, kind="Follow-up")
            self.record("setMission", self.mission, self.round)
        for name, member in self.members.items():
            agent = member["agent"]
            if agent.conversation is None and agent.session is not None:
                agent.conversation = Conversation.load(agent)
            part = request if name == self.leader else self.roundParts.get(name)
            agent.roundRequest = part or ""
            if part is None:
                result = previous.get(name)
                member.update(status="done" if result is not None else "failed", result=result, sitsOut=True,
                              error="" if result is not None else "It has no part in this round, and had no result before.")
            elif first and name != self.leader:
                self.communicate(self.leader, name, prompts.FOLLOW_UP_MESSAGE.format(round=self.round, request=request, part=part))
        sitting = [name for name, member in self.members.items() if member.get("sitsOut")]
        self.getMember(self.leader)["agent"].notifyUser(f"[{self.leader}] Round {self.round}: " + (f"{', '.join(name for name in self.roundParts)} work on it" if self.roundParts else "I work on it alone")
                                                        + (f", and {', '.join(sitting)} sit{'s' if len(sitting) == 1 else ''} it out." if sitting else "."))
