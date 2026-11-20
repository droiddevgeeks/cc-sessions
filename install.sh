#!/usr/bin/env bash
# Install cc-sessions: `cc-sessions` + `ccs` on PATH, and the /sessions skill for Claude Code.
#   bash install.sh             install (symlinks, so `git pull` updates everything)
#   bash install.sh --uninstall remove what install created
# Env: BIN_DIR (default ~/.local/bin), CLAUDE_CONFIG_DIR (default ~/.claude)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
SKILL_LINK="$SKILLS_DIR/sessions"

say() { printf '  %s\n' "$*"; }

# Remove a path only if it is a symlink pointing into this repo.
unlink_ours() {
  local p="$1"
  if [ -L "$p" ] && [[ "$(readlink "$p")" == "$REPO"/* ]]; then rm "$p"; say "removed $p"; fi
}

if [ "${1:-}" = "--uninstall" ]; then
  unlink_ours "$BIN_DIR/cc-sessions"
  unlink_ours "$BIN_DIR/ccs"
  unlink_ours "$SKILL_LINK"
  say "uninstalled (your sessions, transcripts and scratch were not touched)"
  exit 0
fi

# Python 3.9+ (str.removeprefix); stdlib only, nothing to pip install.
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "cc-sessions needs python3 >= 3.9 on PATH" >&2
  exit 1
fi

chmod +x "$REPO/cc_sessions.py"
mkdir -p "$BIN_DIR" "$SKILLS_DIR"
for name in cc-sessions ccs; do  # check both first: no half-installs
  target="$BIN_DIR/$name"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "refusing to overwrite $target (not a symlink) — move it away and re-run" >&2
    exit 1
  fi
done
for name in cc-sessions ccs; do
  target="$BIN_DIR/$name"
  ln -sf "$REPO/cc_sessions.py" "$target"
  say "linked $target"
done

# Skill: replace an existing copy only if it's ours (symlink into this repo, or identical content).
if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
  if cmp -s "$SKILL_LINK/SKILL.md" "$REPO/skills/sessions/SKILL.md" && [ "$(ls -A "$SKILL_LINK")" = "SKILL.md" ]; then
    rm -r "$SKILL_LINK"
  else
    backup="$SKILL_LINK.bak.$(date +%Y%m%d%H%M%S)"
    mv "$SKILL_LINK" "$backup"
    say "existing skill moved to $backup"
  fi
fi
ln -sfn "$REPO/skills/sessions" "$SKILL_LINK"
say "linked $SKILL_LINK  (/sessions in Claude Code)"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "NOTE: $BIN_DIR is not on your PATH — add:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
say "done — run: ccs"
