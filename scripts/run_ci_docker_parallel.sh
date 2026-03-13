#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG_BASE="bluesky-httpserver-test:local"
WORKER_COUNT="3"
CHUNK_COUNT=""
PYTHON_VERSIONS="latest"
PYTEST_EXTRA_ARGS=""
ARTIFACTS_DIR="$ROOT_DIR/.docker-test-artifacts"
DOCKER_NETWORK_NAME="bhs-ci-net"
LDAP_CONTAINER_NAME="bhs-ci-ldap"

SUMMARY_TSV=""
SUMMARY_FAIL_LOGS=""
SUMMARY_TXT=""
SUMMARY_JSON=""
TESTS_START_EPOCH=""
TESTS_START_HUMAN=""

SUPPORTED_PYTHON_VERSIONS=("3.10" "3.11" "3.12" "3.13")

usage() {
    cat <<'EOF'
Run bluesky-httpserver unit tests in Docker with dynamic chunk dispatch and optional Python-version matrix.

Usage:
  scripts/run_ci_docker_parallel.sh [options]

Options:
  --workers N, --worker-count N
      Number of concurrent chunk workers (default: 3).

  --chunks N, --chunk-count N
      Number of total chunks/splits to execute per Python version.
      Default: workers * 3.

  --python-versions VALUE
      Python version selection: latest | all | comma-separated list.
      Examples: latest, all, 3.12, 3.11,3.13
      Default: latest (currently 3.13).

  --pytest-args "ARGS"
      Extra arguments passed to pytest in each chunk.
      Example: --pytest-args "-k oidc --maxfail=1"

  --artifacts-dir PATH
      Output directory for all artifacts.
      Default: .docker-test-artifacts under repository root.

  --image-tag TAG
      Base docker image tag. Per-version tags will append -py<VERSION>.
      Default: bluesky-httpserver-test:local

  -h, --help
      Show this help message.

Examples:
  scripts/run_ci_docker_parallel.sh
  scripts/run_ci_docker_parallel.sh --workers 8 --chunks 24
  scripts/run_ci_docker_parallel.sh --python-versions all --workers 8 --chunks 24
  scripts/run_ci_docker_parallel.sh --python-versions 3.11,3.13 --pytest-args "-k test_access_control"
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers|--worker-count)
            WORKER_COUNT="$2"
            shift 2
            ;;
        --chunks|--chunk-count)
            CHUNK_COUNT="$2"
            shift 2
            ;;
        --python-versions)
            PYTHON_VERSIONS="$2"
            shift 2
            ;;
        --pytest-args)
            PYTEST_EXTRA_ARGS="$2"
            shift 2
            ;;
        --artifacts-dir)
            ARTIFACTS_DIR="$2"
            shift 2
            ;;
        --image-tag)
            IMAGE_TAG_BASE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ "$WORKER_COUNT" -lt 1 ]]; then
    echo "WORKER_COUNT must be >= 1" >&2
    exit 2
fi

if [[ -z "$CHUNK_COUNT" ]]; then
    CHUNK_COUNT=$(( WORKER_COUNT * 3 ))
fi

if [[ "$CHUNK_COUNT" -lt 1 ]]; then
    echo "CHUNK_COUNT must be >= 1" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required but not found in PATH" >&2
    exit 2
fi

if ! docker info >/dev/null 2>&1; then
    echo "docker daemon is not available" >&2
    exit 2
fi

normalize_python_versions() {
    local selection="$1"
    local raw
    local normalized=()

    if [[ "$selection" == "latest" ]]; then
        normalized=("3.13")
    elif [[ "$selection" == "all" ]]; then
        normalized=("${SUPPORTED_PYTHON_VERSIONS[@]}")
    else
        raw="${selection//,/ }"
        read -r -a normalized <<< "$raw"
    fi

    if [[ "${#normalized[@]}" -eq 0 ]]; then
        echo "PYTHON_VERSIONS selection produced no versions" >&2
        exit 2
    fi

    for version in "${normalized[@]}"; do
        if [[ ! " ${SUPPORTED_PYTHON_VERSIONS[*]} " =~ " ${version} " ]]; then
            echo "Unsupported Python version '${version}'. Supported: ${SUPPORTED_PYTHON_VERSIONS[*]}" >&2
            exit 2
        fi
    done

    echo "${normalized[@]}"
}

ensure_ldap_image() {
    local image_ref="bitnami/openldap:latest"
    if docker image inspect "$image_ref" >/dev/null 2>&1; then
        return
    fi

    echo "LDAP image $image_ref not found locally; trying docker pull..."
    if docker pull "$image_ref"; then
        return
    fi

    echo "docker pull failed; building bitnami/openldap:latest from source (CI fallback)."
    local workdir="$ROOT_DIR/.docker-test-artifacts/bitnami-containers"
    rm -rf "$workdir"
    git clone --depth 1 https://github.com/bitnami/containers.git "$workdir"
    (cd "$workdir/bitnami/openldap/2.6/debian-12" && docker build -t "$image_ref" .)
}

start_services() {
    ensure_ldap_image

    docker network rm "$DOCKER_NETWORK_NAME" >/dev/null 2>&1 || true
    docker network create "$DOCKER_NETWORK_NAME" >/dev/null

    docker rm -f "$LDAP_CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --rm \
        --name "$LDAP_CONTAINER_NAME" \
        --network "$DOCKER_NETWORK_NAME" \
        -e LDAP_ADMIN_USERNAME=admin \
        -e LDAP_ADMIN_PASSWORD=adminpassword \
        -e LDAP_USERS=user01,user02 \
        -e LDAP_PASSWORDS=password1,password2 \
        bitnami/openldap:latest >/dev/null

    sleep 2
}

stop_services() {
    docker rm -f "$LDAP_CONTAINER_NAME" >/dev/null 2>&1 || true
    docker network rm "$DOCKER_NETWORK_NAME" >/dev/null 2>&1 || true
}

cleanup() {
    stop_services
}

collect_junit_totals() {
    local artifacts_dir="$1"

    python - "$artifacts_dir" <<'PY'
import glob
import os
import sys
import xml.etree.ElementTree as ET

artifacts_dir = sys.argv[1]
tests = failures = errors = files = 0

for path in sorted(glob.glob(os.path.join(artifacts_dir, "junit.*.xml"))):
    files += 1
    try:
        root = ET.parse(path).getroot()
    except Exception:
        continue

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = root.findall("testsuite")
    else:
        suites = []

    for suite in suites:
        tests += int(suite.attrib.get("tests", 0) or 0)
        failures += int(suite.attrib.get("failures", 0) or 0)
        errors += int(suite.attrib.get("errors", 0) or 0)

print(f"{tests} {failures} {errors} {files}")
PY
}

append_summary_row() {
    local py_version="$1"
    local chunks_total="$2"
    local junit_files="$3"
    local tests="$4"
    local failures="$5"
    local errors="$6"
    local status="$7"

    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$py_version" "$chunks_total" "$junit_files" "$tests" "$failures" "$errors" "$status" >> "$SUMMARY_TSV"
}

write_summary_files() {
    local end_epoch end_human elapsed_sec

    if [[ -z "$SUMMARY_TSV" || -z "$SUMMARY_TXT" || -z "$SUMMARY_JSON" ]]; then
        return
    fi

    if [[ ! -f "$SUMMARY_TSV" ]]; then
        return
    fi

    end_epoch="$(date +%s)"
    end_human="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

    if [[ -n "$TESTS_START_EPOCH" ]]; then
        elapsed_sec=$(( end_epoch - TESTS_START_EPOCH ))
    else
        elapsed_sec=0
    fi

    {
        echo "Test Run Summary"
        echo "Start (UTC): ${TESTS_START_HUMAN:-N/A}"
        echo "End (UTC):   $end_human"
        echo "Elapsed:     ${elapsed_sec}s"
        echo
        printf "%-8s %-8s %-7s %-8s %-10s %-8s %-6s\n" \
            "Python" "Status" "Chunks" "JUnit" "Tests" "Failures" "Errors"
        printf "%-8s %-8s %-7s %-8s %-10s %-8s %-6s\n" \
            "------" "------" "------" "-----" "-----" "--------" "------"

        if [[ -s "$SUMMARY_TSV" ]]; then
            while IFS=$'\t' read -r py_version chunks_total junit_files tests failures errors status; do
                printf "%-8s %-8s %-7s %-8s %-10s %-8s %-6s\n" \
                    "$py_version" "$status" "$chunks_total" "$junit_files" "$tests" "$failures" "$errors"
            done < "$SUMMARY_TSV"
        else
            echo "No per-version summary rows were recorded."
        fi

        if [[ -s "$SUMMARY_FAIL_LOGS" ]]; then
            echo
            echo "Failed Chunk Logs"
            cat "$SUMMARY_FAIL_LOGS"
        fi
    } > "$SUMMARY_TXT"

    python - "$SUMMARY_TSV" "$SUMMARY_FAIL_LOGS" "$SUMMARY_JSON" "${TESTS_START_HUMAN:-N/A}" "$end_human" "$elapsed_sec" <<'PY'
import json
import sys

summary_tsv, fail_logs_path, output_path, start_utc, end_utc, elapsed_sec = sys.argv[1:]

rows = []
with open(summary_tsv) as f:
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 7:
            continue
        py_version, chunks_total, junit_files, tests, failures, errors, status = parts
        rows.append(
            {
                "python_version": py_version,
                "status": status,
                "chunks_total": int(chunks_total),
                "junit_files": int(junit_files),
                "tests": int(tests),
                "failures": int(failures),
                "errors": int(errors),
            }
        )

failed_logs = []
with open(fail_logs_path) as f:
    failed_logs = [line.strip() for line in f if line.strip()]

payload = {
    "start_utc": start_utc,
    "end_utc": end_utc,
    "elapsed_seconds": int(elapsed_sec),
    "python_versions": rows,
    "failed_chunk_logs": failed_logs,
}

with open(output_path, "w") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
PY

    echo "==> Test run end time (UTC): $end_human"
    echo "==> Test run elapsed: ${elapsed_sec}s"
    echo "==> Summary written: $SUMMARY_TXT"
    echo "==> Summary JSON:    $SUMMARY_JSON"
}

on_exit() {
    local exit_code=$?
    write_summary_files || true
    cleanup
    trap - EXIT
    exit "$exit_code"
}

trap on_exit EXIT

read -r -a SELECTED_PYTHON_VERSIONS <<< "$(normalize_python_versions "$PYTHON_VERSIONS")"

echo "==> Preparing artifacts directory: $ARTIFACTS_DIR"
rm -rf "$ARTIFACTS_DIR"
mkdir -p "$ARTIFACTS_DIR"

SUMMARY_TSV="$ARTIFACTS_DIR/.summary_rows.tsv"
SUMMARY_FAIL_LOGS="$ARTIFACTS_DIR/.summary_fail_logs.txt"
SUMMARY_TXT="$ARTIFACTS_DIR/summary.txt"
SUMMARY_JSON="$ARTIFACTS_DIR/summary.json"

: > "$SUMMARY_TSV"
: > "$SUMMARY_FAIL_LOGS"

echo "==> Starting shared services (LDAP)"
start_services

TESTS_START_EPOCH="$(date +%s)"
TESTS_START_HUMAN="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "==> Test run start time (UTC): $TESTS_START_HUMAN"
echo "==> Python versions selected: ${SELECTED_PYTHON_VERSIONS[*]}"

run_chunk() {
    local group="$1"
    local log_file="$CURRENT_ARTIFACTS_DIR/shard.${group}.log"

    if docker run --rm \
        --network "$DOCKER_NETWORK_NAME" \
        -e SHARD_GROUP="$group" \
        -e SHARD_COUNT="$CHUNK_COUNT" \
        -e ARTIFACTS_DIR="/artifacts" \
        -e PYTEST_EXTRA_ARGS="$PYTEST_EXTRA_ARGS" \
        -e QSERVER_TEST_LDAP_HOST="$LDAP_CONTAINER_NAME" \
        -e QSERVER_TEST_LDAP_PORT="1389" \
        -e QSERVER_TEST_REDIS_ADDR="localhost" \
        -e QSERVER_HTTP_TEST_BIND_HOST="127.0.0.1" \
        -e QSERVER_HTTP_TEST_HOST="127.0.0.1" \
        -v "$CURRENT_ARTIFACTS_DIR:/artifacts" \
        "$CURRENT_IMAGE_TAG" >"$log_file" 2>&1; then
        : > "$CURRENT_ARTIFACTS_DIR/.status.${group}.ok"
    else
        : > "$CURRENT_ARTIFACTS_DIR/.status.${group}.fail"
        exit 1
    fi
}

export -f run_chunk
export CHUNK_COUNT PYTEST_EXTRA_ARGS DOCKER_NETWORK_NAME LDAP_CONTAINER_NAME

for PYTHON_VERSION in "${SELECTED_PYTHON_VERSIONS[@]}"; do
    CURRENT_IMAGE_TAG="${IMAGE_TAG_BASE}-py${PYTHON_VERSION}"
    CURRENT_ARTIFACTS_DIR="$ARTIFACTS_DIR/py${PYTHON_VERSION}"
    export CURRENT_IMAGE_TAG CURRENT_ARTIFACTS_DIR

    echo "==> Building test image: $CURRENT_IMAGE_TAG (Python $PYTHON_VERSION)"
    docker build \
        --build-arg PYTHON_VERSION="$PYTHON_VERSION" \
        -f "$ROOT_DIR/docker/test.Dockerfile" \
        -t "$CURRENT_IMAGE_TAG" \
        "$ROOT_DIR"

    mkdir -p "$CURRENT_ARTIFACTS_DIR"

    echo "==> [Python $PYTHON_VERSION] Starting dynamic dispatch: $WORKER_COUNT workers over $CHUNK_COUNT chunks"
    if ! seq 1 "$CHUNK_COUNT" | xargs -P "$WORKER_COUNT" -I {} bash -lc 'run_chunk "$1"' _ {}; then
        echo "One or more chunks failed for Python $PYTHON_VERSION." >&2
        read -r TOTAL_TESTS TOTAL_FAILURES TOTAL_ERRORS TOTAL_JUNIT_FILES < <(collect_junit_totals "$CURRENT_ARTIFACTS_DIR")
        for group in $(seq 1 "$CHUNK_COUNT"); do
            if [[ -f "$CURRENT_ARTIFACTS_DIR/.status.${group}.fail" ]]; then
                echo "Chunk $group failed. Log: $CURRENT_ARTIFACTS_DIR/shard.${group}.log" >&2
                echo "$CURRENT_ARTIFACTS_DIR/shard.${group}.log" >> "$SUMMARY_FAIL_LOGS"
            fi
        done
        append_summary_row "py${PYTHON_VERSION}" "$CHUNK_COUNT" "$TOTAL_JUNIT_FILES" \
            "$TOTAL_TESTS" "$TOTAL_FAILURES" "$TOTAL_ERRORS" "FAIL"
        exit 1
    fi

    for group in $(seq 1 "$CHUNK_COUNT"); do
        if [[ -f "$CURRENT_ARTIFACTS_DIR/.status.${group}.ok" ]]; then
            echo "[Python $PYTHON_VERSION] Chunk $group completed successfully"
        fi
    done

    rm -f "$CURRENT_ARTIFACTS_DIR"/.status.*.ok "$CURRENT_ARTIFACTS_DIR"/.status.*.fail

    echo "==> [Python $PYTHON_VERSION] Merging coverage artifacts"
    docker run --rm \
        --entrypoint bash \
        -v "$CURRENT_ARTIFACTS_DIR:/artifacts" \
        "$CURRENT_IMAGE_TAG" \
        -lc "set -euo pipefail; \
             python -m coverage combine /artifacts/.coverage.* && \
             python -m coverage xml -o /artifacts/coverage.xml && \
             python -m coverage report -m > /artifacts/coverage.txt"

    if [[ "${#SELECTED_PYTHON_VERSIONS[@]}" -eq 1 ]]; then
        cp "$CURRENT_ARTIFACTS_DIR/coverage.xml" "$ROOT_DIR/coverage.xml"
    else
        cp "$CURRENT_ARTIFACTS_DIR/coverage.xml" "$ROOT_DIR/coverage.py${PYTHON_VERSION}.xml"
    fi

    read -r TOTAL_TESTS TOTAL_FAILURES TOTAL_ERRORS TOTAL_JUNIT_FILES < <(collect_junit_totals "$CURRENT_ARTIFACTS_DIR")
    echo "==> [Python $PYTHON_VERSION] JUnit summary: tests=$TOTAL_TESTS failures=$TOTAL_FAILURES errors=$TOTAL_ERRORS files=$TOTAL_JUNIT_FILES"

    VERSION_STATUS="PASS"
    if [[ "$TOTAL_FAILURES" -gt 0 || "$TOTAL_ERRORS" -gt 0 ]]; then
        VERSION_STATUS="FAIL"
    fi

    append_summary_row "py${PYTHON_VERSION}" "$CHUNK_COUNT" "$TOTAL_JUNIT_FILES" \
        "$TOTAL_TESTS" "$TOTAL_FAILURES" "$TOTAL_ERRORS" "$VERSION_STATUS"
done

echo "==> Completed. Artifacts:"
echo "    versioned logs      : $ARTIFACTS_DIR/py<VERSION>/shard.<N>.log"
echo "    versioned junit     : $ARTIFACTS_DIR/py<VERSION>/junit.<N>.xml"
echo "    versioned coverage  : $ARTIFACTS_DIR/py<VERSION>/{coverage.txt,coverage.xml}"
echo "    run summary         : $ARTIFACTS_DIR/{summary.txt,summary.json}"

if [[ "${#SELECTED_PYTHON_VERSIONS[@]}" -eq 1 ]]; then
    echo "    root coverage xml   : $ROOT_DIR/coverage.xml"
else
    echo "    root coverage xmls  : $ROOT_DIR/coverage.py<VERSION>.xml"
fi
