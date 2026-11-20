---
name: sessions
description: Use when the user wants to see, find, resume or clean up Claude Code sessions — "clean up claude sessions", "claude is using too much disk/RAM", "kill old claude processes", "what's in my scratchpad", "find the session where I worked on X", "free up space from ~/.claude or /private/tmp/claude-*", or types /sessions. Covers running processes, transcripts (~/.claude/projects) and per-session scratchpads with cloned repos.
---

# sessions

Everything goes through the `cc-sessions` CLI (alias `ccs`). It already knows which sessions are live, which scratch clones hold unpushed or unreadable work, and which processes are background sessions vs leftovers. Hand-rolled `rm -rf`, `find -delete`, `pkill` or `kill -9` skip those checks, so they are not used here.

## Running it

Use `cc-sessions` if `command -v cc-sessions` finds it. Otherwise run the copy bundled with this skill: `python3 "<base directory of this skill>/../../cc_sessions.py"` (the base directory is shown when the skill loads). Below, `cc-sessions` means whichever of the two works. If neither exists, tell the user and stop — don't improvise deletions.

If the user wants `ccs` in their own terminal and it isn't installed: `python3 "<base>/../../cc_sessions.py" install-cli` writes `ccs` + `cc-sessions` to `~/.local/bin` (ask first; it adds files to their PATH).

## Your Bash is not a terminal

The arrow-key menu (`ccs` with no args) and `cc-sessions pick` need a real TTY. From the Bash tool, bare `cc-sessions` prints the plain report and `pick` refuses. Use the subcommands below. If the user wants to browse or tick sessions with arrow keys themselves, tell them to run `ccs` in their own terminal.

## Quick reference

| User wants | Run |
|---|---|
| Overview of everything | `cc-sessions overview` |
| Processes + RAM | `cc-sessions ps` |
| Disk: transcripts + scratchpads + clone status | `cc-sessions disk --all` |
| Find a session by topic | `cc-sessions ls --all \| grep -i '<topic>'` |
| Details + resume command | `cc-sessions show <session-id-prefix>` |
| Preview scratch cleanup | `cc-sessions clean --scratch 3d` |
| Delete what the preview listed | same command + `--yes` |
| Old transcripts (loses `/resume`) | `cc-sessions clean --transcripts 14d` → then `--yes` |
| One specific session | `cc-sessions clean --only <sid-prefix>` → then `--yes` |
| Kill processes | `cc-sessions kill <pid> [pid…]` |
| What was deleted / recover work | `cc-sessions log` |

Use session-id prefixes, not `#` row numbers: row numbers shift between runs as sizes change.

## Cleanup recipe

1. Run the preview (no `--yes`). Show the user the "Will delete" total and the "Kept" list.
2. Wait for the user to say yes to *that* preview. "Just do it" before seeing it still means preview first, then one short confirmation.
3. Re-run the identical command with `--yes`. Report the "Freed" line.

Default to scratch only. Add `--transcripts` only when the user asks about transcripts/history, and say that those sessions can no longer be resumed.

## ⚠ flagged clones (unpushed / dirty / git error)

These are skipped on purpose. `--force` deletes them, so it is used only when the user has seen what is inside and said to delete it. To show them:

```bash
cd <clone path from `cc-sessions show`> && git log --branches --not --remotes --oneline && git status --short
```

Offer to push the commits first. "git error" means the repo is unreadable — say so; the user decides.

When deletion does go ahead, cc-sessions first saves unpushed commits / uncommitted changes to `~/.claude/cc-sessions-rescue/` and skips the folder if saving fails. Tell the user where it went. `cc-sessions log` lists every deletion and its rescued files — use it to answer "what did I delete?" or "can I get X back?" (`git clone <file>.bundle restored`).

If the deletion isn't in the log, there is no rescue copy: it happened before logging existed, or outside cc-sessions (`rm`, another tool). Say plainly that local-only work from it is gone unless it was pushed — don't promise recovery the log can't back up.

Live sessions are never deleted, even with `--force`.

## Processes

From `cc-sessions ps`:

| KIND | Meaning | Kill? |
|---|---|---|
| `orphan-shell` | Leftover shell from an old tool call | Safe |
| `interactive` | A session open in a terminal tab | Only if the user names it |
| `background` | A background session doing user work | Only if the user names it |
| `pty-host` "daemon gone · hosts N" | Host carrying background session N | Kill together with N, only if N is being killed |
| `daemon`, `spare` | Claude's own runtime | No |

`cc-sessions kill` refuses this session and non-Claude PIDs. RAM is mostly ~400 MB per open session — the honest fix is often "close tabs you're done with", not killing leftovers.

## Finding and resuming a session

`cc-sessions ls --all | grep -i <topic>`, then `cc-sessions show <sid-prefix>`; its last line is the exact `cd … && claude --resume <id>` to give the user. Titles come from the session's title, not a full-text search; if nothing matches, try other words before grepping transcripts.
