import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import cc_sessions as cs


class ParseAge(unittest.TestCase):
    def test_units(self):
        self.assertEqual(cs.parse_age("3d"), 3 * 86400)
        self.assertEqual(cs.parse_age("12h"), 12 * 3600)
        self.assertEqual(cs.parse_age("0d"), 0)
        self.assertEqual(cs.parse_age("7"), 7 * 86400)  # bare number = days

    def test_rejects_garbage(self):
        for bad in ["", "abc", "-1d", "3w", "1.5d"]:
            with self.assertRaises(ValueError, msg=bad):
                cs.parse_age(bad)


class HumanSize(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(cs.human(0), "0B")
        self.assertEqual(cs.human(512), "512B")
        self.assertEqual(cs.human(1024), "1.0K")
        self.assertEqual(cs.human(653 * 1024 * 1024), "653.0M")
        self.assertEqual(cs.human(int(1.4 * 1024 ** 3)), "1.4G")


class ParsePs(unittest.TestCase):
    def test_parses_lines_with_spaces_in_command(self):
        out = (
            "  74559 32179 404560    00:41 ttys007  claude\n"
            "  46556     1  73904 11:30:59 ??       /Users/x/.local/bin/claude daemon run --json-path a b\n"
            "\n"
        )
        procs = cs.parse_ps(out)
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0].pid, 74559)
        self.assertEqual(procs[0].rss_kb, 404560)
        self.assertEqual(procs[1].command, "/Users/x/.local/bin/claude daemon run --json-path a b")
        self.assertEqual(procs[1].ppid, 1)

    def test_ignores_malformed(self):
        self.assertEqual(cs.parse_ps("garbage line\n"), [])


def P(cmd, pid=10, ppid=5):
    return cs.Proc(pid=pid, ppid=ppid, rss_kb=1000, etime="01:00", tty="??", command=cmd)


class Classify(unittest.TestCase):
    def test_interactive_with_meta(self):
        meta = {"kind": "interactive", "name": "ai-f5", "cwd": "/a"}
        self.assertEqual(cs.classify(P("claude"), meta), "interactive")

    def test_bg_session_is_not_spare(self):
        meta = {"kind": "bg", "name": "Passkey demo", "jobId": "61df"}
        self.assertEqual(cs.classify(P("claude bg-spare --bg-spare /tmp/x.sock"), meta), "background")

    def test_spare_without_meta(self):
        self.assertEqual(cs.classify(P("claude bg-spare --bg-spare /tmp/x.sock"), None), "spare")

    def test_daemon_host_shell_skill(self):
        self.assertEqual(cs.classify(P("/u/.local/bin/claude daemon run --json-path x"), None), "daemon")
        self.assertEqual(cs.classify(P("claude bg-pty-host --bg-pty-host /tmp/s"), None), "pty-host")
        self.assertEqual(
            cs.classify(P("/bin/zsh -c source /u/.claude/shell-snapshots/snap.sh && x", ppid=1), None),
            "orphan-shell",
        )
        self.assertEqual(
            cs.classify(P("/opt/Python /u/.claude/skills/connector-hub/scripts/bridge_daemon.py"), None),
            "skill-daemon",
        )

    def test_unrelated_process_is_none(self):
        self.assertIsNone(cs.classify(P("/usr/bin/vim notes-about-claude.md"), None))
        self.assertIsNone(cs.classify(P("/Applications/Claude.app/Contents/MacOS/Claude"), None))


class ScratchCandidates(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000_000

    def S(self, sid, age_days, size=100, risky=()):
        return cs.ScratchDir(
            path=Path(f"/tmp/claude-1/proj/{sid}"), project="proj", session_id=sid,
            last_active=self.now - age_days * 86400, size=size,
            clones=[cs.Clone(path=Path("/x"), size=1, issues=list(risky))] if risky is not None else [],
        )

    def test_age_and_live_filtering(self):
        dirs = [self.S("old", 10), self.S("young", 1), self.S("live", 10)]
        delete, skipped = cs.pick_scratch(dirs, self.now, 3 * 86400, live_ids={"live"}, force=False)
        self.assertEqual([d.session_id for d in delete], ["old"])
        self.assertEqual(skipped, [(dirs[2], "live session")])

    def test_risky_clone_skipped_unless_forced(self):
        dirs = [self.S("risky", 10, risky=("2 unpushed",))]
        delete, skipped = cs.pick_scratch(dirs, self.now, 0, live_ids=set(), force=False)
        self.assertEqual(delete, [])
        self.assertIn("unpushed", skipped[0][1])
        delete, _ = cs.pick_scratch(dirs, self.now, 0, live_ids=set(), force=True)
        self.assertEqual(len(delete), 1)

    def test_live_never_deleted_even_with_force(self):
        dirs = [self.S("live", 10)]
        delete, _ = cs.pick_scratch(dirs, self.now, 0, live_ids={"live"}, force=True)
        self.assertEqual(delete, [])

    def test_empty(self):
        self.assertEqual(cs.pick_scratch([], self.now, 0, set(), False), ([], []))


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class CloneStatus(unittest.TestCase):
    """Real git repos in a temp dir — git is the boundary, not mocked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.clone = self.tmp / "work"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.clone))
        (self.clone / "a.txt").write_text("a")
        git(self.clone, "checkout", "-q", "-b", "main")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", "init")
        git(self.clone, "push", "-q", "-u", "origin", "main")

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_clean_clone_has_no_issues(self):
        self.assertEqual(cs.clone_issues(self.clone), [])

    def test_dirty(self):
        (self.clone / "a.txt").write_text("changed")
        self.assertEqual(cs.clone_issues(self.clone), ["uncommitted changes"])

    def test_unpushed(self):
        (self.clone / "b.txt").write_text("b")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", "local")
        self.assertEqual(cs.clone_issues(self.clone), ["1 unpushed commit(s)"])

    def test_local_branch_without_upstream(self):
        git(self.clone, "checkout", "-q", "-b", "feature")
        (self.clone / "c.txt").write_text("c")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", "feat")
        self.assertEqual(cs.clone_issues(self.clone), ["1 unpushed commit(s)"])

    def test_no_remote(self):
        solo = self.tmp / "solo"
        solo.mkdir()
        git(solo, "init", "-q")
        (solo / "x").write_text("x")
        git(solo, "add", ".")
        git(solo, "commit", "-q", "-m", "x")
        self.assertEqual(cs.clone_issues(solo), ["no remote"])

    def test_broken_repo_reported_not_crash(self):
        broken = self.tmp / "broken"
        (broken / ".git").mkdir(parents=True)
        self.assertEqual(cs.clone_issues(broken), ["git error"])


class ScanScratch(unittest.TestCase):
    def test_scan_uses_transcript_mtime_and_finds_clones(self):
        tmp = Path(tempfile.mkdtemp())
        root, projects = tmp / "scratch", tmp / "projects"
        sdir = root / "proj-a" / "sid-1" / "scratchpad"
        (sdir / "repo" / ".git").mkdir(parents=True)
        (sdir / "file.bin").write_bytes(b"x" * 5000)
        (projects / "proj-a").mkdir(parents=True)
        t = projects / "proj-a" / "sid-1.jsonl"
        t.write_text("{}")
        old = time.time() - 5 * 86400
        os.utime(t, (old, old))
        dirs = cs.scan_scratch(root, projects)
        self.assertEqual(len(dirs), 1)
        d = dirs[0]
        self.assertEqual((d.project, d.session_id), ("proj-a", "sid-1"))
        self.assertAlmostEqual(d.last_active, old, delta=2)
        self.assertGreaterEqual(d.size, 5000)
        self.assertEqual([c.path.name for c in d.clones], ["repo"])
        subprocess.run(["rm", "-rf", str(tmp)])

    def test_missing_root(self):
        self.assertEqual(cs.scan_scratch(Path("/nonexistent/x"), Path("/nonexistent/y")), [])


class KillGuard(unittest.TestCase):
    def test_refuses_own_ancestry(self):
        self.assertIn("this session", cs.kill_refusal(os.getppid(), {os.getppid()}, {}))

    def test_refuses_non_claude_pid(self):
        self.assertIn("not a Claude process", cs.kill_refusal(999999, set(), {}))

    def test_allows_known_proc(self):
        self.assertIsNone(cs.kill_refusal(42, set(), {42: P("claude", pid=42)}))


class Formatting(unittest.TestCase):
    def test_fmt_etime(self):
        self.assertEqual(cs.fmt_etime("00:05"), "<1m")
        self.assertEqual(cs.fmt_etime("09:31"), "9m")
        self.assertEqual(cs.fmt_etime("02:12:01"), "2h 12m")
        self.assertEqual(cs.fmt_etime("07-11:59:27"), "7d 11h")
        self.assertEqual(cs.fmt_etime("weird"), "weird")

    def test_trunc(self):
        self.assertEqual(cs.trunc("abcdef", 4), "abc…")
        self.assertEqual(cs.trunc("abc", 4), "abc")
        self.assertEqual(cs.trunc("", 4), "")
        self.assertEqual(cs.trunc("abc", 1), "…")

    def test_pretty_project(self):
        home = "-" + str(Path.home()).strip("/").replace("/", "-")
        self.assertEqual(cs.pretty_project(home + "-Development-AI"), "AI")
        self.assertEqual(cs.pretty_project(home + "-Development-JS-pgnextgenconsumer"), "JS/pgnextgenconsumer")
        self.assertEqual(cs.pretty_project(home), "~")
        self.assertEqual(cs.pretty_project(home + "--claude-spares-spare-NIUynf"), "(spare)")
        self.assertEqual(cs.pretty_project("bash-edit-diff"), "bash-edit-diff")
        self.assertEqual(cs.pretty_project(home + "-Development-AI-passkey-demo"), "AI/passkey-demo")

    def test_ago(self):
        self.assertEqual(cs.ago(100, now=130), "<1m")
        self.assertEqual(cs.ago(0, now=3 * 86400), "3d")
        self.assertEqual(cs.ago(200, now=100), "<1m")  # future mtime clamps

    def test_bar(self):
        self.assertEqual(cs.bar(0, 4), "····")
        self.assertEqual(cs.bar(1, 4), "████")
        self.assertEqual(cs.bar(5, 4), "████")
        self.assertEqual(cs.bar(0.5, 4), "██··")
        self.assertEqual(cs.bar(0.01, 4), "█···")  # nonzero is always visible

    def test_style_disabled_is_plain(self):
        s = cs.Style(enabled=False)
        self.assertEqual(s("x", "red", "bold"), "x")
        self.assertIn("\033[", cs.Style(enabled=True)("x", "red"))


def jl(*records) -> str:
    import json as _j
    return "\n".join(_j.dumps(r) for r in records) + "\n"


def user(text, meta=False):
    r = {"type": "user", "message": {"role": "user", "content": text}, "cwd": "/w/proj",
         "timestamp": "2026-11-01T10:00:00Z", "gitBranch": "feat/x"}
    if meta:
        r["isMeta"] = True
    return r


def asst(req, inp, out, model="claude-x", ts="2026-11-01T11:00:00Z"):
    return {"type": "assistant", "requestId": req, "timestamp": ts,
            "message": {"model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                                    "cache_read_input_tokens": 10,
                                                    "cache_creation_input_tokens": 5}}}


class Titles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def write(self, text):
        f = self.tmp / "s.jsonl"
        f.write_text(text)
        return f

    def test_latest_ai_title_wins(self):
        f = self.write(jl(user("hi"), {"type": "ai-title", "aiTitle": "Old"}, {"type": "ai-title", "aiTitle": "New"}))
        self.assertEqual(cs.read_title(f), "New")

    def test_custom_title_beats_ai_title(self):
        f = self.write(jl({"type": "custom-title", "customTitle": "Mine"}, {"type": "ai-title", "aiTitle": "Auto"}))
        self.assertEqual(cs.read_title(f), "Mine")

    def test_title_outside_tail_window_still_found(self):
        filler = jl(*[user("x" * 200, meta=True)] * 50)
        f = self.write(jl({"type": "ai-title", "aiTitle": "Early"}) + filler)
        self.assertEqual(cs.read_title(f, window=1024), "Early")

    def test_falls_back_to_first_real_prompt(self):
        f = self.write(jl(user("<local-command-caveat>x</local-command-caveat>"), user("meta", meta=True),
                          user([{"type": "text", "text": "fix the  crash\nin checkout"}])))
        self.assertEqual(cs.read_title(f), "fix the crash in checkout")

    def test_unicode_and_escapes(self):
        f = self.write(jl({"type": "ai-title", "aiTitle": "UPI “intent” \\ ₹ flow"}))
        self.assertEqual(cs.read_title(f), "UPI “intent” \\ ₹ flow")

    def test_empty_or_missing(self):
        self.assertEqual(cs.read_title(self.write("")), "")
        self.assertEqual(cs.read_title(self.tmp / "nope.jsonl"), "")
        self.assertEqual(cs.read_title(self.write("not json\n{broken")), "")


class Stats(unittest.TestCase):
    def test_stats_dedupes_usage_by_request(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "s.jsonl"
        f.write_text(jl(user("first ask"), asst("r1", 100, 20), asst("r1", 100, 20),  # same request, split blocks
                        user("second"), asst("r2", 1, 5, ts="2026-11-02T09:00:00Z"),
                        "garbage-line-is-ignored"))
        st = cs.session_stats(f)
        self.assertEqual(st.prompts, 2)
        self.assertEqual(st.turns, 2)
        self.assertEqual(st.input_tokens, 101 + 2 * 15)  # input + cache read + cache creation, per request
        self.assertEqual(st.output_tokens, 25)
        self.assertEqual(st.first_prompt, "first ask")
        self.assertEqual(st.cwd, "/w/proj")
        self.assertEqual(st.branch, "feat/x")
        self.assertEqual(st.models, ["claude-x"])
        self.assertEqual((st.started, st.ended), ("2026-11-01T10:00:00Z", "2026-11-02T09:00:00Z"))
        subprocess.run(["rm", "-rf", str(tmp)])

    def test_missing_file(self):
        st = cs.session_stats(Path("/nonexistent.jsonl"))
        self.assertEqual((st.prompts, st.turns, st.input_tokens), (0, 0, 0))


class MergeSessions(unittest.TestCase):
    def test_merge_and_sort(self):
        sa = cs.ScratchDir(Path("/s/p/A"), "p", "A", 100, 500, [])
        ta = cs.Transcript(Path("/t/p/A.jsonl"), "p", "A", 200, 50)
        tb = cs.Transcript(Path("/t/p/B.jsonl"), "p", "B", 300, 900)
        rows = cs.merge_sessions([sa], [ta, tb], live_ids={"B"}, titles={"A": "Alpha"})
        self.assertEqual([r.sid for r in rows], ["B", "A"])  # by total size desc
        a = rows[1]
        self.assertEqual((a.size, a.title, a.last_active, a.live), (550, "Alpha", 200, False))
        self.assertTrue(rows[0].live)
        self.assertEqual(rows[0].title, "")

    def test_risky(self):
        s = cs.ScratchDir(Path("/s"), "p", "A", 0, 1, [cs.Clone(Path("/s/kc"), 1, ["2 unpushed commit(s)"])])
        row = cs.merge_sessions([s], [], set(), {})[0]
        self.assertEqual(row.risky, ["kc: 2 unpushed commit(s)"])


class Selection(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cs.parse_selection("1,3,5-7", 10), [1, 3, 5, 6, 7])
        self.assertEqual(cs.parse_selection(" 2 , 2 ", 3), [2])
        self.assertEqual(cs.parse_selection("all", 3), [1, 2, 3])
        self.assertEqual(cs.parse_selection("1 4", 5), [1, 4])

    def test_invalid(self):
        for bad in ["", "0", "9", "3-1", "a", "1,,x", "2-"]:
            with self.assertRaises(ValueError, msg=bad):
                cs.parse_selection(bad, 5)


class BuildPlan(unittest.TestCase):
    def rows(self):
        clean = cs.ScratchDir(Path("/s/A"), "p", "A", 0, 10, [])
        risky = cs.ScratchDir(Path("/s/B"), "p", "B", 0, 20, [cs.Clone(Path("/s/B/kc"), 1, ["no remote"])])
        live = cs.ScratchDir(Path("/s/C"), "p", "C", 0, 30, [])
        tA = cs.Transcript(Path("/t/A.jsonl"), "p", "A", 0, 5)
        tC = cs.Transcript(Path("/t/C.jsonl"), "p", "C", 0, 5)
        return [
            cs.SessionRow("A", "p", "", 0, False, clean, tA),
            cs.SessionRow("B", "p", "", 0, False, risky, None),
            cs.SessionRow("C", "p", "", 0, True, live, tC),
        ]

    def test_scratch_only_excludes_live_and_risky(self):
        scratch, trans, excluded = cs.build_plan(self.rows(), [1, 2, 3], include_risky=False, everything=False)
        self.assertEqual([s.session_id for s in scratch], ["A"])
        self.assertEqual(trans, [])
        self.assertEqual({sid: why for sid, why in excluded}, {"B": "kc: no remote", "C": "live session"})

    def test_everything_with_risky(self):
        scratch, trans, excluded = cs.build_plan(self.rows(), [1, 2, 3], include_risky=True, everything=True)
        self.assertEqual([s.session_id for s in scratch], ["A", "B"])
        self.assertEqual([t.session_id for t in trans], ["A"])
        self.assertEqual(excluded, [("C", "live session")])

    def test_empty_pick(self):
        self.assertEqual(cs.build_plan(self.rows(), [], False, True), ([], [], []))


class FmtCount(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(cs.fmt_count(0), "0")
        self.assertEqual(cs.fmt_count(999), "999")
        self.assertEqual(cs.fmt_count(1500), "1.5K")
        self.assertEqual(cs.fmt_count(12_300_000), "12.3M")
        self.assertEqual(cs.fmt_count(2_000_000_000), "2.0B")


class Menu(unittest.TestCase):
    def test_choices(self):
        self.assertEqual(cs.menu_action("1"), "overview")
        self.assertEqual(cs.menu_action(" 4 "), "clean")
        self.assertEqual(cs.menu_action("6"), "leftovers")
        self.assertEqual(cs.menu_action("7"), "log")
        self.assertEqual(cs.menu_action("q"), "quit")
        self.assertEqual(cs.menu_action("Q"), "quit")
        self.assertIsNone(cs.menu_action(""))  # just redraw

    def test_unknown(self):
        for bad in ["0", "8", "x", "12"]:
            with self.assertRaises(ValueError, msg=bad):
                cs.menu_action(bad)

    def test_every_menu_item_has_handler(self):
        for key, (action, _label) in cs.MENU.items():
            self.assertIn(action, cs.MENU_HANDLERS, key)


class Leftovers(unittest.TestCase):
    def test_only_orphan_shells(self):
        procs = [
            P("/bin/zsh -c source /u/.claude/shell-snapshots/a.sh", pid=1, ppid=1),        # orphan
            P("/bin/zsh -c source /u/.claude/shell-snapshots/b.sh", pid=2, ppid=500),      # live tool shell
            P("claude bg-pty-host --bg-pty-host /tmp/s", pid=3, ppid=1),                   # hosts a bg session
            P("claude bg-spare --bg-spare /tmp/x", pid=4, ppid=3),
        ]
        metas = {4: {"kind": "bg", "name": "Passkey"}}
        self.assertEqual([p.pid for p in cs.find_leftovers(procs, metas)], [1])

    def test_none(self):
        self.assertEqual(cs.find_leftovers([], {}), [])


class NoArgsNonTty(unittest.TestCase):
    def test_piped_run_prints_report_not_menu(self):
        r = subprocess.run([sys.executable, str(Path(cs.__file__))], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Live processes", r.stdout)
        self.assertNotIn("What do you want to do", r.stdout)


class Keys(unittest.TestCase):
    def test_decode(self):
        cases = {b"\x1b[A": "up", b"\x1bOA": "up", b"k": "up", b"\x1b[B": "down", b"j": "down",
                 b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn", b"\r": "enter", b"\n": "enter", b" ": "space",
                 b"\x1b": "esc", b"q": "esc", b"\x03": "esc", b"a": "a", b"3": "3", b"\x1b[Z": "other"}
        for raw, want in cases.items():
            self.assertEqual(cs.decode_key(raw), want, raw)


class Selector(unittest.TestCase):
    def st(self, n=5, page=3, multi=False):
        return cs.SelState(n=n, page=page, multi=multi)

    def test_move_wraps_and_scrolls(self):
        st = self.st()
        cs.sel_step(st, "up")                       # wrap to last
        self.assertEqual((st.cursor, st.offset), (4, 2))
        cs.sel_step(st, "down")                     # wrap to first
        self.assertEqual((st.cursor, st.offset), (0, 0))
        for _ in range(3):
            cs.sel_step(st, "down")
        self.assertEqual((st.cursor, st.offset), (3, 1))  # cursor kept visible

    def test_paging_clamps(self):
        st = self.st(n=10, page=4)
        cs.sel_step(st, "pgdn"); self.assertEqual(st.cursor, 4)
        cs.sel_step(st, "pgdn"); cs.sel_step(st, "pgdn"); self.assertEqual(st.cursor, 9)
        cs.sel_step(st, "pgup"); self.assertEqual(st.cursor, 5)

    def test_single_enter_and_cancel(self):
        st = self.st()
        cs.sel_step(st, "down")
        self.assertEqual(cs.sel_step(st, "enter"), "done")
        self.assertEqual(st.result(), [1])
        self.assertEqual(cs.sel_step(self.st(), "esc"), "cancel")

    def test_space_is_ignored_in_single_mode(self):
        st = self.st()
        self.assertIsNone(cs.sel_step(st, "space"))
        self.assertEqual(st.chosen, set())

    def test_multi_toggle_all_and_enter(self):
        st = self.st(multi=True)
        cs.sel_step(st, "space")                    # toggles 0, moves to 1
        cs.sel_step(st, "down")
        cs.sel_step(st, "space")                    # toggles 2
        self.assertEqual(st.chosen, {0, 2})
        for _ in range(3):
            cs.sel_step(st, "up")                   # space moved us to 3; back to 0
        cs.sel_step(st, "space")                    # untoggle 0
        self.assertEqual(st.chosen, {2})
        cs.sel_step(st, "a"); self.assertEqual(st.chosen, set(range(5)))
        cs.sel_step(st, "a"); self.assertEqual(st.chosen, set())

    def test_multi_space_at_end_does_not_wrap(self):
        st = self.st(multi=True)
        cs.sel_step(st, "up")
        cs.sel_step(st, "space")
        self.assertEqual((st.chosen, st.cursor), ({4}, 4))

    def test_multi_enter_with_nothing_ticked_takes_highlighted(self):
        st = self.st(multi=True)
        cs.sel_step(st, "down")
        self.assertEqual(cs.sel_step(st, "enter"), "done")
        self.assertEqual(st.result(), [1])

    def test_hotkey(self):
        st = self.st()
        self.assertEqual(cs.sel_step(st, "3", hotkeys={"3": 2}), "done")
        self.assertEqual(st.result(), [2])
        self.assertIsNone(cs.sel_step(self.st(), "9", hotkeys={"3": 2}))

    def test_empty_list_cancels(self):
        self.assertEqual(cs.sel_step(cs.SelState(n=0, page=3), "enter"), "cancel")


class RenderSelector(unittest.TestCase):
    def test_marks_and_scroll_hints(self):
        old, cs.S = cs.S, cs.Style(enabled=False)
        try:
            st = cs.SelState(n=6, page=3, multi=True, cursor=3, offset=2, chosen={2})
            items = [(f"item{i}", None) for i in range(6)]
            lines = cs.render_lines(st, items)
            self.assertEqual(lines[0].strip(), "↑ 2 more")
            self.assertIn("◉ item2", lines[1])
            self.assertTrue(lines[2].lstrip().startswith("❯"))
            self.assertIn("○ item3", lines[2])
            self.assertEqual(lines[4].strip(), "↓ 1 more")
        finally:
            cs.S = old


class ScratchRoot(unittest.TestCase):
    def test_env_override_wins(self):
        self.assertEqual(cs.find_scratch_root({"CC_SESSIONS_SCRATCH": "/x/y"}, uid=7, exists=lambda p: False), Path("/x/y"))

    def test_macos_private_tmp(self):
        seen = {Path("/private/tmp/claude-7"), Path("/tmp/claude-7")}
        self.assertEqual(cs.find_scratch_root({}, uid=7, exists=lambda p: p in seen), Path("/private/tmp/claude-7"))

    def test_linux_tmp(self):
        self.assertEqual(cs.find_scratch_root({}, uid=7, exists=lambda p: p == Path("/tmp/claude-7")), Path("/tmp/claude-7"))

    def test_tmpdir_env(self):
        r = cs.find_scratch_root({"TMPDIR": "/var/tmp/x/"}, uid=7, exists=lambda p: p == Path("/var/tmp/x/claude-7"))
        self.assertEqual(r, Path("/var/tmp/x/claude-7"))

    def test_nothing_exists_falls_back_to_tmp(self):
        self.assertEqual(cs.find_scratch_root({}, uid=7, exists=lambda p: False), Path("/tmp/claude-7"))


class RescueClone(unittest.TestCase):
    """Real git repos: rescue must produce something restorable, or refuse."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.repo = self.tmp / "kc"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.out = self.tmp / "rescue"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def commit(self, name, msg):
        (self.repo / name).write_text(name)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", msg)

    def test_unpushed_commits_become_restorable_bundle(self):
        self.commit("b.txt", "local work")
        saved = cs.rescue_clone(self.repo, self.out, "abc12345")
        bundles = [p for p in saved if p.suffix == ".bundle"]
        self.assertEqual(len(bundles), 1)
        restored = self.tmp / "restored"
        r = subprocess.run(["git", "clone", "-q", str(bundles[0]), str(restored)], capture_output=True, text=True)
        self.assertNotIn("nonexistent ref", r.stderr)              # HEAD is in the bundle: checks out cleanly
        self.assertEqual((restored / "b.txt").read_text(), "b.txt")  # working tree restored, not just history
        log = subprocess.run(["git", "-C", str(restored), "log", "--all", "--format=%s"],
                             capture_output=True, text=True).stdout
        self.assertIn("local work", log)

    def test_dirty_tree_saved_as_patch(self):
        (self.repo / "a.txt").write_text("edited")
        saved = cs.rescue_clone(self.repo, self.out, "abc12345")
        patch = [p for p in saved if p.suffix == ".patch"][0]
        self.assertIn("+edited", patch.read_text())

    def test_untracked_files_listed_and_copied(self):
        (self.repo / "new.txt").write_text("brand new")
        saved = cs.rescue_clone(self.repo, self.out, "abc12345")
        tar = [p for p in saved if p.name.endswith(".untracked.tar")][0]
        import tarfile
        with tarfile.open(tar) as tf:
            self.assertIn("new.txt", tf.getnames())

    def test_no_remote_bundles_everything(self):
        solo = self.tmp / "solo"
        solo.mkdir()
        git(solo, "init", "-q")
        (solo / "x").write_text("x")
        git(solo, "add", ".")
        git(solo, "commit", "-q", "-m", "only copy")
        saved = cs.rescue_clone(solo, self.out, "abc12345")
        self.assertTrue(any(p.suffix == ".bundle" for p in saved))

    def test_clean_clone_saves_nothing(self):
        self.assertEqual(cs.rescue_clone(self.repo, self.out, "abc12345"), [])

    def test_unreadable_repo_raises(self):
        broken = self.tmp / "broken"
        (broken / ".git").mkdir(parents=True)
        with self.assertRaises(cs.RescueError):
            cs.rescue_clone(broken, self.out, "abc12345")


class DeleteItemsSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "log.jsonl"
        self.rescue = self.tmp / "rescue"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def scratch(self, sid, clone_issues=None, broken=False):
        d = self.tmp / "scratch" / "proj" / sid
        clones = []
        if clone_issues is not None:
            repo = d / "scratchpad" / "kc"
            if broken:
                (repo / ".git").mkdir(parents=True)
            else:
                repo.mkdir(parents=True)
                git(repo, "init", "-q")
                (repo / "f").write_text("f")
                git(repo, "add", ".")
                git(repo, "commit", "-q", "-m", "work")
            clones = [cs.Clone(repo, 1, clone_issues)]
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob").write_text("x" * 100)
        return cs.ScratchDir(d, "proj", sid, 0, 100, clones)

    def test_rescues_then_deletes_and_logs(self):
        sd = self.scratch("s1", ["no remote"])
        rc = cs.delete_items([sd], [], log_path=self.log, rescue_dir=self.rescue)
        self.assertEqual(rc, 0)
        self.assertFalse(sd.path.exists())
        entries = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(entries[0]["kind"], "scratch")
        self.assertEqual(entries[0]["session"], "s1")
        self.assertTrue(entries[0]["rescued"] and Path(entries[0]["rescued"][0]).exists())

    def test_unrescuable_risky_clone_is_not_deleted(self):
        sd = self.scratch("s2", ["git error"], broken=True)
        rc = cs.delete_items([sd], [], log_path=self.log, rescue_dir=self.rescue)
        self.assertEqual(rc, 1)
        self.assertTrue(sd.path.exists())
        self.assertFalse(self.log.exists() and self.log.read_text().strip())

    def test_unrescuable_allowed_when_explicitly_accepted(self):
        sd = self.scratch("s3", ["git error"], broken=True)
        rc = cs.delete_items([sd], [], log_path=self.log, rescue_dir=self.rescue, allow_unrescuable=True)
        self.assertEqual(rc, 0)
        self.assertFalse(sd.path.exists())
        self.assertIn("unrescuable", self.log.read_text())

    def test_transcript_logged_with_title(self):
        f = self.tmp / "proj" / "t1.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(jl({"type": "ai-title", "aiTitle": "My task"}))
        tr = cs.Transcript(f, "proj", "t1", 0, f.stat().st_size)
        cs.delete_items([], [tr], log_path=self.log, rescue_dir=self.rescue)
        self.assertFalse(f.exists())
        e = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual((e["kind"], e["title"]), ("transcript", "My task"))


class ConfirmRisky(unittest.TestCase):
    def test_must_type_every_clone_name(self):
        self.assertTrue(cs.risky_confirmed("kc", ["kc"]))
        self.assertTrue(cs.risky_confirmed(" pg, kc ", ["kc", "pg"]))
        self.assertTrue(cs.risky_confirmed("kc pg", ["kc", "pg"]))
        self.assertFalse(cs.risky_confirmed("kc", ["kc", "pg"]))
        self.assertFalse(cs.risky_confirmed("y", ["kc"]))
        self.assertFalse(cs.risky_confirmed("", ["kc"]))
        self.assertFalse(cs.risky_confirmed("kc", []))


if __name__ == "__main__":
    unittest.main()
