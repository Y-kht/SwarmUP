# The miners that run in the background, one for each project while a swarm of the project works (acquire when a run starts, release when
# it ends; several swarms of the same project share one miner). The miner polls the journals of the project every few seconds, more slowly
# when nothing happens, and tells its subscribers (the architects of the swarms, architect_manager.py) of the patterns that start to repeat.
# Two programs can run swarms of the same project: the one that holds the lock of the project mines, and the other one only reads the store
# and its signals, until the lock is free.
import os
import threading
import time
import traceback

from pattern_miner import Miner
from pattern_store import DB_FILE, PatternStore
from project_identity import projectDir


POLL_SECONDS = 2.0
IDLE_SECONDS = 10.0
JOIN_SECONDS = 5.0
LOCK_FILE = "miner.lock"


# A lock of the system that is taken at once or not at all.
class ProjectLock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def take(self):
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def free(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class MinerService:
    def __init__(self, project):
        self.project = project
        self.users, self.subscribers = {}, []
        self.lock, self.busy = threading.Lock(), threading.Lock()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.thread, self.miner, self.reader = None, None, None
        self.projectLock = ProjectLock(projectDir(project) / LOCK_FILE)
        self.delivered, self.polls, self.problem = 0, 0, None

    @property
    def leading(self):
        return self.miner is not None

    def start(self):
        self.stopping.clear()
        self.thread = threading.Thread(target=self.work, name=f"swarmup-miner-{self.project}", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(JOIN_SECONDS)

    def subscribe(self, callback):
        with self.lock:
            self.subscribers.append(callback)
        self.wake.set()

    def unsubscribe(self, callback):
        with self.lock:
            self.subscribers = [item for item in self.subscribers if item is not callback]

    def nudge(self):
        self.wake.set()

    def storeOf(self):
        if self.miner is not None:
            return self.miner.store
        if self.reader is None:
            self.reader = PatternStore(projectDir(self.project) / DB_FILE, readOnly=True)
        return self.reader

    def pollOnce(self):
        if self.miner is None and self.projectLock.take():
            self.miner = Miner(self.project)
        if self.miner is None:
            return 0
        with self.busy:
            read = self.miner.poll()
        self.polls += 1
        return read

    # Reads what is left in the journals, for at most seconds (the end of a run: the architect then sees all of it).
    def drain(self, seconds=30.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if not self.pollOnce():
                    return True
            except Exception:
                traceback.print_exc()
                return False
        return False

    def work(self):
        try:
            while not self.stopping.is_set():
                read = 0
                try:
                    read = self.pollOnce()
                    self.deliver()
                    self.problem = None
                except Exception as error:
                    self.problem = error
                    traceback.print_exc()
                self.wake.wait(POLL_SECONDS if read else IDLE_SECONDS)
                self.wake.clear()
        finally:
            if self.miner is not None:
                self.miner.close()
                self.miner = None
            if self.reader is not None:
                self.reader.close()
                self.reader = None
            self.projectLock.free()

    # The signals of the store that the subscribers did not get yet.
    def deliver(self):
        with self.lock:
            subscribers = list(self.subscribers)
        if not subscribers:
            return
        signals = [signal for signal in self.storeOf().pendingSignals() if signal["id"] > self.delivered]
        if not signals:
            return
        self.delivered = signals[-1]["id"]
        for callback in subscribers:
            try:
                callback(signals)
            except Exception:
                traceback.print_exc()


class MinerRegistry:
    def __init__(self):
        self.services, self.lock = {}, threading.Lock()

    # A user (a swarm) of the miner of a project: the miner runs while it has one.
    def acquire(self, project, user):
        with self.lock:
            service = self.services.get(project)
            if service is None or service.stopping.is_set():
                service = self.services[project] = MinerService(project)
                service.start()
            service.users[user] = service.users.get(user, 0) + 1
            return service

    def release(self, project, user):
        with self.lock:
            service = self.services.get(project)
            if service is None or user not in service.users:
                return
            service.users[user] -= 1
            if service.users[user] <= 0:
                service.users.pop(user)
            if service.users:
                return
            self.services.pop(project, None)
        service.stop()

    def get(self, project):
        with self.lock:
            return self.services.get(project)

    def stopAll(self):
        with self.lock:
            services, self.services = list(self.services.values()), {}
        for service in services:
            service.stop()


MINERS = MinerRegistry()
