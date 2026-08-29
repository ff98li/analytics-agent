#!/bin/bash
# Launch one service from an explicit 0600 allowlist environment file.
set -euo pipefail

supervise_child=false
if [ "${1:-}" = "--supervise-child" ]; then
    supervise_child=true
    shift
fi

env_file="$1"
working_dir="$2"
shift 2

[ -r "$env_file" ] || { echo "missing service env file: $env_file" >&2; exit 1; }
case "$env_file" in
    "${STACK_RUNTIME_DIR:-/nonexistent}"/service-env/*.env) ;;
    *) echo "refusing service env outside runtime directory: $env_file" >&2; exit 1 ;;
esac

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
cd "$working_dir"

if [ "$supervise_child" = true ]; then
    # Some daemons (notably Redis 5) reuse argv/environ storage for their
    # process title, erasing the deployment marker from /proc/$pid/environ.
    # Keep this minimal shell as the tracked process while the daemon remains
    # its direct child in the same setsid-created process group.
    child_pid=""
    forward_signal() {
        local signal_name="$1" signal_rc="$2"
        trap - TERM INT
        [ -z "$child_pid" ] || kill -s "$signal_name" "$child_pid" 2>/dev/null || true
        [ -z "$child_pid" ] || wait "$child_pid" 2>/dev/null || true
        exit "$signal_rc"
    }
    trap 'forward_signal TERM 143' TERM
    trap 'forward_signal INT 130' INT
    "$@" &
    child_pid=$!
    set +e
    wait "$child_pid"
    child_rc=$?
    set -e
    trap - TERM INT
    exit "$child_rc"
fi

exec "$@"
