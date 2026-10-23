You are reviewing one attempt by an agent learning a ZX Spectrum game from first principles. Your job is to update the agent's knowledge base so the next attempt does better.

You get:

- the attempt's outcome and length. `fail:death`: the game said the player died or it was over. `fail:stuck`: 50 moves went by without reaching a new place or meeting a new character or object. `fail:resigned`: a review, stepping back from a long run without progress, judged there was nothing sensible left to try. For `stuck` and `resigned`, the loop it was caught in is itself worth a lesson,
- its transcript (each move: what the agent did, what it expected, and what the game showed afterwards),
- the moves where the outcome differed from what the agent expected (surprises),
- the current knowledge base.

Use only what this transcript and the existing knowledge base show. Don't add knowledge about the game from memory: every lesson must point to moves in the transcript.

Produce the complete new knowledge base:

- `lessons`: short, actionable statements, each with:
  - `kind`: `works` (✔), `fails` (✖) or `unsure` (?)
  - `confidence` (0–1)
  - `support` and `contradict`: how many attempts back it up or contradict it
  - `evidence`: `<session>#<move>` references
  - `status`: `active` or `retired`

  Keep existing lessons. Strengthen or weaken them with this attempt's evidence, retire lessons that were clearly contradicted, merge duplicates, and add new ones. Keep it under 60 active lessons, most useful first.
- `experiments`: up to 5 things worth testing deliberately next time, each with why.
- `summary`: two or three sentences on what this attempt taught, for the audience.
