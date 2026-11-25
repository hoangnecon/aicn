#!/usr/bin/env bash
# Link or sync aicn (and vendored stage skills) into agent skill folders.
# Pass --sync [--force] to refresh managed directory copies and prune retired v1.x/v2.x files;
# default mode links missing folders without overwriting existing directories/links.
set -euo pipefail
SK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
  (cd "$src" && tar --exclude='.git' --exclude='__pycache__' --exclude='.cache' --exclude='*.pyc' --exclude='.aicn-managed' --exclude='SPEC_V3.md' -cf - .) | (cd "$dst" && tar -xf -)
  for rf in "${RETIRED_FILES[@]}"; do
    find "$dst" -type f -name "$rf" -delete 2>/dev/null || true
  done
  printf '%s\n' "$src" > "$dst/.aicn-managed"
}

sync_skill() {
  local src="$1" dst="$2"
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

for dest in "$HOME/.claude/skills" "$HOME/.gemini/config/skills"; do
  mkdir -p "$dest"
  sync_skill "$SK" "$dest/aicn"
  for v in "$SK"/vendor/*/; do
    name="$(basename "$v")"
    sync_skill "${v%/}" "$dest/$name"
  done
done
python3 "$SK/scripts/aicn.py" --help >/dev/null && echo "ok     aicn.py runs"
