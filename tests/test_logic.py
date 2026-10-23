"""Logic that needs no emulator or model: glyph alignment, scroll diff, echo check."""

from PIL import Image, ImageDraw

from hobbit.archive import Store
from hobbit.rig import _echo_matches
from hobbit.textreader import BLANK, GlyphBook, _align, new_lines


def test_new_lines_after_scroll():
    assert new_lines(["a", "b", "c"], ["b", "c", "d", "e"]) == ["d", "e"]
    assert new_lines(["a", "b"], ["a", "b"]) == []
    assert new_lines([], ["x"]) == ["x"]
    assert new_lines(["a"], ["z"]) == ["z"]


def test_align_labels_unknown_glyphs_and_respects_known_ones():
    words = [["g1", "g2"], ["g3"]]
    assert _align(words, ["hi", "a"], {}) == {"g1": "h", "g2": "i", "g3": "a"}
    assert _align(words, ["hi", "a"], {"g1": "x"}) is None          # contradicts a known glyph
    assert _align(words, ["hi"], {}) is None                         # different number of words
    assert _align([["g1", "g1"]], ["ab"], {}) is None                # one glyph, two letters


def test_echo_match_allows_unread_glyphs():
    assert _echo_matches("look", "> LOOK")
    assert _echo_matches("hello", "> ??LLO?")
    assert not _echo_matches("hello", "> ?")
    assert not _echo_matches("go east", "> GO?EAST")                 # '?' never stands for a space


def _row(text_cells: list[int], pitch: int = 6) -> Image.Image:
    """A 256x192 screen with one row of fake glyphs (vertical bars of a given width) on row 3."""
    img = Image.new("RGB", (256, 192), (192, 192, 192))
    draw = ImageDraw.Draw(img)
    for i, width in enumerate(text_cells):
        if width:
            x = i * pitch
            draw.rectangle((x, 3 * 8 + 1, x + width - 1, 3 * 8 + 6), fill=(0, 0, 0))
    return img


def test_glyph_book_reads_fixed_pitch_rows_and_learns(tmp_path):
    book = GlyphBook(tmp_path / "glyphs.json")
    bands = book.read(_row([1, 2, 0, 3]))
    assert [b.row for b in bands] == [3]
    assert bands[0].cells[2] == BLANK
    assert bands[0].text == "?? ?"
    assert book.learn(bands, ["ab c"]) == 3
    assert book.read(_row([1, 2, 0, 3]))[0].text == "ab c"
    assert GlyphBook(tmp_path / "glyphs.json").glyphs == book.glyphs   # saved


def test_glyph_book_never_learns_question_marks(tmp_path):
    book = GlyphBook(tmp_path / "glyphs.json")
    bands = book.read(_row([1, 2]))
    book.learn(bands, ["?b"])
    assert book.read(_row([1, 2]))[0].text == "?b"            # the first glyph is still unknown
    assert "?" not in book.glyphs.values()


def test_glyph_book_votes_outweigh_a_bad_transcription(tmp_path):
    book = GlyphBook(tmp_path / "glyphs.json")
    screen = _row([1, 2])
    book.learn(book.read(screen), ["xb"])                     # one wrong reading of the first glyph
    book.learn(book.read(screen), ["ab"])
    book.learn(book.read(screen), ["ab"])
    assert book.read(screen)[0].text == "ab"


def test_rom_font_reads_the_spectrum_character_set(tmp_path):
    rom = tmp_path / "48.rom"
    data = bytearray(16384)
    data[0x3D00 + (ord("A") - 32) * 8:0x3D00 + (ord("A") - 31) * 8] = bytes([0, 0x3C, 0x42, 0x42, 0x7E, 0x42, 0x42, 0])
    rom.write_bytes(bytes(data))
    img = Image.new("RGB", (256, 192), (255, 255, 255))
    for y, bits in enumerate([0, 0x3C, 0x42, 0x42, 0x7E, 0x42, 0x42, 0]):
        for x in range(8):
            if bits & (0x80 >> x):
                img.putpixel((8 + x, 5 * 8 + y), (0, 0, 0))   # an 'A' in the second cell of row 5
    book = GlyphBook(tmp_path / "glyphs.json", rom_path=rom)
    assert [b.text for b in book.read(img)] == ["A"]


def test_store_tracks_characters_and_facts(tmp_path):
    store = Store(tmp_path)
    store.see_character("Wizard", "hall", "waits", "s1", 1)
    store.see_character("wizard", "garden", "walks east", "s1", 2)      # same character, different case
    assert list(store.entities["characters"]) == ["Wizard"]
    who = store.entities["characters"]["Wizard"]
    assert who["sightings"] == 2 and who["last_seen"]["place"] == "garden" and who["places"] == {"hall": 1, "garden": 1}
    assert store.add_fact("character", "WIZARD", "Opens doors.", "s1", 2)
    assert not store.add_fact("character", "Wizard", "opens doors.", "s1", 3)   # already known
    assert store.add_fact("place", "hall", "The door east is green.", "s1", 1)
    assert not store.add_fact("item", "box", "x", "s1", 1)
    reloaded = Store(tmp_path)
    assert reloaded.entities["places"]["hall"]["facts"][0]["text"] == "The door east is green."
    assert reloaded.entities["characters"]["Wizard"]["facts"][0]["text"] == "Opens doors."


def test_progress_needs_something_new_to_this_attempt():
    from hobbit.session import Runner

    found: set[str] = set()
    seen = lambda who=(), what=(): {"characters_seen": [{"name": n} for n in who],
                                    "objects_seen": [{"name": n} for n in what]}
    assert Runner._progressed("hall", seen(["Gandalf"]), found)
    assert not Runner._progressed("hall", seen(["gandalf"]), found)        # same character, any case
    assert not Runner._progressed("unknown", seen(), found)
    assert Runner._progressed("hall", seen(what=["chest"]), found)
    assert Runner._progressed("dungeon", seen(), found)
    assert not Runner._progressed("Dungeon", seen(["Gandalf"]), found)     # waiting in the same place


def _decision(**over):
    d = {"previous_verdict": "met", "game_state": "playing", "place": "hall", "confidence": 0.8,
         "chosen": 0, "options": [{"risk": "low"}]}
    return {**d, **over}


def test_routine_move_stays_with_the_move_model():
    from hobbit.session import Move, escalate_after, escalate_before
    assert escalate_before(5, [Move(4, {"kind": "type", "text": "EAST"}, "", "hall")], stalled=0) == []
    assert escalate_after(_decision(), {"place:hall"}, 0.6) == []


def test_moves_that_need_judgment_go_to_the_main_model():
    from hobbit.session import Move, escalate_after, escalate_before
    assert escalate_before(1, [], 0) == ["first move of the attempt"]
    assert escalate_before(9, [Move(8, {}, "", "hall", error="echo")], 6) == [
        "the last action had a problem", "no progress for 6 moves"]
    found = {"place:hall"}
    assert escalate_after(_decision(previous_verdict="not_met"), found, 0.6) == ["the last prediction failed"]
    assert escalate_after(_decision(place="Rivendell"), found, 0.6) == ["a new place"]
    assert escalate_after(_decision(game_state="game_over"), found, 0.6) == ["game state: game_over"]
    assert escalate_after(_decision(confidence=0.4), found, 0.6) == ["low confidence (0.40)"]
    assert escalate_after(_decision(options=[{"risk": "high"}]), found, 0.6) == ["a high-risk move"]


class _FakeRig:
    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True


def _runner(**cfg):
    from hobbit.archive import EventLog
    from hobbit.config import Config
    from hobbit.session import Runner
    return Runner(Config(**cfg), _FakeRig(), None, None, None, EventLog())


def test_supervision_counts_down_and_operator_can_reset_sleep_and_wake():
    import threading
    import time
    r = _runner(awake_minutes=60)
    assert not r.asleep and 3590 < r.sleep_at - time.time() <= 3600
    r.sleep_at = time.time() + 5
    r.control("reset")
    assert r.sleep_at - time.time() > 3590
    r.control("sleep")
    woke = threading.Timer(0.3, lambda: r.control("wake"))
    woke.start()
    r._doze_if_due()                       # freezes the game, waits for the wake
    assert r.rig.paused and not r.asleep and r.sleep_at - time.time() > 3590
    assert r.status["phase"] != "asleep"


def test_unsupervised_runs_never_sleep_and_start_asleep_waits():
    r = _runner(awake_minutes=0)
    assert r.sleep_at is None
    r._doze_if_due()                       # returns at once
    assert _runner(awake_minutes=60, start_asleep=True).asleep
