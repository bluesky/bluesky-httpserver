#!/usr/bin/env bash
set -euo pipefail

SHARD_GROUP="${SHARD_GROUP:-1}"
SHARD_COUNT="${SHARD_COUNT:-3}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-/artifacts}"
PYTEST_EXTRA_ARGS="${PYTEST_EXTRA_ARGS:-}"

mkdir -p "$ARTIFACTS_DIR"

if [[ "$SHARD_GROUP" -lt 1 || "$SHARD_COUNT" -lt 1 || "$SHARD_GROUP" -gt "$SHARD_COUNT" ]]; then
    echo "Invalid shard settings: SHARD_GROUP=$SHARD_GROUP SHARD_COUNT=$SHARD_COUNT" >&2
    exit 2
fi

export COVERAGE_FILE="$ARTIFACTS_DIR/.coverage.${SHARD_GROUP}"

redis-server --save "" --appendonly no --daemonize yes
for _ in $(seq 1 50); do
    if redis-cli ping >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

if ! redis-cli ping >/dev/null 2>&1; then
    echo "Failed to start redis-server inside container" >&2
    exit 2
fi

mapfile -t shard_tests < <(
    python - <<'PY' "$SHARD_GROUP" "$SHARD_COUNT"
import glob
import sys

group = int(sys.argv[1])
count = int(sys.argv[2])

tests = sorted(glob.glob("bluesky_httpserver/tests/test_*.py"))
selected = [path for idx, path in enumerate(tests) if idx % count == (group - 1)]

for path in selected:
    print(path)
PY
)

if [[ "${#shard_tests[@]}" -eq 0 ]]; then
    echo "No tests selected for shard ${SHARD_GROUP}/${SHARD_COUNT}; treating as success."
    exit 0
fi

pytest_cmd=(
    coverage
    run
    -m
    pytest
    --junitxml="$ARTIFACTS_DIR/junit.${SHARD_GROUP}.xml"
    -vv
)

if [[ -n "$PYTEST_EXTRA_ARGS" ]]; then
    read -r -a extra_args <<< "$PYTEST_EXTRA_ARGS"
    pytest_cmd+=("${extra_args[@]}")
fi

pytest_cmd+=("${shard_tests[@]}")

set +e
"${pytest_cmd[@]}"
test_status=$?
set -e

if [[ "$test_status" -eq 5 ]]; then
    echo "Pytest collected no tests for shard ${SHARD_GROUP}/${SHARD_COUNT}; treating as success."
    test_status=0
fi

redis-cli shutdown nosave >/dev/null 2>&1 || true

exit "$test_status"
