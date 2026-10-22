# The patterns of one project, in a SQLite database (long-term-memory/.architect/projects/<id>/patterns.sqlite), written by its miner
# (pattern_miner.py) and read by the architect, the command line and the window at the same time (WAL). A pattern is a step, a problem or a
# piece of reasoning that the agents of the project met, with how many times, by which agents, in which missions and requests, what it cost
# (tokens, seconds), a few examples (the first ones and the latest ones, with where they are in the journals), and its status:
# new (seen once), emerging (twice), repeating (enough times in enough requests to be worth a wheel), cross (it repeats across projects, or a
# note of an architect of another project describes it), covered (a wheel does it), noted (an architect wrote a note about it), dismissed
# (an architect decided it cannot be automated: it comes back when its count doubles).
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 1
DB_FILE = "patterns.sqlite"
FIRST_EXAMPLES = 4
EXAMPLES = 12
SPREAD_VALUES = 256
TEXT_SIGNATURES = 50000
EXACT_VALUES = 20
REPEAT_COUNT = 3
REPEAT_SPREAD = 2
EMERGING_COUNT = 2
AGE_DAYS = 14
# How much a pattern of each kind is worth automating, all else equal.
KIND_WEIGHTS = {"decision": 2.0, "wheelfail": 1.8, "fix": 1.6, "reasoning": 1.5, "redo": 1.5, "loop": 1.4, "sequence": 1.3, "correction": 1.3,
                "error": 1.2, "command": 1.0, "badcall": 1.0, "hotspot": 1.0, "reread": 0.8, "action": 0.7, "permission": 0.6, "message": 0.6,
                "intent": 0.5, "wheel": 0.0}
# The kinds that tell the architect something to automate or fix (wheel only counts the runs of the wheels).
ACTIONABLE = tuple(kind for kind in KIND_WEIGHTS if kind != "wheel")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, mission TEXT, kind TEXT, inode INTEGER, size INTEGER, offset INTEGER, lines INTEGER, bad INTEGER,
    updated REAL, gone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS patterns(id INTEGER PRIMARY KEY, fp TEXT UNIQUE, kind TEXT, level INTEGER, shape TEXT, label TEXT, count INTEGER, requests INTEGER,
    agents INTEGER, missions INTEGER, firstSeen REAL, lastSeen REAL, tokens REAL, seconds REAL, size REAL, errors INTEGER, status TEXT, statusAt REAL,
    score REAL, wheel TEXT, note TEXT, claimedBy TEXT, claimedUntil REAL, dismissedAt INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS patternsByKind ON patterns(kind, status, score);
CREATE TABLE IF NOT EXISTS spread(pattern INTEGER, dim TEXT, value TEXT, count INTEGER, PRIMARY KEY(pattern, dim, value));
CREATE TABLE IF NOT EXISTS examples(id INTEGER PRIMARY KEY, pattern INTEGER, mission TEXT, agent TEXT, t REAL, source TEXT, offset INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS examplesByPattern ON examples(pattern, id);
CREATE TABLE IF NOT EXISTS links(a INTEGER, b INTEGER, kind TEXT, count INTEGER, PRIMARY KEY(a, b, kind));
CREATE TABLE IF NOT EXISTS textsigs(id INTEGER PRIMARY KEY, kind TEXT, cluster TEXT, sig TEXT);
CREATE TABLE IF NOT EXISTS anchors(kind TEXT, anchor TEXT, sig INTEGER, PRIMARY KEY(kind, anchor, sig));
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, pattern INTEGER, kind TEXT, t REAL, consumed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS passes(id INTEGER PRIMARY KEY, mission TEXT, started REAL, ended REAL, outcome TEXT, spent REAL, patterns TEXT, report TEXT);
"""
COLUMNS = ("fp", "kind", "level", "shape", "label", "count", "requests", "agents", "missions", "firstSeen", "lastSeen", "tokens", "seconds", "size", "errors",
           "status", "statusAt", "score", "wheel", "note", "claimedBy", "claimedUntil", "dismissedAt", "data")


def foldData(old, new):
    data = dict(old or {})
    for key, value in (new or {}).items():
        if key == "numbers":
            stats = data.setdefault("numbers", {})
            for name, number in value.items():
                low, high, total, count = stats.get(name, [number, number, 0.0, 0])
                stats[name] = [min(low, number), max(high, number), total + number, count + 1]
        elif key == "exact":
            counts = data.setdefault("exact", {})
            if value in counts or len(counts) < EXACT_VALUES:
                counts[value] = counts.get(value, 0) + 1
            else:
                data["exactOther"] = data.get("exactOther", 0) + 1
        elif key == "recipes":
            recipes = data.setdefault("recipes", {})
            for recipe in value:
                if recipe in recipes or len(recipes) < EXACT_VALUES:
                    recipes[recipe] = recipes.get(recipe, 0) + 1
        else:
            data[key] = value
    return data


# The hints the architect reads: the step is always the same (deterministic), its numbers barely change (stable), it has a known fix.
def hintsOf(row):
    data, hints = row.get("data") or {}, []
    exact = data.get("exact") or {}
    if exact and row["count"] >= 2 and max(exact.values()) >= 0.8 * row["count"]:
        hints.append("deterministic")
    numbers = data.get("numbers") or {}
    if numbers and all(high - low <= max(5, 0.02 * abs(total / max(count, 1))) for low, high, total, count in numbers.values()):
        hints.append("stableArgs")
    if data.get("recipes"):
        hints.append("fixRecipe")
    if data.get("toolOnly"):
        hints.append("toolOnly")
    if data.get("local"):
        hints.append("local")
    return hints


def statusOf(row, cross=False, covered=None, noted=None):
    noted = bool(row.get("note")) if noted is None else noted
    if covered:
        return "covered"
    if row.get("status") == "dismissed" and row["count"] < 2 * (row.get("dismissedAt") or 0):
        return "dismissed"
    if row["count"] >= REPEAT_COUNT and max(row["requests"], row["agents"], row["missions"]) >= REPEAT_SPREAD:
        return "repeating"
    if cross:
        return "cross"
    if noted:
        return "noted"
    return "emerging" if row["count"] >= EMERGING_COUNT else "new"


def scoreOf(row, now=None):
    now = now or time.time()
    spread = 1 + 0.5 * max(row["missions"] - 1, 0) + 0.25 * max(row["agents"] - 1, 0)
    cost = 1 + math.log1p((row["tokens"] or 0) / max(row["count"], 1) / 500) + math.log1p((row["seconds"] or 0) / max(row["count"], 1) / 30) + \
        0.5 * math.log1p((row["errors"] or 0))
    age = math.exp(-max(now - (row["lastSeen"] or now), 0) / (AGE_DAYS * 86400))
    hinted = 1.5 if {"deterministic", "stableArgs"} & set(hintsOf(row)) else 1.0
    hinted *= 0.5 if (row.get("data") or {}).get("reading") else 1.0
    return round(math.log2(1 + row["count"]) * spread * cost * age * KIND_WEIGHTS.get(row["kind"], 1.0) * hinted, 4)


class PatternStore:
    def __init__(self, path, readOnly=False):
        self.path, self.readOnly = Path(path), readOnly
        self.lock = threading.RLock()
        self.connection = self.open()

    # A reader (the architect, the command line, the window) never changes the schema; the miner makes it again when its version changed.
    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION and (version == 0 or not self.readOnly):
            connection.execute("PRAGMA journal_mode=WAL")
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                connection.execute(f"DROP TABLE IF EXISTS {name}")
            connection.executescript(SCHEMA)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if not self.readOnly:
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def close(self):
        with self.lock:
            self.connection.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            self.connection.execute("COMMIT")

    def query(self, sql, parameters=()):
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, parameters).fetchall()]

    def value(self, sql, parameters=(), default=None):
        rows = self.query(sql, parameters)
        return next(iter(rows[0].values())) if rows else default

    # ---------- What the miner keeps between its polls. ----------
    def getMeta(self, key, default=None):
        value = self.value("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(value) if value is not None else default

    def setMeta(self, key, value, db=None):
        (db or self.connection).execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def getState(self, key, default=None):
        value = self.value("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(value) if value is not None else default

    def setState(self, db, key, value):
        db.execute("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, default=str)))

    def sources(self):
        return {row["path"]: row for row in self.query("SELECT * FROM sources")}

    def setSource(self, db, path, **fields):
        row = {"mission": "", "kind": "journal", "inode": 0, "size": 0, "offset": 0, "lines": 0, "bad": 0, "updated": time.time(), "gone": 0, **fields}
        db.execute("INSERT INTO sources(path, mission, kind, inode, size, offset, lines, bad, updated, gone) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                   "mission=excluded.mission, kind=excluded.kind, inode=excluded.inode, size=excluded.size, offset=excluded.offset, lines=excluded.lines, "
                   "bad=excluded.bad, updated=excluded.updated, gone=excluded.gone", (path, row["mission"], row["kind"], row["inode"], row["size"], row["offset"],
                                                                                        row["lines"], row["bad"], row["updated"], row["gone"]))

    # ---------- The patterns. ----------
    def rowsOf(self, db, fps):
        rows = {}
        fps = list(fps)
        for start in range(0, len(fps), 500):
            part = fps[start:start + 500]
            for row in db.execute(f"SELECT * FROM patterns WHERE fp IN ({','.join('?' * len(part))})", part).fetchall():
                row = dict(row)
                row["data"] = json.loads(row["data"] or "{}")
                rows[row["fp"]] = row
        return rows

    def writeRow(self, db, row):
        values = [json.dumps(row.get("data") or {}, default=str) if column == "data" else row.get(column) for column in COLUMNS]
        db.execute(f"INSERT INTO patterns({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))}) ON CONFLICT(fp) DO UPDATE SET " +
                   ", ".join(f"{column}=excluded.{column}" for column in COLUMNS[1:]), values)
        if row.get("id") is None:
            row["id"] = db.execute("SELECT id FROM patterns WHERE fp=?", (row["fp"],)).fetchone()[0]
        return row["id"]

    # Notes that a pattern was met in one more place of a dimension (agent, mission, request); it says if the place is new.
    def addSpread(self, db, pattern, dim, value):
        changed = db.execute("UPDATE spread SET count=count+1 WHERE pattern=? AND dim=? AND value=?", (pattern, dim, value)).rowcount
        if changed:
            return False
        if db.execute("SELECT COUNT(*) FROM spread WHERE pattern=? AND dim=?", (pattern, dim)).fetchone()[0] >= SPREAD_VALUES:
            return True
        db.execute("INSERT INTO spread(pattern, dim, value, count) VALUES(?,?,?,1)", (pattern, dim, value))
        return True

    def addExample(self, db, pattern, mission, agent, t, source, offset, text):
        count = db.execute("SELECT COUNT(*) FROM examples WHERE pattern=?", (pattern,)).fetchone()[0]
        if count >= EXAMPLES:
            db.execute("DELETE FROM examples WHERE id=(SELECT id FROM examples WHERE pattern=? ORDER BY id LIMIT 1 OFFSET ?)", (pattern, FIRST_EXAMPLES))
        db.execute("INSERT INTO examples(pattern, mission, agent, t, source, offset, text) VALUES(?,?,?,?,?,?,?)", (pattern, mission, agent, t, source, offset, text))

    def addLink(self, db, a, b, kind):
        db.execute("INSERT INTO links(a, b, kind, count) VALUES(?,?,?,1) ON CONFLICT(a, b, kind) DO UPDATE SET count=count+1", (a, b, kind))

    # ---------- The sketches of the texts, to group the reasoning that is near the same. ----------
    def nearest(self, db, kind, sketch, anchors, similarity, limit=60):
        candidates = db.execute(f"SELECT DISTINCT textsigs.id, textsigs.cluster, textsigs.sig FROM anchors JOIN textsigs ON textsigs.id=anchors.sig WHERE anchors.kind=? AND anchor IN "
                                f"({','.join('?' * len(anchors))}) LIMIT {limit}", [kind, *[str(anchor) for anchor in anchors]]).fetchall()
        best = (0.0, None)
        for row in candidates:
            near = similarity(sketch, [int(value) for value in row["sig"].split(",")])
            if near > best[0]:
                best = (near, row["cluster"])
        return best

    def addSketch(self, db, kind, cluster, sketch, anchors):
        sig = db.execute("INSERT INTO textsigs(kind, cluster, sig) VALUES(?,?,?)", (kind, cluster, ",".join(str(value) for value in sketch))).lastrowid
        db.executemany("INSERT OR IGNORE INTO anchors(kind, anchor, sig) VALUES(?,?,?)", [(kind, str(anchor), sig) for anchor in anchors])
        if sig % 1000 == 0:
            oldest = db.execute("SELECT id FROM textsigs ORDER BY id DESC LIMIT 1 OFFSET ?", (TEXT_SIGNATURES,)).fetchone()
            if oldest:
                db.execute("DELETE FROM anchors WHERE sig<=?", (oldest[0],))
                db.execute("DELETE FROM textsigs WHERE id<=?", (oldest[0],))

    # ---------- What the architect reads and decides. ----------
    def addSignal(self, db, pattern, kind):
        db.execute("INSERT INTO signals(pattern, kind, t) VALUES(?,?,?)", (pattern, kind, time.time()))

    def pendingSignals(self):
        return self.query("SELECT signals.*, patterns.kind AS patternKind FROM signals JOIN patterns ON patterns.id=signals.pattern WHERE consumed=0 ORDER BY signals.id")

    def consumeSignals(self, upTo):
        with self.transaction() as db:
            db.execute("UPDATE signals SET consumed=1 WHERE id<=?", (upTo,))

    def pattern(self, ident):
        rows = self.query("SELECT * FROM patterns WHERE id=? OR fp=?", (ident, str(ident)))
        if not rows:
            return None
        row = rows[0]
        row["data"] = json.loads(row["data"] or "{}")
        return row

    def patterns(self, kinds=ACTIONABLE, statuses=None, limit=50, since=None, order="score"):
        clauses, parameters = [f"kind IN ({','.join('?' * len(kinds))})"], list(kinds)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            parameters += list(statuses)
        if since:
            clauses.append("lastSeen>=?")
            parameters.append(since)
        rows = self.query(f"SELECT * FROM patterns WHERE {' AND '.join(clauses)} ORDER BY {order} DESC LIMIT ?", [*parameters, limit])
        for row in rows:
            row["data"] = json.loads(row["data"] or "{}")
        return rows

    def spreadOf(self, pattern, dim, limit=8):
        return self.query("SELECT value, count FROM spread WHERE pattern=? AND dim=? ORDER BY count DESC LIMIT ?", (pattern, dim, limit))

    def examplesOf(self, pattern, limit=EXAMPLES):
        return self.query("SELECT * FROM examples WHERE pattern=? ORDER BY id DESC LIMIT ?", (pattern, limit))

    def linksOf(self, pattern):
        return self.query("SELECT links.*, a.label AS fromLabel, b.label AS toLabel, a.kind AS fromKind, b.kind AS toKind FROM links JOIN patterns a ON a.id=links.a "
                          "JOIN patterns b ON b.id=links.b WHERE links.a=? OR links.b=? ORDER BY links.count DESC LIMIT 12", (pattern, pattern))

    def setStatus(self, ident, status, note=None, wheel=None):
        found = self.pattern(ident)
        if found is None:
            return None
        with self.transaction() as db:
            row = self.rowsOf(db, [found["fp"]])[found["fp"]]
            row.update(status=status, statusAt=time.time())
            if status == "dismissed":
                row["dismissedAt"] = row["count"]
            if note is not None:
                row["note"] = note
            if wheel is not None:
                row["wheel"] = wheel
            self.writeRow(db, row)
        return row

    # Two architects (two tabs, two programs) never work on the same pattern at the same time.
    def claim(self, ids, by, seconds=1200):
        now = time.time()
        with self.transaction() as db:
            free = [ident for ident in ids if db.execute("SELECT 1 FROM patterns WHERE id=? AND (claimedBy IS NULL OR claimedBy=? OR claimedUntil<?)",
                                                         (ident, by, now)).fetchone()]
            db.executemany("UPDATE patterns SET claimedBy=?, claimedUntil=? WHERE id=?", [(by, now + seconds, ident) for ident in free])
        return free

    def release(self, by):
        with self.transaction() as db:
            db.execute("UPDATE patterns SET claimedBy=NULL, claimedUntil=NULL WHERE claimedBy=?", (by,))

    def addPass(self, mission, started, ended, outcome, spent, patterns, report):
        with self.transaction() as db:
            db.execute("INSERT INTO passes(mission, started, ended, outcome, spent, patterns, report) VALUES(?,?,?,?,?,?,?)",
                       (mission, started, ended, outcome, spent, json.dumps(patterns), report))

    def lastPass(self):
        rows = self.query("SELECT * FROM passes ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def counts(self):
        return {"patterns": self.value("SELECT COUNT(*) FROM patterns", default=0), "entries": self.value("SELECT SUM(lines) FROM sources", default=0) or 0,
                "missions": self.value("SELECT COUNT(DISTINCT mission) FROM sources", default=0),
                "byStatus": {row["status"]: row["n"] for row in self.query("SELECT status, COUNT(*) AS n FROM patterns GROUP BY status")}}

    # A mission the user deleted leaves no example and no source here; what was counted stays counted.
    def forgetMission(self, mission):
        with self.transaction() as db:
            db.execute("DELETE FROM examples WHERE mission=?", (mission,))
            db.execute("DELETE FROM spread WHERE dim='mission' AND value=?", (mission,))
            db.execute("UPDATE sources SET gone=1 WHERE mission=?", (mission,))
