#!/bin/bash
#SBATCH --job-name=lumilake-stack
#SBATCH --partition=gpu-long
#SBATCH --gres=gpu:a100-40:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=48G
#SBATCH --time=3-00:00:00
#SBATCH --signal=B:TERM@600
#SBATCH --output=08-lumilake-stack-%j.out
#SBATCH --error=08-lumilake-stack-%j.err

# Single-node stack with authenticated loopback services. Slurm normally shares
# the node network namespace, so loopback alone is never treated as isolation.
#
# One-hour integration jobs can submit this same file with --time=01:00:00 and
# --export=ALL,STACK_TEST_MODE=1. Test mode starts, runs L2 + E2E + an atomic
# checkpoint, then exits cleanly so Slurm records COMPLETED (no scancel needed).

set -Eeuo pipefail
STACK_DIR="${STACK_DIR:-$HOME/lakehouse/stack}"
# shellcheck disable=SC1090
source "$STACK_DIR/stack-env.sh"
# shellcheck disable=SC1090
source "$STACK_DIR/process-lib.sh"
# shellcheck disable=SC1090
source "$STACK_DIR/checkpoint.sh"

mkdir -p "$STACK_CHECKPOINT_ROOT" "$STACK_LOG_ARCHIVE_ROOT"
chmod 700 "$STACK_CHECKPOINT_ROOT" "$STACK_LOG_ARCHIVE_ROOT"

cleanup_started=0
cleanup() {
    local original_rc="$1" cleanup_rc=0 archive
    if [ "$cleanup_started" -eq 1 ]; then
        exit "$original_rc"
    fi
    cleanup_started=1
    trap - EXIT TERM INT
    set +e

    echo "=== cleanup deployment=$STACK_DEPLOYMENT_ID rc=$original_rc $(date -Iseconds) ==="

    # Quiesce all writers first. If this cannot be confirmed, do not advertise
    # a potentially racing final checkpoint as last-good.
    if ! bash "$STACK_DIR/stack-down.sh" --writers-only; then
        cleanup_rc=1
    elif pg_ctl -D "$STACK_PGDATA" status >/dev/null 2>&1 && tracked_process_alive minio; then
        checkpoint_create final || cleanup_rc=1
    else
        echo "[cleanup] stores unavailable; final checkpoint skipped" >&2
        cleanup_rc=1
    fi

    bash "$STACK_DIR/stack-down.sh" --stores-only || cleanup_rc=1

    archive="$STACK_LOG_ARCHIVE_ROOT/stack-logs-${SLURM_JOB_ID:-manual}-$(hostname)"
    mkdir -p "$archive"
    chmod 700 "$archive"
    cp -a "$STACK_ROOT/logs/." "$archive/" || cleanup_rc=1
    printf '%s\n' \
        "deployment=$STACK_DEPLOYMENT_ID" \
        "slurm_job_id=${SLURM_JOB_ID:-manual}" \
        "original_exit_code=$original_rc" \
        "cleanup_exit_code=$cleanup_rc" >"$archive/cleanup-summary.txt"
    chmod 600 "$archive/cleanup-summary.txt"

    # Preserve any original failure. A cleanup/checkpoint failure turns an
    # otherwise-successful run into failure instead of being swallowed.
    if [ "$original_rc" -ne 0 ]; then
        exit "$original_rc"
    fi
    exit "$cleanup_rc"
}

on_signal() {
    local signal_name="$1" signal_rc="$2"
    echo "=== received $signal_name; beginning early cleanup ===" >&2
    trap - TERM INT
    exit "$signal_rc"
}

trap 'cleanup "$?"' EXIT
trap 'on_signal TERM 143' TERM
trap 'on_signal INT 130' INT

bash "$STACK_DIR/stack-up.sh"
bash "$STACK_DIR/health.sh" --level 2

if [ "${STACK_TEST_MODE:-${STACK_ONESHOT:-0}}" = "1" ]; then
    echo "=== STACK_TEST_MODE: running state-changing smoke ==="
    bash "$STACK_DIR/e2e-smoke.sh"
    if [ "${STACK_TEST_RESTORE:-1}" = "1" ]; then
        echo "=== STACK_TEST_MODE: quiesced checkpoint + in-allocation restore rehearsal ==="
        bash "$STACK_DIR/stack-down.sh" --writers-only
        checkpoint_create test
        checkpoint_validate_latest
        bash "$STACK_DIR/stack-down.sh" --stores-only
        rm -f "$STACK_RUNTIME_DIR/database.ready" "$STACK_RUNTIME_DIR/restore.complete"
        bash "$STACK_DIR/stack-up.sh"
        bash "$STACK_DIR/health.sh" --level 2
        bash "$STACK_DIR/e2e-smoke.sh"
    else
        checkpoint_create test-best-effort
        checkpoint_validate_latest
    fi
    echo "=== STACK_TEST_MODE_OK; normal EXIT cleanup will test final checkpoint + teardown ==="
    exit 0
fi

echo "=== stack up; entering monitored keepalive loop ==="
last_checkpoint="$(date +%s)"
last_deep_health="$last_checkpoint"
consecutive_health_failures=0

while true; do
    sleep 60
    now="$(date +%s)"

    if ! required_stack_processes_alive; then
        echo "required stack process lost; exiting" >&2
        exit 1
    fi

    if [ $((now - last_deep_health)) -ge 300 ]; then
        if bash "$STACK_DIR/health.sh" --level 2; then
            consecutive_health_failures=0
        else
            consecutive_health_failures=$((consecutive_health_failures + 1))
            echo "deep health failure $consecutive_health_failures/3" >&2
            if [ "$consecutive_health_failures" -ge 3 ]; then
                exit 1
            fi
        fi
        last_deep_health="$now"
    fi

    if [ $((now - last_checkpoint)) -ge 3600 ]; then
        if checkpoint_create periodic; then
            last_checkpoint="$now"
        else
            # Keep the old last-good and retry next minute; three health failures
            # still terminate the allocation if dependencies are unhealthy.
            echo "periodic checkpoint failed; last-good unchanged" >&2
        fi
    fi
done
