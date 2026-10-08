# The desk of the window: several missions open at the same time, each in a tab of its own (a Session of session_core.py), as the chats of a
# chatbot. Each tab builds and runs its own swarm, and the user goes from one tab to another while they run. The desk keeps what the tabs share:
# the API keys and the Hugging Face tokens given in this window (only in memory), the dialogs of the system, and the history of the missions
# (mission_history.py), where a mission is opened in a tab (to continue it or to follow it up) or deleted with its memory and its temp folder.
# The browser says which tab it shows (the header X-SwarmUP-Tab), and every change of any tab wakes its poll, so the other tabs show when they
# need the user.
import os
import secrets
import threading
import time

from mission_history import deleteMission, listMissions
from session_core import POLL_SECONDS, Session


HISTORY_SECONDS = 2
SHUTDOWN_DELAY = 0.5
# The actions of the desk itself. Every other action goes to the Session of the tab.
DESK_ACTIONS = ("newTab", "closeTab", "openMission", "deleteMission", "quit")


class Desk:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.keys, self.tokens = {}, {}
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.tabs, self.number = {}, 0
        self.closing = False
        self.chooser = None
        self.history, self.historyAt, self.seenTabs = [], 0, None
        self.newTab()

    # ---------- The tabs. ----------
    def newTab(self, payload=None):
        session = Session(keys=self.keys, tokens=self.tokens, onTouch=self.touch)
        session.dialog = self.chooser
        with self.lock:
            self.number += 1
            tab = f"t{self.number}"
            session.tab = tab
            self.tabs[tab] = session
        self.touch()
        return {"tab": tab}

    # The session of a tab. An unknown tab (closed in another window) is the first one.
    def session(self, tab=None):
        with self.lock:
            return self.tabs.get(tab) or next(iter(self.tabs.values()))

    # The tab a mission is open in: its swarm, or the mission it is opening (its form waits for the secrets).
    def tabOf(self, missionId):
        with self.lock:
            return next((tab for tab, session in self.tabs.items() if (session.swarm.id if session.swarm is not None else getattr(session, "openingId", None)) == missionId), None)

    def closeTab(self, payload):
        tab = payload.get("tab")
        with self.lock:
            session = self.tabs.get(tab)
        if session is None:
            return {}
        if session.isBusy():
            raise ValueError("This mission is working. Stop it first, or leave its tab open: it goes on while you look at another one.")
        session.close()
        with self.lock:
            self.tabs.pop(tab, None)
            if not self.tabs:
                self.newTab()
        self.touch()
        return {"tab": next(iter(self.tabs))}

    # A mission of the history in a tab: the one it is already open in (with its form again, if it was not brought back yet), or a new tab
    # that brings it back.
    def openMission(self, payload):
        missionId = str(payload.get("id") or "")
        open = self.tabOf(missionId)
        if open:
            session = self.session(open)
            return {"tab": open, **(session.resumeForm({"id": missionId}) if session.swarm is None else {})}
        tab = self.newTab()["tab"]
        try:
            form = self.session(tab).resumeForm({"id": missionId})
            self.session(tab).opening, self.session(tab).openingId = form["resume"]["mission"], missionId
        except Exception:
            self.closeTab({"tab": tab})
            raise
        return {"tab": tab, **form}

    def deleteMission(self, payload):
        missionId = str(payload.get("id") or "")
        tab = self.tabOf(missionId)
        if tab is not None:
            self.closeTab({"tab": tab})
        deleteMission(missionId)
        self.refreshHistory(force=True)
        return {}

    # ---------- The history, read again at most every few seconds. ----------
    def refreshHistory(self, force=False):
        if force or time.monotonic() - self.historyAt > HISTORY_SECONDS:
            self.history, self.historyAt = listMissions(), time.monotonic()
        return self.history

    # ---------- What the browser draws. ----------
    def touch(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def describeTab(self, tab, session):
        swarm, run = session.swarm, session.swarm is not None and session.swarm.isRunning()
        waiting = bool(session.questions) or bool(swarm is not None and swarm.getReadyAgents())
        state = "waiting" if waiting else "running" if run else "finished" if session.runInfo.get("finishedAt") or session.runInfo.get("reopened") else "building"
        title = (swarm.mission if swarm is not None else session.mission) or getattr(session, "opening", "") or "Untitled mission"
        return {"tab": tab, "title": title, "state": state,
                "missionId": swarm.id if swarm is not None else None, "round": swarm.round if swarm is not None else 1}

    # The lock of the desk is never held while a session or a swarm is read, because they call the desk (touch) with their own locks held.
    def describe(self, tab=None):
        session = self.session(tab)
        with self.lock:
            items, version = list(self.tabs.items()), self.version
        tabs = [self.describeTab(name, other) for name, other in items]
        # A mission that starts, ends or is opened changes the history at once.
        seen = [(tab["missionId"], tab["state"], tab["round"]) for tab in tabs]
        history = self.refreshHistory(force=seen != self.seenTabs)
        self.seenTabs = seen
        return {**session.describe(), "version": version, "tab": session.tab, "tabs": tabs, "history": history}

    # Waits until something changed in any tab after the version the browser has, then gives the state of its tab and the new lines of its feed.
    def poll(self, version, feedAfter, tab=None):
        session = self.session(tab)
        with self.changed:
            self.changed.wait_for(lambda: self.version != version or self.closing, timeout=POLL_SECONDS)
        with session.lock:
            feed = [item for item in session.feed if item["id"] > feedAfter]
        return {"state": self.describe(tab), "feed": feed}

    def act(self, name, payload, tab=None):
        payload = payload or {}
        if name in DESK_ACTIONS:
            result = getattr(self, name)(payload) or {}
            return {"ok": True, **result, "state": self.describe(result.get("tab", tab))}
        result = self.session(tab).act(name, payload)
        if name in ("resume", "abandon", "refreshUnfinished", "followUp"):
            self.refreshHistory(force=True)
        return {**result, "state": self.describe(tab)}

    # ---------- Leaving the program: every tab saves the swarm it runs. ----------
    # The dialogs of the system (to choose a folder or a file), which the window gives once it is open: every tab uses them.
    @property
    def dialog(self):
        return self.chooser

    @dialog.setter
    def dialog(self, dialog):
        self.chooser = dialog
        with self.lock:
            for session in self.tabs.values():
                session.dialog = dialog

    def close(self):
        with self.changed:
            self.closing = True
            self.changed.notify_all()
        with self.lock:
            sessions = list(self.tabs.values())
        for session in sessions:
            session.close()

    def quit(self, payload):
        threading.Timer(SHUTDOWN_DELAY, self.shutdown).start()
        return {"message": "SwarmUP is closing. Your missions are saved: start the program again to continue them."}

    def shutdown(self):
        self.close()
        os._exit(0)
