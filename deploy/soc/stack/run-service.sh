#!/bin/bash
# Launch one service from an explicit 0600 allowlist environment file.
set -euo pipefail

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
exec "$@"
