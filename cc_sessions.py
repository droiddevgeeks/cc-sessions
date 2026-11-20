#!/usr/bin/env python3
"""cc-sessions: see and clean up Claude Code sessions — processes, transcripts, scratchpads.

  cc-sessions   (or: ccs)          interactive menu; piped/scripted → plain report
  cc-sessions overview             summary of everything
  cc-sessions ls [--all]           numbered session list with titles
  cc-sessions show <#|sid>         one session: title, tokens, first ask, clones, resume cmd
  cc-sessions pick [--all]         interactive: choose sessions by number, then delete
  cc-sessions ps                   live Claude processes with RAM + session name
  cc-sessions disk [--all]         transcripts + scratchpads (clones flagged if unpushed/dirty)
  cc-sessions clean [opts]         preview deletion; add --yes to actually delete
      --scratch AGE                scratch dirs idle longer than AGE (e.g. 3d, 12h)
      --transcripts AGE            transcripts idle longer than AGE (loses /resume)
      --only SID [--only SID]      restrict to session-id prefixes
      --force                      also delete scratch dirs whose clones have unpushed/dirty work
  cc-sessions kill PID [PID...]    SIGTERM Claude processes (refuses this session)
  cc-sessions log [-n N]           what was deleted, when, and where rescued work was saved

Live sessions are never deleted. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"


def find_scratch_root(env, uid: int, exists=lambda p: p.is_dir()) -> Path:
    """Claude Code keeps per-session scratch in <tmp>/claude-<uid>: /private/tmp on macOS, /tmp on Linux."""
    if env.get("CC_SESSIONS_SCRATCH"):
        return Path(env["CC_SESSIONS_SCRATCH"])
    candidates = [Path(f"/private/tmp/claude-{uid}"), Path(f"/tmp/claude-{uid}")]
    if env.get("TMPDIR"):
        candidates.append(Path(env["TMPDIR"]) / f"claude-{uid}")
    for c in candidates:
        if exists(c):
            return c
    return Path(f"/tmp/claude-{uid}")


SCRATCH_ROOT = find_scratch_root(os.environ, os.getuid())
SKIP_DIRS = {"node_modules", ".git", ".gradle", "build", "dist", ".venv", "venv", "Pods"}
CLONE_DEPTH = 4


# ---------- small helpers ----------

def parse_age(s: str) -> int:
    m = re.fullmatch(r"(\d+)([dh]?)", s.strip())
    if not m:
        raise ValueError(f"bad age {s!r}: use e.g. 3d or 12h")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return n * (86400 if unit == "d" else 3600)


def human(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    for unit in "KMGT":
        n /= 1024
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}"
    raise AssertionError("unreachable")


def ago(ts: float, now: float | None = None) -> str:
    d = max(0, (now or time.time()) - ts)
    if d < 60:
        return "<1m"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    return f"{int(d // 86400)}d"


# ---------- formatting ----------

class Style:
    CODES = {"bold": 1, "dim": 2, "red": 31, "green": 32, "yellow": 33, "blue": 34,
             "magenta": 35, "cyan": 36, "gray": 90}

    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return "\033[" + ";".join(str(self.CODES[s]) for s in styles) + "m" + text + "\033[0m"


S = Style()


def fmt_etime(etime: str) -> str:
    """ps etime ([[dd-]hh:]mm:ss) -> '7d 11h' / '2h 12m' / '9m'."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", etime.strip())
    if not m:
        return etime
    d, h, mi = (int(x or 0) for x in m.groups()[:3])
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {mi}m"
    return f"{mi}m" if mi else "<1m"


def trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return "…" if n <= 1 else s[: n - 1] + "…"


def pretty_project(project: str) -> str:
    home = "-" + str(HOME).strip("/").replace("/", "-")
    if project == home:
        return "~"
    if not project.startswith(home + "-"):
        return project
    rest = project[len(home) + 1:]
    if rest.startswith("-claude-spares"):
        return "(spare)"
    if rest.startswith("Development-"):
        rest = rest[len("Development-"):]
    # first dash separates the category folder (JS/, android/, Backend/...) when it's not the leaf
    head, _, tail = rest.partition("-")
    return f"{head}/{tail}" if tail and head in {"AI", "JS", "android", "Backend", "Plugin", "ReactNative", "flutter"} else rest


def bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    if frac > 0 and filled == 0:
        filled = 1
    return "█" * filled + "·" * (width - filled)


def header(title: str, meta: str) -> str:
    return f"\n{S('▍', 'cyan')}{S(title, 'bold')}  {S(meta, 'gray')}\n"


def pad(text: str, width: int, right: bool = False) -> str:
    """Pad before styling so ANSI codes don't break alignment."""
    return text.rjust(width) if right else text.ljust(width)


def du_bytes(paths: list[Path]) -> dict[Path, int]:
    """Batch `du -sk`; far faster than walking node_modules in Python."""
    out: dict[Path, int] = {p: 0 for p in paths}
    if not paths:
        return out
    r = subprocess.run(["du", "-sk", *map(str, paths)], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        kb, _, p = line.partition("\t")
        if kb.isdigit():
            out[Path(p)] = int(kb) * 1024
    return out


# ---------- processes ----------

@dataclass
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    etime: str
    tty: str
    command: str


def parse_ps(out: str) -> list[Proc]:
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not (parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit()):
            continue
        procs.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4], parts[5]))
    return procs


def list_procs() -> list[Proc]:
    r = subprocess.run(["ps", "-eo", "pid=,ppid=,rss=,etime=,tty=,command="], capture_output=True, text=True)
    return parse_ps(r.stdout)


def classify(p: Proc, meta: dict | None) -> str | None:
    cmd = p.command
    exe = cmd.split()[0] if cmd else ""
    if ".claude/shell-snapshots/" in cmd:
        return "orphan-shell" if p.ppid == 1 else "tool-shell"
    if ".claude/skills/" in cmd:
        return "skill-daemon"
    if not (exe == "claude" or exe.endswith("/claude") or "/claude/versions/" in exe):
        return None
    if " daemon run" in cmd:
        return "daemon"
    if "bg-pty-host" in cmd:
        return "pty-host"
    if meta:
        return "background" if meta.get("kind") == "bg" else meta.get("kind", "interactive")
    if "bg-spare" in cmd:
        return "spare"
    return "interactive"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load_session_meta() -> dict[int, dict]:
    metas: dict[int, dict] = {}
    if not SESSIONS_DIR.is_dir():
        return metas
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            m = json.loads(f.read_text())
            pid = int(m["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid_alive(pid):
            metas[pid] = m
    return metas


def ancestry(procs: list[Proc]) -> set[int]:
    by_pid = {p.pid: p for p in procs}
    chain, pid = set(), os.getpid()
    while pid > 1 and pid not in chain:
        chain.add(pid)
        pid = by_pid[pid].ppid if pid in by_pid else 1
    return chain


# ---------- disk ----------

@dataclass
class Clone:
    path: Path
    size: int
    issues: list[str] = field(default_factory=list)


@dataclass
class ScratchDir:
    path: Path
    project: str
    session_id: str
    last_active: float
    size: int
    clones: list[Clone] = field(default_factory=list)


@dataclass
class Transcript:
    path: Path
    project: str
    session_id: str
    last_active: float
    size: int


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", f"--git-dir={repo / '.git'}", f"--work-tree={repo}", *args],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def clone_issues(repo: Path) -> list[str]:
    """What would be lost by deleting this clone. Empty list = safe."""
    try:
        issues = []
        if _git(repo, "status", "--porcelain").strip():
            issues.append("uncommitted changes")
        if not _git(repo, "remote").strip():
            issues.append("no remote")
            return issues
        n = int(_git(repo, "rev-list", "--count", "--branches", "--not", "--remotes").strip() or 0)
        if n:
            issues.append(f"{n} unpushed commit(s)")
        return issues
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError):
        return ["git error"]


def find_clones(root: Path, depth: int = CLONE_DEPTH) -> list[Path]:
    found = []

    def walk(d: Path, level: int):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        if any(e.name == ".git" for e in entries):
            found.append(d)
            return
        if level >= depth:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS:
                walk(Path(e.path), level + 1)

    walk(root, 0)
    return sorted(found)


def scan_scratch(root: Path = SCRATCH_ROOT, projects: Path = PROJECTS_DIR) -> list[ScratchDir]:
    if not root.is_dir():
        return []
    sessions: list[tuple[Path, str, str]] = []
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        for sdir in sorted(proj.iterdir()):
            if sdir.is_dir():
                sessions.append((sdir, proj.name, sdir.name))
    clones_by_session = {s[0]: find_clones(s[0]) for s in sessions}
    all_clones = [c for cs_ in clones_by_session.values() for c in cs_]
    sizes = du_bytes([s[0] for s in sessions] + all_clones)
    result = []
    for sdir, proj, sid in sessions:
        transcript = projects / proj / f"{sid}.jsonl"
        try:
            last = transcript.stat().st_mtime if transcript.exists() else sdir.stat().st_mtime
        except OSError:
            last = 0.0
        clones = [Clone(c, sizes.get(c, 0), clone_issues(c)) for c in clones_by_session[sdir]]
        result.append(ScratchDir(sdir, proj, sid, last, sizes.get(sdir, 0), clones))
    return result


def scan_transcripts(projects: Path = PROJECTS_DIR) -> list[Transcript]:
    out = []
    if not projects.is_dir():
        return out
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            size = st.st_size
            side = proj / f.stem  # subagent transcripts / tool results live here
            if side.is_dir():
                size += du_bytes([side])[side]
            out.append(Transcript(f, proj.name, f.stem, st.st_mtime, size))
    return out


# ---------- rescue (save unpushed/dirty work before deleting a clone) ----------

LOG_PATH = CLAUDE_DIR / "cc-sessions.log"
RESCUE_DIR = CLAUDE_DIR / "cc-sessions-rescue"


class RescueError(Exception):
    pass


def rescue_clone(repo: Path, out_dir: Path, tag: str) -> list[Path]:
    """Save what deleting `repo` would lose. Returns files written ([] if nothing at risk).

    - commits not on any remote (or repo has no remote) -> full bundle of branches+tags (clone-able on its own)
    - tracked uncommitted changes -> .patch (git diff --binary HEAD)
    - untracked, non-ignored files -> .untracked.tar
    Raises RescueError if the repo can't be read or a save fails: caller must then NOT delete.
    """
    import tarfile
    try:
        has_head = True
        try:
            _git(repo, "rev-parse", "--verify", "-q", "HEAD")
        except RuntimeError:
            has_head = False
        if not has_head and not (repo / ".git" / "HEAD").exists():
            raise RescueError("unreadable repository")
        _git(repo, "status", "--porcelain")  # raises on a broken repo
        remotes = _git(repo, "remote").strip()
        unpushed = has_head and (not remotes or
                                 int(_git(repo, "rev-list", "--count", "--branches", "--not", "--remotes").strip() or 0) > 0)
        patch = _git(repo, "diff", "--binary", "HEAD") if has_head else ""
        untracked = [f for f in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if f]
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as e:
        raise RescueError(str(e) or "git error") from e

    if not (unpushed or patch or untracked):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{tag}-{repo.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    saved: list[Path] = []
    try:
        if unpushed:
            bundle = base.with_suffix(".bundle")
            _git(repo, "bundle", "create", str(bundle), "HEAD", "--branches", "--tags")
            saved.append(bundle)
        if patch:
            p = base.with_suffix(".patch")
            p.write_text(patch)
            saved.append(p)
        if untracked:
            tar = Path(str(base) + ".untracked.tar")
            with tarfile.open(tar, "w") as tf:
                for f in untracked:
                    tf.add(repo / f, arcname=f)
            saved.append(tar)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        raise RescueError(f"saving failed: {e}") from e
    return saved


def clone_preview(repo: Path, limit: int = 10) -> list[str]:
    """What's at risk in a clone, for showing before a delete."""
    try:
        lines = []
        if _git(repo, "remote").strip():
            commits = _git(repo, "log", "--branches", "--not", "--remotes", "--oneline", f"-n{limit}").splitlines()
        else:
            commits = _git(repo, "log", "--branches", "--oneline", f"-n{limit}").splitlines()
        lines += [f"commit  {c}" for c in commits]
        lines += [f"change  {c}" for c in _git(repo, "status", "--short").splitlines()[:limit]]
        return lines or ["(nothing at risk)"]
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return ["(unreadable repository — can't show or save its contents)"]


def risky_confirmed(typed: str, names: list[str]) -> bool:
    tokens = {t for t in re.split(r"[,\s]+", typed.strip()) if t}
    return bool(names) and tokens == set(names)


def _log(log_path: Path, entry: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"  warning: couldn't write delete log {log_path}: {e}", file=sys.stderr)


# ---------- transcript contents ----------

TITLE_WINDOW = 512 * 1024


def _prompt_text(rec: dict) -> str:
    """Text of a real human prompt, '' for meta/tool-result/command noise."""
    if rec.get("type") != "user" or rec.get("isMeta"):
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return ""
    text = " ".join(content.split())
    return "" if not text or text.startswith("<") else text[:200]


def _titles_in(lines) -> tuple[str, str]:
    custom = ai = ""
    for line in lines:
        if "Title" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        custom = rec.get("customTitle") or custom
        ai = rec.get("aiTitle") or ai
    return custom, ai


def read_title(path: Path, window: int = TITLE_WINDOW) -> str:
    """Session title: custom > latest AI title > first real prompt. Reads only the tail when possible."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - window))
            tail = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    if size > window and tail:
        tail = tail[1:]  # first line is likely cut mid-record
    custom, ai = _titles_in(tail)
    if custom or ai:
        return custom or ai
    # title not in tail (or file small): one streaming pass
    first = ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "Title" in line:
                    c, a = _titles_in([line])
                    custom, ai = c or custom, a or ai
                elif not first and '"user"' in line:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        first = _prompt_text(rec)
    except OSError:
        return ""
    return custom or ai or first


@dataclass
class Stats:
    prompts: int = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    first_prompt: str = ""
    cwd: str = ""
    branch: str = ""
    models: list[str] = field(default_factory=list)
    started: str = ""
    ended: str = ""


def session_stats(path: Path) -> Stats:
    st = Stats()
    seen_req: set[str] = set()
    try:
        f = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return st
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = rec.get("timestamp")
            if isinstance(ts, str):
                st.started = min(st.started, ts) if st.started else ts
                st.ended = max(st.ended, ts)
            st.cwd = st.cwd or rec.get("cwd") or ""
            st.branch = st.branch or rec.get("gitBranch") or ""
            if rec.get("type") == "user":
                text = _prompt_text(rec)
                if text:
                    st.prompts += 1
                    st.first_prompt = st.first_prompt or text
            elif rec.get("type") == "assistant":
                msg = rec.get("message") or {}
                model = msg.get("model")
                if model and model not in st.models and not model.startswith("<"):
                    st.models.append(model)
                req = rec.get("requestId")
                usage = msg.get("usage")
                if not req or req in seen_req or not isinstance(usage, dict):
                    continue
                seen_req.add(req)
                st.turns += 1
                st.input_tokens += sum(int(usage.get(k) or 0) for k in
                                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                st.output_tokens += int(usage.get("output_tokens") or 0)
    return st


# ---------- sessions (scratch + transcript joined) ----------

@dataclass
class SessionRow:
    sid: str
    project: str
    title: str
    last_active: float
    live: bool
    scratch: ScratchDir | None = None
    transcript: Transcript | None = None

    @property
    def size(self) -> int:
        return (self.scratch.size if self.scratch else 0) + (self.transcript.size if self.transcript else 0)

    @property
    def risky(self) -> list[str]:
        if not self.scratch:
            return []
        return [f"{c.path.name}: {', '.join(c.issues)}" for c in self.scratch.clones if c.issues]


def merge_sessions(scratch, transcripts, live_ids, titles) -> list[SessionRow]:
    rows: dict[str, SessionRow] = {}
    for t in transcripts:
        rows[t.session_id] = SessionRow(t.session_id, t.project, titles.get(t.session_id, ""), t.last_active,
                                        t.session_id in live_ids, transcript=t)
    for s in scratch:
        r = rows.get(s.session_id)
        if r:
            r.scratch = s
            r.last_active = max(r.last_active, s.last_active)
        else:
            rows[s.session_id] = SessionRow(s.session_id, s.project, titles.get(s.session_id, ""), s.last_active,
                                            s.session_id in live_ids, scratch=s)
    return sorted(rows.values(), key=lambda r: (-r.size, r.sid))


def parse_selection(text: str, n: int) -> list[int]:
    text = text.strip().lower()
    if text == "all":
        return list(range(1, n + 1))
    tokens = [t for t in re.split(r"[,\s]+", text) if t]
    if not tokens:
        raise ValueError("empty selection")
    picked: set[int] = set()
    for tok in tokens:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if not m:
            raise ValueError(f"can't read {tok!r}")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a > b or a < 1 or b > n:
            raise ValueError(f"{tok!r} is out of range 1-{n}")
        picked.update(range(a, b + 1))
    return sorted(picked)


def build_plan(rows: list[SessionRow], picked: list[int], include_risky: bool, everything: bool):
    """1-based picks -> (scratch dirs, transcripts, [(sid, why_excluded)]). Live sessions are never included."""
    scratch, trans, excluded = [], [], []
    for i in picked:
        r = rows[i - 1]
        if r.live:
            excluded.append((r.sid, "live session"))
            continue
        if r.risky and not include_risky:
            excluded.append((r.sid, "; ".join(r.risky)))
            continue
        if r.scratch:
            scratch.append(r.scratch)
        if everything and r.transcript:
            trans.append(r.transcript)
    return scratch, trans, excluded


def fmt_count(n: int) -> str:
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


# ---------- selection (pure) ----------

def pick_scratch(dirs, now, older_than, live_ids, force):
    delete, skipped = [], []
    for d in dirs:
        if d.session_id in live_ids:
            skipped.append((d, "live session"))
        elif now - d.last_active < older_than:
            continue
        else:
            risky = [f"{c.path.name}: {', '.join(c.issues)}" for c in d.clones if c.issues]
            if risky and not force:
                skipped.append((d, "; ".join(risky)))
            else:
                delete.append(d)
    return delete, skipped


def pick_transcripts(ts, now, older_than, live_ids):
    return [t for t in ts if t.session_id not in live_ids and now - t.last_active >= older_than]


def kill_refusal(pid: int, own: set[int], procs_by_pid: dict[int, Proc]) -> str | None:
    if pid in own:
        return "that's this session (or its parent)"
    if pid not in procs_by_pid:
        return "not a Claude process (or already gone)"
    return None


def _match(sid: str, prefixes: list[str] | None) -> bool:
    return not prefixes or any(sid.startswith(p) for p in prefixes)


def _short(project: str) -> str:
    home = "-" + str(HOME).strip("/").replace("/", "-") + "-"
    return project[len(home):] if project.startswith(home) else project


# ---------- commands ----------

STATUS_COLOR = {"busy": "green", "idle": "gray", "shell": "yellow", "waiting": "yellow"}
SESSION_KINDS = {"interactive", "background"}


def _proj_of_cwd(cwd: str) -> str:
    return pretty_project(cwd.replace("/", "-")) if cwd else ""


def cmd_ps(_args) -> None:
    procs = list_procs()
    metas = load_session_meta()
    own = ancestry(procs)
    rows = [(p, classify(p, metas.get(p.pid)), metas.get(p.pid)) for p in procs]
    # hide this tool's own child shells: pure noise
    rows = [r for r in rows if r[1] and not (r[1] == "tool-shell" and r[0].ppid in own)]
    total = sum(p.rss_kb for p, _, _ in rows) * 1024
    children: dict[int, list[int]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p.pid)

    print(header("Live processes", f"{len(rows)} · {human(total)} RAM"))
    sessions = sorted([r for r in rows if r[1] in SESSION_KINDS], key=lambda r: -r[0].rss_kb)
    plumbing = sorted([r for r in rows if r[1] not in SESSION_KINDS], key=lambda r: -r[0].rss_kb)

    if sessions:
        print(S(f"  {'':2}{pad('SESSION', 32)}{pad('STATUS', 8)}{pad('PROJECT', 24)}"
                f"{pad('RAM', 7, True)}  {pad('UP', 8)}{pad('TTY', 9)}PID", "gray"))
    for p, kind, m in sessions:
        m = m or {}
        status = m.get("status", "?")
        dot = S("●", "green") if kind == "interactive" else S("◐", "yellow")
        name = trunc(m.get("name") or "(unnamed)", 30)
        tty = p.tty if p.tty not in ("??", "?") else ""
        note = S("  ◀ you", "cyan", "bold") if p.pid in own else (S("  bg", "yellow") if kind == "background" else "")
        print(f"  {dot} {S(pad(name, 32), 'bold')}{S(pad(status, 8), STATUS_COLOR.get(status, 'gray'))}"
              f"{pad(trunc(_proj_of_cwd(m.get('cwd', '')), 22), 24)}"
              f"{pad(human(p.rss_kb * 1024), 7, True)}  {pad(fmt_etime(p.etime), 8)}{S(pad(tty, 9), 'gray')}"
              f"{S(str(p.pid), 'gray')}{note}")

    if plumbing:
        print(S(f"\n  {'':2}{pad('PLUMBING', 32)}{pad('', 8)}{pad('', 24)}{pad('RAM', 7, True)}  {pad('UP', 8)}{pad('', 9)}PID", "gray"))
    for p, kind, _m in plumbing:
        note = ""
        if kind == "pty-host" and p.ppid == 1:
            kids = ", ".join(map(str, children.get(p.pid, []))) or "nothing"
            note = S(f"  daemon gone · hosts {kids}", "yellow")
        elif kind == "orphan-shell":
            note = S("  leftover · safe to kill", "red")
        elif kind == "skill-daemon":
            m_ = re.search(r"\.claude/skills/([^/]+)/", p.command)
            note = S(f"  skill: {m_.group(1) if m_ else '?'}", "gray")
        elif kind == "spare":
            note = S("  pre-warmed, idle", "gray")
        print(f"  {S('·', 'gray')} {S(pad(kind, 32), 'dim')}{pad('', 8)}{pad('', 24)}"
              f"{pad(human(p.rss_kb * 1024), 7, True)}  {pad(fmt_etime(p.etime), 8)}{pad('', 9)}"
              f"{S(str(p.pid), 'gray')}{note}")


def cmd_disk(args) -> None:
    now = time.time()
    live = {m["sessionId"] for m in load_session_meta().values() if "sessionId" in m}

    ts = scan_transcripts()
    by_proj: dict[str, list[Transcript]] = {}
    for t in ts:
        by_proj.setdefault(t.project, []).append(t)
    projs = sorted(by_proj.items(), key=lambda kv: -sum(t.size for t in kv[1]))
    biggest = max((sum(t.size for t in v) for _, v in projs), default=1) or 1
    home = str(HOME)
    print(header("Transcripts", f"{len(ts)} sessions · {human(sum(t.size for t in ts))} · "
                 f"{str(PROJECTS_DIR).replace(home, '~')}"))
    for proj, items in projs:
        size = sum(t.size for t in items)
        newest = max(t.last_active for t in items)
        print(f"  {pad(human(size), 7, True)}  {S(bar(size / biggest), 'blue')}  "
              f"{pad(str(len(items)), 3, True)} sess  {S(pad(ago(newest, now) + ' ago', 9, True), 'gray')}  "
              f"{trunc(pretty_project(proj), 44)}")

    dirs = sorted(scan_scratch(), key=lambda d: -d.size)
    shown = [d for d in dirs if d.size > 0 or d.clones]
    empty = len(dirs) - len(shown)
    limit = None if args.all else 12
    biggest = max((d.size for d in dirs), default=1) or 1
    print(header("Scratchpads", f"{len(dirs)} sessions · {human(sum(d.size for d in dirs))} · {SCRATCH_ROOT}"))
    for d in shown[:limit]:
        tag = S("  ● live", "green") if d.session_id in live else ""
        title = read_title(PROJECTS_DIR / d.project / f"{d.session_id}.jsonl")
        print(f"  {pad(human(d.size), 7, True)}  {S(bar(d.size / biggest), 'magenta')}  "
              f"{S(pad(ago(d.last_active, now), 4, True), 'gray')} idle  {S(d.session_id[:8], 'gray')}  "
              f"{S(pad(trunc(title or '(no title)', 38), 40), 'bold' if title else 'gray')}"
              f"{S(trunc(pretty_project(d.project), 28), 'gray')}{tag}")
        for i, c in enumerate(d.clones):
            branch = "└─" if i == len(d.clones) - 1 else "├─"
            if c.issues:
                status = S("⚠ " + ", ".join(c.issues), "red", "bold")
            else:
                status = S("✓ pushed", "green")
            rel = str(c.path.relative_to(d.path)).removeprefix("scratchpad/")
            print(f"  {'':7}  {'':12}  {S(branch, 'gray')} {pad(trunc(rel, 18), 18)} {pad(human(c.size), 7, True)}  {status}")
    extra = []
    if limit and len(shown) > limit:
        extra.append(f"{len(shown) - limit} smaller (--all)")
    if empty:
        extra.append(f"{empty} empty")
    if extra:
        print(S(f"  {'':7}  + " + " · ".join(extra), "gray"))
    print(S("\n  → cc-sessions ls · show <#> · pick      browse, inspect, choose & delete", "gray"))


def cmd_clean(args) -> int:
    if args.scratch is None and args.transcripts is None and not args.only:
        print("Nothing selected. Pass --scratch AGE and/or --transcripts AGE (or --only SID).", file=sys.stderr)
        return 2
    now = time.time()
    live = {m["sessionId"] for m in load_session_meta().values() if "sessionId" in m}
    do_scratch = args.scratch is not None or (args.only and args.transcripts is None)
    scratch_age = parse_age(args.scratch) if args.scratch else 0
    plan_scratch, skipped = [], []
    if do_scratch:
        dirs = [d for d in scan_scratch() if _match(d.session_id, args.only)]
        plan_scratch, skipped = pick_scratch(dirs, now, scratch_age, live, args.force)
    plan_ts = []
    if args.transcripts is not None:
        ts = [t for t in scan_transcripts() if _match(t.session_id, args.only)]
        plan_ts = pick_transcripts(ts, now, parse_age(args.transcripts), live)

    total = sum(d.size for d in plan_scratch) + sum(t.size for t in plan_ts)
    mode = S("DRY RUN", "yellow", "bold") if not args.yes else S("DELETING", "red", "bold")
    print(header("Will delete", f"{len(plan_scratch)} scratch · {len(plan_ts)} transcripts · {human(total)}") .rstrip()
          + f"  {mode}\n")

    def row(kind: str, size: int, last: float, sid: str, proj: str) -> None:
        print(f"  {S(pad(kind, 11), 'gray')}{pad(human(size), 7, True)}  {S(pad(ago(last, now), 4, True), 'gray')} idle  "
              f"{S(sid[:8], 'gray')}  {pretty_project(proj)}")

    nonempty = sorted([d for d in plan_scratch if d.size], key=lambda d: -d.size)
    for d in nonempty:
        row("scratch", d.size, d.last_active, d.session_id, d.project)
    if len(plan_scratch) > len(nonempty):
        print(S(f"  {pad('scratch', 11)}+ {len(plan_scratch) - len(nonempty)} empty dirs", "gray"))
    for t in sorted(plan_ts, key=lambda t: -t.size):
        row("transcript", t.size, t.last_active, t.session_id, t.project)
    if not plan_scratch and not plan_ts:
        print(S("  nothing matches", "gray"))

    if skipped:
        print(header("Kept", f"{len(skipped)} protected"))
        for d, why in skipped:
            icon, color = (S("●", "green"), "green") if why == "live session" else (S("⚠", "red"), "red")
            print(f"  {icon} {pad(human(d.size), 7, True)}  {S(d.session_id[:8], 'gray')}  "
                  f"{pad(pretty_project(d.project), 24)}{S(why, color)}")
        if not args.force and any(w != "live session" for _, w in skipped):
            print(S("\n  ⚠ = clone with unpushed/dirty/unreadable work. --force deletes those too (never live ones).", "gray"))
    if not args.yes:
        print(S("\n  Nothing deleted. Re-run with --yes to delete.", "yellow"))
        return 0

    return delete_items(plan_scratch, plan_ts, allow_unrescuable=args.force)


def delete_items(scratch: list[ScratchDir], transcripts: list[Transcript], *, log_path: Path | None = None,
                 rescue_dir: Path | None = None, allow_unrescuable: bool = False) -> int:
    """Delete, but first save unpushed/dirty clone work. A scratch dir whose at-risk clone can't be
    saved is kept unless allow_unrescuable. Every deletion is appended to the JSONL log."""
    log_path = log_path or LOG_PATH
    rescue_dir = rescue_dir or RESCUE_DIR
    failed = freed = 0
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for d in scratch:
        rescued: list[Path] = []
        notes: list[str] = []
        blocked = ""
        for c in d.clones:
            if not c.issues:
                continue
            try:
                got = rescue_clone(c.path, rescue_dir, d.session_id[:8])
                rescued += got
                if got:
                    print(S(f"  saved {c.path.name} work → {got[0].parent}", "green"))
            except RescueError as e:
                if allow_unrescuable:
                    notes.append(f"{c.path.name}: unrescuable ({e})")
                else:
                    blocked = f"{c.path.name}: can't save its work ({e})"
                    break
        if blocked:
            failed += 1
            print(S(f"  kept {d.session_id[:8]} — {blocked}", "yellow"))
            continue
        title = read_title(PROJECTS_DIR / d.project / f"{d.session_id}.jsonl")
        try:
            shutil.rmtree(d.path)
        except OSError as e:
            failed += 1
            print(f"  failed {d.path}: {e}", file=sys.stderr)
            continue
        freed += d.size
        _log(log_path, {"ts": now_iso, "kind": "scratch", "session": d.session_id, "project": d.project,
                        "title": title, "size": d.size, "path": str(d.path),
                        "rescued": [str(r) for r in rescued], "notes": notes})
    for t in transcripts:
        title = read_title(t.path)
        try:
            t.path.unlink()
            side = t.path.with_suffix("")
            if side.is_dir():
                shutil.rmtree(side)
        except OSError as e:
            failed += 1
            print(f"  failed {t.path}: {e}", file=sys.stderr)
            continue
        freed += t.size
        _log(log_path, {"ts": now_iso, "kind": "transcript", "session": t.session_id, "project": t.project,
                        "title": title, "size": t.size, "path": str(t.path), "rescued": [], "notes": []})
    msg = f"\n  ✓ Freed {human(freed)}"
    print(S(msg, "green", "bold") + (S(f" · {failed} not deleted", "red") if failed else ""))
    if freed:
        print(S(f"  logged to {str(log_path).replace(str(HOME), '~')}  (cc-sessions log)", "gray"))
    return 1 if failed else 0


def cmd_log(args) -> int:
    try:
        lines = LOG_PATH.read_text().splitlines()
    except OSError:
        print(S("  No deletions logged yet.", "gray"))
        return 0
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    entries = entries[-args.n:]
    print(header("Deletion log", f"last {len(entries)} · {str(LOG_PATH).replace(str(HOME), '~')}"))
    for e in entries:
        rescued = e.get("rescued") or []
        mark = S(f"  ⛑ {len(rescued)} saved", "green") if rescued else ""
        note = S("  " + "; ".join(e["notes"]), "yellow") if e.get("notes") else ""
        print(f"  {S(e.get('ts', '')[:16].replace('T', ' '), 'gray')}  {pad(e.get('kind', ''), 10)}"
              f"{pad(human(int(e.get('size') or 0)), 7, True)}  {S(e.get('session', '')[:8], 'gray')}  "
              f"{trunc(e.get('title') or '(no title)', 40)}{mark}{note}")
        for r in rescued:
            print(S(f"  {'':40}↳ {r}", "gray"))
    if any(e.get("rescued") for e in entries):
        print(S("\n  restore a bundle:  git clone <file>.bundle restored   ·   patch:  git apply <file>.patch", "gray"))
    return 0


# ---------- ls / show / pick ----------

def load_rows() -> list[SessionRow]:
    live = {m["sessionId"] for m in load_session_meta().values() if "sessionId" in m}
    ts = scan_transcripts()
    titles = {t.session_id: read_title(t.path) for t in ts}
    return merge_sessions(scan_scratch(), ts, live, titles)


def print_rows(rows: list[SessionRow], limit: int | None) -> list[SessionRow]:
    """Numbered session table. Returns exactly the rows shown, so numbers map back."""
    now = time.time()
    shown = rows[:limit] if limit else rows
    total = sum(r.size for r in rows)
    print(header("Sessions", f"{len(rows)} · {human(total)} on disk"))
    print(S(f"  {'#':>3}  {'SIZE':>7}  {'IDLE':>4}  {pad('TITLE', 44)}{pad('PROJECT', 22)}PARTS", "gray"))
    for i, r in enumerate(shown, 1):
        parts = []
        if r.scratch:
            parts.append(f"scratch {human(r.scratch.size)}")
        if r.transcript:
            parts.append(f"log {human(r.transcript.size)}")
        flag = ""
        if r.live:
            flag = S("  ● live", "green")
        elif r.risky:
            flag = S("  ⚠ " + "; ".join(r.risky), "red", "bold")
        title = r.title or "(no title)"
        print(f"  {S(pad(str(i), 3, True), 'cyan')}  {pad(human(r.size), 7, True)}  "
              f"{S(pad(ago(r.last_active, now), 4, True), 'gray')}  "
              f"{S(pad(trunc(title, 42), 44), 'bold' if r.title else 'gray')}"
              f"{pad(trunc(pretty_project(r.project), 20), 22)}{S(' · '.join(parts), 'gray')}{flag}")
    if limit and len(rows) > limit:
        print(S(f"  {'':3}  + {len(rows) - limit} smaller (--all)", "gray"))
    return shown


def cmd_ls(args) -> None:
    print_rows(load_rows(), None if args.all else 25)
    print(S("\n  → cc-sessions show <#|sid>   details   ·   cc-sessions pick   choose & delete", "gray"))


def _local(ts: str) -> str:
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return ts


def cmd_show(args, rows: list[SessionRow] | None = None) -> int:
    rows = rows if rows is not None else load_rows()
    key = args.session
    if key.isdigit() and len(key) <= 3:
        n = int(key)
        if not 1 <= n <= len(rows):
            print(f"error: #{n} out of range 1-{len(rows)}", file=sys.stderr)
            return 2
        matches = [rows[n - 1]]
    else:
        matches = [r for r in rows if r.sid.startswith(key)]
    if not matches:
        print(f"error: no session matches {key!r}", file=sys.stderr)
        return 2
    if len(matches) > 1:
        print(f"{key!r} matches {len(matches)} sessions — use a longer prefix:", file=sys.stderr)
        for r in matches[:10]:
            print(f"  {r.sid}  {r.title}", file=sys.stderr)
        return 2
    r = matches[0]
    st = session_stats(r.transcript.path) if r.transcript else Stats()
    now = time.time()

    def kv(k: str, v: str) -> None:
        print(f"  {S(pad(k, 11), 'gray')}{v}")

    print(header(r.title or "(no title)", r.sid))
    status = S("● running", "green") if r.live else S("not running", "gray")
    kv("status", f"{status}  · idle {ago(r.last_active, now)}")
    kv("project", pretty_project(r.project) + (S(f"   {st.cwd}", "gray") if st.cwd else ""))
    if st.branch:
        kv("branch", st.branch)
    if st.started:
        kv("when", f"{_local(st.started)} → {_local(st.ended)}")
    if r.transcript:
        kv("activity", f"{st.prompts} prompts · {st.turns} model turns" + (f" · {', '.join(st.models)}" if st.models else ""))
        kv("tokens", f"{fmt_count(st.input_tokens)} in (incl. cache) · {fmt_count(st.output_tokens)} out")
    if st.first_prompt:
        kv("first ask", f"“{trunc(st.first_prompt, 100)}”")
    print()
    kv("transcript", f"{human(r.transcript.size):>7}  {S(str(r.transcript.path).replace(str(HOME), '~'), 'gray')}"
       if r.transcript else S("none", "gray"))
    kv("scratch", f"{human(r.scratch.size):>7}  {S(str(r.scratch.path), 'gray')}" if r.scratch else S("none", "gray"))
    if r.scratch:
        for i, c in enumerate(r.scratch.clones):
            branch = "└─" if i == len(r.scratch.clones) - 1 else "├─"
            status = S("⚠ " + ", ".join(c.issues), "red", "bold") if c.issues else S("✓ pushed", "green")
            print(f"  {'':11}{S(branch, 'gray')} {pad(c.path.name, 18)} {pad(human(c.size), 7, True)}  {status}")
    if r.transcript:
        cwd = st.cwd or "."
        print(f"\n  {S('resume', 'gray')}     cd {cwd} && claude --resume {r.sid}")
    return 0


def _ask(prompt: str) -> str:
    return input(S(prompt, "cyan", "bold")).strip()


def cmd_pick(args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("error: pick is interactive; use `clean` in scripts", file=sys.stderr)
        return 2
    rows = load_rows()
    if not rows:
        print(S("  No sessions.", "gray"))
        return 0
    print(header("Pick sessions to delete", f"{len(rows)} sessions · {human(sum(r.size for r in rows))}"))
    now = time.time()
    title = f"        {'SIZE':>7}  {'IDLE':>4}  {pad('TITLE', 44)}PROJECT"
    idx = select_list([row_item(r, now) for r in rows], multi=True, title=title)
    if idx is None:
        print(S("  Cancelled.", "gray"))
        return 0
    picked = [i + 1 for i in idx]
    chosen = [rows[i] for i in idx]
    print(S(f"  › {len(chosen)} picked: ", "cyan") + ", ".join(trunc(r.title or r.sid[:8], 30) for r in chosen[:5])
          + (S(f" +{len(chosen) - 5} more", "gray") if len(chosen) > 5 else ""))

    live = [r for r in chosen if r.live]
    risky = [r for r in chosen if r.risky and not r.live]
    if live:
        print(S(f"  Skipping {len(live)} live session(s).", "yellow"))
    include_risky = False
    if risky:
        print(S(f"\n  {len(risky)} pick(s) hold work that may not exist anywhere else:", "red", "bold"))
        for r in risky:
            print(S(f"    ⚠ {r.title or r.sid[:8]} — {'; '.join(r.risky)}", "red"))
        for r in risky:
            for cl in r.scratch.clones if r.scratch else []:
                if not cl.issues:
                    continue
                print(f"\n    {S(cl.path.name, 'bold')}  {S(str(cl.path), 'gray')}")
                for line in clone_preview(cl.path):
                    print(S(f"      {line}", "yellow"))
        print(S(f"\n  If deleted, unpushed commits are first saved to {str(RESCUE_DIR).replace(str(HOME), '~')}"
                " (unreadable repos can't be saved).", "gray"))
        c = choose(["Keep them (safe)", "Delete them too"])
        if c is None:
            print(S("  Cancelled. Nothing deleted.", "gray"))
            return 0
        if c == 1:
            names = sorted({cl.path.name for r in risky if r.scratch for cl in r.scratch.clones if cl.issues})
            typed = _ask(f"  Type {' '.join(names)} to confirm: ")
            include_risky = risky_confirmed(typed, names)
            if not include_risky:
                print(S("  Didn't match — keeping them.", "yellow"))
    if all(r.live or (r.risky and not include_risky) for r in chosen):
        print(S("  Nothing to delete.", "gray"))
        return 0
    c = choose(["Scratch only — keeps /resume", "Everything — scratch + transcript (no /resume)"],
               title="  What should go?")
    if c is None:
        print(S("  Cancelled. Nothing deleted.", "gray"))
        return 0
    everything = c == 1
    scratch, trans, _ = build_plan(rows, picked, include_risky, everything)
    if not scratch and not trans:
        print(S("  Nothing to delete.", "gray"))
        return 0
    total = sum(d.size for d in scratch) + sum(t.size for t in trans)
    print(f"\n  {len(scratch)} scratch dir(s) + {len(trans)} transcript(s) = {S(human(total), 'bold')}")
    try:
        if _ask("  Type 'yes' to delete: ") != "yes":
            print(S("  Cancelled. Nothing deleted.", "gray"))
            return 0
    except (EOFError, KeyboardInterrupt):
        print(S("\n  Cancelled. Nothing deleted.", "gray"))
        return 130
    return delete_items(scratch, trans, allow_unrescuable=include_risky)


def cmd_kill(args) -> int:
    procs = list_procs()
    metas = load_session_meta()
    own = ancestry(procs)
    claude = {p.pid: p for p in procs if classify(p, metas.get(p.pid))}
    rc = 0
    for pid in args.pids:
        why = kill_refusal(pid, own, claude)
        if why:
            print(f"  {pid}: refused — {why}", file=sys.stderr)
            rc = 1
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  {pid}: SIGTERM sent ({classify(claude[pid], metas.get(pid))})")
        except OSError as e:
            print(f"  {pid}: {e}", file=sys.stderr)
            rc = 1
    return rc


# ---------- arrow-key selector (stdlib only) ----------

_SEQ = {b"\x1b[A": "up", b"\x1bOA": "up", b"\x1b[B": "down", b"\x1bOB": "down",
        b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn"}
_CH = {b"k": "up", b"j": "down", b"\r": "enter", b"\n": "enter", b" ": "space",
       b"\x1b": "esc", b"q": "esc", b"\x03": "esc"}


def decode_key(raw: bytes) -> str:
    if raw in _SEQ:
        return _SEQ[raw]
    if raw in _CH:
        return _CH[raw]
    if len(raw) == 1 and raw.isalnum():
        return raw.decode()
    return "other"


@dataclass
class SelState:
    n: int
    page: int
    multi: bool = False
    cursor: int = 0
    offset: int = 0
    chosen: set[int] = field(default_factory=set)

    def _clamp_view(self) -> None:
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + self.page:
            self.offset = self.cursor - self.page + 1

    def result(self) -> list[int]:
        return sorted(self.chosen) if self.multi and self.chosen else [self.cursor]


def sel_step(st: SelState, key: str, hotkeys: dict[str, int] | None = None) -> str | None:
    """Apply one key. Returns 'done', 'cancel' or None (keep going)."""
    if st.n == 0:
        return "cancel"
    if key == "esc":
        return "cancel"
    if hotkeys and key in hotkeys:
        st.cursor = hotkeys[key]
        st._clamp_view()
        return "done"
    if key == "up":
        st.cursor = (st.cursor - 1) % st.n
    elif key == "down":
        st.cursor = (st.cursor + 1) % st.n
    elif key == "pgup":
        st.cursor = max(0, st.cursor - st.page)
    elif key == "pgdn":
        st.cursor = min(st.n - 1, st.cursor + st.page)
    elif key == "space" and st.multi:
        st.chosen ^= {st.cursor}
        st.cursor = min(st.n - 1, st.cursor + 1)
    elif key == "a" and st.multi:
        st.chosen = set() if len(st.chosen) == st.n else set(range(st.n))
    elif key == "enter":
        return "done"
    else:
        return None
    st._clamp_view()
    return None


def render_lines(st: SelState, items: list[tuple[str, str | None]]) -> list[str]:
    lines = []
    if st.offset:
        lines.append(S(f"    ↑ {st.offset} more", "gray"))
    for i in range(st.offset, min(st.n, st.offset + st.page)):
        text, tone = items[i]
        mark = ("◉ " if i in st.chosen else "○ ") if st.multi else ""
        if i == st.cursor:
            lines.append(f"  {S('❯', 'cyan', 'bold')} {S(mark + text, 'bold', tone or 'cyan')}")
        else:
            body = S(mark, "green") if i in st.chosen else mark
            lines.append(f"    {body}{S(text, tone) if tone else text}")
    below = st.n - (st.offset + st.page)
    if below > 0:
        lines.append(S(f"    ↓ {below} more", "gray"))
    return lines


def _read_key(fd: int) -> str:
    import select as _select
    raw = os.read(fd, 1)
    if raw == b"\x1b":
        while len(raw) < 6 and _select.select([fd], [], [], 0.03)[0]:
            raw += os.read(fd, 1)
            if raw[-1:].isalpha() or raw[-1:] == b"~":
                break
    return decode_key(raw)


def select_list(items: list[tuple[str, str | None]], *, multi: bool = False, title: str = "",
                hotkeys: dict[str, int] | None = None) -> list[int] | None:
    """Inline arrow-key picker. Returns 0-based indices, or None if cancelled."""
    import termios
    import tty
    if not items:
        return None
    rows = shutil.get_terminal_size((100, 30)).lines
    st = SelState(n=len(items), page=max(5, min(len(items), rows - 8)), multi=multi)
    hint = ("↑↓ move · space tick · a all · enter confirm · esc back" if multi
            else "↑↓ move · enter select · esc back")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    drawn = 0

    def draw() -> None:
        nonlocal drawn
        lines = ([S(title, "gray")] if title else []) + render_lines(st, items) + [S(f"    {hint}", "gray", "dim")]
        if drawn:
            out.write(f"\x1b[{drawn}F")
        for line in lines:
            out.write("\x1b[2K" + line + "\n")
        out.write("\x1b[J")
        out.flush()
        drawn = len(lines)

    try:
        tty.setcbreak(fd)
        out.write("\x1b[?25l\x1b[?7l")  # hide cursor, no line wrap (keeps redraw math exact)
        draw()
        while True:
            outcome = sel_step(st, _read_key(fd), hotkeys)
            if outcome:
                break
            draw()
    except KeyboardInterrupt:
        outcome = "cancel"
    finally:
        if drawn:
            out.write(f"\x1b[{drawn}F\x1b[J")  # collapse the picker
        out.write("\x1b[?25h\x1b[?7h")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return st.result() if outcome == "done" else None


def row_item(r: SessionRow, now: float) -> tuple[str, str | None]:
    """Plain one-line text for a session (selector rows can't carry inline color)."""
    flag, tone = "", None
    if r.live:
        flag, tone = "  ● live", "green"
    elif r.risky:
        flag, tone = "  ⚠ " + "; ".join(r.risky), "red"
    return (f"{human(r.size):>7}  {ago(r.last_active, now):>4}  {pad(trunc(r.title or '(no title)', 42), 44)}"
            f"{pad(trunc(pretty_project(r.project), 20), 22)}{flag}", tone)


def pick_session(rows: list[SessionRow], multi: bool) -> list[SessionRow] | None:
    now = time.time()
    title = f"      {'SIZE':>7}  {'IDLE':>4}  {pad('TITLE', 44)}PROJECT"
    idx = select_list([row_item(r, now) for r in rows], multi=multi, title=title)
    return None if idx is None else [rows[i] for i in idx]


def choose(options: list[str], title: str = "") -> int | None:
    idx = select_list([(o, None) for o in options], title=title)
    return None if idx is None else idx[0]


# ---------- menu ----------

MENU = {
    "1": ("overview", "Overview — processes, disk, everything"),
    "2": ("ls", "List sessions"),
    "3": ("show", "Inspect a session"),
    "4": ("clean", "Clean old scratch (preview first)"),
    "5": ("pick", "Pick sessions to delete"),
    "6": ("leftovers", "Kill leftover processes"),
    "7": ("log", "Deletion log & rescued work"),
    "q": ("quit", "Quit"),
}


def menu_action(choice: str) -> str | None:
    c = choice.strip().lower()
    if not c:
        return None
    if c not in MENU:
        raise ValueError(f"no option {choice.strip()!r}")
    return MENU[c][0]


def find_leftovers(procs: list[Proc], metas: dict[int, dict]) -> list[Proc]:
    """Only orphaned tool shells: never daemon hosts, which may still carry a background session."""
    return [p for p in procs if classify(p, metas.get(p.pid)) == "orphan-shell"]


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _m_overview() -> None:
    cmd_ps(None)
    cmd_disk(_ns(all=False))


def _m_ls() -> None:
    cmd_ls(_ns(all=False))


def _m_show() -> None:
    rows = load_rows()
    print(header("Inspect a session", f"{len(rows)} sessions"))
    picked = pick_session(rows, multi=False)
    if picked:
        cmd_show(_ns(session=picked[0].sid), rows)


CLEAN_AGES = [("1d", "idle more than 1 day"), ("3d", "idle more than 3 days"),
              ("7d", "idle more than 7 days"), ("14d", "idle more than 14 days")]


def _m_clean() -> None:
    c = choose([label for _, label in CLEAN_AGES], title="  Delete scratch folders…")
    if c is None:
        return
    age = CLEAN_AGES[c][0]
    cmd_clean(_ns(scratch=age, transcripts=None, only=None, force=False, yes=False))
    if _ask("\n  Delete the items above? Type 'yes': ") == "yes":
        cmd_clean(_ns(scratch=age, transcripts=None, only=None, force=False, yes=True))
    else:
        print(S("  Nothing deleted.", "gray"))


def _m_pick() -> None:
    cmd_pick(_ns(all=False))


def _m_leftovers() -> None:
    procs = list_procs()
    left = find_leftovers(procs, load_session_meta())
    if not left:
        print(S("\n  No leftovers. All clean.", "green"))
        return
    print(header("Leftovers", f"{len(left)} orphaned shell(s) from old tool calls"))
    for p in left:
        print(f"  {S(pad(str(p.pid), 6, True), 'gray')}  up {fmt_etime(p.etime):8}  {S(trunc(p.command, 70), 'gray')}")
    if choose(["No, keep them", "Yes, kill them"], title="  Kill them?") == 1:
        cmd_kill(_ns(pids=[p.pid for p in left]))


MENU_HANDLERS = {
    "overview": _m_overview, "ls": _m_ls, "show": _m_show, "clean": _m_clean,
    "pick": _m_pick, "leftovers": _m_leftovers, "log": lambda: cmd_log(_ns(n=30)), "quit": None,
}


def run_menu() -> int:
    keys = list(MENU)
    items = [(f"{k}  {label}", None) for k, (_a, label) in MENU.items()]
    hotkeys = {k: i for i, k in enumerate(keys)}
    while True:
        print(header("cc-sessions", "what do you want to do?"))
        idx = select_list(items, hotkeys=hotkeys)
        if idx is None:
            return 0
        action, label = MENU[keys[idx[0]]]
        if action == "quit":
            return 0
        print(S(f"  › {label}", "cyan", "bold"))
        try:
            MENU_HANDLERS[action]()
        except ValueError as e:
            print(S(f"  error: {e}", "red"))
        except (EOFError, KeyboardInterrupt):
            print(S("\n  Cancelled.", "gray"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cc-sessions", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ps")
    sub.add_parser("overview")
    sub.add_parser("menu")
    d = sub.add_parser("disk")
    d.add_argument("--all", action="store_true")
    c = sub.add_parser("clean")
    c.add_argument("--scratch", metavar="AGE")
    c.add_argument("--transcripts", metavar="AGE")
    c.add_argument("--only", action="append", metavar="SID")
    c.add_argument("--force", action="store_true")
    c.add_argument("--yes", action="store_true")
    k = sub.add_parser("kill")
    k.add_argument("pids", nargs="+", type=int)
    ls = sub.add_parser("ls")
    ls.add_argument("--all", action="store_true")
    sh = sub.add_parser("show")
    sh.add_argument("session", help="row # from `ls` or a session-id prefix")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=30)
    pk = sub.add_parser("pick")
    pk.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "log":
            return cmd_log(args)
        elif args.cmd == "ls":
            cmd_ls(args)
        elif args.cmd == "show":
            return cmd_show(args)
        elif args.cmd == "pick":
            return cmd_pick(args)
        elif args.cmd == "ps":
            cmd_ps(args)
        elif args.cmd == "disk":
            cmd_disk(args)
        elif args.cmd == "clean":
            return cmd_clean(args)
        elif args.cmd == "kill":
            return cmd_kill(args)
        elif args.cmd == "menu" or (args.cmd is None and sys.stdin.isatty() and sys.stdout.isatty()):
            return run_menu()
        else:  # overview, or no args in a pipe/script
            cmd_ps(args)
            args.all = False
            cmd_disk(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
