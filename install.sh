#!/usr/bin/env bash
# Install AICN v3.1+ as a native Antigravity plugin (~/.gemini/config/plugins/aicn),
# sync the 7 user-invocable aicn-* skills into ~/.claude/skills/, and retire legacy loose skill folders.
set -euo pipefail
SK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MODE="link"
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --sync) MODE="sync" ;;
    --force) FORCE=1 ;;
  esac
done

RETIRED_FILES=(
  "apply_channel_branding.py"
  "render_vertical_9x16.py"
  "split_compiled_episodes.py"
  "assemble_master_audio.py"
  "synthesize_and_audit_pacing.py"
)

copy_clean_tree() {
  local src="$1" dst="$2"
  rm -rf "$dst"
  mkdir -p "$dst"
  (cd "$src" && tar --exclude='.git' --exclude='__pycache__' --exclude='.cache' --exclude='.pytest_cache' --exclude='*.pyc' --exclude='.aicn-managed' --exclude='SPEC_V3.md' -cf - .) | (cd "$dst" && tar -xf -)
  for rf in "${RETIRED_FILES[@]}"; do
    find "$dst" -type f -name "$rf" -delete 2>/dev/null || true
  done
  printf '%s\n' "$src" > "$dst/.aicn-managed"
}

sync_dir() {
  local src="$1" dst="$2"
  local src_real dst_real=""
  src_real="$(cd "$src" && pwd -P)"
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    dst_real="$(cd "$dst" && pwd -P)"
  elif [ -d "$(dirname "$dst")" ]; then
    dst_real="$(cd "$(dirname "$dst")" && pwd -P)/$(basename "$dst")"
  fi
  if [ "$src_real" = "$dst_real" ]; then
    echo "self   $dst"
    return
  fi
  if [ "$MODE" = "sync" ]; then
    if [ -L "$dst" ]; then
      rm -f "$dst"
      copy_clean_tree "$src" "$dst"
      echo "upgrade $dst (symlink -> real dir)"
    elif [ -d "$dst" ]; then
      if [ -f "$dst/.aicn-managed" ] || [ "$FORCE" -eq 1 ]; then
        copy_clean_tree "$src" "$dst"
        echo "sync   $dst"
      else
        echo "keep   $dst (unmanaged; pass --force to overwrite)"
      fi
    else
      copy_clean_tree "$src" "$dst"
      echo "copy   $dst"
    fi
  else
    if [ -e "$dst" ] || [ -L "$dst" ]; then
      echo "keep   $dst"
    else
      ln -s "$src" "$dst" && echo "link   $dst"
    fi
  fi
}

retire_legacy_dir() {
  local d="$1"
  [ -e "$d" ] || [ -L "$d" ] || return 0
  if [ -L "$d" ]; then
    rm -f "$d"
    echo "retire $d (unlinked legacy symlink)"
    return 0
  fi
  local d_real
  d_real="$(cd "$d" && pwd -P)"
  if [ "$d_real" = "$SK" ]; then
    echo "skip   $d (active source repo)"
    return 0
  fi
  if [ -f "$d/.aicn-managed" ] || [ "$FORCE" -eq 1 ]; then
    rm -rf "$d"
    echo "retire $d"
  fi
}

AGY_PLUGIN="$HOME/.gemini/config/plugins/aicn"
mkdir -p "$(dirname "$AGY_PLUGIN")"
sync_dir "$SK" "$AGY_PLUGIN"

RETIRED_LOOSE=(
  "aicn"
  "video-localization-orchestrator"
  "video-asr-visual-audit"
  "zh-vi-script-adapter"
  "vieneu-voice-pacing-mixer"
  "video-localization-qa-review"
)
for root in "$HOME/.gemini/config/skills" "$HOME/.claude/skills"; do
  for old in "${RETIRED_LOOSE[@]}"; do
    retire_legacy_dir "$root/$old"
  done
done

mkdir -p "$HOME/.claude/skills"
for s in "$AGY_PLUGIN"/skills/*/; do
  name="$(basename "$s")"
  sync_dir "${s%/}" "$HOME/.claude/skills/$name"
done

PY_BIN="${AICN_PY:-python3}"
"$PY_BIN" "$AGY_PLUGIN/scripts/aicn.py" --help >/dev/null && echo "ok     aicn.py runs"
