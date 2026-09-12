"""Copy stdin to stdout AND to a timestamped file. Used by `dev api`.

WHY THIS EXISTS

Three times in one day a number that decided something existed only in a
terminal window and could not be read back: the `sam2_box_union` box positions,
`plate_area_ratio_measured model=0.45`, and the `piece_share=0.7052` that
confirmed the height flip. Each time the answer was "start it with
`> survey-api-log.txt` next time", and each time that depended on somebody
remembering.

So it is not optional any more. `dev api` pipes through this, every run, and
the evidence is on disk whether anyone thought about it or not.

WHY NOT `>` OR `Tee-Object`

  `dev api > file`     one file, clobbered by the next start, and on Windows
                       the handle is held EXCLUSIVELY -- a second server cannot
                       even open it, which is how one attempt failed with
                       "Device or resource busy" rather than a port error.
  PowerShell `>`       writes UTF-16, so the file greps as binary.
  `Tee-Object`         PowerShell only; `dev.bat` runs under cmd.

WHY THE FILENAME IS MADE HERE AND NOT IN dev.bat

cmd's `%DATE%` and `%TIME%` are locale-dependent and `%TIME%` puts a LEADING
SPACE in front of single-digit hours, so `api-2026091 9-3015.txt` is what that
approach produces at nine in the morning. Given a directory, this picks the
name itself.

Never fails the process it is wrapping: if the file cannot be opened, the
stream still reaches the screen and the reason is printed once.
"""
from __future__ import annotations

import datetime
import pathlib
import sys


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    dest = pathlib.Path(args[0]) if args else pathlib.Path(".")
    prefix = args[1] if len(args) > 1 else "api"

    handle = None
    try:
        if dest.suffix:                      # an explicit filename was given
            path = dest
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = dest / f"{prefix}-{stamp}.txt"
        # utf-8 explicitly: the default on Windows is the ANSI codepage, and a
        # log full of structlog output is not ASCII.
        handle = open(path, "w", encoding="utf-8", buffering=1)
        print(f"[tee] logging to {path}", flush=True)
    except OSError as exc:
        print(f"[tee] could NOT open a log file ({exc}); screen only",
              file=sys.stderr, flush=True)

    # Line at a time, flushing both. A buffered tee reproduces the exact bug
    # this replaces: a file that sits at zero bytes for the whole run, where a
    # healthy server and a hung one look identical.
    try:
        for line in sys.stdin:
            sys.stdout.write(line)
            sys.stdout.flush()
            if handle is not None:
                handle.write(line)
    except KeyboardInterrupt:
        # Ctrl-C reaches every process in the pipeline. The server is shutting
        # down and its last lines matter most, so this is not an error path.
        pass
    finally:
        if handle is not None:
            handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
