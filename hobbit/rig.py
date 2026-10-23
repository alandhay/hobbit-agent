"""The emulator rig: Fuse in 48K mode driven through specbot.

Locally this is specbot's Win32 window backend: screenshots with PrintWindow (the
window may be covered) and key presses posted to Fuse's window as messages, so
Fuse never needs the keyboard focus and can stay behind other windows. Between
moves the Fuse process is suspended, so the game is frozen while the agent thinks.

A suspended window cannot paint, and PrintWindow waits for it to, so nothing here
captures while the emulator is paused: the last frame is kept instead.
"""

from __future__ import annotations

import atexit
import ctypes
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from specbot.capture import changed_fraction
from specbot.config import load_config
from specbot.keys import Chord, text_to_chords
from specbot.window_backend import WindowEmulator

log = logging.getLogger("hobbit")

SETTLE_QUIET_S = 3.0          # the screen must stay unchanged this long (the game can pause before it answers)
SETTLE_THRESHOLD = 0.003      # changed fraction below which the screen counts as still (a blinking cursor is ~0.001)
SETTLE_POLL_S = 0.2
SETTLE_MAX_S = 20.0
PROCESS_SUSPEND_RESUME = 0x0800
ECHO_ATTEMPTS = 3
ENTER_HOLD_FRAMES = 10       # the game only acts on an ENTER held this long (3 frames gives a new line but no action)

# Keys the agent may press on their own (besides typing a line).
SINGLE_KEYS = {"ENTER", "SPACE", "DELETE", "BREAK", *"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}


def _echo_matches(typed: str, shown: str) -> bool:
    """Does the screen row contain what was typed? Unread glyphs ('?') match any letter."""
    typed, shown = typed.upper(), shown.upper()
    n = len(typed)
    return any(all(s == t or (s == "?" and t != " ") for t, s in zip(typed, shown[i:i + n]))
               for i in range(len(shown) - n + 1))


@dataclass
class Frame:
    seq: int
    at: float
    image: Image.Image = field(repr=False)

    def png(self) -> bytes:
        out = io.BytesIO()
        self.image.save(out, "PNG")
        return out.getvalue()


class PostMessageDriver:
    """Key presses posted straight to the emulator's window, so it needs no keyboard focus.

    Fuse's Windows build reads the keyboard from its window messages, so posting
    WM_KEYDOWN / WM_KEYUP works while it sits behind other windows, and nothing the
    person at the computer types can land in it (or the other way round).
    """

    def __init__(self, hwnd: Callable[[], int]) -> None:
        import win32con

        self._hwnd = hwnd
        self._named = {"shift": win32con.VK_SHIFT, "ctrl": win32con.VK_CONTROL, "alt": win32con.VK_MENU,
                       "enter": win32con.VK_RETURN, "space": win32con.VK_SPACE}

    def _post(self, key: str, down: bool) -> None:
        import win32api
        import win32con
        import win32gui

        vk = self._named.get(key) or (ord(key.upper()) if len(key) == 1 and key.isalnum() else None)
        if vk is None:
            raise RigError(f"no Windows key code for {key!r}")
        lparam = 1 | (win32api.MapVirtualKey(vk, 0) << 16)
        if not down:
            lparam |= (1 << 30) | (1 << 31)      # previous state down, transition up
        win32gui.PostMessage(self._hwnd(), win32con.WM_KEYDOWN if down else win32con.WM_KEYUP, vk, lparam)

    def key_down(self, key: str) -> None:
        self._post(key, True)

    def key_up(self, key: str) -> None:
        self._post(key, False)


class BackgroundWindowEmulator(WindowEmulator):
    """specbot's window backend, with keys posted to the window instead of sent with SendInput."""

    def __init__(self, config) -> None:
        self._capture_lock = threading.Lock()
        super().__init__(config, driver=PostMessageDriver(lambda: self.hwnd))

    def _capture_raw(self) -> Image.Image:
        # The dashboard's stream and the move loop both capture; specbot's PrintWindow
        # path is not safe to run on two threads at once (GDI objects get mixed up).
        with self._capture_lock:
            return super()._capture_raw()

    def _press_chord(self, chord: Chord, hold_frames: int) -> None:
        self._player.play(chord, hold_frames)        # no need to bring the window to the front


class RigError(Exception):
    """Something the rig could not do, worded for the agent's log."""


class Rig:
    def __init__(self, tape: Path, specbot_config: Path, *, paused_clock: bool = True) -> None:
        self.tape = tape
        self.paused_clock = paused_clock
        self.emu = BackgroundWindowEmulator(load_config(str(specbot_config)))
        self._lock = threading.RLock()
        self._suspended = False
        self._seq = 0
        self.last: Frame | None = None
        self.stream: ScreenStream | None = None
        atexit.register(self.resume)

    # -- process control -------------------------------------------------

    def _pid(self) -> int:
        import win32process

        return win32process.GetWindowThreadProcessId(self.emu.hwnd)[1]

    def _suspend_call(self, name: str) -> None:
        ntdll = ctypes.WinDLL("ntdll")
        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, self._pid())
        if not handle:
            raise RigError(f"cannot open the emulator process to {name}")
        try:
            status = getattr(ntdll, name)(handle)
            if status != 0:
                raise RigError(f"{name} failed with status {status:#x}")
        finally:
            kernel32.CloseHandle(handle)

    def pause(self) -> None:
        with self._lock:
            if self.paused_clock and not self._suspended:
                self._suspend_call("NtSuspendProcess")
                self._suspended = True

    def resume(self) -> None:
        with self._lock:
            if self._suspended:
                try:
                    self._suspend_call("NtResumeProcess")
                finally:
                    self._suspended = False

    @property
    def paused(self) -> bool:
        return self._suspended

    # -- seeing ----------------------------------------------------------

    def _capture(self) -> Image.Image:
        if self._suspended:
            raise RigError("cannot capture while the emulator is paused")
        return self.emu.screenshot()

    def _settle(self) -> Image.Image:
        """Wait until the screen has stopped changing (or SETTLE_MAX_S passes); return it."""
        start = time.monotonic()
        img = self._capture()
        still_since = time.monotonic()
        while time.monotonic() - start < SETTLE_MAX_S:
            time.sleep(SETTLE_POLL_S)
            now = self._capture()
            if changed_fraction(img, now) > SETTLE_THRESHOLD:
                still_since = time.monotonic()
            img = now
            if time.monotonic() - still_since >= SETTLE_QUIET_S:
                return img
        log.info("screen still changing after %.0f s; carrying on", SETTLE_MAX_S)
        return img

    def observe(self) -> Frame:
        """Wait for the screen to stop changing, then capture it."""
        with self._lock:
            self.resume()
            self._seq += 1
            self.last = Frame(self._seq, time.time(), self._settle())
            return self.last

    def start_stream(self, fps: float = 25.0) -> "ScreenStream":
        self.stream = ScreenStream(self, fps)
        self.stream.start()
        return self.stream

    # -- acting ----------------------------------------------------------

    def new_attempt(self) -> Frame:
        """Restart Fuse with the tape (a clean machine) and return the first settled screen."""
        with self._lock:
            self.resume()
            self.emu.load_file(str(self.tape))
            time.sleep(1.0)
            return self.observe()

    def type_line(self, text: str, bottom_text: Callable[[Image.Image], str] | None = None) -> tuple[Frame, bool]:
        """Type a line, check it shows on screen, press ENTER and wait for the game to answer.

        Keys pressed while the game is busy printing are lost, so with ``bottom_text``
        (which reads the lowest text row) the line is checked before ENTER and typed
        again if it did not all arrive. Returns the frame and whether the echo matched.
        """
        text = " ".join(text.split())
        try:
            chords = text_to_chords(text.lower())
        except ValueError as exc:
            raise RigError(f"cannot type {text!r}: {exc}") from None
        echoed = bottom_text is None
        with self._lock:
            self.resume()
            self._settle()                      # keys pressed while the game is still printing are lost
            for attempt in range(ECHO_ATTEMPTS):
                self.emu.tap_sequence(chords)
                if bottom_text is None:
                    break
                shown = bottom_text(self.observe().image)
                if _echo_matches(text, shown):
                    echoed = True
                    break
                log.info("typed %r but the screen shows %r; typing it again", text, shown)
                for _ in range(len(text) + 4):
                    self.emu.press("DELETE")
                time.sleep(1.0)
            self.emu.press("ENTER", hold_frames=ENTER_HOLD_FRAMES)
            return self.observe(), echoed

    def press_key(self, key: str) -> Frame:
        key = key.strip().upper()
        if key not in SINGLE_KEYS:
            raise RigError(f"{key!r} is not a key that can be pressed on its own; use one of ENTER, SPACE, DELETE, BREAK, A-Z, 0-9")
        with self._lock:
            self.resume()
            self.emu.press(key, hold_frames=ENTER_HOLD_FRAMES)
            return self.observe()

    def wait(self, seconds: float) -> Frame:
        """Let game time pass without pressing anything."""
        with self._lock:
            self.resume()
            time.sleep(max(0.0, min(seconds, 60.0)))
            return self.observe()

    def close(self) -> None:
        self.resume()
        self.emu.close()


class ScreenStream(threading.Thread):
    """Captures the emulator for the dashboard at a steady rate, on its own thread.

    Only changed frames are encoded, blank captures (Fuse restarting, or Windows handing
    back an empty bitmap) are dropped so the page keeps the last good picture, and while
    the game is frozen the frozen frame is shown without touching the window at all.
    """

    def __init__(self, rig: Rig, fps: float) -> None:
        super().__init__(daemon=True, name="hobbit-screen")
        self.rig = rig
        self.period = 1.0 / fps
        self.png: bytes | None = None
        self.version = 0
        self._raw: bytes | None = None
        self._changed = threading.Condition()

    def _publish(self, img: Image.Image) -> None:
        raw = img.tobytes()
        if raw == self._raw:
            return
        out = io.BytesIO()
        img.save(out, "PNG", compress_level=1)
        with self._changed:
            self._raw, self.png = raw, out.getvalue()
            self.version += 1
            self._changed.notify_all()

    def run(self) -> None:
        while True:
            started = time.monotonic()
            img = None
            if self.rig.paused:
                img = self.rig.last.image if self.rig.last else None
            else:
                try:
                    img = self.rig.emu.screenshot()
                except Exception:           # the window is gone for a moment (restart) or minimised
                    img = None
                if img is not None and img.convert("L").getextrema()[1] == 0:
                    img = None              # an all-black capture: keep the last good frame
            if img is not None:
                self._publish(img)
            time.sleep(max(0.0, self.period - (time.monotonic() - started)))

    def wait_newer(self, seen: int, timeout: float) -> tuple[int, bytes | None]:
        with self._changed:
            self._changed.wait_for(lambda: self.version != seen, timeout)
            return self.version, self.png
