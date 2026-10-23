"""hobbit.toml -> Config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    tape: Path = ROOT / "data" / "game" / "hobbit.tzx"
    specbot_config: Path = ROOT / "specbot.toml"
    move_interval_seconds: float = 300.0
    clock: str = "paused"
    brain_backend: str = "agent_sdk"
    model: str = "claude-sonnet-5-5"           # moves that need judgment, stuck reviews, reflection
    effort: str = "medium"
    move_model: str = "claude-haiku-5-5"       # routine moves; "" sends every move to `model`
    escalate_below_confidence: float = 0.6     # a routine move below this confidence goes to `model`
    max_moves_per_attempt: int = 300
    max_stale_moves: int = 50
    stuck_review_every: int = 10
    daily_cost_cap_usd: float = 25.0
    allow_human_hints: bool = False
    awake_minutes: float = 0.0                 # supervised: sleep this long after starting or the last reset (0 = never)
    start_asleep: bool = False                 # supervised: wait for the operator to wake it before playing
    host: str = "127.0.0.1"
    port: int = 8800
    data: Path = ROOT / "data"
    sessions: Path = ROOT / "sessions"
    prompts: Path = ROOT / "prompts"


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "hobbit.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    pace, brain, limits = raw.get("pace", {}), raw.get("brain", {}), raw.get("limits", {})
    learning, server = raw.get("learning", {}), raw.get("server", {})
    supervision = raw.get("supervision", {})
    cfg = Config()
    if "tape" in raw:
        cfg.tape = (ROOT / raw["tape"]).resolve()
    if "specbot_config" in raw:
        cfg.specbot_config = (ROOT / raw["specbot_config"]).resolve()
    if "data_dir" in raw:
        cfg.data = (ROOT / raw["data_dir"]).resolve()
    if "sessions_dir" in raw:
        cfg.sessions = (ROOT / raw["sessions_dir"]).resolve()
    cfg.move_interval_seconds = float(pace.get("move_interval_seconds", cfg.move_interval_seconds))
    cfg.clock = pace.get("clock", cfg.clock)
    cfg.brain_backend = brain.get("backend", cfg.brain_backend)
    cfg.model = brain.get("model", cfg.model)
    cfg.effort = brain.get("effort", cfg.effort)
    cfg.move_model = brain.get("move_model", cfg.move_model)
    cfg.escalate_below_confidence = float(brain.get("escalate_below_confidence", cfg.escalate_below_confidence))
    cfg.max_moves_per_attempt = int(limits.get("max_moves_per_attempt", cfg.max_moves_per_attempt))
    cfg.max_stale_moves = int(limits.get("max_stale_moves", cfg.max_stale_moves))
    cfg.stuck_review_every = int(limits.get("stuck_review_every", cfg.stuck_review_every))
    cfg.daily_cost_cap_usd = float(limits.get("daily_cost_cap_usd", cfg.daily_cost_cap_usd))
    cfg.allow_human_hints = bool(learning.get("allow_human_hints", cfg.allow_human_hints))
    cfg.awake_minutes = float(supervision.get("awake_minutes", cfg.awake_minutes))
    cfg.start_asleep = bool(supervision.get("start_asleep", cfg.start_asleep))
    cfg.host = server.get("host", cfg.host)
    cfg.port = int(server.get("port", cfg.port))
    if cfg.clock not in ("paused", "live"):
        raise ValueError(f"pace.clock must be 'paused' or 'live', not {cfg.clock!r}")
    return cfg
