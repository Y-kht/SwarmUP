import json
from datetime import datetime

import agent_prompts as prompts
from harness_utils import STOPPED_MESSAGE, SUMMARY_LENGTH, SwarmStopped, USER_LOCK, USER_NAME, isNo, isOnline, isYes, stripFences


CORRECTION_REPLY = "The user asked for a correction. It is in your newest messages, apply it."

REMOVED_MESSAGE = "This agent was removed from the swarm before it finished."


# What a swarm (Swarm in swarm_harness.py) asks the user: the drafts of the agents, the summaries and the corrections of the leader,
# the lost connections, and the stop.
class SwarmReview:
    def logMessage(self, sender, receiver, message):
        self.messages.append({"time": f"{datetime.now():%H:%M:%S}", "sender": sender, "receiver": receiver, "message": message})
        self.emit("message", receiver, sender=sender, receiver=receiver, message=message)

    def communicate(self, sender, receiver, message):
        self.getMember(sender)
        self.getMember(receiver)["agent"].receive(sender, str(message))
        self.logMessage(sender, receiver, str(message))

    # The agent reads the message with its next prompt, so it works while the agent is waiting or working.
    # An agent that is done or failed cannot read it anymore, so the user is told instead of being ignored silently.
    # If the draft of the agent is waiting for the user, the message is taken as a correction and the agent writes the draft again.
    def sendUserMessage(self, name, message):
        member = self.getMember(name)
        message = str(message).strip()
        if not message:
            raise ValueError("The message is empty.")
        if member["status"] in ("done", "failed"):
            raise ValueError(f"{name} has already finished, so it cannot read new messages.")
        member["agent"].receiveFromUser(message)
        self.logMessage(USER_NAME, name, message)
        self.askCorrection(member)

    # Gives the answer of the user to the agent that waits for it. It must be called with self.changed held.
    def decide(self, member, reply):
        member["decision"] = reply
        member["review"] = "approved" if isYes(reply) else "rejected" if isNo(reply) else ""
        self.changed.notify_all()
        self.emit("review", member["name"], review=member["review"])

    def askCorrection(self, member):
        with self.changed:
            if member["review"] == "ready":
                self.decide(member, CORRECTION_REPLY)

    # The reviewer of every agent while the swarm runs. The agent sleeps here until the user decides about its draft.
    # The revision counts the drafts of the agent, so an answer can be tied to the draft it was given for.
    def waitForReview(self, name, draft, problem):
        member = self.anyMember(name)
        with self.changed:
            if self.stopped or member.get("removed"):
                raise SwarmStopped(STOPPED_MESSAGE if self.stopped else REMOVED_MESSAGE)
            member.update(review="ready", draft=draft, problem=problem, decision=None, revision=member["revision"] + 1)
            self.changed.notify_all()
            self.emit("review", name, review="ready")
            self.changed.wait_for(lambda: member["decision"] is not None or self.stopped or member.get("removed"))
            if member["decision"] is None:
                member["review"] = ""
                raise SwarmStopped(REMOVED_MESSAGE if member.get("removed") else STOPPED_MESSAGE)
            return member["decision"]

    def checkReady(self, name):
        if self.getMember(name)["review"] != "ready":
            raise ValueError(f"{name} has nothing waiting for the user.")

    # What the user does after clicking on the name of an agent whose draft is ready. The agent goes on right away.
    # With a revision, the user decides about the draft that was looked at, and not about a newer one.
    def decideDraft(self, name, reply, revision=None):
        with self.changed:
            self.checkReady(name)
            if revision is not None and self.members[name]["revision"] != revision:
                raise ValueError(f"{name} wrote a new draft since you looked at it.")
            self.decide(self.members[name], reply)

    def approveDraft(self, name, revision=None):
        self.decideDraft(name, "yes", revision)

    def rejectDraft(self, name, revision=None):
        self.decideDraft(name, "no", revision)

    def correctDraft(self, name, comment):
        self.checkReady(name)
        self.sendUserMessage(name, comment)

    def describeDrafts(self, names):
        return "\n".join(f"- {name} ({self.members[name]['role']}): {self.members[name]['draft']}" for name in names)

    def describeMember(self, name, member):
        approved = " and approved by the user" if member["review"] == "approved" else ""
        if member["status"] == "paused":
            state = "paused because the connection was lost"
        elif member["review"] == "rejected":
            state = "rejected by the user"
        elif member["status"] == "failed":
            state = f"failed ({member['error']})"
        elif member["review"] == "ready":
            warning = f" (WARNING, the automatic checks found a problem: {member['problem']})" if member["problem"] else ""
            state = f"waiting for the user{warning}: {member['draft']}"
        elif member["status"] == "done":
            state = f"done{approved}: {str(member['result'])[:SUMMARY_LENGTH]}"
        elif member["review"] == "approved":
            state = f"approved by the user, now at work: {member['draft']}"
        else:
            state = "writing its draft" if member["status"] == "working" else "has not started yet"
        return f"- {name} ({member['role']}): {state}"

    # Where every agent stands. The leader writes its summary from it, so what the user approved on its own is echoed in the summary.
    def describeProgress(self):
        lines = [self.describeMember(name, member) for name, member in list(self.members.items())]
        lines += [f"- {item['name']} ({item['role']}): removed from the swarm when it was {item['status']}. Why: {item['reason'] or 'not given'}" for item in list(self.removed)]
        return "\n".join(lines)

    # The facts for a user who may stop the swarm: where every agent stands, what it changed outside of itself (an email sent, a file
    # written...), and what it would leave half done.
    def describeChanges(self):
        lines = []
        for name, member in self.members.items():
            lines.append(self.describeMember(name, member))
            lines += [f"    changed, {action['time']}: {action['text']}" for action in member["agent"].actions]
            pending = member["agent"].describePending() if member["status"] != "done" else ""
            if pending:
                lines.append(f"    half done: {pending}")
        return "\n".join(lines)

    def checkSummary(self, draft, names):
        missing = [name for name in names if name not in draft]
        return f"Name every agent, you forgot: {missing}" if missing else ""

    # The summary names every agent that started. If the leader cannot write it, the state of the agents is shown as it is, so the user can always decide.
    # A swarm of one agent has nothing to summarise: the user reads the draft itself, instead of a summary of it.
    def writeSummary(self, request):
        leader = self.getMember(self.leader)
        progress = self.describeProgress()
        started = [name for name, member in self.members.items() if member["status"] != "waiting"]
        template = prompts.PLAN_SUMMARY_PROMPT if leader["mode"] == "plan" else prompts.EXECUTION_SUMMARY_PROMPT
        summary = None
        if len(self.members) > 1:
            try:
                prompt = template.format(mission=self.mission, agents=progress, request=request or "nothing")
                summary = leader["agent"].reviewLoop(prompt, lambda draft: self.checkSummary(draft, started), ask=False, own=False)
            except Exception:
                summary = None
            if summary is None:
                leader["agent"].notifyUser("The leader could not write the summary, so the state of every agent is shown as it is.")
        self.summary = summary or progress
        self.emit("summary", self.leader, text=self.summary)
        return self.summary

    def checkRoute(self, draft, names):
        try:
            route = json.loads(stripFences(draft))
            valid = isinstance(route, dict) and all(isinstance(text, str) and text.strip() for text in route.values())
        except ValueError:
            valid = False
        if not valid:
            return f"Reply only with JSON like {prompts.CORRECTION_ROUTE_EXAMPLE}, or with {{}} if no agent is concerned."
        unknown = [name for name in route if name not in names]
        return f"These are not agents you can ask: {unknown}. Choose from {names}" if unknown else ""

    # The leader decides which agents the correction concerns. If it cannot, all of them get it, and each applies what concerns it.
    def routeCorrection(self, names, comment):
        prompt = prompts.CORRECTION_ROUTE_PROMPT.format(mission=self.mission, correction=comment, agents=self.describeDrafts(names),
                                                        example=prompts.CORRECTION_ROUTE_EXAMPLE)
        try:
            route = self.getMember(self.leader)["agent"].reviewLoop(prompt, lambda draft: self.checkRoute(draft, names), ask=False, own=False)
        except Exception:
            route = None
        return json.loads(stripFences(route)) if route is not None else {name: comment for name in names}

    # The leader messages the agents the correction concerns, and they write their drafts again.
    # If no agent is concerned the correction is about the summary itself, and it is returned to write the summary again.
    def correctWithLeader(self, names, comment, what):
        route = self.routeCorrection(names, comment)
        self.getMember(self.leader)["agent"].notifyUser(f"Asking {', '.join(route)} to correct their {what}." if route else "No agent is concerned, so only the summary is changed.")
        for name, instruction in route.items():
            self.communicate(self.leader, name, f"The user asked for a correction: {comment}\nWhat to change in your {what}: {instruction}")
            self.askCorrection(self.members[name])
        return "" if route else comment

    # What the user does in the console instead of clicking on an agent: look at its draft and decide about it alone.
    def reviewAlone(self, name, revision, what):
        leader = self.getMember(self.leader)["agent"]
        member = self.members[name]
        with USER_LOCK:
            leader.notifyUser(f"[{name}] its {what}:\n{member['draft']}")
            if member["problem"]:
                leader.notifyUser(f"Warning, the automatic checks found a problem: {member['problem']}")
            reply = leader.askUser(f"{name}: type yes to approve, no to reject, or write what you want changed:")
        try:
            if isYes(reply):
                self.approveDraft(name, revision)
            elif isNo(reply):
                self.rejectDraft(name, revision)
            else:
                self.correctDraft(name, reply)
        except ValueError as error:
            leader.notifyUser(f"{error} Your answer was not used.")

    # One round of the user with the leader: the summary of where the agents stand, and the answer of the user.
    # shown is {agent: revision of its draft} for the drafts that wait. An answer only applies to the drafts it was given for,
    # so a draft that changed meanwhile is left out of it and comes back in the next summary.
    def reviewRound(self, shown, request):
        leader = self.getMember(self.leader)["agent"]
        what = "plan" if self.members[self.leader]["mode"] == "plan" else "result"
        summary = self.writeSummary(request)
        with USER_LOCK:
            leader.notifyUser(f"[{self.leader}] Summary of the {what}s:\n{summary}")
            leader.notifyUser(f"You can also check, approve or correct the {what} of each agent by clicking on its name in the swarm.")
            for name in shown:
                if name in self.members and self.members[name]["problem"]:
                    leader.notifyUser(f"Warning for {name}: the automatic checks found a problem with its draft: {self.members[name]['problem']}")
            reply = leader.askUser("Do you approve? Type yes to approve, no to reject, or write what you want changed:" if len(self.members) == 1 else
                                   "Do you approve? Type yes to approve all of them, no to reject all of them, the name of an agent to look at it alone, or write what you want changed:")
        answered = isYes(reply) or isNo(reply)
        with self.changed:
            waiting = [name for name in shown if name in self.members and self.members[name]["review"] == "ready" and self.members[name]["revision"] == shown[name]]
            changed = [name for name in shown if name in self.members and (self.members[name]["review"] == "" or self.members[name]["revision"] != shown[name])]
            if answered:
                for name in waiting:
                    self.decide(self.members[name], "yes" if isYes(reply) else "no")
        if changed:
            leader.notifyUser(f"The {what} of {', '.join(changed)} changed while you were reading, so it is not part of your answer. The summary will be updated.")
        shown = {name: revision for name, revision in shown.items() if name in self.members}
        alone = next((name for name in waiting if reply.strip().lower() == name.lower()), None)
        if alone:
            self.reviewAlone(alone, shown[alone], what)
        return "" if answered or alone or not waiting else self.correctWithLeader(waiting, reply, what)

    # Called in the thread of an agent that lost its connection. The agent is paused here, with everything it did, until the user decides.
    # The agents that lose the connection together share one question. It returns to try again, or raises SwarmStopped.
    def waitForResume(self, name, error):
        member = self.anyMember(name)
        with self.changed:
            if self.stopped or member.get("removed"):
                raise SwarmStopped(STOPPED_MESSAGE if self.stopped else REMOVED_MESSAGE)
            if self.interruption is None:
                self.interruption = {"agents": {}, "decision": None}
            interruption = self.interruption
            interruption["agents"][name] = str(error)
            self.setStatus(name, "paused")
            self.emit("connectionLost", name, reason=str(error))
            self.changed.notify_all()
            self.changed.wait_for(lambda: interruption["decision"] is not None or member.get("removed"))
            if member.get("removed") and interruption["decision"] is None:
                # The agent left: if it was the only one without connection, the swarm goes on without asking anymore.
                interruption["agents"].pop(name, None)
                if not interruption["agents"] and self.interruption is interruption:
                    interruption["decision"], self.interruption = "continue", None
                    self.changed.notify_all()
                    self.emit("resumed")
                raise SwarmStopped(REMOVED_MESSAGE)
            if interruption["decision"] == "stop":
                raise SwarmStopped(STOPPED_MESSAGE)
            self.setStatus(name, "working")

    def resolveInterruption(self, decision):
        with self.changed:
            if self.interruption is None:
                raise ValueError("The swarm is not waiting for a decision about a lost connection.")
            self.interruption["decision"] = decision
            self.interruption = None
            self.changed.notify_all()

    # The user wants to go on after the connection was lost: the agents that were paused try again the step that failed.
    def continueWork(self):
        if self.interruption is None:
            raise ValueError("The swarm is not waiting for a decision about a lost connection.")
        if not isOnline():
            raise ValueError("There is still no internet connection. Check it, then try again.")
        self.resolveInterruption("continue")
        self.emit("resumed")

    # The user confirmed to stop the swarm. The agents that wait are released with SwarmStopped, and the ones at work stop at their next step.
    def stopWork(self):
        with self.changed:
            self.stopped = True
            if self.interruption is not None:
                self.interruption["decision"] = "stop"
                self.interruption = None
            for member in self.members.values():
                member["wake"].set()
            self.changed.notify_all()
        self.emit("stopped")

    # The leader tells the user every change that was made, before the user confirms to stop. If the leader cannot write it (the lost
    # connection is the very reason it is needed), the facts are given as they are.
    def summarizeChanges(self):
        facts = self.describeChanges()
        try:
            summary = self.getMember(self.leader)["agent"].reviewLoop(prompts.CANCEL_SUMMARY_PROMPT.format(mission=self.mission, agents=facts),
                                                                      lambda draft: self.checkSummary(draft, list(self.members)), ask=False, own=False)
        except Exception:
            summary = None
        return summary or f"The leader could not write the summary, so these are the facts:\n{facts}"

    # The user wants to cancel: after the summary, the user confirms to stop there (which can cut the agents in the middle of their task),
    # or goes on until the end.
    def confirmStop(self, leader):
        leader.notifyUser(f"[{self.leader}] Summary of what the swarm did so far:\n{self.summarizeChanges()}")
        leader.notifyUser("If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task.")
        reply = leader.askUser("Type stop to stop here, or continue to go on until the end:").strip().lower()
        if reply == "stop":
            self.stopWork()
        elif reply == "continue":
            self.continueWork()
        else:
            leader.notifyUser("Nothing was changed, the swarm is still paused.")

    # What the leader asks in the console when the connection is lost. A user interface does the same with continueWork,
    # summarizeChanges and stopWork, and may decide first: the question is then dropped.
    def askAboutInterruption(self, interruption):
        leader = self.getMember(self.leader)["agent"]
        reasons = "\n".join(f"- {name}: {reason}" for name, reason in interruption["agents"].items())
        with USER_LOCK:
            leader.notifyUser(f"[{self.leader}] The swarm lost its connection and is paused. Everything done so far is saved, so nothing is lost.\n{reasons}")
            while self.interruption is interruption:
                reply = leader.askUser("Type continue to try again, or cancel to stop here:").strip().lower()
                if self.interruption is not interruption:
                    return
                try:
                    if reply == "continue":
                        self.continueWork()
                    elif reply == "cancel":
                        self.confirmStop(leader)
                    else:
                        leader.notifyUser("Please type continue or cancel.")
                except ValueError as error:
                    leader.notifyUser(str(error))
