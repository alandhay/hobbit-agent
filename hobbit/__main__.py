"""python -m hobbit run [--attempts N] [--interval SECONDS]   play, with the dashboard on http://127.0.0.1:8800
python -m hobbit check                                     load the game, read the screen, one model call; no keys pressed
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser

from .archive import EventLog, Store
from .brain import Brain
from .config import load_config
from .rig import Rig
from .server import serve
from .session import Runner
from .textreader import GlyphBook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hobbit")
    parser.add_argument("--config", help="path to hobbit.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="play attempts back to back with the dashboard")
    run.add_argument("--attempts", type=int, help="stop after this many attempts (default: keep going)")
    run.add_argument("--interval", type=float, help="seconds per move (default from hobbit.toml)")
    run.add_argument("--max-moves", type=int, help="moves per attempt (default from hobbit.toml)")
    run.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    sub.add_parser("check", help="load the game, read the screen and make one model call, without acting")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if getattr(args, "interval", None):
        cfg.move_interval_seconds = args.interval
    if getattr(args, "max_moves", None):
        cfg.max_moves_per_attempt = args.max_moves
    if not cfg.tape.is_file():
        print(f"hobbit: the game tape was not found at {cfg.tape}", file=sys.stderr)
        return 2

    rig = Rig(cfg.tape, cfg.specbot_config, paused_clock=cfg.clock == "paused")
    book = GlyphBook(cfg.data / "glyphs.json", rom_path=rig.emu.rom_path)     # the ROM font reads the input line exactly
    store = Store(cfg.data)
    brain = Brain(cfg)
    events = EventLog()
    runner = Runner(cfg, rig, brain, book, store, events)

    try:
        if args.command == "check":
            return _check(rig, brain, book, runner)
        rig.start_stream()
        server = serve(cfg.host, cfg.port, events, store, rig, runner)
        url = f"http://{cfg.host}:{cfg.port}/"
        print(f"dashboard: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        try:
            runner.run(args.attempts)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0
    finally:
        rig.close()


def _check(rig: Rig, brain: Brain, book: GlyphBook, runner: Runner) -> int:
    started = time.time()
    frame = rig.new_attempt()
    print(f"game loaded in {time.time() - started:.1f} s; text rows: {[b.text for b in book.read(frame.image)]}")
    rig.pause()
    context = runner._context(0, 1, [b.text for b in book.read(frame.image)], [], "", [])
    decision, record = brain.decide(context, frame.png())
    rig.resume()
    print(f"model call: {record['seconds']} s, ${record['cost_usd']:.4f}, usage {record['usage'].get('input_tokens')}"
          f" in + {record['usage'].get('cache_read_input_tokens')} cached + "
          f"{record['usage'].get('cache_creation_input_tokens')} cache-write, {record['usage'].get('output_tokens')} out")
    print(f"observation: {decision['observation']}\nwould do: {decision['action']}\nexpects: {decision['expected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
