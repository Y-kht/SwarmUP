# What keeps the agents of a mission from undoing each other's work on the files of the user (CRD-04, and what a real swarm of ten agents
# showed): five agents wrote the same file in eleven seconds, each over the last one, without reading it.
# - claim: one agent changes a path at a time; another one is told who changes it, and tries again later.
# - own: a file that an agent changed in this run is its own until that agent is done (or failed, or left). Another agent that wants to
#   change it is told whose it is, and asks that agent or the leader. So its undo can never remove the work of another agent either.
# - know: an agent replaces or deletes a file only as it read it last. A file it never read, or that changed since, must be read first.
import contextlib
import hashlib
import threading
from pathlib import Path

from mcp_kit import ToolProblem


STAMP_BYTES = 50_000_000


# What a file is now: its size and the hash of its bytes (a time of change can be equal for two changes that come close together).
def stampOf(path):
    path = Path(path)
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        return (size, hashlib.sha256(path.read_bytes()).hexdigest()) if size <= STAMP_BYTES else (size, path.stat().st_mtime_ns)
    except OSError:
        return None


class PathGuard:
    def __init__(self):
        self.lock = threading.RLock()
        self.claims, self.owners, self.stamps = {}, {}, {}

    @contextlib.contextmanager
    def claim(self, path, agent):
        key = str(path)
        with self.lock:
            owner = self.claims.get(key)
            if owner is not None and owner != agent:
                raise ToolProblem(f"{Path(path).name} is being changed by {owner} right now. Try again in a few seconds.")
            self.claims[key] = agent
        try:
            yield
        finally:
            with self.lock:
                if self.claims.get(key) == agent and owner is None:
                    del self.claims[key]

    def ownerOf(self, path):
        with self.lock:
            return self.owners.get(str(path))

    # The agent changes the path: it becomes its own, unless another agent that is not done changed it before.
    def own(self, path, agent):
        key = str(path)
        with self.lock:
            owner = self.owners.get(key)
            if owner is not None and owner != agent:
                raise ToolProblem(f"{Path(path).name} was changed by {owner} in this run, and {owner} is not done yet: it is {owner}'s file until then. Ask {owner} "
                                  "with team__send_message to make the change, or ask your leader who must change it. Do not write it under another name.")
            self.owners[key] = agent

    # The agent is done, failed or left: its files are free for the others.
    def release(self, agent):
        with self.lock:
            for key in [key for key, owner in self.owners.items() if owner == agent]:
                del self.owners[key]

    # The agent read or wrote the file: this is the file as it knows it.
    def note(self, path, agent):
        stamp = stampOf(path)
        with self.lock:
            if stamp is None:
                self.stamps.pop((agent, str(path)), None)
            else:
                self.stamps[(agent, str(path))] = stamp

    # Before an agent replaces or deletes a file that exists: it must know the file as it is now.
    def checkKnown(self, path, agent, name):
        now = stampOf(path)
        if now is None:
            return
        with self.lock:
            known = self.stamps.get((agent, str(path)))
        if known is None:
            raise ToolProblem(f"{name} already exists and you have not read it: read it first (files__read_file), so that you do not lose what is in it. "
                              "To change a part of it, use files__edit_file.")
        if known != now:
            raise ToolProblem(f"{name} changed since you last read it (another agent or the user changed it): read it again first, then make your change.")
