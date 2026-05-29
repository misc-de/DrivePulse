#!/usr/bin/env python3
"""Measure dashcam recorder memory while hammering save_event().

Run this on the target device to reproduce the "save event stresses memory"
report with hard numbers. It drives the real DashcamRecorder (same in-process
GStreamer pipeline the app uses), samples the process RSS at a fixed interval,
periodically fires save_event(), and prints a summary with start/peak/end RSS
and growth.

Examples
--------
    # 60 s run, save an event every 5 s, sample RSS once a second
    python scripts/dashcam_mem_probe.py --duration 60 --save-interval 5

    # Match a specific config
    python scripts/dashcam_mem_probe.py --codec vp9 --resolution 1920x1080 \
        --fps 30 --segment-minutes 1 --duration 120

A steadily climbing "RSS" column across the run (not just transient bumps at
each SAVE) points at a real leak in the pipeline; a flat line that only blips on
SAVE means the copy is the only cost. GST_DEBUG can be layered on for plugin
detail, e.g. ``GST_DEBUG=3 python scripts/dashcam_mem_probe.py ...``.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

# Allow running directly (python scripts/dashcam_mem_probe.py) without an
# editable install by putting the repo root on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from drivepulse_app.dashcam.recorder import DashcamRecorder


def _rss_mb() -> float:
    """Resident set size of this process in MiB (Linux /proc)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MiB
    except OSError:
        pass
    return 0.0


def _dir_size_mb(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in path.iterdir() if f.is_file()) / 1_048_576
    except OSError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", default="/dev/video0")
    ap.add_argument("--codec", default="vp8", choices=["vp8", "vp9", "av1"])
    ap.add_argument("--resolution", default="1280x720")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--segment-minutes", type=int, default=1)
    ap.add_argument("--max-segments", type=int, default=10)
    ap.add_argument("--duration", type=float, default=60.0, help="total run time in seconds")
    ap.add_argument("--interval", type=float, default=1.0, help="RSS sample interval in seconds")
    ap.add_argument("--save-interval", type=float, default=5.0,
                    help="fire save_event() every N seconds (0 disables)")
    ap.add_argument("--rolling-dir", type=Path, default=Path("/tmp/dashcam_probe/tmp"))
    ap.add_argument("--protected-dir", type=Path, default=Path("/tmp/dashcam_probe/saved"))
    args = ap.parse_args()

    # The in-process GStreamer path initialises its pipeline via GLib.idle_add,
    # so a running main loop is required to exercise the same code as the app.
    loop = GLib.MainLoop()
    loop_thread = threading.Thread(target=loop.run, daemon=True, name="glib-mainloop")
    loop_thread.start()

    rec = DashcamRecorder()
    rec.camera = args.camera
    rec.codec = args.codec
    rec.resolution = args.resolution
    rec.fps = args.fps
    rec.segment_minutes = args.segment_minutes
    rec.max_segments = args.max_segments
    rec.rolling_dir = args.rolling_dir
    rec.protected_dir = args.protected_dir

    print(f"# codec={args.codec} res={args.resolution} fps={args.fps} "
          f"seg={args.segment_minutes}min dur={args.duration}s save_every={args.save_interval}s")
    print(f"{'t(s)':>6}  {'RSS(MiB)':>9}  {'roll(MiB)':>9}  {'saved(MiB)':>10}  event")

    rss_start = _rss_mb()
    rss_peak = rss_start
    n_saved = 0
    rec.start()

    t0 = time.monotonic()
    next_save = args.save_interval if args.save_interval > 0 else float("inf")
    try:
        while True:
            now = time.monotonic() - t0
            if now >= args.duration:
                break
            rss = _rss_mb()
            rss_peak = max(rss_peak, rss)
            event = ""
            if now >= next_save:
                paths = rec.save_event()
                n_saved += len(paths)
                event = f"SAVE (+{len(paths)})"
                next_save += args.save_interval
            print(f"{now:6.1f}  {rss:9.1f}  {_dir_size_mb(args.rolling_dir):9.1f}  "
                  f"{_dir_size_mb(args.protected_dir):10.1f}  {event}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n# interrupted")
    finally:
        rec.stop()
        loop.quit()

    rss_end = _rss_mb()
    print("\n# summary")
    print(f"#   RSS start : {rss_start:8.1f} MiB")
    print(f"#   RSS peak  : {rss_peak:8.1f} MiB  (+{rss_peak - rss_start:.1f})")
    print(f"#   RSS end   : {rss_end:8.1f} MiB  (+{rss_end - rss_start:.1f})")
    print(f"#   events    : {n_saved} clip(s) saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
