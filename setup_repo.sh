#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
for d in forge/library targets evidence vault ledgers desk prompts; do mkdir -p "$d"; done
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git repo; run: git init -b main"; exit 1; }
git add -A
git commit -m "[ops] MATTevo repo bootstrap" --allow-empty
git status --short; git log --oneline -n 3
