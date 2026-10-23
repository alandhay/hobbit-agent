"""Exact screen text for a game that draws its own font off the 8x8 character grid.

Each 8-pixel row of the screen is cut into cells at a fixed character pitch (6 pixels for
the game's own font, 8 for the Spectrum ROM font) from an offset where every cell has a
blank last column. When both pitches fit, the one that reads more known glyphs wins.
Each cell is then looked up in the glyph book.

The book has two parts:

- the ROM font, loaded from the 48K ROM: exact, never changed;
- learned glyphs: after each move the agent transcribes the text it can see, and rows
  whose words line up with a transcribed line (same number of words, same lengths) cast
  a vote for each glyph's letter. A glyph reads as the letter with the most votes, so one
  bad transcription is outvoted later instead of sticking for good.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

PAPER_W, PAPER_H, ROWS = 256, 192, 24
PITCHES = (6, 8)
UNKNOWN = "?"
BLANK = ""
CONFIDENT = 3                      # votes after which a learned glyph is treated as certain when pairing lines
_ROM_FONT_OFFSET = 0x3D00          # character set in the 48K BASIC ROM, codes 32-127
_ROM_SPECIAL = {0x60: "£", 0x7F: "©"}


@dataclass
class Band:
    row: int              # 0-23, the 8-pixel row of the screen
    cells: list[str]      # glyph keys, BLANK for an empty cell, from the first glyph to the last
    text: str             # as read with the current book ('?' for unknown glyphs)

    def words(self) -> list[list[str]]:
        out: list[list[str]] = [[]]
        for cell in self.cells:
            if cell == BLANK:
                if out[-1]:
                    out.append([])
            else:
                out[-1].append(cell)
        return [w for w in out if w]


def _paper(img: Image.Image) -> Image.Image:
    w, h = img.size
    img = img.convert("RGB")
    if (w, h) == (PAPER_W, PAPER_H):
        return img
    left, top = (w - PAPER_W) // 2, (h - PAPER_H) // 2
    return img.crop((left, top, left + PAPER_W, top + PAPER_H))


def _key(pitch: int, columns: list[int]) -> str:
    return f"{pitch}:{bytes(columns).hex()}"


def _band_columns(band: Image.Image) -> list[int] | None:
    """Ink columns of one 256x8 strip (bit 0x80 = top row), or None if it isn't two-colour."""
    colours = band.getcolors(3)
    if colours is None or len(colours) != 2:
        return None
    paper = max(colours)[1]
    px = band.load()
    return [sum(0x80 >> y for y in range(8) if px[x, y] != paper) for x in range(PAPER_W)]


def _cuttings(columns: list[int]) -> list[list[str]]:
    """Every pitch at which the row splits cleanly into cells: a list of cell-key lists."""
    found = []
    for pitch in PITCHES:
        for offset in range(pitch):
            if any(columns[:offset]) or any(columns[x] for x in range(offset + pitch - 1, PAPER_W, pitch)):
                continue
            cells = []
            for start in range(offset, PAPER_W - pitch + 1, pitch):
                glyph = columns[start:start + pitch]
                cells.append(_key(pitch, glyph) if any(glyph) else BLANK)
            while cells and cells[-1] == BLANK:
                cells.pop()
            while cells and cells[0] == BLANK:
                cells.pop(0)
            if cells:
                found.append(cells)
            break                       # the first clean offset for this pitch
    return found


def rom_font(rom_path: str | Path) -> dict[str, str]:
    """The ROM character set as glyph keys at pitch 8, read from a Spectrum ROM image."""
    data = Path(rom_path).read_bytes()
    rom = data[-16384:]                 # in multi-ROM images the 48K BASIC ROM comes last
    font = {}
    for code in range(33, 128):         # 32 is space, which reads as a blank cell
        rows = rom[_ROM_FONT_OFFSET + (code - 32) * 8:_ROM_FONT_OFFSET + (code - 31) * 8]
        columns = [sum(0x80 >> y for y in range(8) if rows[y] & (0x80 >> x)) for x in range(8)]
        font.setdefault(_key(8, columns), _ROM_SPECIAL.get(code, chr(code)))
    return font


class GlyphBook:
    def __init__(self, path: Path, rom_path: str | Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.fixed: dict[str, str] = rom_font(rom_path) if rom_path and Path(rom_path).is_file() else {}
        self.votes: dict[str, Counter] = {}
        self.glyphs: dict[str, str] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, tally in data.get("votes", {}).items():
                self.votes[key] = Counter(tally)
            for key, label in data.get("glyphs", {}).items():      # files from before votes were kept
                self.votes.setdefault(key, Counter({label: 1}))
        for key in list(self.votes):
            self.votes[key].pop(UNKNOWN, None)                      # '?' is never a letter
            if key in self.fixed or not self.votes[key]:
                del self.votes[key]
        self._refresh()

    def _refresh(self) -> None:
        self.glyphs = {**{k: v.most_common(1)[0][0] for k, v in self.votes.items()}, **self.fixed}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = {"glyphs": {k: self.glyphs[k] for k in sorted(self.votes)},
                    "votes": {k: dict(self.votes[k]) for k in sorted(self.votes)}}
        self.path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def read(self, img: Image.Image) -> list[Band]:
        """Every text row on the screen, top to bottom."""
        paper = _paper(img)
        bands = []
        for row in range(ROWS):
            columns = _band_columns(paper.crop((0, row * 8, PAPER_W, row * 8 + 8)))
            if columns is None:
                continue
            cuttings = _cuttings(columns)
            if not cuttings:
                continue
            # When the row splits cleanly at both pitches, believe the one we can read more of.
            cells = max(cuttings, key=lambda c: sum(k in self.glyphs for k in c if k != BLANK))
            bands.append(Band(row, cells, self._spell(cells)))
        return bands

    def _spell(self, cells: list[str]) -> str:
        return "".join(" " if c == BLANK else self.glyphs.get(c, UNKNOWN) for c in cells)

    def known_fraction(self, bands: list[Band]) -> float:
        keys = [c for b in bands for c in b.cells if c != BLANK]
        return sum(k in self.glyphs for k in keys) / len(keys) if keys else 1.0

    def learn(self, bands: list[Band], transcript: list[str]) -> int:
        """Vote on glyphs from the agent's transcription of the screen; return how many were new."""
        lines = [t.split() for t in transcript if t.strip()]
        added, next_line = 0, 0
        with self._lock:
            # Glyphs the book is sure of (the ROM font, or several agreeing votes) must match:
            # that stops a line pairing with a different row of the same shape.
            sure = {**{k: v.most_common(1)[0][0] for k, v in self.votes.items() if v.most_common(1)[0][1] >= CONFIDENT},
                    **self.fixed}
            for band in bands:
                for index in range(next_line, len(lines)):      # the transcript runs top to bottom too
                    found = _align(band.words(), lines[index], sure)
                    if found is None:
                        continue
                    next_line = index + 1
                    for key, letter in found.items():
                        if key in self.fixed or letter == UNKNOWN:
                            continue
                        if key not in self.votes:
                            added += 1
                        self.votes.setdefault(key, Counter())[letter] += 1
                    break
            self._refresh()
        for band in bands:
            band.text = self._spell(band.cells)
        self.save()
        return added


def _align(cell_words: list[list[str]], text_words: list[str], fixed: dict[str, str]) -> dict[str, str] | None:
    """Pair each glyph with a letter if the row and the line have the same shape, else None.

    Glyphs in ``fixed`` are certain, so a line that contradicts one of them is not this row.
    """
    if len(cell_words) != len(text_words):
        return None
    labels: dict[str, str] = {}
    for cells, word in zip(cell_words, text_words):
        if len(cells) != len(word):
            return None
        for key, letter in zip(cells, word):
            if fixed.get(key, letter) != letter or labels.setdefault(key, letter) != letter:
                return None
    return labels


def new_lines(previous: list[str], current: list[str]) -> list[str]:
    """Lines in ``current`` that were not on screen before, allowing for the text scrolling up."""
    for overlap in range(min(len(previous), len(current)), 0, -1):
        if previous[-overlap:] == current[:overlap]:
            return current[overlap:]
    return list(current)
