#!/usr/bin/env bash
# Run every practice kitchen from the root of the starter repo.
set -u
for level in levels/*/*.yaml; do
  echo "== $level"
  uv run cook agent --level "$level" 2>&1 | grep -Ei "score|delivered|completed" | tail -3
done
