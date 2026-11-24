#!/usr/bin/env python3
"""Build a fake Claude Code world for recording the README demo. Nothing real is touched.

    python3 tools/demo/make_demo.py /tmp/ccs-demo [--live-pid PID]

Creates <root>/home/.claude/{projects,sessions} and <root>/scratch with made-up sessions,
including one scratch clone that holds 3 unpushed commits. Prints the env to export.
"""
import argparse
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

DAY, HOUR = 86400, 3600

# (title, project leaf, idle seconds, scratch MB, clone: None | ("name", unpushed_commit_msgs))
SESSIONS = [
    ("Node 24 migration", "web-api", 20 * 60, 48, ("web-api", [])),
    ("Fix auth on public routes", "infra", 4 * DAY, 31, ("gateway-config", [
        "Allow anonymous GET on /health and /status",
        "Rate-limit public routes per IP",
        "Fix CORS preflight for /v2",
    ])),
    ("Checkout screen redesign", "android-app", 7 * DAY, 24, None),
    ("Flaky CI on release branch", "web-api", 12 * DAY, 12, ("ci-scripts", [])),
    ("Spike: offline sync with SQLite", "notes-app", 19 * DAY, 9, None),
    ("Rename env vars across services", "infra", 26 * DAY, 3, None),
]

GIT = ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", "-c", "init.defaultBranch=main",
       "-c", "advice.detachedHead=false"]


def git(cwd, *args):
    subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True)


def make_clone(remotes: Path, dest: Path, name: str, unpushed: list[str]) -> None:
    bare = remotes / f"{name}.git"
    git(remotes, "init", "-q", "--bare", str(bare))
    git(dest.parent, "clone", "-q", str(bare), str(dest))
    (dest / "README.md").write_text(f"# {name}\n")
    git(dest, "add", ".")
    git(dest, "commit", "-qm", "Initial commit")
    git(dest, "push", "-q", "origin", "HEAD")
    for i, msg in enumerate(unpushed):
        (dest / f"change{i}.txt").write_text(msg + "\n")
        git(dest, "add", ".")
        git(dest, "commit", "-qm", msg)


def transcript(path: Path, sid: str, title: str, cwd: str, idle: int, n_turns: int) -> None:
    end = time.time() - idle
    start = end - 50 * 60
    ts = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
    recs = [{"type": "user", "sessionId": sid, "cwd": cwd, "gitBranch": "main", "timestamp": ts(start),
             "message": {"role": "user", "content": title.lower()}}]
    for i in range(n_turns):
        t = start + (end - start) * (i + 1) / (n_turns + 1)
        recs.append({"type": "assistant", "sessionId": sid, "requestId": f"req_{sid[:8]}_{i}", "timestamp": ts(t),
                     "message": {"model": "claude-opus-5-5", "role": "assistant",
                                 "content": [{"type": "text", "text": "x" * 4000}],
                                 "usage": {"input_tokens": 1200, "cache_read_input_tokens": 40000,
                                           "output_tokens": 900}}})
    recs.append({"type": "ai-title", "sessionId": sid, "aiTitle": title})
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    os.utime(path, (end, end))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--live-pid", type=int, help="mark the first session live, owned by this running pid")
    a = ap.parse_args()
    root = a.root
    if root.exists():
        if not (root / ".ccs-demo").exists():
            raise SystemExit(f"{root} exists and isn't a demo dir; refusing to delete it")
        shutil.rmtree(root)
    home, scratch, remotes = root / "home", root / "scratch", root / "remotes"
    claude = home / ".claude"
    for d in (claude / "projects", claude / "sessions", scratch, remotes):
        d.mkdir(parents=True)
    (root / ".ccs-demo").touch()

    proj_prefix = "-" + str(home).strip("/").replace("/", "-")
    for n, (title, leaf, idle, mb, clone) in enumerate(SESSIONS):
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ccs-demo/{n}"))
        proj = f"{proj_prefix}-{leaf}"
        cwd = f"{home}/{leaf}"
        (claude / "projects" / proj).mkdir(exist_ok=True)
        transcript(claude / "projects" / proj / f"{sid}.jsonl", sid, title, cwd, idle, 40 + 30 * (len(SESSIONS) - n))

        sdir = scratch / proj / sid / "scratchpad"
        sdir.mkdir(parents=True)
        with open(sdir / "screenshots.bin", "wb") as f:
            f.write(os.urandom(mb * 1024 * 1024))
        if clone:
            make_clone(remotes, sdir / clone[0], clone[0], clone[1])
        for p in (sdir, sdir.parent):
            os.utime(p, (time.time() - idle,) * 2)

        if n == 0 and a.live_pid:
            (claude / "sessions" / f"{a.live_pid}.json").write_text(json.dumps(
                {"pid": a.live_pid, "sessionId": sid, "cwd": cwd, "kind": "interactive", "name": title,
                 "status": "busy"}))

    print(f"export HOME={home} CLAUDE_CONFIG_DIR={claude} CC_SESSIONS_SCRATCH={scratch}")


if __name__ == "__main__":
    main()
