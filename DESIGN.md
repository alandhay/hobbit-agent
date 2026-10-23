# hobbit-agent — design

An AI agent that plays *The Hobbit* (Melbourne House, 1982) on a ZX Spectrum emulator, live, from first principles. It looks at the screen, works out what it means, decides what to type, and shows all of that on screen while it plays. Every attempt is archived. A knowledge base and a map of the game world build up across attempts, and the whole thing can be streamed to YouTube, Twitch or any other RTMP service.

The emulator layer comes from [spec-agent](https://github.com/alandhay/spec-agent) (`specbot`). This document notes what is reused as is, what gets wrapped, and what has to be added.

---

## 1. Goals and non-goals

**Goals**

- Play the real game in a real emulator, with the screen as the only input: no memory peeking and no game-specific code paths.
- Learn only from its own experience: no walkthroughs, no web access, no hand-written hints.
- Make the agent's reasoning watchable: what it sees, how it reads it, the options it weighs, what it picks, and whether it was right.
- Give every attempt a clear outcome (success or fail, with a reason) and archive all of it: frames, moves, model I/O and logs.
- Build up across attempts: a knowledge base (facts and distilled lessons) and a world map.
- Keep stats on screen all the time: attempts, moves, best run, locations found, lessons learned.
- Run locally or in the cloud from the same container. Broadcast over RTMP/RTMPS (YouTube, Twitch, Kick, or a restream relay).

**Non-goals (for now)**

- Supporting other games. The design stays generic where that costs nothing, but nothing is built for it.
- Superhuman speed. The pace is set so humans can follow it.
- Shipping the game. The user supplies their own copy of the tape (`.tap`/`.tzx`).

### The "first principles" problem, stated honestly

The model may already know things about *The Hobbit* from its training data. That can't be switched off, so the design makes any leakage **visible and measurable** instead of pretending it can't happen:

1. **No outside sources.** The agent has no web, file or shell tools. Its only actions are "type a command" and "wait".
2. **System prompt.** It is told to reason only from what it has seen in this run and in its own knowledge base, and to label anything else as a guess.
3. **Evidence on every decision.** Each decision has to cite evidence ids: screen lines (`S12:L3`), KB entries (`K47`), map edges (`E9`). A decision with no citation must be marked `exploration`.
4. **Leak audit.** After each session a judge pass flags commands that mention a noun or place that had never appeared on screen or in the KB before that move. The leak count is a stat on screen and in the archive.

---

## 2. Architecture overview

```
                       ┌──────────────────────────── container (local Docker or cloud VM) ────────────────────────────┐
                       │                                                                                              │
  Hobbit snapshot ───► │  RIG (specbot X11Emulator)          SCREEN READER            BRAIN                           │
  (.z80/.szx)          │  Xvfb :101 ── Fuse 48K ──frames──►  glyph OCR, text diff ──► context builder ──► Claude      │
                       │       ▲  keys (XTEST)               settle detection         (prompt, memory,    (decision   │
                       │       │  pause/resume (SIGSTOP)                               KB, map)             JSON)     │
                       │       └────────────────────── type command ◄──────────────────────────────────────┘          │
                       │                                                                                              │
                       │  EVENT BUS (append-only, one per session) ──► ARCHIVE  sessions/<id>/…                       │
                       │       │                                   ──► KB + MAP  (updated at the end of a session)    │
                       │       ▼                                                                                      │
                       │  DASHBOARD SERVER (HTTP + SSE) ──► dashboard page (browser locally / Chromium on Xvfb :102)  │
                       │                                                                                              │
                       │  BROADCASTER (ffmpeg):  x11grab :101 (emulator, 50 fps) ─┐                                   │
                       │                         x11grab :102 (dashboard)  ────────┼─► composite ─► H.264/AAC ─► RTMP  │
                       │                         emulator PCM (AudioTap) ──────────┘                    └─► local MP4 │
                       └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

Each component talks to the others only through the **event bus** and a few narrow interfaces. Every component can be run and tested without the ones above it, the same way `specbot` → `specdev.bench` → `specdev.agent` are layered.

---

## 3. The emulator rig (built on specbot)

### 3.1 What is reused from spec-agent

| specbot piece | Use here |
|---|---|
| `Emulator` base: `screenshot`, `type_text`, `press`, `wait_for_change`, `screen_text` | The rig's whole see/act interface. |
| `x11_backend.X11Emulator`: private Xvfb + Fuse, XTEST keys, no focus needed | **Main backend**, both locally (Docker/WSL2) and in the cloud. Several can run side by side. |
| `x11_backend.AudioTap`: SDL disk-audio output followed as it grows (48 kHz, s16le, stereo) | Audio for the broadcast. |
| `capture.changed_fraction`, `postprocess` | Detecting when the screen has settled. |
| `ocr.load_font` / `read_screen_text` (exact 8×8 cell matching) | Seed for the game-font reader (§4.1). |
| `keys.text_to_chords` (literal typing) | Typing commands. The game reads the keyboard itself, so typing letter by letter is right. |
| `window_backend.WindowEmulator` | Native-Windows "dev mode" only. It takes keyboard focus, so it isn't used for unattended runs. |
| `deploy/` patterns (CloudFormation, setup.sh with `fuse-emulator-sdl` + `xvfb` + `spectrum-roms`, Bedrock role, idle stop) | Starting point for the cloud deployment. |
| `specdev.agent` patterns (event list, generation counter, guarded tools) | Model for the event bus and the brain's I/O logging. |

`specbot` is a dependency (`specbot @ git+https://github.com/alandhay/spec-agent`), not a copy.

### 3.2 What has to be added (in a thin `hobbit/rig.py` wrapper, upstreamed later where it fits)

| Gap | Approach |
|---|---|
| **48K machine** | `LaunchConfig(path="fuse-sdl", args=["--machine", "48", …])`. |
| **Fast, repeatable start** | Loading from tape takes minutes. One-off setup: load the user's tape in Fuse and save a snapshot just after it loads (`hobbit-start.szx`). Each attempt calls `load_file(snapshot)`, which restarts Fuse and gives a clean, identical start. |
| **Pause while thinking** | `SIGSTOP`/`SIGCONT` on the Fuse process (`X11Emulator.pid` is already exposed). This freezes emulation, picture and sound exactly. On Windows the equivalent is `NtSuspendProcess`. See the clock modes in §5.4. |
| **Display name for ffmpeg** | Expose `X11Emulator._name` (e.g. `:101`) so the broadcaster can `x11grab` the emulator at 50 fps without going through Python. |
| **Audio fan-out to ffmpeg** | `AudioTap.listen()` → a thread that writes chunks into a named pipe, with silence written when the queue is empty so ffmpeg's clock never stalls (also while the emulator is paused). |
| **Settle detection** | See §4.2. |
| **Game font** | See §4.1. |
| **Input verification** | Type the command, read the input line back with OCR, fix any dropped characters (DELETE + retype), *then* press ENTER. Raise `hold_frames`/`gap_frames` if keys are dropped a lot. |

### 3.3 Rig interface (what the rest of the system sees)

```python
class Rig:
    def new_attempt(self) -> Frame            # restart from the snapshot; returns the first settled frame
    def observe(self) -> Frame                # wait until settled, capture, read the text
    def send(self, command: str) -> Frame     # type, check the echo, ENTER, then observe()
    def wait(self, seconds: float) -> Frame   # let game time pass without typing
    def pause(self) / resume(self)            # clock control
    video_display: str                        # ":101", for ffmpeg
    audio_fifo: Path                          # PCM, for ffmpeg

@dataclass
class Frame:
    seq: int; t: float
    png: bytes                                # 320×240, border included, 1 Spectrum px = 1 image px
    text: list[str]                           # 24 OCR rows, '?' for cells it can't read
    new_text: list[str]                       # lines that appeared since the previous frame (scroll-aware)
    picture_hash: str                         # perceptual hash of the graphics region
```

---

## 4. Screen understanding

The vision model always gets the screenshot. The deterministic reader runs alongside it because it gives **exact** text for logs, diffs, the KB and map keys, and the leak audit, and that is cheaper and more reliable than asking the model to transcribe.

### 4.1 Glyph book (exact text from a custom font)

The game may well draw text with its own font rather than the ROM one, so `ocr.read_screen_text` is generalised into a **learned glyph table**:

1. Start from the ROM font (the `ocr.load_font` mapping).
2. Collect any 8×8 cell (two colours, normalised so ink = 1) that matches nothing in the table.
3. Group identical unknown bitmaps. When a new group appears, crop an example line around it and ask the model once: "which character is the highlighted cell?" Save the answer to `data/glyphs.json`, flagged `auto`, so a human can check it in the review UI.
4. After a few sessions the table is complete and OCR is exact and free.

It's also worth checking whether text is drawn on the 8×8 cell grid at all. If it isn't (proportional text, say), the reader falls back to model transcription, and the design still holds.

### 4.2 Settle detection: when to look

The game draws its location pictures progressively, text scrolls, and a cursor may blink. The rig treats the screen as **settled** when `changed_fraction` stays below 0.3% for 600 ms, ignoring the single cell where the cursor blinks (found automatically as the cell that toggles at a steady rate). There is also a hard cap, 20 s by default, so a scene that keeps animating can't stall the loop.

### 4.3 Regions and text diff

- The screen is split into a **graphics region** and a **text region**, learned from where the OCR finds readable rows versus `?` cells. Nothing about the game's layout is hard-coded.
- **New text**: the text region scrolls, so the new rows are found by lining the previous text region up against the current one (longest overlap of the last rows) and taking whatever comes after it. That is "what the game just said".
- **Picture hash**: a perceptual hash of the graphics region. Together with the location text it identifies a place for the map.

---

## 5. The brain (agent loop)

### 5.1 Why the harness owns the loop

`specdev` runs an open-ended Agent SDK conversation, which suits a coding task. Playing the game is a fixed rhythm (*look → think → act → check*), and it needs tight control over context size, pacing and logging. So the **harness runs the loop and makes one model call per move**, and every call has to return a structured decision. The model sits behind a `Brain` interface with two implementations:

- `ApiBrain`: the Anthropic API, or Bedrock in the cloud (the instance role, as spec-agent's deploy does). This is the default for unattended and streamed runs.
- `AgentSdkBrain`: uses the installed Claude Code login, like `specdev`, which is handy for local experiments with no API key.

The models are configuration: a strong vision model for moves (e.g. `claude-opus-5-5`) and a cheaper one (e.g. `claude-haiku-5-5`) for glyph labelling, summaries and the map parser.

### 5.2 One move

```
1. frame = rig.observe()                       # settled screenshot + OCR + new text
2. rig.pause()                                 # if clock = "paused"
3. ctx = build_context(frame)                  # §5.3
4. decision = brain.decide(ctx)                # streamed to the dashboard as it arrives
5. rig.resume()
6. after = rig.send(decision.command)          # or rig.wait(...) when the action is "wait"
7. check = compare(decision.expected, after)   # was the prediction right? → "surprise" score
8. bus.emit(move, frame, decision, after, check)
9. every N moves: memory.compact(); map.update()
```

### 5.3 Context for each call (kept small and stable)

| Part | Contents | Size |
|---|---|---|
| System prompt | Role, rules (screen and own KB only, cite evidence, label guesses), output schema, the action vocabulary ("type a line", "wait N s"). **No game hints.** | fixed, cached |
| Distilled lessons | The top ~15 KB lessons, ranked by confidence × relevance | ~1 k tokens, cached per session |
| Local knowledge | KB facts and map neighbourhood for the current location (exits tried, results, items seen, dangers) | small |
| Working memory | Goal stack, inventory as the agent believes it to be, open hypotheses, rolling summary of the run so far | ~1 k tokens |
| Recent transcript | The last ~8 moves as text (command → new text), no old images | ~1–2 k tokens |
| Current frame | Screenshot (320×240, upscaled 2× nearest for legibility) + OCR text + new text | 1 image |

Old screenshots are never resent. History is carried in text, which keeps the cost of a move flat however long the run gets.

### 5.4 Clock modes

The game may keep running while nobody types, and the agent can be slow, so time is an explicit setting:

- `paused` (**chosen**): the emulator is frozen from the moment the screen settles until the next command is typed. Game time only moves while a command is being carried out. On screen it reads as "the agent is thinking…" with the emulator frozen.
- `live` (kept as an option, off): the emulator keeps running and the agent has to cope with whatever happened meanwhile. It would show up as a different "league" in the stats.
- `move_interval_seconds = 120` (**chosen**): one move every two minutes. The model call usually takes 10–40 s. For the rest of the interval the dashboard plays the decision out step by step (observation → interpretation → options → choice), with a countdown to the action, so the time is spent explaining rather than sitting idle. Then the command is typed.

### 5.5 Decision schema (also what viewers see)

```json
{
  "observation":    "What I see: the picture shows …; the text says …",
  "interpretation": "What I think it means: I'm in a place I call 'X'; there's an exit east; the last command was not understood",
  "location_guess": "short-name-of-place",
  "evidence":       ["S41:L2", "K12", "E7"],
  "options": [
    {"command": "…", "why": "…", "expected": "…", "risk": "low"},
    {"command": "…", "why": "…", "expected": "…", "risk": "med"}
  ],
  "chosen":         0,
  "command":        "…",
  "expected":       "What I think will happen",
  "mode":           "exploit | explore | test-parser | recover",
  "confidence":     0.6,
  "memory_updates": {"goals": […], "inventory": […], "hypotheses": […]}
}
```

`expected` matters. Comparing it with what actually happened produces a **surprise** score for every move, and the surprises are the raw material for learning (§7).

### 5.6 What it has to discover for itself

- **The parser**: which verbs and phrasings work. The agent keeps a "parser notes" section of the KB, filled from the game's responses (understood or not, did something or didn't).
- **Movement**: which directions exist and how the game confirms a move.
- **Other characters, time, danger, items**: all learned through hypotheses, tests, and lessons.
- **What winning means**: see §6.

---

## 6. Sessions and outcomes

One **session** is one attempt from the snapshot, ending in one of these:

| Outcome | Detection |
|---|---|
| `success` | The outcome judge (a model reading the last frames) concludes that the game says it is completed. Confirmed by a human in review before it counts as a win in the headline stats. |
| `fail:death` | The judge sees that the game is over or that the player died, or the game restarts by itself. |
| `fail:death` (confirmed) | When the agent first reports the game as over, the harness doesn't press its key: it waits and looks again, so a key that restarts the game can't hide the death. Two reports in a row end the attempt. |
| `fail:resigned` | A stuck review (below) judged that nothing sensible is left to try. |
| `fail:stuck` | 50 moves in a row without progress (`max_stale_moves`). |
| `stopped:move_limit`, `stopped:cost_cap` | The operator's limits were reached. These are not game results, and the reflection is told not to learn from them. |
| `aborted` | Operator stopped it, or the rig failed. Not counted as an attempt. |

**Progress and the stuck check.** A move makes progress if it reaches a place, or meets a character or object, for the first time in this attempt. New text alone doesn't count: characters wander about and talk, so a game keeps printing something new while the player waits in one spot (the first attempt spent 45 moves waiting at a locked dungeon door this way). After 5 moves without progress, the agent's context carries a "Progress check" warning with the count. Every 10 such moves (`stuck_review_every`), a separate **stuck review** call steps back: it gets the screenshot, the last 30 moves with the game's answers, the memory and the lessons, lists what has been tried and what hasn't, and either resigns the attempt or sets a plan, which then goes into the agent's context until it makes progress. The dashboard shows the count against the limit and each review in the journal. At 50 the attempt ends as `fail:stuck`, whatever the reviews said.

Each session also records **progress metrics**, which matter more than win/fail early on: new locations found, total locations known, distinct commands the game accepted, items handled, the game's own score if the agent has worked out how to ask for it, and the move number of the furthest milestone reached.

The run controller loops attempts back to back (a "marathon"), and the KB update between attempts runs while the dashboard shows a short "what I learned" interstitial.

---

## 7. Archive, knowledge base and learning

### 7.1 Archive layout (one folder per session, all append-only)

```
sessions/2026-10-18T19-03-11Z_a0042/
  session.json         # config, model ids, KB version used, clock mode, start/end, outcome, metrics
  events.jsonl         # the full event stream (every bus event, in order) — the source of truth
  moves.jsonl          # one line per move: frame seq, command, decision JSON, result text, surprise, latency, tokens
  brain/0001.json …    # exact request/response for each model call (prompt, image ref, raw output, usage)
  frames/000123.png    # every settled frame (≈2 KB each), plus 1 fps between moves
  transcript.txt       # human-readable: game text interleaved with the agent's commands
  reflection.md        # end-of-session lessons, with evidence links
  audit.json           # leak-audit findings
  recording.mp4        # local copy of the broadcast (optional)
```

Locally this goes to `sessions/`. In the cloud it syncs to S3, the same pattern spec-agent uses for programs.

### 7.2 Knowledge base (SQLite, `data/kb.sqlite`, exported to `data/kb.md`)

Two layers, and every row records **where it came from** (session id + move numbers):

- **Facts** (episodic, from observation, high trust): `location L has exit east → M (seen 5×)`, `command "…" → "not understood"`, `item I seen at L`, `character C seen at L at move 30`. These come out of the event log by rule, with no model call.
- **Lessons** (semantic, distilled, can be wrong): `"Doing X before Y led to death 3 of 3 times"`, `"The parser seems to want VERB NOUN; adjectives get ignored"`. Each lesson tracks `support`, `contradict`, `confidence` and `status` (`active | retired | disputed`).

**Reflection pass** (end of each session, one model call): the input is `transcript.txt`, the surprises (moves where `expected` ≠ actual), the outcome, and the current lessons. The output is a JSON patch: add lessons, strengthen or weaken existing ones (each change citing moves), retire lessons that were contradicted, and propose follow-up experiments ("next time, test whether …"). Experiments go into an **experiment queue** that the brain sees as candidate goals in later sessions, which is how the agent learns on purpose rather than by accident.

**Distillation for the screen**: the top N active lessons by `confidence × support`, rewritten as one-liners with a ✔ / ✖ / ? marker. These are shown on stream (§8) and fed into §5.3.

**Human review feedback (optional, off by default)**: reviewers can mark moves in the replay UI ("bad interpretation", "good idea"). Those notes are kept with `provenance = human` and are only fed to the agent if `allow_human_hints = true`, so purely self-taught runs stay pure, and the stats show which league a run belongs to.

### 7.3 Memory inside a session

- A **rolling summary**: every 15 moves the cheap model squeezes older transcript into a paragraph.
- **Working memory** (goals, inventory as believed, hypotheses) is kept up to date from `memory_updates` in each decision, and it can be corrected when the screen contradicts it (for example, the game's own inventory listing wins).

---

## 8. On-screen presentation (the broadcast layout)

1920×1080 at 50 fps (it matches the Spectrum's 50 Hz; YouTube and Twitch both accept 50 fps). 1280×720 at 50 is the low-cost preset.

```
┌──────────────────────────────────────────────┬─────────────────────────────────────────────┐
│                                              │  AGENT · move 137 · thinking ▮▮▮▯ 4.2s      │
│                                              │  SEE    The picture shows … text: "…"       │
│        EMULATOR  (320×240 → 960×720,         │  THINK  I'm probably at 'gate-2'; the last  │
│        nearest-neighbour, 50 fps)            │         command wasn't understood …         │
│                                              │  OPTIONS  ① … (low risk) ② … ③ …          │
│                                              │  ▶ TYPE  "…"     expect: "…"   conf 0.62    │
│                                              │  RESULT  ✔ as expected / ✖ surprise          │
├──────────────────────────────────────────────┼─────────────────────────────────────────────┤
│ STATS  attempt #42 · moves 137 · best 311    │  WORLD MAP (live)                           │
│ wins 0 · deaths 31 · stuck 10                │     [ ]──[ ]──[◉]                           │
│ locations 23 known (+2 this run)             │      │          ╎ (untested)                │
│ lessons 57 active · leaks 0 · clock paused   │     [ ]   [☠]   [?]                         │
├──────────────────────────────────────────────┴─────────────────────────────────────────────┤
│ LEARNED SO FAR  ✔ … · ✖ … · ? testing: … · ✔ …                     (rotates every 10 s)    │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

The **dashboard page** (`hobbit/static/dashboard.html`) is the single implementation of everything except the emulator picture. It subscribes to the event bus over SSE. The same page works:

- locally in a browser (with the emulator shown through an MJPEG/PNG stream, as specdev's `/api/screen.stream` does);
- in the broadcast, rendered by headless Chromium on its own Xvfb display, with a transparent hole where ffmpeg places the emulator (§9);
- as the **replay viewer** for a finished session: `replay.html?session=…` reads `events.jsonl` and plays it back through the same components, with a scrubber, per-move frames, the decision JSON and annotation buttons.

---

## 9. Live streaming

```
ffmpeg \
  -f x11grab -framerate 50 -video_size 320x240   -i :101        # emulator
  -f x11grab -framerate 50 -video_size 1920x1080 -i :102        # dashboard (Chromium kiosk)
  -f s16le -ar 48000 -ac 2 -i /run/hobbit/audio.fifo            # emulator sound
  -filter_complex "[0]scale=960:720:flags=neighbor[e];[1][e]overlay=40:40[v]"
  -map "[v]" -map 2 -c:v libx264 -preset veryfast -b:v 6000k -g 100 -pix_fmt yuv420p
  -c:a aac -b:a 128k
  -f tee "[f=flv]rtmps://live.twitch.tv/app/KEY|[f=flv]rtmp://a.rtmp.youtube.com/live2/KEY|[f=mp4]sessions/…/recording.mp4"
```

- **Why compose in ffmpeg rather than the browser**: the emulator stays a pixel-perfect 50 fps and never has to go through Python or the browser, while the dashboard is free to be an ordinary web page.
- **Platforms (chosen): YouTube + Twitch** through ffmpeg's `tee` muxer, as above, with `onfail=ignore` on each output so one platform dropping doesn't stop the other. If a third platform is added, or cloud upload cost matters (§15), switch to sending one stream to a relay that fans out.
- **Bitrate**: with the game frozen, the picture is static most of the time and compresses very well. 720p50 at about 3 Mbps per platform is the default; 1080p50 at 6 Mbps is optional.
- **Stream keys** come from environment variables or a secrets manager and are never logged or archived.
- **Sound while paused**: the audio FIFO writer inserts silence, so A/V sync holds.
- **Between attempts** the dashboard shows the "what I learned" interstitial while the rig reloads the snapshot (a few seconds).
- **Local without Docker**: OBS works as an alternative broadcaster (window capture of Fuse plus a browser source pointed at the dashboard) for anyone who prefers it.

---

## 10. World map

### 10.1 Model (`data/map.json`, rebuilt from KB facts, persistent across sessions)

- **Node** = a location. Its identity is `normalise(location text) + picture_hash`, with a model-assisted merge when two nodes look like the same place reached by different routes. Fields: agent-chosen short name, description, thumbnail (best frame), visit count, first-seen session, items/characters seen, death count.
- **Edge** = `(from, command) → to`, with a status: `confirmed` (seen n times), `blocked` (the game refused), `one-way` (the reverse isn't confirmed), `mentioned` (an exit named in the text but never tried), `inconsistent` (went to different places at different times).
- Directions that aren't compass moves (up/down/in/out, or any other command that changed location) are edges like any other, labelled with the command.

### 10.2 Layout

1. **Compass-constrained placement**: BFS from the start node, placing each neighbour one grid step in the direction of its edge (N up, E right, NE diagonal; up/down shown as small offsets and badges).
2. **Conflicts** (two nodes on one cell, or a loop that doesn't close; adventure maps often aren't geometric): those nodes get force-directed relaxation, and the edge is drawn curved and flagged `non-euclidean`.
3. The layout is **stable**: existing nodes are pinned and only new ones are placed, so the map doesn't jump around on stream.

Rendering uses Cytoscape.js (or D3) in the dashboard: the current location pulses, the planned move is a ghost edge, untested exits are dashed, deaths get ☠, and new nodes glow for this session. Exports to SVG/PNG go into each session folder.

### 10.3 Uses beyond showing it

- **Navigation**: the brain can say `goto <node>`, and the harness expands it into the shortest confirmed path. Each step still goes through observe/verify, and the route is abandoned on the first surprise.
- **Exploration targets**: `mentioned` edges and unvisited neighbours feed the "explore" options.

---

## 11. Repository layout

```
hobbit-agent/
  DESIGN.md
  pyproject.toml            # depends on specbot (git), anthropic, pillow, imagehash, fastapi/uvicorn (or stdlib http like specdev)
  hobbit.toml.example       # models, clock mode, limits, stream targets (keys via env), paths
  hobbit/
    rig.py                  # §3  X11Emulator wrapper: snapshot, pause, settle, echo-check, audio fifo
    screen.py               # §4  glyph book, regions, scroll-aware diff, picture hash
    glyphs.py
    brain.py                # §5  Brain interface, ApiBrain, AgentSdkBrain, prompts, decision schema
    memory.py               # §7.3 working memory, rolling summary
    session.py              # §6  move loop, outcome judge, marathon controller
    kb.py                   # §7.2 SQLite facts/lessons, reflection, distillation, experiments
    worldmap.py             # §10 graph, merging, layout, path finding
    audit.py                # §1  leak audit
    events.py               # bus: in-memory pub/sub + events.jsonl writer
    archive.py              # §7.1 session folders, S3 sync
    server.py               # dashboard API + SSE + replay endpoints
    broadcast.py            # §9  Chromium kiosk + ffmpeg supervisor
    static/dashboard.html  replay.html  map.js
    prompts/system.md  reflect.md  judge.md  audit.md
  data/                     # kb.sqlite, map.json, glyphs.json, hobbit-start.szx (git-ignored: the user's own copy)
  sessions/                 # archive (git-ignored)
  docker/Dockerfile         # debian + fuse-emulator-sdl + xvfb + spectrum-roms + chromium + ffmpeg + python
  deploy/                   # AWS stack, adapted from spec-agent/deploy
  tests/                    # rig/screen/kb/map logic with injected fakes (specbot's style); recorded frames as fixtures
```

---

## 12. Running it

### 12.1 Local (built, 2026-10-18): native Windows

This machine has Fuse 1.6.0 and a working specbot, but no Docker or WSL distribution. So the local rig uses specbot's **Win32 window backend**, which is already proven here; the Linux/X11 container is the cloud form (§12.2).

| Piece | Local form |
|---|---|
| Emulator | `C:/Program Files (x86)/Fuse/fuse.exe --machine 48`, restarted with `data/game/hobbit.tzx` for each attempt. With tape traps the game loads in ~3 s, so no snapshot is needed. |
| Screen | `PrintWindow` capture, so the window may be covered. |
| Keys | `SendInput`. Fuse comes to the front for the second or two it takes to type a command, once every 2 minutes. |
| Freeze | `NtSuspendProcess` / `NtResumeProcess` on the Fuse process. The dashboard shows the last frame while the game is frozen, because a suspended window can't be captured. |
| Text | `hobbit/textreader.py`: the game's font has a fixed 6-pixel pitch (the input line uses the 8-pixel ROM font). Rows are cut into cells and matched against a glyph book that learns from the agent's own transcriptions. One move taught it the whole opening screen. |
| Model | Claude Agent SDK on the Claude Code login: no tools, no connectors (`strict_mcp_config`), no settings or CLAUDE.md, structured JSON output, summarised thinking streamed to the dashboard. With the connectors off, a move's prompt is ~3k tokens; with them on it was ~110k. |
| Dashboard | `http://127.0.0.1:8800`: stdlib server, long-polling events. |

```
.venv\Scripts\python -m hobbit check                                # load the game, one model call, no keys
.venv\Scripts\python -m hobbit run                                  # attempts back to back, one move every 2 minutes
.venv\Scripts\python -m hobbit run --attempts 1 --max-moves 10 --interval 30   # a short trial
```

**Cost locally:** the calls go through the Claude Code login, so on a subscription they count against the plan's usage limits rather than being billed per token. The dashboard's "spent" figure is the notional API price the SDK reports. For long unattended or 24/7 runs, use an API key (the `ApiBrain` in §12.2) so plan limits don't stop the run.

**Not built locally yet:** streaming (§9). On Windows the simplest route is OBS: a window capture of Fuse plus a browser source pointed at the dashboard, sending to YouTube and Twitch. The ffmpeg pipeline in §9 is the cloud form.

### 12.2 Cloud: cost-efficient architecture

At two minutes a move, the machine does very little: it is idle almost all the time, the picture is static between moves, and the only steady load is the video encoder. So there is no need for AWS's per-hour compute or its expensive bandwidth.

```
 one small ARM VM (Hetzner CAX21 class: 4 vCPU, 8 GB, 20 TB traffic included)
 └─ docker compose
    ├─ hobbit   : Xvfb :101 + fuse-sdl --machine 48 (specbot X11Emulator), agent loop, dashboard server
    ├─ kiosk    : Chromium on Xvfb :102 showing the dashboard (1280×720)
    └─ encoder  : ffmpeg  x11grab :101 + :102 + emulator PCM → 720p30 H.264 ~2.5 Mbps
                  ─tee→ rtmp://a.rtmp.youtube.com/live2/KEY
                  ─tee→ rtmp://live.twitch.tv/app/KEY
 model calls  → Anthropic API (ApiBrain: Messages API, prompt caching, structured output)
 archive      → nightly sync of sessions/ and data/ to Cloudflare R2 (no egress fees; 10 GB free)
 recordings   → none kept locally: YouTube's VOD is the video archive
```

Design choices that keep the cost down:

- **A provider with bandwidth included.** Two 2.5 Mbps streams running 24/7 come to ~1.6 TB a month: about $145 of AWS egress, but well inside a 20 TB allowance.
- **ARM is fine.** Debian packages `fuse-emulator-sdl`, `xvfb`, `ffmpeg` and `chromium` for arm64.
- **720p30, not 1080p50.** The game is frozen 98% of the time, so 50 fps buys nothing on screen and costs CPU. x264 `veryfast` at 720p30 uses about one core. Keyframes every 2 s (`-g 60`) suit both YouTube and Twitch.
- **No AWS Bedrock.** Opus 5.5 wasn't enabled on Bedrock in the spec-agent account; the API is direct and simpler.
- **No idle stop needed.** At ~€8/month, the VM can run around the clock; a systemd timer can start and stop *streaming* for scheduled shows.

Alternatives, if staying on AWS or paying nothing matters more:

| Option | Fits? | Approx. per month |
|---|---|---|
| **Hetzner CAX21** (4 ARM vCPU, 8 GB, 20 TB traffic) — recommended | yes, with headroom | **~€8** |
| AWS Lightsail 4 GB / 2 vCPU (4 TB transfer included) | yes, at 720p30 | ~$24 |
| Oracle Cloud Always Free (Ampere A1, 4 OCPU / 24 GB, 10 TB egress) | yes, but free capacity is often unavailable and idle VMs can be reclaimed | $0 |
| AWS EC2 on-demand, as first designed | yes | ~$300 (VM + egress) |

Prices are approximate; check them with the provider before committing.

---

## 13. Build plan

| Milestone | Delivers | Done when |
|---|---|---|
| **M0 Rig** | Docker image, snapshot start, `Rig.observe/send`, settle detection, echo check, pause/resume | A script types 20 commands with no dropped keys and saves settled frames |
| **M1 Reader** | Glyph book, regions, scroll-aware diff, picture hash | OCR matches hand-checked text on 50 recorded frames; diff gives exactly the new lines |
| **M2 Brain loop** | `ApiBrain`, prompts, decision schema, events, archive, outcome judge | Full attempts run unattended and leave complete session folders |
| **M3 Dashboard + replay** | Live page over SSE, replay viewer, stats | A human can follow a live run and review a past one move by move |
| **M4 Learning** | KB facts/lessons, reflection, experiment queue, leak audit, distilled lessons panel | Progress metrics go up across 20 attempts (more locations, fewer repeated deaths) |
| **M5 Map** | Graph from facts, merging, stable layout, `goto` navigation | The map matches the transcript for a 100-move run |
| **M6 Broadcast** | Chromium kiosk + ffmpeg composite + audio + tee/relay | A one-hour unlisted YouTube/Twitch test with steady A/V sync |
| **M7 Cloud** | Deploy stack, S3 sync, scheduled marathons | A scheduled stream starts, runs and archives with nobody watching |

Each milestone can be tested on its own. Unit tests use injected fakes and recorded frames, so no emulator is needed, which is how specbot is tested.

---

## 14. Risks and open questions

| Risk / question | Mitigation or decision needed |
|---|---|
| The model's prior knowledge of the game | §1: no tools, evidence citations, leak audit, leak count shown publicly. Can't be fully removed; it is measured. |
| Game font not on the cell grid | §4.1 falls back to model transcription. Check early in M1. |
| Real-time game vs model latency | Clock modes (§5.4). **Decision: which mode does the public stream default to?** |
| Locations that look alike (the same text in different places) | Picture hash + graph context in node identity, plus a merge/split step in review. |
| Cost per attempt | Flat per-move context (no old images), cached system prompt and lessons, cheap model for side jobs, move/token budgets per session. |
| Copyright | The user supplies their own tape. The snapshot and ROM stay out of git and out of public archives. Check each platform's rules about streaming retro games. |
| Human hints contaminating "self-taught" | `allow_human_hints` off by default; provenance on every KB row; leagues in the stats. |

**Decisions (2026-10-18):**

1. Clock: **`paused`**. The game is frozen while the agent thinks.
2. Human review notes: **an option**, `allow_human_hints = false` by default. Runs that use hints count in a separate league.
3. Platforms: **YouTube + Twitch** through `tee`.
4. Pace: **one move every two minutes** (`move_interval_seconds = 120`, changed from 60 on 2026-10-18). Cost assessment in §15.

---

## 15. Cost assessment (30-second pace, measured 2026-10-18)

Model prices are Anthropic API list prices as of October 2026. Infrastructure prices are approximate; check them with the provider before committing.

### 15.1 Model cost per move (Claude Opus 5.5: $4 input / $20 output per million tokens, cache reads $0.20)

**Measured over a 109-move attempt (82 calls archived, 2026-10-18), local run on the Claude Code login:** $5.96 in total, **≈ $0.073 a move**. The figure is the SDK's own `total_cost_usd`, priced at API rates; on the Claude Code login it is notional (it counts against plan usage, not a bill). It rebuilds from the token counts as follows:

| Part of the call | Tokens per move | Rate | Cost per move |
|---|---|---|---|
| Fresh context (memory, places, characters, recent moves, screen text, image), written to cache | ~5,400 | 1-hour cache write, $8/M | $0.043 |
| System prompt and schema, read from cache | ~3,000 | $0.20/M | $0.0006 |
| Output: decision JSON (~600) + thinking at `medium` effort (~800) | ~1,430 | $20/M | $0.029 |
| **Per move, local (measured)** | | | **≈ $0.073** |

Two things pushed this above the first estimate ($0.025–0.035):

- **Cache writes.** Claude Code writes each prompt to its 1-hour cache, at twice the input price. The per-move context changes every move, so almost none of that write is read back. (Only inferred from the totals: $8/M is the rate that makes them add up.)
- **Bigger answers.** The memory fields (characters, objects and facts) added output. The prompt now asks for shorter fields: 2–3 options instead of 2–4, notes of at most about 60 words, and only the new text in `screen_text`. That should take roughly 15–20% off the output.

**On the API (cloud), with the direct SDK**, the fresh context goes in as ordinary input ($4/M), with a cache breakpoint only on the stable prefix:

| Part of the call | Tokens per move | Rate | Cost per move |
|---|---|---|---|
| Fresh context | ~5,400 | $4/M | $0.022 |
| Cached prefix | ~3,000 | $0.20/M | $0.0006 |
| Output (after the trims above) | ~1,200 | $20/M | $0.024 |
| **Per move, cloud (planning figure)** | | | **≈ $0.05** (range $0.04–0.07) |

### 15.2 Running totals at the measured pace (~40 moves an hour)

The interval is 30 s from the start of one move to the next action, but thinking (~40 s), typing and waiting for the screen to settle push the measured rate to about **40 moves an hour**.

| | Per hour | Per day (24 h) | Per month (24/7) | Per month (4 h a day) |
|---|---|---|---|---|
| Moves, local (notional, 40/h × $0.073) | ~$2.90 | ~$70 | ~$2,100 | ~$350 |
| Moves, cloud API (40/h × $0.05) | ~$2.00 | ~$48 | ~$1,440 | ~$240 |
| Reflections and stuck reviews (~$0.10 each; a few an hour at most) | ~$0.10 | ~$2.50 | ~$75 | ~$12 |
| **Model total, cloud** | **~$2.10** | **~$50** | **~$1,515** | **~$252** |

### 15.3 Scenarios

| Scenario | Model | Infrastructure | **Per month** |
|---|---|---|---|
| **Local, this PC, Claude Code login** (as built) | counts against plan usage limits; no per-token bill | electricity, ~5 Mbps upload when streaming | **~$0 extra** (if the plan's limits allow it) |
| Local, this PC, API key, 4 h a day | ~$252 | — | **~$252** |
| **Cloud, Hetzner-class VM (§12.2), 4 h of play a day** | ~$252 | ~$9 | **~$260** |
| **Cloud, Hetzner-class VM, 24/7** | ~$1,515 | ~$9 | **~$1,525** |
| Cloud, Hetzner-class VM, 24/7, Sonnet 5.5 for moves ($2/$10) | ~$760 | ~$9 | **~$770** |
| Cloud, 24/7, back at one move every 2 minutes (~25/h) | ~$950 | ~$9 | **~$960** |

**The model is practically the whole cost**, so the levers that matter are pace, effort, model choice, and how many hours a day it plays. The next lever after the output trims is effort: `low` would cut most of the ~800 thinking tokens a move (about a third of the bill), at some cost to the quality of play.

### 15.4 Cost controls built in

- **Daily cost cap** (`daily_cost_cap_usd`, default $25): the run stops when today's spend reaches it.
- **Stuck limit**: an attempt that goes 50 moves without progress ends as `fail:stuck`, with a review every 10 such moves that can resign it earlier (§6), so a loop can't burn money indefinitely.
- **Effort**: `medium` by default. The next step is raising it only after a surprise, a death or getting stuck.
- **Free moves**: following a known route on the map takes no model call (§10.3; not built yet).
- **Flat context**: no old images and a fixed window of recent moves, so a move's cost doesn't grow with the length of the attempt.
- **Spend on screen**: "spent today" is one of the dashboard stats.
