#!/usr/bin/env python3
"""Drive an interactive program through a real pty, step by step.

Each step waits for `marker` to appear in output produced AFTER the previous key
was sent (no stale-buffer matches), then sends `keys`. Used to verify the arrow-key UI.

usage: drive_tty.py SCRIPT.json -- cmd args...
  SCRIPT.json: [["marker", "keys"], ...]   keys support \\r, \\x1b[A etc. via JSON escapes
"""
import json
import os
import pty
import re
import select
import sys
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]")


def main() -> int:
    script = json.load(open(sys.argv[1]))
    cmd = sys.argv[sys.argv.index("--") + 1:]
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["COLUMNS"], os.environ["LINES"] = "140", "40"
        os.execvp(cmd[0], cmd)
    buf, since = b"", 0

    def read_for(sec: float) -> None:
        nonlocal buf
        end = time.time() + sec
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk

    for i, (marker, keys) in enumerate(script):
        deadline = time.time() + 20
        while marker.encode() not in ANSI.sub(b"", buf[since:]):
            if time.time() > deadline:
                print(f"STEP {i} TIMEOUT waiting for {marker!r}", file=sys.stderr)
                sys.stdout.write(ANSI.sub(b"", buf[since:]).decode(errors="replace")[-1500:])
                os.kill(pid, 9)
                return 1
            read_for(0.1)
        read_for(0.15)  # let the screen settle
        since = len(buf)
        if keys:
            try:
                os.write(fd, keys.encode())
            except OSError:  # program already exited
                break
    read_for(1.5)
    _, status = os.waitpid(pid, os.WNOHANG) if True else (0, 0)
    sys.stdout.write(ANSI.sub(b"", buf).decode(errors="replace").replace("\r", ""))
    print(f"\n[driver] all {len(script)} steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
