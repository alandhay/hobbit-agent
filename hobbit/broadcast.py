"""The broadcast (Linux server): the dashboard and the game's sound, sent to YouTube.

Chrome shows the dashboard full-screen on a virtual display of its own; ffmpeg captures
that display, mixes in the emulator's sound, and sends 720p H.264 over RTMP. The
dashboard already contains the live emulator picture, so nothing else needs compositing.

The sound is fed to ffmpeg at a steady real-time rate: what the emulator produced if
there is any, silence otherwise (the game is frozen most of the time, and then the
emulator produces nothing). That keeps sound and picture in step.

Chrome and ffmpeg are restarted if they stop. The stream key is never logged.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("hobbit")

WIDTH, HEIGHT, FPS = 1280, 720, 30
VIDEO_KBPS = 2500
RATE, CHANNELS = 48000, 2                       # what the emulator's sound tap produces (16-bit samples)
TICK_S = 0.02
TICK_BYTES = RATE * CHANNELS * 2 * 20 // 1000   # 20 ms of sound
MAX_BUFFERED = RATE * CHANNELS * 2 // 2         # never fall more than half a second behind
YOUTUBE = "rtmp://a.rtmp.youtube.com/live2/"


def stream_target() -> str | None:
    """Where to send the stream: HOBBIT_STREAM_URL (any RTMP URL or a file, for testing), else YouTube."""
    if os.environ.get("HOBBIT_STREAM_URL"):
        return os.environ["HOBBIT_STREAM_URL"]
    key = os.environ.get("YOUTUBE_STREAM_KEY", "").strip()
    return YOUTUBE + key if key else None


def _chrome() -> str:
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return name
    raise RuntimeError("no Chrome or Chromium found for the broadcast")


class Broadcast:
    def __init__(self, page: str, target: str, audio: Callable[[], object | None], logs: Path) -> None:
        self.page, self.target, self.audio, self.logs = page, target, audio, logs
        self._secret = target.rsplit("/", 1)[-1] if target.startswith("rtmp") else ""
        self._xvfb: subprocess.Popen | None = None
        self._chrome: subprocess.Popen | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self._display = ""
        self._profile = tempfile.mkdtemp(prefix="hobbit-chrome-")
        self._stop = threading.Event()
        self._ffmpeg_lock = threading.Lock()

    # -- processes -------------------------------------------------------

    def start(self) -> None:
        self._display = self._start_display()
        self._start_chrome()
        time.sleep(3.0)                         # let the page draw before the first frames go out
        self._start_ffmpeg()
        threading.Thread(target=self._feed_sound, daemon=True, name="hobbit-sound").start()
        threading.Thread(target=self._watch, daemon=True, name="hobbit-broadcast").start()
        log.info("broadcasting the dashboard from display %s", self._display)

    def _start_display(self) -> str:
        read_fd, write_fd = os.pipe()
        try:
            self._xvfb = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"],
                pass_fds=[write_fd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            os.close(write_fd)
        with os.fdopen(read_fd, "rb") as pipe:
            number = pipe.readline().strip()
        if not number.isdigit():
            raise RuntimeError("Xvfb did not start for the broadcast")
        return f":{number.decode()}"

    def _start_chrome(self) -> None:
        cmd = [_chrome(), "--no-sandbox", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
               "--disable-dev-shm-usage", "--disable-features=Translate", "--hide-scrollbars", "--noerrdialogs",
               "--disable-session-crashed-bubble", "--force-device-scale-factor=1", "--window-position=0,0",
               f"--window-size={WIDTH},{HEIGHT}", f"--user-data-dir={self._profile}", "--kiosk", self.page]
        self._chrome = subprocess.Popen(cmd, env={**os.environ, "DISPLAY": self._display},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _start_ffmpeg(self) -> None:
        out = ["-f", "flv", self.target]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
               "-thread_queue_size", "512", "-f", "x11grab", "-draw_mouse", "0", "-framerate", str(FPS),
               "-video_size", f"{WIDTH}x{HEIGHT}", "-i", f"{self._display}.0",
               "-thread_queue_size", "512", "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
               "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
               "-b:v", f"{VIDEO_KBPS}k", "-maxrate", f"{VIDEO_KBPS}k", "-bufsize", f"{VIDEO_KBPS * 2}k",
               "-g", str(FPS * 2), "-keyint_min", str(FPS * 2),
               "-c:a", "aac", "-b:a", "128k", "-ar", str(RATE), *out]
        with self._ffmpeg_lock:
            self._ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE)
        threading.Thread(target=self._log_ffmpeg, args=(self._ffmpeg,), daemon=True, name="hobbit-ffmpeg-log").start()

    def _log_ffmpeg(self, proc: subprocess.Popen) -> None:
        """ffmpeg's warnings, to logs/ffmpeg.log, with the stream key blanked out."""
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / "ffmpeg.log", "a", encoding="utf-8") as out:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if self._secret:
                    line = line.replace(self._secret, "****")
                out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
                out.flush()

    def _watch(self) -> None:
        """Restart Chrome or ffmpeg if either stops; back off if ffmpeg keeps failing (e.g. a wrong key)."""
        backoff = 5.0
        while not self._stop.wait(5.0):
            if self._chrome and self._chrome.poll() is not None:
                log.warning("broadcast: Chrome stopped (code %s); restarting it", self._chrome.returncode)
                self._start_chrome()
            if self._ffmpeg and self._ffmpeg.poll() is not None:
                log.warning("broadcast: ffmpeg stopped (code %s, see logs/ffmpeg.log); restarting in %.0f s",
                            self._ffmpeg.returncode, backoff)
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 120.0)
                self._start_ffmpeg()
            elif backoff > 5.0 and self._ffmpeg:
                backoff = 5.0

    def _feed_sound(self) -> None:
        listener, tap = None, None
        buffered = bytearray()
        silence = bytes(TICK_BYTES)
        due = time.monotonic()
        while not self._stop.is_set():
            current = self.audio()
            if current is not tap:              # the tap appeared (or changed)
                if tap is not None and listener is not None:
                    tap.leave(listener)
                tap, listener = current, current.listen() if current is not None else None
            if listener is not None:
                try:
                    while True:
                        buffered += listener.get_nowait()
                except queue.Empty:
                    pass
                if len(buffered) > MAX_BUFFERED:
                    del buffered[:len(buffered) - MAX_BUFFERED]
            chunk = bytes(buffered[:TICK_BYTES])
            del buffered[:TICK_BYTES]
            chunk += silence[len(chunk):]
            with self._ffmpeg_lock:
                proc = self._ffmpeg
            try:
                if proc and proc.poll() is None:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass                            # ffmpeg stopped; the watcher restarts it
            due += TICK_S
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:                  # far behind (the machine stalled): start the clock again
                due = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        for proc in (self._ffmpeg, self._chrome, self._xvfb):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(self._profile, ignore_errors=True)
