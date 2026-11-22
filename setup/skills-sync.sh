#!/bin/bash
# One home for your agent skills, shared by every tool and every device (macOS and Linux).
#
#   skills-sync.sh [STORE]
#
# STORE is the folder that holds the skills, one folder per skill with a SKILL.md inside. Default:
# $SWITCHBOARD_SKILLS, else <root>/agent-skills, where <root> is $SWITCHBOARD_ROOT or the folder this checkout
# sits in. Put STORE inside the folder you sync between devices and a skill edited on one device reaches the others.
#
# What it does, each time it runs:
# - ~/.agents/skills becomes a link to STORE (Codex and Cursor read that folder directly).
# - A skill created in a tool's own folder (~/.claude/skills, ~/.codex/skills, ~/.cursor/skills) moves into STORE
#   once it has not changed for two minutes. A same-named folder that differs is left alone and logged.
# - Claude Code gets one link per shared skill in ~/.claude/skills; links to deleted skills are removed.
# Run it after creating or installing a skill, or on a timer. Safe to run at any time.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${SWITCHBOARD_ROOT:-$(cd "$HERE/../.." && pwd)}"
STORE="${1:-${SWITCHBOARD_SKILLS:-$ROOT/agent-skills}}"
SKILLS="$HOME/.agents/skills"
CLAUDE="$HOME/.claude/skills"
CODEX="$HOME/.codex/skills"
CURSOR="$HOME/.cursor/skills"
LOG="${SWITCHBOARD_SKILLS_LOG:-$HOME/.switchboard/skills-sync.log}"
SETTLE_MINUTES=2

log() {
  mkdir -p "${LOG%/*}"
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"
}

# Left where they are: hidden and app-managed folders.
stays_put() {
  case "$1" in
    .*|synced) return 0 ;;
  esac
  return 1
}

if [ ! -d "$STORE" ]; then
  echo "skills-sync: no skills folder at $STORE (create it, or pass its path)" >&2
  exit 0
fi
mkdir -p "$HOME/.agents"
if [ -e "$SKILLS" ] && [ ! -L "$SKILLS" ]; then
  echo "skills-sync: $SKILLS exists and is not a link; move its skills into $STORE first" >&2
  exit 1
fi
[ -e "$SKILLS" ] || ln -s "$STORE" "$SKILLS"
mkdir -p "$CLAUDE" "$CODEX" "$CURSOR"

# Bring new skills from each tool's folder into the shared home.
for source in "$CLAUDE" "$CODEX" "$CURSOR"; do
  for dir in "$source"/*/; do
    dir="${dir%/}"
    name="${dir##*/}"
    [ -L "$dir" ] && continue
    [ -f "$dir/SKILL.md" ] || continue
    stays_put "$name" && continue
    [ -n "$(find "$dir" -mmin "-$SETTLE_MINUTES" -print -quit)" ] && continue

    home="$SKILLS/$name"
    if [ ! -e "$home" ]; then
      mv "$dir" "$home" && log "moved $dir to $home"
    elif diff -r "$dir" "$home" >/dev/null 2>&1; then
      rm -rf "$dir" && log "removed identical copy $dir"
    elif ! grep -qF "conflict: $dir " "$LOG" 2>/dev/null; then
      log "conflict: $dir differs from $home; left in place"
    fi
  done
done

# Give Claude Code a link to every shared skill.
for dir in "$SKILLS"/*/; do
  dir="${dir%/}"
  [ -f "$dir/SKILL.md" ] || continue
  link="$CLAUDE/${dir##*/}"
  if [ -L "$link" ]; then
    case "$(readlink "$link")" in
      "$dir") ;;
      "$CODEX"/*) ln -sfn "$dir" "$link" && log "repointed $link" ;;
    esac
  elif [ ! -e "$link" ]; then
    ln -s "$dir" "$link" && log "linked $link"
  fi
done

# Drop Claude links whose shared skill was deleted.
for link in "$CLAUDE"/*; do
  [ -L "$link" ] && [ ! -e "$link" ] || continue
  case "$(readlink "$link")" in
    "$SKILLS"/*) rm "$link" && log "removed dangling $link" ;;
  esac
done
