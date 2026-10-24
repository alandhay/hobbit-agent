"""The move loop: look, think, act, check, once every ``move_interval_seconds``; attempts back to back."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from specbot.errors import SpecbotError

from .archive import EventLog, SessionFolder, Store
from .brain import Brain, BrainError, OutOfCredit
from .config import Config
from .rig import Frame, Rig, RigError
from .textreader import GlyphBook, new_lines

log = logging.getLogger("hobbit")

RECENT_MOVES = 8
FACTS_IN_CONTEXT = 5           # most recent facts per place, character or object given to the agent
GAMEPLAY_IN_CONTEXT = 25       # most recent facts about how the game works given to the agent
END_CONFIRMATIONS = 2          # game_over / won must be reported this many moves running
WARN_STALLED = 5               # tell the agent how long it has gone without progress after this many moves
STUCK_TRIED = 30               # moves in the stuck review's material
CREDIT_CHECK_S = 300           # paused for lack of API credit: try again this often
STREAM_GRACE_S = 300           # asleep this long (other than for credit), the broadcast stops until it wakes
NO_CREDIT = "the API account is out of credit"
WON = "it won the game"
KIND_MARK = {"works": "✔", "fails": "✖", "unsure": "?"}


@dataclass
class Move:
    number: int
    action: dict
    expected: str
    place: str
    result_lines: list[str] = field(default_factory=list)
    error: str = ""


class StopRun(Exception):
    """Stop after this attempt (budget reached, or the operator asked)."""


class Runner:
    def __init__(self, cfg: Config, rig: Rig, brain: Brain, book: GlyphBook, store: Store, events: EventLog) -> None:
        self.cfg, self.rig, self.brain, self.book, self.store, self.events = cfg, rig, brain, book, store, events
        self.status: dict = {"phase": "idle"}
        self.stalled, self.advice = 0, ""      # moves in a row without progress, and the stuck review's plan
        self._stop = threading.Event()
        # Supervision: the agent plays for awake_minutes, then sleeps (game frozen, no model calls)
        # until the operator wakes it. Resetting starts the hour again.
        self._woken = threading.Event()
        self.asleep = cfg.start_asleep
        self.sleep_reason = "started asleep" if cfg.start_asleep else ""
        self.sleep_at = None if cfg.start_asleep else self._hour_from_now()
        self.next_check: float | None = None    # paused for credit: when it tries again
        self._credit_left: float | None = None  # awake time that was left when the credit ran out
        self.asleep_since: float | None = None
        self.victory: dict | None = None        # the winning stats, shown until the operator wakes it
        self._publish_supervision()

    def stop(self) -> None:
        self._stop.set()
        self._woken.set()

    # -- supervision -----------------------------------------------------

    def _hour_from_now(self) -> float | None:
        return time.time() + self.cfg.awake_minutes * 60 if self.cfg.awake_minutes > 0 else None

    def _publish_supervision(self) -> None:
        if self.asleep and self.asleep_since is None:
            self.asleep_since = time.time()
        elif not self.asleep:
            self.asleep_since = None
        state = {"asleep": self.asleep, "sleep_at": self.sleep_at, "awake_minutes": self.cfg.awake_minutes,
                 "reason": self.sleep_reason if self.asleep else "",
                 "next_check": self.next_check if self.asleep else None}
        self.status = {**self.status, "supervision": state}
        self.events.emit("supervision", **state)

    def control(self, action: str) -> dict:
        """The operator's buttons: 'reset' (another full awake period), 'sleep' (after this move), 'wake'."""
        if action == "reset":
            if not self.asleep:                     # asleep, only Wake starts it again
                self.sleep_at = self._hour_from_now()
        elif action == "sleep":
            self.sleep_at = time.time()
        elif action == "wake":
            self.asleep, self.sleep_at, self.sleep_reason = False, self._hour_from_now(), ""
            self.victory = None
            self.status = {**self.status, "victory": None}
            self._woken.set()
        else:
            raise ValueError(f"unknown action {action!r}")
        log.info("operator: %s", action)
        self._publish_supervision()
        return self.status["supervision"]

    def _sleep_now(self, reason: str) -> None:
        """Go to sleep at once (not after the move), e.g. when the API account has run out of credit."""
        log.warning("going to sleep: %s", reason)
        self.events.emit("notice" if reason == WON else "error", text=f"going to sleep: {reason}")
        if reason.startswith(NO_CREDIT) and self.sleep_at:
            self._credit_left = max(60.0, self.sleep_at - time.time())
        self.asleep, self.sleep_at, self.sleep_reason = True, None, reason
        self._publish_supervision()

    @property
    def out_of_credit(self) -> bool:
        return self.asleep and self.sleep_reason.startswith(NO_CREDIT)

    def wants_broadcast(self) -> bool:
        """Stream while playing, while paused for credit (viewers see why), and for STREAM_GRACE_S after
        going to sleep; after that the stream stops until the agent is woken."""
        return (not self.asleep or self.out_of_credit or self.victory is not None
                or time.time() - (self.asleep_since or time.time()) < STREAM_GRACE_S)

    def _credit_is_back(self) -> None:
        """A model call worked while paused for credit: carry on with the awake time that was left."""
        left, self._credit_left = self._credit_left, None
        self.asleep, self.sleep_reason, self.next_check = False, "", None
        self.sleep_at = time.time() + left if left else self._hour_from_now()
        log.info("API credit is back: carrying on")
        self.events.emit("notice", text="API credit is back: carrying on")
        self._publish_supervision()

    def _doze_if_due(self) -> None:
        """Between moves: if the awake period is over, freeze the game and wait to be woken.

        Paused for lack of credit, it waits only CREDIT_CHECK_S and then returns, still asleep, so the
        next model call checks the account: if that call works the agent carries on by itself.
        """
        if not self.asleep and (self.sleep_at is None or time.time() < self.sleep_at):
            return
        if not self.asleep:
            self.asleep, self.sleep_at = True, None
            self.sleep_reason = self.sleep_reason or "the awake period is over"
            self._publish_supervision()
        try:
            self.rig.pause()
        except RigError:
            pass                                    # no emulator yet (before the first attempt)
        before = self.status.get("phase", "idle")
        self._phase("asleep")
        check = self.out_of_credit
        if check:
            self.next_check = time.time() + CREDIT_CHECK_S
            self._publish_supervision()
            log.info("paused for API credit; trying again in %d s", CREDIT_CHECK_S)
        else:
            log.info("asleep until the operator wakes it")
        while self.asleep and not self._stop.is_set() and not (check and time.time() >= self.next_check):
            self._woken.wait(1.0)
            self._woken.clear()
        self.next_check = None
        self._phase(before if before not in ("asleep", "idle") else ("thinking" if check else "idle"))

    def _phase(self, phase: str, **extra) -> None:
        self.status = {**self.status, "phase": phase, **extra}
        self.events.emit("phase", phase=phase, **extra)

    # -- the run ---------------------------------------------------------

    def run(self, attempts: int | None = None) -> None:
        done = 0
        while not self._stop.is_set() and (attempts is None or done < attempts):
            self._doze_if_due()
            if self._stop.is_set():
                break
            try:
                self.attempt()
            except StopRun as exc:
                self.events.emit("run_stopped", reason=str(exc))
                break
            done += 1
        self._phase("idle")

    def attempt(self) -> None:
        number = self.store.start_attempt()
        folder = SessionFolder(self.cfg.sessions, number)
        self.events.sink = folder.path / "events.jsonl"
        started = time.time()
        folder.write("session.json", {"id": folder.id, "attempt": number, "started": started,
                                      "model": self.cfg.model, "move_model": self.cfg.move_model,
                                      "effort": self.cfg.effort, "clock": self.cfg.clock,
                                      "move_interval_seconds": self.cfg.move_interval_seconds,
                                      "kb_version": self.store.kb.get("version", 0),
                                      "allow_human_hints": self.cfg.allow_human_hints})
        self.status = {"attempt": number, "session": folder.id, "move": 0, "attempt_cost": 0.0,
                       "supervision": self.status.get("supervision")}
        self.events.emit("attempt_start", attempt=number, session=folder.id)
        self._phase("loading")

        outcome, moves, recalled, stop_reason = "aborted", [], 0, None
        try:
            outcome, moves, recalled = self._play(number, folder)
        except StopRun as exc:
            outcome, stop_reason = "stopped:cost_cap", str(exc)
        except KeyboardInterrupt:
            outcome, stop_reason = "aborted", "stopped by the operator"
        except (SpecbotError, RigError) as exc:
            # The emulator went away (window closed, focus refused...): this attempt is void,
            # and the next one starts a fresh Fuse.
            outcome = "aborted"
            self.events.emit("error", text=f"emulator problem, attempt abandoned: {exc}")
            log.warning("attempt %d abandoned: %s", number, exc)
        except Exception as exc:
            # Anything else unexpected: keep the show going with a fresh attempt, but log it in full.
            outcome = "aborted"
            self.events.emit("error", text=f"unexpected error, attempt abandoned: {type(exc).__name__}: {exc}")
            log.exception("attempt %d abandoned", number)
            self._stop.wait(60)                     # don't start the next one straight away
        finally:
            try:
                self.rig.resume()
            except RigError:
                pass

        if outcome != "aborted":
            self.store.end_attempt(outcome, len(moves), recalled)
        self.events.emit("outcome", attempt=number, outcome=outcome, moves=len(moves))
        if outcome.startswith("success"):
            self._celebrate(number, folder, started, moves, recalled)
        summary = self._reflect(folder, outcome, moves) if outcome != "aborted" and moves else ""
        folder.write("session.json", {"id": folder.id, "attempt": number, "started": started, "ended": time.time(),
                                      "outcome": outcome, "moves": len(moves), "recalled": recalled,
                                      "cost_usd": self.status.get("attempt_cost", 0.0), "summary": summary,
                                      "model": self.cfg.model, "move_model": self.cfg.move_model,
                                      "effort": self.cfg.effort})
        self.events.sink = None
        if stop_reason:
            raise StopRun(stop_reason)

    def _play(self, number: int, folder: SessionFolder) -> tuple[str, list[Move], int]:
        frame = self.rig.new_attempt()
        notes, moves = "", []
        prev_lines: list[str] = []
        prev_place, recalled = "", 0
        ending = {"game_over": 0, "won": 0}
        # Progress: reaching a place, or meeting a character or object, for the first time in this attempt.
        # New text isn't enough: characters wander about and talk, so the game keeps printing
        # something new while the player stays stuck in one spot.
        found: set[str] = set()
        self.stalled, self.advice = 0, ""

        for n in range(1, self.cfg.max_moves_per_attempt + 1):
            self._doze_if_due()
            if self._stop.is_set():
                return "aborted", moves, recalled
            t0 = time.monotonic()
            bands = self.book.read(frame.image)
            lines = [b.text for b in bands]
            fresh = new_lines(prev_lines, lines)
            png = frame.png()
            name = folder.save_frame(frame.seq, png)
            self.status.update(move=n, frame=name)
            self.events.emit("frame", move=n, file=f"{folder.id}/{name}", lines=lines, new=fresh)
            if moves:
                moves[-1].result_lines = fresh
                folder.add_transcript("\n".join(f"  | {l}" for l in fresh) or "  | (no new text)")

            self.rig.pause()
            self._phase("thinking", move=n)
            decision, record = self._think(number, n, png, lines, fresh, notes, moves, found)
            self.store.add_cost(record["cost_usd"])
            self.store.add_model_move(record["model"], record["escalated"])
            self.status["attempt_cost"] = round(self.status.get("attempt_cost", 0.0) + record["cost_usd"], 4)
            folder.save_brain_call(n, {**record, "decision": decision})

            added = self.book.learn(bands, decision["screen_text"])
            if added:
                lines = [b.text for b in bands]
                fresh = new_lines(prev_lines, lines)
                if moves:
                    moves[-1].result_lines = fresh
                self.events.emit("glyphs", added=added, known=len(self.book.glyphs), lines=lines)
            if decision.get("recalled", "").strip():
                recalled += 1
            verdict = decision["previous_verdict"]
            if verdict != "n/a" and moves:
                self.store.add_prediction(verdict)

            place = decision["place"].strip() or "unknown"
            if place != "unknown":
                description = next((l for l in fresh if l.strip()), "")
                self.store.visit(place, description, folder.id, n)
                if moves and prev_place not in ("", "unknown") and place != prev_place:
                    self.store.link(prev_place, _action_label(moves[-1].action), place)
            # Memory: sightings and anything new it learned, saved now so later attempts have it.
            for who in decision["characters_seen"]:
                where = who["where"].strip() or "unknown"
                self.store.see("characters", who["name"], place if where == "unknown" else where, who["doing"], folder.id, n)
            for thing in decision["objects_seen"]:
                where = thing["where"].strip() or "unknown"
                self.store.see("objects", thing["name"], place if where == "unknown" else where, thing["state"], folder.id, n)
            learned = [f for f in decision["facts"]
                       if self.store.add_fact(f["about"], f["name"], f["fact"], folder.id, n)]
            if decision["characters_seen"] or decision["objects_seen"] or learned:
                self.events.emit("entities", move=n, seen=[c["name"] for c in decision["characters_seen"]],
                                 objects=[o["name"] for o in decision["objects_seen"]], learned=learned)
            progressed = self._progressed(place, decision, found)
            self.stalled = 0 if progressed else self.stalled + 1
            if progressed:
                self.advice = ""
            prev_place, prev_lines, notes = place, lines, decision["notes"]
            self.events.emit("decision", move=n, decision=decision, seconds=record["seconds"],
                             cost_usd=record["cost_usd"], model=record["model"], escalated=record["escalated"],
                             stats=self.stats(),
                             stalled=self.stalled, stall_limit=self.cfg.max_stale_moves)

            for state in ending:
                ending[state] = ending[state] + 1 if decision["game_state"] == state else 0
            if ending["game_over"] >= END_CONFIRMATIONS:
                self.store.mark_death(place)
                return "fail:death", moves, recalled
            if ending["won"] >= END_CONFIRMATIONS:
                return "success:unconfirmed", moves, recalled
            if self.stalled >= self.cfg.max_stale_moves:
                self.events.emit("review", move=n, verdict="limit", stalled=self.stalled,
                                 assessment=f"No progress in {self.stalled} moves: the attempt ends as stuck.")
                return "fail:stuck", moves, recalled
            if self.stalled and self.stalled % self.cfg.stuck_review_every == 0:
                review = self._review(n, png, moves, decision, folder)
                if review and review["verdict"] == "resign":
                    return "fail:resigned", moves, recalled
            if self.store.cost_today() >= self.cfg.daily_cost_cap_usd:
                raise StopRun(f"today's cost cap of ${self.cfg.daily_cost_cap_usd:.2f} was reached")

            # Give the audience the rest of the interval to follow the reasoning.
            act_at = t0 + self.cfg.move_interval_seconds
            self._phase("explaining", move=n, act_at=time.time() + max(0.0, act_at - time.monotonic()))
            while time.monotonic() < act_at and not self._stop.is_set():
                time.sleep(0.2)

            action = decision["action"]
            if decision["game_state"] in ending and ending[decision["game_state"]] < END_CONFIRMATIONS:
                # It thinks the game has ended: look again before anything is pressed, so a key
                # that restarts the game can't hide the ending.
                action = {"kind": "wait", "text": "2"}
            move = Move(n, action, decision["expected"], place)
            self._phase("acting", move=n, action=action)
            frame, move.error, echoed = self._act(action)
            moves.append(move)
            folder.add_move({"move": n, "frame": name, "action": action, "expected": decision["expected"],
                             "place": place, "mode": decision["mode"], "confidence": decision["confidence"],
                             "previous_verdict": decision["previous_verdict"], "previous_note": decision["previous_note"],
                             "evidence": decision["evidence"], "echoed": echoed, "error": move.error,
                             "seconds": record["seconds"], "cost_usd": record["cost_usd"],
                             "model": record["model"], "escalated": record["escalated"]})
            folder.add_transcript(f"[{n}] {place}: {_action_label(action)}   (expects: {decision['expected']})")
            self.events.emit("action", move=n, action=action, echoed=echoed, error=move.error)
        return "stopped:move_limit", moves, recalled

    @staticmethod
    def _progressed(place: str, decision: dict, found: set[str]) -> bool:
        """True if this move reached a place, or met a character or object, new to this attempt."""
        keys = {f"place:{place.lower()}"} if place != "unknown" else set()
        keys |= {f"who:{' '.join(c['name'].split()).lower()}" for c in decision["characters_seen"]}
        keys |= {f"what:{' '.join(o['name'].split()).lower()}" for o in decision["objects_seen"]}
        new = keys - found
        found |= new
        return bool(new)

    def _review(self, n: int, png: bytes, moves: list[Move], decision: dict, folder: SessionFolder) -> dict | None:
        """The stuck check: step back from the moves and decide whether to keep trying here or resign."""
        self._phase("reviewing", move=n, stalled=self.stalled)
        recent = "\n".join(
            f"- move {m.number} at {m.place}: {_action_label(m.action)}\n  then the screen showed: "
            + (" / ".join(m.result_lines) if m.result_lines else "(no new text)")
            for m in moves[-STUCK_TRIED:])
        material = "\n\n".join([
            f"Move {n}. No progress for {self.stalled} moves. The attempt ends as stuck after "
            f"{self.cfg.max_stale_moves} moves without progress; this review comes every "
            f"{self.cfg.stuck_review_every}.",
            "## The agent's notes now\n" + (decision["notes"] or "(none)"),
            "## Its reading of the screen now\n" + decision["interpretation"],
            "## Earlier advice in this stretch\n" + (self.advice or "(none)"),
            "## Recent moves\n" + recent,
            "## How the game works (learned while playing)\n" + "\n".join(
                f"- {f['text']}" for f in self.store.entities["gameplay"][-GAMEPLAY_IN_CONTEXT:]),
            "## Known places\n" + (self._places() or "(none yet)"),
            "## Lessons from earlier attempts\n" + ("\n".join(f"- {l['text']}" for l in self.store.top_lessons())
                                                    or "(none)"),
        ])
        try:
            review, record = self.brain.review(material, png)
        except BrainError as exc:
            self.events.emit("error", move=n, text=f"stuck review failed: {exc}")
            if isinstance(exc, OutOfCredit):
                self._sleep_now(NO_CREDIT)
            return None
        self.store.add_cost(record["cost_usd"])
        self.status["attempt_cost"] = round(self.status.get("attempt_cost", 0.0) + record["cost_usd"], 4)
        self.advice = review["plan"]
        self.events.emit("review", move=n, stalled=self.stalled, **review)
        folder.write(f"review_{n:04d}.json", {**review, "move": n, "stalled": self.stalled, "cost_usd": record["cost_usd"]})
        folder.add_transcript(f"[review at move {n}, {self.stalled} moves without progress] "
                              f"{review['verdict']}: {review['assessment']} Plan: {review['plan']}")
        return review

    def _act(self, action: dict) -> tuple[Frame, str, bool]:
        kind, text = action["kind"], action["text"]
        try:
            if kind == "type":
                frame, echoed = self.rig.type_line(text, self._bottom_text)
                return frame, "" if echoed else "not all of the line showed on screen before ENTER", echoed
            if kind == "key":
                return self.rig.press_key(text), "", True
            seconds = float(text) if text.replace(".", "", 1).isdigit() else 5.0
            return self.rig.wait(seconds), "", True
        except RigError as exc:
            return self.rig.observe(), str(exc), False

    def _bottom_text(self, image) -> str:
        bands = self.book.read(image)
        return bands[-1].text if bands else ""

    def _think(self, number: int, n: int, png: bytes, lines: list[str], fresh: list[str],
               notes: str, moves: list[Move], found: set[str]) -> tuple[dict, dict]:
        """One decision. Routine moves go to the cheap move model; moves that need judgment go to the
        main model, either straight away or after the move model's answer shows it was out of its depth."""
        context = self._context(number, n, lines, fresh, notes, moves)
        cheap, main = self.cfg.move_model, self.cfg.model
        reasons = escalate_before(n, moves, self.stalled) if cheap and cheap != main else []
        if not cheap or cheap == main or reasons:
            decision, record = self._decide(n, context, png, main)
            return decision, {**record, "escalated": reasons}
        decision, record = self._decide(n, context, png, cheap)
        reasons = escalate_after(decision, found, self.cfg.escalate_below_confidence)
        if not reasons:
            return decision, {**record, "escalated": []}
        self.events.emit("escalate", move=n, reasons=reasons, model=main, first_model=cheap)
        first = {"model": cheap, "decision": decision, "seconds": record["seconds"], "cost_usd": record["cost_usd"]}
        decision, second = self._decide(n, context, png, main)
        return decision, {**second, "escalated": reasons, "first": first,
                          "seconds": round(second["seconds"] + first["seconds"], 1),
                          "cost_usd": second["cost_usd"] + first["cost_usd"]}

    def _decide(self, n: int, context: str, png: bytes, model: str) -> tuple[dict, dict]:
        buffers: dict[str, list[str]] = {"thinking": [], "draft": []}
        last = [time.monotonic()]

        def flush() -> None:
            for kind, buffer in buffers.items():
                if buffer:
                    self.events.emit(kind, move=n, text="".join(buffer))
                    buffer.clear()
            last[0] = time.monotonic()

        def on_stream(kind: str, chunk: str) -> None:
            buffers[kind].append(chunk)
            if time.monotonic() - last[0] > 0.3:
                flush()

        attempt = 0
        while True:
            attempt += 1
            try:
                decision, record = self.brain.decide(context, png, on_stream, model)
                flush()
                if self.out_of_credit:
                    self._credit_is_back()
                return decision, record
            except OutOfCredit as exc:
                if not self.out_of_credit:
                    self._sleep_now(f"{NO_CREDIT} ({exc})")
                self._doze_if_due()             # back after the operator wakes it, or CREDIT_CHECK_S
                if self._stop.is_set():
                    raise
                attempt = 0
                continue
            except BrainError as exc:
                self.events.emit("error", move=n, text=str(exc))
                if attempt >= 2:
                    raise
                time.sleep(5)

    def _context(self, number: int, n: int, lines: list[str], fresh: list[str], notes: str,
                 moves: list[Move]) -> str:
        parts = [f"Attempt {number}, move {n}."]
        lessons = self.store.top_lessons()
        parts.append("## What earlier attempts taught me\n" + ("\n".join(
            f"- [{l['id']} {KIND_MARK.get(l['kind'], '?')} conf {l['confidence']:.1f}] {l['text']}" for l in lessons)
            or "(nothing yet: this is the first attempt, or nothing has been learned so far)"))
        experiments = self.store.kb.get("experiments", [])
        if experiments:
            parts.append("## Experiments suggested by earlier attempts\n" + "\n".join(
                f"- {e['text']} ({e['why']})" for e in experiments))
        gameplay = self.store.entities["gameplay"][-GAMEPLAY_IN_CONTEXT:]
        parts.append("## How the game works (learned while playing)\n" +
                     ("\n".join(f"- {f['text']}" for f in gameplay) or "(nothing yet)"))
        parts.append("## Known places\n" + (self._places() or "(none yet)"))
        parts.append("## Characters I know\n" + (self._sightings("characters") or "(none yet)"))
        parts.append("## Objects I know\n" + (self._sightings("objects") or "(none yet)"))
        parts.append("## My notes from the last move\n" + (notes or "(none)"))
        if self.stalled >= WARN_STALLED:
            parts.append(f"## Progress check\nNo progress for {self.stalled} moves: no new place, character or object "
                         f"in this attempt. At {self.cfg.max_stale_moves} the attempt ends as stuck. Try something different "
                         "from what you've been doing." +
                         (f"\nAdvice from stepping back: {self.advice}" if self.advice else ""))
        if moves:
            recent = []
            for m in moves[-RECENT_MOVES:]:
                result = " / ".join(m.result_lines) if m.result_lines else "(no new text)"
                err = f" [problem: {m.error}]" if m.error else ""
                recent.append(f"- move {m.number} at {m.place}: {_action_label(m.action)}{err}\n"
                              f"  expected: {m.expected}\n  then the screen showed: {result}")
            parts.append("## Recent moves\n" + "\n".join(recent))
        parts.append("## Text that is new since my last action\n" + ("\n".join(fresh) or "(none)"))
        parts.append("## All text on screen now (from the screen reader; ? = not read yet)\n" +
                     ("\n".join(lines) or "(no text found)"))
        return "\n\n".join(parts)

    def _places(self) -> str:
        nodes, edges = self.store.map["nodes"], self.store.map["edges"]
        out = []
        for name, node in sorted(nodes.items(), key=lambda kv: -kv[1]["visits"]):
            exits = [f"{e['command']} -> {e['to']}" for e in edges if e["from"] == name]
            death = f", died here {node['deaths']}x" if node.get("deaths") else ""
            out.append(f"- {name} (visited {node['visits']}x{death})" + (f": {'; '.join(exits)}" if exits else ""))
            facts = self.store.entities["places"].get(name, {}).get("facts", [])[-FACTS_IN_CONTEXT:]
            out.extend(f"    - {f['text']}" for f in facts)
        return "\n".join(out[:80])

    def _sightings(self, kind: str) -> str:
        """Characters or objects: where each was last seen (this attempt or an earlier one) and what is known."""
        out = []
        current = self.status.get("session")
        for name, entry in sorted(self.store.entities[kind].items(), key=lambda kv: -kv[1].get("sightings", 0)):
            last = entry.get("last_seen")
            if last:
                when = f"move {last['move']}" if last["session"] == current else "in an earlier attempt"
                seen = f"last seen at {last['place']} ({when}): {last['doing']}"
            else:
                seen = "not seen yet"
            out.append(f"- {name}: seen {entry.get('sightings', 0)}x, {seen}")
            out.extend(f"    - {f['text']}" for f in entry.get("facts", [])[-FACTS_IN_CONTEXT:])
        return "\n".join(out[:80])

    def _reflect(self, folder: SessionFolder, outcome: str, moves: list[Move]) -> str:
        self._phase("reflecting")
        surprises = [m for m in moves if m.error]
        material = "\n\n".join([
            f"Session {folder.id}. Outcome: {outcome}. Moves: {len(moves)}.\n"
            "Outcomes starting with 'stopped:' were set by the operator's limits (moves per attempt, daily cost), "
            "not by the game: they say nothing about the game, so don't draw lessons from them.",
            "## Transcript\n" + folder.transcript()[-60000:],
            "## Moves with problems\n" + ("\n".join(f"- move {m.number}: {m.error}" for m in surprises) or "(none)"),
            "## Current knowledge base\n" + json.dumps(self.store.kb, ensure_ascii=False, indent=1),
            "## Memory recorded move by move (places, characters, objects, how the game works)\n" +
            json.dumps({book: self.store.entities[book] for book in ("gameplay", "places", "characters", "objects")},
                       ensure_ascii=False)[-40000:],
        ])
        try:
            kb, record = self.brain.reflect(material)
        except BrainError as exc:
            self.events.emit("error", text=f"reflection failed: {exc}")
            if isinstance(exc, OutOfCredit):
                self._sleep_now(NO_CREDIT)
            return ""
        self.store.add_cost(record["cost_usd"])
        self.store.save_kb(kb)
        folder.write("reflection.json", {**kb, "cost_usd": record["cost_usd"]})
        self.events.emit("kb", lessons=self.store.top_lessons(), summary=kb.get("summary", ""),
                         version=self.store.kb["version"])
        return kb.get("summary", "")

    def _celebrate(self, number: int, folder: SessionFolder, started: float, moves: list[Move], recalled: int) -> None:
        """It won: pause the session and put the winning stats on the dashboard (and the stream)."""
        s = self.store.stats
        p = s["predictions"]
        judged = p.get("met", 0) + p.get("partly", 0) + p.get("not_met", 0)
        self.victory = {
            "attempt": number, "moves": len(moves), "minutes": round((time.time() - started) / 60, 1),
            "attempt_cost_usd": round(self.status.get("attempt_cost", 0.0), 2),
            "attempts": s["attempts"], "deaths_before": s["outcomes"].get("fail:death", 0),
            "moves_all_attempts": s["moves_total"], "places": len(self.store.map["nodes"]),
            "characters": len(self.store.entities["characters"]), "objects": len(self.store.entities["objects"]),
            "lessons": len(self.store.top_lessons(1000)),
            "predictions_right": round(100 * p.get("met", 0) / judged) if judged else None,
            "moves_by_model": dict(s["moves_by_model"]), "recalled_moves": recalled,
            "cost_total_usd": round(s["cost_usd_total"], 2), "session": folder.id, "won_at": time.time(),
        }
        self.store.add_win(self.victory)
        folder.write("victory.json", self.victory)
        folder.add_transcript(f"[won] attempt {number} in {len(moves)} moves")
        log.info("WON: attempt %d in %d moves", number, len(moves))
        self.status = {**self.status, "victory": self.victory}
        self.events.emit("victory", **self.victory)
        self._sleep_now(WON)

    def stats(self) -> dict:
        s = self.store.stats
        return {"attempts": s["attempts"], "outcomes": s["outcomes"], "best_moves": s["best_moves"],
                "moves_total": s["moves_total"], "places": len(self.store.map["nodes"]),
                "lessons": len(self.store.top_lessons(1000)),
                "characters": len(self.store.entities["characters"]), "objects": len(self.store.entities["objects"]),
                "gameplay": len(self.store.entities["gameplay"]), "cost_today": round(self.store.cost_today(), 2),
                "recalled_total": s["recalled_total"], "glyphs": len(self.book.glyphs),
                "predictions": s["predictions"], "moves_by_model": s["moves_by_model"],
                "escalations": s["escalations"], "wins": len(s["wins"])}


def escalate_before(n: int, moves: list[Move], stalled: int) -> list[str]:
    """Moves that go straight to the main model, before the move model is asked."""
    reasons = []
    if n == 1:
        reasons.append("first move of the attempt")
    if moves and moves[-1].error:
        reasons.append("the last action had a problem")
    if stalled >= WARN_STALLED:
        reasons.append(f"no progress for {stalled} moves")
    return reasons


def escalate_after(decision: dict, found: set[str], min_confidence: float) -> list[str]:
    """Reasons to redo the move model's decision with the main model, judged from that decision."""
    reasons = []
    if decision["previous_verdict"] == "not_met":
        reasons.append("the last prediction failed")
    if decision["game_state"] in ("game_over", "won", "unknown"):
        reasons.append(f"game state: {decision['game_state']}")
    place = decision["place"].strip()
    if place and place != "unknown" and f"place:{place.lower()}" not in found:
        reasons.append("a new place")
    if decision["confidence"] < min_confidence:
        reasons.append(f"low confidence ({decision['confidence']:.2f})")
    options = decision["options"]
    if options and 0 <= decision["chosen"] < len(options) and options[decision["chosen"]]["risk"] == "high":
        reasons.append("a high-risk move")
    return reasons


def _action_label(action: dict) -> str:
    if action["kind"] == "type":
        return f'type "{action["text"]}"'
    if action["kind"] == "key":
        return f"press {action['text']}"
    return f"wait {action['text']}s"
