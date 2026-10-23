# hobbit-agent

An AI agent that plays *The Hobbit* (1982) on a ZX Spectrum emulator, live, from first principles. It reads the screen, decides what to type, and shows its reasoning on a dashboard while it plays. Each attempt is archived, and what it learns carries over to the next attempt.

`DESIGN.md` has the full design, including cost (§15) and cloud hosting (§12.2).

## Running locally (Windows)

Needs Fuse at `C:/Program Files (x86)/Fuse/`, your own tape at `data/game/hobbit.tzx`, and a Claude Code login. `specbot` comes from the sibling `spec-emulator-window` checkout of [spec-agent](https://github.com/alandhay/spec-agent).

```
python -m venv .venv
.venv\Scripts\pip install -e ..\spec-emulator-window claude-agent-sdk imagehash

.venv\Scripts\python -m hobbit check          # load the game, read the screen, one model call; no keys pressed
.venv\Scripts\python -m hobbit run            # play attempts back to back, one move every 5 minutes
                                              # dashboard: http://127.0.0.1:8800
.venv\Scripts\python -m hobbit run --attempts 1 --max-moves 10 --interval 30   # a short trial
```

Settings are in `hobbit.toml` (pace, model, effort, limits, daily cost cap). Fuse comes to the front for a second or two each time a command is typed, so don't type into other windows while it plays. Ctrl+C stops it, and the game is unfrozen on exit.

## Where things go

| Path | What |
|---|---|
| `sessions/<time>_aNNNN/` | One attempt: `frames/`, `moves.jsonl`, `brain/NNNN.json` (each model call with its reasoning and cost), `events.jsonl`, `transcript.txt`, `reflection.json`, `session.json` |
| `data/stats.json` | Attempts, outcomes, best run, spend by day |
| `data/kb.json` | Lessons and experiments, rewritten after each attempt by the reflection pass |
| `data/map.json` | Places and the commands that link them |
| `data/glyphs.json` | The game font, learned from the agent's transcriptions |

## Layout

| Module | Job |
|---|---|
| `hobbit/rig.py` | Fuse via specbot: restart with the tape, settle detection, freeze and unfreeze, typing with an echo check |
| `hobbit/textreader.py` | Reads the game's own 6-pixel font with a self-teaching glyph book; finds new text as it scrolls |
| `hobbit/brain.py` | Model calls through the Agent SDK: no tools, no connectors, structured JSON decisions, live thinking |
| `hobbit/session.py` | The move loop, outcomes, map updates, reflection after each attempt |
| `hobbit/archive.py` | Event log, session folders, stats, knowledge base and map storage |
| `hobbit/server.py`, `hobbit/static/dashboard.html` | The live dashboard |
| `prompts/` | The agent's system prompt and the reflection prompt (no game knowledge) |
