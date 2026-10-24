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

Settings are in `hobbit.toml` (pace, models, effort, limits, daily cost cap). Routine moves go to Claude Haiku 5.5 and moves that need judgment to Claude Sonnet 5.5 (DESIGN §15.5). Fuse comes to the front for a second or two each time a command is typed, so don't type into other windows while it plays. Ctrl+C stops it, and the game is unfrozen on exit.

## Running on the server (AWS Lightsail)

One Lightsail server (Ubuntu 24.04, 2 vCPU / 4 GB, $24/month) runs the emulator, the agent, the dashboard and the YouTube broadcast. From Git Bash, with the AWS CLI signed in:

```
bash deploy/create.sh                 # the server, a static IP, ports 22/80/443 only (once)
cp deploy/hobbit.env.example deploy/hobbit.env    # then fill in the API key and the YouTube stream key
bash deploy/push.sh <server-ip>       # install or update (sends the committed code, the tape, and the memory the first time)
```

- Dashboard: `https://<ip-with-dashes>.sslip.io/`. Add `?operator` for the buttons (Reset timer, Sleep now, Wake); they ask for `HOBBIT_OPERATOR_PASSWORD` from `deploy/hobbit.env`.
- It is **supervised**: the service starts asleep (game frozen, no model calls). Wake plays for `awake_minutes` (60), counting down on the page, then it sleeps again. Reset timer starts the hour again.
- The broadcast (Chrome showing the dashboard, plus the game's sound) goes to YouTube when `YOUTUBE_STREAM_KEY` is set. It stops 5 minutes after the agent goes to sleep and starts again when it wakes.
- Out of API credit: it pauses at once ("Gameplay paused, out of credits" on the stream, which stays up), tries again every 5 minutes, and carries on by itself, same move and the awake time it had left, once the account is topped up.
- From this PC without the page: `bash deploy/control.sh <ip> status|wake|sleep|reset|restart|logs`.
- Logs: `ssh ubuntu@<ip> sudo journalctl -u hobbit -f`; ffmpeg's warnings in `/var/lib/hobbit/logs/ffmpeg.log`.

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
