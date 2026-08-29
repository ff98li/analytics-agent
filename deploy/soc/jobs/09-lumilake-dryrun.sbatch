#!/bin/bash
#SBATCH --job-name=lumilake-dryrun
#SBATCH --partition=normal
#SBATCH --cpus-per-task=8
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=09-lumilake-dryrun-%j.out
#SBATCH --error=09-lumilake-dryrun-%j.err

# Dry-run the Lumilake server + CLI preview WITHOUT the full stack:
# validates server startup, env contract, template compilation, and the
# preview path. FlowMesh and lumid-gateway are intentionally not started. A
# preview may therefore either succeed or reach the one expected FlowMesh
# connection failure; parse/validation/API failures remain fatal.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$PATH"
source "$HOME/lakehouse/stack/stack-env.sh"
# Keep the CLI pointed at the same (possibly overridden) port as this server.
export LUMILAKE_BASE_URL="http://127.0.0.1:${STACK_LUMILAKE_PORT}"

cd "$HOME/src/Lumilake"

SERVER_PID=""
SERVER_LOG="$HOME/slurm/dryrun-server-${SLURM_JOB_ID:-$$}.log"

cleanup() {
    local rc=$?
    trap - EXIT TERM INT
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        for _ in $(seq 1 15); do
            if ! kill -0 "$SERVER_PID" 2>/dev/null; then
                break
            fi
            # A dead-but-not-yet-waited child is a zombie; it is safe to reap.
            if ps -o stat= -p "$SERVER_PID" 2>/dev/null | grep -q '^Z'; then
                break
            fi
            sleep 1
        done
        if kill -0 "$SERVER_PID" 2>/dev/null && \
           ! ps -o stat= -p "$SERVER_PID" 2>/dev/null | grep -q '^Z'; then
            kill -KILL "$SERVER_PID" 2>/dev/null || true
        fi
    fi
    if [ -n "$SERVER_PID" ]; then
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

run_preview() { # label  line-count  log-file  command...
    local label="$1"
    local lines="$2"
    local output="$3"
    shift 3

    local rc=0
    if "$@" >"$output" 2>&1; then
        tail -n "$lines" "$output"
        echo "${label}_OK"
        return 0
    else
        rc=$?
    fi
    tail -n "$lines" "$output"

    # Both templates compile before Lumilake asks the absent FlowMesh server
    # for workers. Only that exact dependency failure is expected here.
    if grep -Fq 'schedule preview failed: Failed to connect to ' "$output" && \
       grep -Fq '/api/v1/workers: All connection attempts failed' "$output"; then
        echo "${label}_EXPECTED_FLOWMESH_UNAVAILABLE rc=$rc"
        return 0
    fi

    echo "${label}_FAILED rc=$rc (full output: $output)" >&2
    return "$rc"
}

# Server smoke: start and wait for process liveness. Dependency-aware
# /readyz (/healthz alias) is expected to be 503 because this dry-run
# intentionally omits FlowMesh and lumid-gateway.
mkdir -p "$HOME/slurm"
uv run env PYTHONPATH=src python -m lumilake_server >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

UP=0
for _ in $(seq 1 90); do
    if curl --connect-timeout 2 --max-time 10 -fsS \
        "http://127.0.0.1:$STACK_LUMILAKE_PORT/livez" >/dev/null 2>&1; then
        UP=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "SERVER_EXITED_EARLY"
        tail -30 "$SERVER_LOG"
        exit 1
    fi
    sleep 1
done
if [ "$UP" != "1" ]; then
    echo "SERVER_LIVEZ_TIMEOUT"
    tail -40 "$SERVER_LOG"
    exit 1
fi
echo "SERVER_LIVEZ_OK"

echo "=== preview: trading-agent (Q2) ==="
run_preview PREVIEW_Q2 30 \
    "$HOME/slurm/dryrun-preview-q2-${SLURM_JOB_ID:-$$}.log" \
    uv run lumilake job preview --format yaml \
    examples/templates/yaml/trading-agent.yaml --input Stock=AAPL

echo "=== preview: smoke-sql ==="
run_preview PREVIEW_SMOKE 30 \
    "$HOME/slurm/dryrun-preview-smoke-${SLURM_JOB_ID:-$$}.log" \
    uv run lumilake job preview --format yaml \
    "$HOME/lakehouse/workflows/smoke-sql.yaml" --input Stock=AAPL

# job list requires the omitted lumid-gateway-backed persistent job store. The
# historical command always returned HTTP 500, so claiming it as a dry-run
# check was misleading; it belongs in the full-stack smoke test instead.
echo "=== job list skipped (requires full-stack lumid-gateway) ==="

echo "=== DRYRUN_DONE ==="
