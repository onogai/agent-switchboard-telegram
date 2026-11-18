#!/usr/bin/env bash
# No production state, network or credentials are needed.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker build -t switchboard-telegram-test:local .
docker run --rm --network none --entrypoint python \
  --mount "type=bind,src=$PWD/tests,dst=/app/tests,readonly" \
  switchboard-telegram-test:local \
  -m unittest discover -s /app/tests -p 'test_*.py' -v
