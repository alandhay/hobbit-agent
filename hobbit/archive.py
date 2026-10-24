"""Where everything is kept: the live event log, session folders, and what lasts across attempts.

- ``EventLog``: every event of the current run, in memory for the dashboard (long-poll)
  and appended to the current session's ``events.jsonl``.
- ``SessionFolder``: ``sessions/<time>_a<NNNN>/`` with frames, moves, brain calls, transcript.
- ``Store``: small JSON files under ``data/`` that outlive a session: stats, knowledge base, map,
  and ``entities.json``: characters and objects (sightings and facts), places (facts) and
  gameplay (facts about how the game works).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import Any


RECENT_SIGHTINGS = 12          # sightings kept per character
MAX_FACTS = 30                 # facts kept per place, character or object (the oldest go first)
MAX_GAMEPLAY = 80              # facts kept about how the game works
FACT_BOOKS = {"place": "places", "character": "characters", "object": "objects", "gameplay": "gameplay"}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, indent=1, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    # OneDrive (and virus scanners) briefly lock files they are looking at, which makes the
    # swap fail with "access denied" now and then: try again for a moment before giving up.
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    path.write_text(text, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class EventLog:
    KEEP = 2000             # events held in memory for the page

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._first = 0     # number of the oldest event still held
        self._changed = threading.Condition()
        self.sink: Path | None = None

    def emit(self, kind: str, **data: Any) -> dict:
        with self._changed:
            event = {"n": self._first + len(self._events), "kind": kind, "at": time.time(), **data}
            self._events.append(event)
            if len(self._events) > self.KEEP:
                drop = len(self._events) - self.KEEP
                del self._events[:drop]
                self._first += drop
            if self.sink:
                with self.sink.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._changed.notify_all()
        return event

    def since(self, after: int, timeout: float) -> list[dict]:
        """Events numbered ``after`` or later, waiting up to ``timeout`` seconds for one."""
        with self._changed:
            self._changed.wait_for(lambda: self._first + len(self._events) > after, timeout)
            start = max(0, after - self._first)
            return self._events[start:]


class SessionFolder:
    def __init__(self, root: Path, attempt: int) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        self.id = f"{stamp}_a{attempt:04d}"
        self.path = root / self.id
        (self.path / "frames").mkdir(parents=True, exist_ok=True)
        (self.path / "brain").mkdir(exist_ok=True)

    def save_frame(self, seq: int, png: bytes) -> str:
        name = f"{seq:06d}.png"
        (self.path / "frames" / name).write_bytes(png)
        return name

    def save_brain_call(self, move: int, record: dict) -> None:
        _write_json(self.path / "brain" / f"{move:04d}.json", record)

    def add_move(self, record: dict) -> None:
        with (self.path / "moves.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def add_transcript(self, text: str) -> None:
        with (self.path / "transcript.txt").open("a", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n")

    def transcript(self) -> str:
        try:
            return (self.path / "transcript.txt").read_text(encoding="utf-8")
        except OSError:
            return ""

    def write(self, name: str, data: Any) -> None:
        _write_json(self.path / name, data)


class Store:
    """Stats, knowledge base and map, shared by every attempt."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self._lock = threading.Lock()
        self.stats = _read_json(data / "stats.json", {})
        self.stats.setdefault("attempts", 0)
        self.stats.setdefault("outcomes", {})
        self.stats.setdefault("moves_total", 0)
        self.stats.setdefault("best_moves", 0)
        self.stats.setdefault("cost_usd_total", 0.0)
        self.stats.setdefault("cost_by_day", {})
        self.stats.setdefault("recalled_total", 0)
        self.stats.setdefault("predictions", {})
        self.stats.setdefault("moves_by_model", {})
        self.stats.setdefault("escalations", {})
        self.stats.setdefault("wins", [])
        self.kb = _read_json(data / "kb.json", {"lessons": [], "experiments": [], "version": 0})
        self.map = _read_json(data / "map.json", {"nodes": {}, "edges": []})
        self.entities = _read_json(data / "entities.json", {})
        for book, empty in (("characters", {}), ("objects", {}), ("places", {}), ("gameplay", [])):
            self.entities.setdefault(book, empty)

    # -- stats -----------------------------------------------------------

    def add_cost(self, usd: float) -> None:
        day = dt.date.today().isoformat()
        with self._lock:
            self.stats["cost_usd_total"] = round(self.stats["cost_usd_total"] + usd, 6)
            self.stats["cost_by_day"][day] = round(self.stats["cost_by_day"].get(day, 0.0) + usd, 6)
            _write_json(self.data / "stats.json", self.stats)

    def cost_today(self) -> float:
        return self.stats["cost_by_day"].get(dt.date.today().isoformat(), 0.0)

    def add_prediction(self, verdict: str) -> None:
        with self._lock:
            self.stats["predictions"][verdict] = self.stats["predictions"].get(verdict, 0) + 1
            _write_json(self.data / "stats.json", self.stats)

    def add_model_move(self, model: str, escalated: list[str]) -> None:
        """Which model made each move, and why moves were sent to the main model."""
        with self._lock:
            self.stats["moves_by_model"][model] = self.stats["moves_by_model"].get(model, 0) + 1
            for reason in escalated:
                key = reason.split(" (")[0] if reason.startswith("low confidence") else reason
                key = "no progress" if key.startswith("no progress for") else key
                self.stats["escalations"][key] = self.stats["escalations"].get(key, 0) + 1
            _write_json(self.data / "stats.json", self.stats)

    def add_win(self, victory: dict) -> None:
        with self._lock:
            self.stats["wins"].append(victory)
            _write_json(self.data / "stats.json", self.stats)

    def start_attempt(self) -> int:
        with self._lock:
            self.stats["attempts"] += 1
            _write_json(self.data / "stats.json", self.stats)
            return self.stats["attempts"]

    def end_attempt(self, outcome: str, moves: int, recalled: int) -> None:
        with self._lock:
            self.stats["outcomes"][outcome] = self.stats["outcomes"].get(outcome, 0) + 1
            self.stats["moves_total"] += moves
            self.stats["best_moves"] = max(self.stats["best_moves"], moves)
            self.stats["recalled_total"] += recalled
            _write_json(self.data / "stats.json", self.stats)

    # -- knowledge base --------------------------------------------------

    def save_kb(self, kb: dict) -> None:
        with self._lock:
            kb["version"] = self.kb.get("version", 0) + 1
            self.kb = kb
            _write_json(self.data / "kb.json", kb)

    def top_lessons(self, n: int = 15) -> list[dict]:
        active = [l for l in self.kb.get("lessons", []) if l.get("status", "active") == "active"]
        return sorted(active, key=lambda l: l.get("confidence", 0) * (1 + l.get("support", 0)), reverse=True)[:n]

    # -- map -------------------------------------------------------------

    def visit(self, place: str, description: str, session: str, move: int) -> None:
        with self._lock:
            node = self.map["nodes"].setdefault(place, {"visits": 0, "description": description,
                                                        "first_seen": f"{session}#{move}", "deaths": 0})
            node["visits"] += 1
            if description and not node.get("description"):
                node["description"] = description
            _write_json(self.data / "map.json", self.map)

    def link(self, src: str, command: str, dst: str) -> None:
        with self._lock:
            for edge in self.map["edges"]:
                if edge["from"] == src and edge["command"] == command:
                    if edge["to"] == dst:
                        edge["count"] += 1
                    else:
                        edge["inconsistent"] = True
                        edge.setdefault("also", [])
                        if dst not in edge["also"]:
                            edge["also"].append(dst)
                    break
            else:
                self.map["edges"].append({"from": src, "command": command, "to": dst, "count": 1})
            _write_json(self.data / "map.json", self.map)

    # -- memory: characters, objects, places and how the game works ------
    #
    # Written the moment something is learned, so a later attempt (or a crash) never loses it.

    def _entry(self, book: str, name: str, session: str, move: int) -> dict:
        """The entry for ``name`` in a book, matched without regard to case; made if new."""
        entries = self.entities.setdefault(book, {})
        key = next((k for k in entries if k.lower() == name.lower()), name)
        if book == "places":
            return entries.setdefault(key, {"facts": []})
        return entries.setdefault(key, {"first_seen": {"session": session, "move": move, "place": ""},
                                        "sightings": 0, "places": {}, "recent": [], "facts": []})

    def see(self, kind: str, name: str, place: str, doing: str, session: str, move: int) -> None:
        """Record a sighting of a character or an object: where it was and what it was doing (or its state)."""
        name = " ".join(name.split())
        if not name or kind not in ("characters", "objects"):
            return
        with self._lock:
            entry = self._entry(kind, name, session, move)
            if not entry["first_seen"]["place"]:
                entry["first_seen"]["place"] = place
            entry["sightings"] += 1
            entry["last_seen"] = {"session": session, "move": move, "place": place, "doing": doing, "at": time.time()}
            if place and place != "unknown":
                entry["places"][place] = entry["places"].get(place, 0) + 1
            entry["recent"] = (entry["recent"] + [{"session": session, "move": move, "place": place,
                                                   "doing": doing}])[-RECENT_SIGHTINGS:]
            _write_json(self.data / "entities.json", self.entities)

    def see_character(self, name: str, place: str, doing: str, session: str, move: int) -> None:
        self.see("characters", name, place, doing, session, move)

    def add_fact(self, about: str, name: str, fact: str, session: str, move: int) -> bool:
        """Remember something learned about a place, character, object or the game itself.

        Returns False if it was already known (or makes no sense), True if it is new.
        """
        name, fact = " ".join(name.split()), " ".join(fact.split())
        if not fact or about not in FACT_BOOKS or (about != "gameplay" and not name):
            return False
        record = {"text": fact, "session": session, "move": move}
        with self._lock:
            if about == "gameplay":
                known = self.entities.setdefault("gameplay", [])
                if any(f["text"].lower() == fact.lower() for f in known):
                    return False
                self.entities["gameplay"] = (known + [record])[-MAX_GAMEPLAY:]
            else:
                entry = self._entry(FACT_BOOKS[about], name, session, move)
                if any(f["text"].lower() == fact.lower() for f in entry["facts"]):
                    return False
                entry["facts"] = (entry["facts"] + [record])[-MAX_FACTS:]
            _write_json(self.data / "entities.json", self.entities)
            return True

    def mark_death(self, place: str) -> None:
        with self._lock:
            if place in self.map["nodes"]:
                self.map["nodes"][place]["deaths"] += 1
                _write_json(self.data / "map.json", self.map)
