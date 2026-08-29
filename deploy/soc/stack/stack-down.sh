#!/bin/bash
# Exact, idempotent teardown. Never searches by command substring.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stack-env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/process-lib.sh"

mode="${1:---all}"
case "$mode" in
    --all|--writers-only|--stores-only) ;;
    *) echo "usage: bash stack-down.sh [--all|--writers-only|--stores-only]" >&2; exit 2 ;;
esac

log() { echo "[stack-down] $*"; }
rc=0

stop_one() {
    local name="$1" timeout="${2:-20}"
    stop_tracked_service "$name" "$timeout" || {
        log "failed to confirm $name stopped"
        rc=1
    }
}

stop_writers() {
    # Stop admission first, then FlowMesh + native workers while gateway and
    # Redis remain available for final result writes. Gateway stops only after
    # workers are confirmed gone, before the DB/S3 checkpoint cut.
    stop_one lumilake 30
    stop_one flowmesh 45
    stop_one gateway 20
    if [ "$rc" -eq 0 ]; then
        printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$STACK_RUNTIME_DIR/writers.stopped"
        chmod 600 "$STACK_RUNTIME_DIR/writers.stopped"
    else
        # A checkpoint must never infer quiescence from a stale marker when
        # any writer survived or its ownership could not be verified.
        rm -f "$STACK_RUNTIME_DIR/writers.stopped"
    fi
}

redis_cli() {
    env -i "HOME=$HOME" "PATH=$PATH" \
        "REDISCLI_AUTH=$STACK_REDIS_PASSWORD" redis-cli \
        --no-auth-warning --user "$STACK_REDIS_USER" \
        -h 127.0.0.1 -p "$STACK_REDIS_PORT" "$@"
}

stop_stores() {
    # Redis is deliberately not checkpointed. Authenticated SHUTDOWN avoids
    # addressing another job's listener; the tracked-process fallback is exact.
    if tracked_process_alive redis; then
        redis_cli shutdown nosave >/dev/null 2>&1 || true
    fi
    stop_one redis 10
    stop_one minio 20

    if [ -f "$STACK_PGDATA/PG_VERSION" ]; then
        if pg_ctl -D "$STACK_PGDATA" status >/dev/null 2>&1; then
            pg_ctl -D "$STACK_PGDATA" stop -m fast -t 30 >/dev/null 2>&1 || {
                log "PostgreSQL did not stop cleanly"
                rc=1
            }
        fi
    fi
}

case "$mode" in
    --writers-only) stop_writers ;;
    --stores-only) stop_stores ;;
    --all) stop_writers; stop_stores ;;
esac

if [ "$rc" -eq 0 ]; then
    log "STACK_DOWN_OK mode=$mode deployment=$STACK_DEPLOYMENT_ID"
else
    log "STACK_DOWN_INCOMPLETE mode=$mode deployment=$STACK_DEPLOYMENT_ID"
fi
exit "$rc"
