#!/usr/bin/env bash
# Link aicn (and vendored stage skills that are not installed yet) into the agent skill folders.
# Never overwrites an existing folder or link.
set -euo pipefail
SK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for dest in "$HOME/.claude/skills" "$HOME/.gemini/config/skills"; do
  mkdir -p "$dest"
  if [ -e "$dest/aicn" ] || [ -L "$dest/aicn" ]; then
    echo "keep   $dest/aicn"
  else
    ln -s "$SK" "$dest/aicn" && echo "link   $dest/aicn"
  fi
  for v in "$SK"/vendor/*/; do
    name="$(basename "$v")"
    if [ -e "$dest/$name" ]; then
      echo "keep   $dest/$name"
    else
      ln -s "${v%/}" "$dest/$name" && echo "link   $dest/$name"
    fi
  done
done
python3 "$SK/scripts/aicn.py" --help >/dev/null && echo "ok     aicn.py runs"
