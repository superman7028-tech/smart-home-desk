#!/bin/zsh
# Add one smart-home post. Launchd runs this at 00:00, 06:00, 12:00, and 18:00.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs data docs/posts
exec /opt/homebrew/bin/python3 "$ROOT/scripts/generate_post.py" "$@"
