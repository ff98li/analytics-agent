#!/bin/bash
# Offline regression checks; does not contact Slurm or start data services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_root="$(mktemp -d /tmp/cp5105-stack-selftest.XXXXXX)"
test_child_pid=""
cleanup() {
    if [ -n "$test_child_pid" ]; then
        kill "$test_child_pid" 2>/dev/null || true
        wait "$test_child_pid" 2>/dev/null || true
    fi
    case "$test_root" in
        /tmp/cp5105-stack-selftest.*) rm -rf -- "$test_root" ;;
        *) echo "refusing unsafe self-test cleanup target: $test_root" >&2 ;;
    esac
}
trap cleanup EXIT

for file in "$SCRIPT_DIR"/*.sh "$SCRIPT_DIR"/*.sbatch; do
    bash -n "$file"
done

if grep -En 'set -x|pkill[[:space:]]+-f|auth=trust|devtoken|lumilake_password' \
    "$SCRIPT_DIR/stack-env.sh" "$SCRIPT_DIR/stack-up.sh" \
    "$SCRIPT_DIR/stack-down.sh" "$SCRIPT_DIR/health.sh" \
    "$SCRIPT_DIR/checkpoint.sh" "$SCRIPT_DIR/slurm-stack.sbatch"; then
    echo "unsafe legacy pattern found" >&2
    exit 1
fi

STACK_ROOT="$test_root/deployment-a"
STACK_DEPLOYMENT_ID=selftest-a
export STACK_ROOT STACK_DEPLOYMENT_ID
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stack-env.sh"
first_pg_password="$STACK_PGPASSWORD"
first_flowmesh_key="$FLOWMESH_API_KEY"
first_lumilake_key="$LUMILAKE_SERVER_API_KEY"
[ -n "$first_pg_password" ]
[ -n "$first_flowmesh_key" ]
[ -n "$first_lumilake_key" ]
[ "$first_flowmesh_key" = "$first_lumilake_key" ]
[ "$MINIO_APP_PASSWORD" != "$MINIO_ROOT_PASSWORD" ]
case "$STACK_REDIS_AUTH_MODE" in acl|password) ;; *) exit 1 ;; esac
case "$REDIS_URL" in redis://*"$STACK_REDIS_PASSWORD"@127.0.0.1:*) ;; *) exit 1 ;; esac
mode="$(if stat -c '%a' "$STACK_SECRETS_FILE" >/dev/null 2>&1; then stat -c '%a' "$STACK_SECRETS_FILE"; else stat -f '%Lp' "$STACK_SECRETS_FILE"; fi)"
[ "$mode" = "600" ]
openssl verify -CAfile "$SERVER_GRPC_TLS_CA_FILE" "$SERVER_GRPC_TLS_CERT_FILE" >/dev/null

# Re-sourcing the same deployment must preserve credentials.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stack-env.sh"
[ "$STACK_PGPASSWORD" = "$first_pg_password" ]
[ "$FLOWMESH_API_KEY" = "$first_flowmesh_key" ]

# A different deployment must not reuse credentials.
second_pg_password="$(
    env -u STACK_RUNTIME_DIR -u STACK_SECRETS_FILE -u STACK_GRPC_TLS_DIR \
        STACK_ROOT="$test_root/deployment-b" STACK_DEPLOYMENT_ID=selftest-b \
        bash -c 'source "$1/stack-env.sh"; printf %s "$STACK_PGPASSWORD"' _ "$SCRIPT_DIR"
)"
[ "$second_pg_password" != "$first_pg_password" ]

# Auto mode must only select the ACL config when both the server supports the
# channel-pattern rule (Redis 6.2+) and the paired CLI supports --user.
detect_redis_mode() {
    local version="$1" cli_help="$2" case_name="$3"
    env -i "HOME=$HOME" "PATH=$PATH" \
        "STACK_TEST_REDIS_VERSION=$version" \
        "STACK_TEST_REDIS_CLI_HELP=$cli_help" \
        "STACK_ROOT=$test_root/redis-$case_name" \
        "STACK_DEPLOYMENT_ID=redis-$case_name" \
        bash -c '
            redis-server() { printf "Redis server v=%s\n" "$STACK_TEST_REDIS_VERSION"; }
            redis-cli() { printf "%s\n" "$STACK_TEST_REDIS_CLI_HELP"; }
            source "$1/stack-env.sh"
            printf "%s" "$STACK_REDIS_AUTH_MODE"
        ' _ "$SCRIPT_DIR"
}
[ "$(detect_redis_mode 5.0.3 '--user username' 5-0)" = password ]
[ "$(detect_redis_mode 6.1.9 '--user username' 6-1)" = password ]
[ "$(detect_redis_mode 6.2.0 '--user username' 6-2)" = acl ]
[ "$(detect_redis_mode 7.4.0 'redis-cli help' 7-cli-mismatch)" = password ]

grep -Fq 'ON CONFLICT (symbol, date) DO UPDATE' "$SCRIPT_DIR/seed-demo-data.sql"
grep -Fq 'ON CONFLICT (symbol, "timestamp") DO UPDATE' "$SCRIPT_DIR/seed-demo-data.sql"
grep -Fq 'redis_restore_policy=discard_inflight' "$SCRIPT_DIR/checkpoint.sh"
grep -Fq '#SBATCH --signal=B:TERM@600' "$SCRIPT_DIR/slurm-stack.sbatch"
grep -Fq 'STACK_TEST_MODE' "$SCRIPT_DIR/slurm-stack.sbatch"
grep -Fq 'pid record retained' "$SCRIPT_DIR/process-lib.sh"
grep -Fq 'MINIO_APP_USER' "$SCRIPT_DIR/stack-up.sh"
grep -Fq "printf 'requirepass %s\\n'" "$SCRIPT_DIR/stack-up.sh"
grep -Fq "printf 'user default off\\n'" "$SCRIPT_DIR/stack-up.sh"
grep -Fq 'stack_redis_cli_has_user' "$SCRIPT_DIR/stack-env.sh"
grep -Fq 'stack_redis_minor' "$SCRIPT_DIR/stack-env.sh"
grep -Fq 'redis rejects unauthenticated clients' "$SCRIPT_DIR/health.sh"
grep -Fq 'ACL WHOAMI' "$SCRIPT_DIR/health.sh"

# A live same-identity PID whose ownership evidence cannot be revalidated must
# never be signalled and must retain its record for inspection/retry. `/proc`
# is available on the SoC Linux nodes; macOS still runs the static assertions.
grep -Fq 'remove_conclusively_stale_record "$name"' "$SCRIPT_DIR/process-lib.sh"
grep -Fq 'ownership verification failed; record retained and no process was signalled' \
    "$SCRIPT_DIR/process-lib.sh"
if [ -r "/proc/$$/stat" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/process-lib.sh"
    env STACK_DEPLOYMENT_ID="$STACK_DEPLOYMENT_ID" sleep 30 &
    test_child_pid=$!
    sleep 0.1
    test_child_start="$(process_start_time "$test_child_pid")"
    test_child_pgid="$(process_pgid "$test_child_pid")"
    printf '%s %s %s %s\n' \
        "$test_child_pid" "$((test_child_pgid + 1))" "$test_child_start" \
        "$STACK_DEPLOYMENT_ID" >"$STACK_PID_DIR/ownership-failure.pid"
    chmod 600 "$STACK_PID_DIR/ownership-failure.pid"
    if stop_tracked_service ownership-failure 1 >/dev/null 2>&1; then
        echo "stop unexpectedly accepted ambiguous ownership evidence" >&2
        exit 1
    fi
    kill -0 "$test_child_pid"
    [ -e "$STACK_PID_DIR/ownership-failure.pid" ]
    kill "$test_child_pid"
    wait "$test_child_pid" 2>/dev/null || true
    test_child_pid=""
    stop_tracked_service ownership-failure 1 >/dev/null
    [ ! -e "$STACK_PID_DIR/ownership-failure.pid" ]
fi

echo "STACK_SCRIPT_SELF_TEST_OK"
