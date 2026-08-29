# Shared environment for one Slurm allocation of the Lumilake stack.
#
# Security invariants:
#   * every allocation gets an isolated /tmp root and freshly generated secrets;
#   * the generated secret file and runtime directory are private to the owner;
#   * services bind to loopback and authenticate even though Slurm jobs on the
#     same node normally share a network namespace.
#
# Source this file; do not execute it. It intentionally never enables xtrace.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "stack-env.sh requires bash" >&2
    return 1 2>/dev/null || exit 1
fi

stack_env_fail() {
    echo "[stack-env] ERROR: $*" >&2
    return 1
}

stack_random_hex() {
    local bytes="${1:-32}"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$bytes"
    else
        od -An -N "$bytes" -tx1 /dev/urandom | tr -d ' \n'
    fi
}

stack_stat_mode() {
    if stat -c '%a' "$1" >/dev/null 2>&1; then
        stat -c '%a' "$1"
    else
        stat -f '%Lp' "$1"
    fi
}

stack_stat_uid() {
    if stat -c '%u' "$1" >/dev/null 2>&1; then
        stat -c '%u' "$1"
    else
        stat -f '%u' "$1"
    fi
}

stack_lock_acquire() {
    local lock_path="$1" attempt
    if command -v flock >/dev/null 2>&1; then
        STACK_LOCK_STYLE=flock
        exec 8>"$lock_path"
        flock 8
        return 0
    fi
    STACK_LOCK_STYLE=mkdir
    for attempt in $(seq 1 200); do
        mkdir "$lock_path.d" 2>/dev/null && return 0
        sleep 0.05
    done
    stack_env_fail "timed out acquiring lock $lock_path"
}

stack_lock_release() {
    local lock_path="$1"
    if [ "${STACK_LOCK_STYLE:-}" = "flock" ]; then
        flock -u 8
        exec 8>&-
    else
        rmdir "$lock_path.d" 2>/dev/null || true
    fi
    unset STACK_LOCK_STYLE
}

export STACK_DEPLOYMENT_ID="${STACK_DEPLOYMENT_ID:-${SLURM_JOB_ID:-manual-${UID}}}"
case "$STACK_DEPLOYMENT_ID" in
    *[!A-Za-z0-9_.-]*)
        stack_env_fail "STACK_DEPLOYMENT_ID contains unsafe characters" || {
            return 1 2>/dev/null || exit 1
        }
        ;;
esac

export STACK_ROOT="${STACK_ROOT:-/tmp/lumilake-stack-${STACK_DEPLOYMENT_ID}}"
export STACK_RUNTIME_DIR="${STACK_RUNTIME_DIR:-$STACK_ROOT/run}"
export STACK_SECRETS_FILE="${STACK_SECRETS_FILE:-$STACK_RUNTIME_DIR/secrets.env}"
export STACK_CHECKPOINT_ROOT="${STACK_CHECKPOINT_ROOT:-$HOME/lakehouse-checkpoints}"
export STACK_CHECKPOINT_KEEP="${STACK_CHECKPOINT_KEEP:-12}"
export STACK_LOG_ARCHIVE_ROOT="${STACK_LOG_ARCHIVE_ROOT:-$HOME/slurm}"

umask 077
mkdir -p "$STACK_RUNTIME_DIR"
chmod 700 "$STACK_RUNTIME_DIR"

stack_ensure_secrets() {
    local secrets_tmp mode owner required_secret flowmesh_key

    # The SoC nodes use flock; the mkdir fallback keeps local validation
    # portable while preserving atomic generation.
    stack_lock_acquire "$STACK_RUNTIME_DIR/.secrets.lock" || return 1

    if [ ! -e "$STACK_SECRETS_FILE" ]; then
        secrets_tmp="$(mktemp "$STACK_RUNTIME_DIR/.secrets.XXXXXX")"
        flowmesh_key="$(stack_random_hex 32)"
        {
            printf 'export STACK_PGADMIN_PASSWORD=%s\n' "$(stack_random_hex 32)"
            printf 'export STACK_PGPASSWORD=%s\n' "$(stack_random_hex 32)"
            printf 'export STACK_REDIS_PASSWORD=%s\n' "$(stack_random_hex 32)"
            printf 'export MINIO_ROOT_USER=lm%s\n' "$(stack_random_hex 8)"
            printf 'export MINIO_ROOT_PASSWORD=%s\n' "$(stack_random_hex 32)"
            printf 'export MINIO_APP_USER=gw%s\n' "$(stack_random_hex 8)"
            printf 'export MINIO_APP_PASSWORD=%s\n' "$(stack_random_hex 32)"
            printf 'export STACK_GATEWAY_TOKEN=%s\n' "$(stack_random_hex 32)"
            # Lumilake forwards the inbound principal bearer unchanged to
            # FlowMesh. Single-tenant static mode therefore needs one shared
            # control-plane bearer across both hops (gateway remains separate).
            printf 'export FLOWMESH_API_KEY=%s\n' "$flowmesh_key"
            printf 'export LUMILAKE_SERVER_API_KEY=%s\n' "$flowmesh_key"
        } >"$secrets_tmp"
        chmod 600 "$secrets_tmp"
        mv "$secrets_tmp" "$STACK_SECRETS_FILE"
    fi

    mode="$(stack_stat_mode "$STACK_SECRETS_FILE")"
    owner="$(stack_stat_uid "$STACK_SECRETS_FILE")"
    if [ "$mode" != "600" ] || [ "$owner" != "$UID" ]; then
        stack_lock_release "$STACK_RUNTIME_DIR/.secrets.lock"
        stack_env_fail "$STACK_SECRETS_FILE must be owned by uid $UID with mode 0600 (got uid=$owner mode=$mode)"
        return 1
    fi

    # shellcheck disable=SC1090
    source "$STACK_SECRETS_FILE"
    stack_lock_release "$STACK_RUNTIME_DIR/.secrets.lock"

    for required_secret in \
        STACK_PGADMIN_PASSWORD STACK_PGPASSWORD STACK_REDIS_PASSWORD \
        MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_APP_USER MINIO_APP_PASSWORD \
        STACK_GATEWAY_TOKEN FLOWMESH_API_KEY LUMILAKE_SERVER_API_KEY; do
        if [ -z "${!required_secret:-}" ]; then
            stack_env_fail "$required_secret is missing from $STACK_SECRETS_FILE"
            return 1
        fi
    done
}

stack_ensure_secrets || { return 1 2>/dev/null || exit 1; }
unset -f stack_ensure_secrets stack_random_hex stack_stat_mode stack_stat_uid

# curl bearer headers live in private config files so tokens never appear in
# argv (and therefore never in ps output). Generated values are hexadecimal,
# so no curl-config escaping is required.
export STACK_CURL_CONFIG_DIR="$STACK_RUNTIME_DIR/curl"
export STACK_GATEWAY_CURL_CONFIG="$STACK_CURL_CONFIG_DIR/gateway.conf"
export STACK_FLOWMESH_CURL_CONFIG="$STACK_CURL_CONFIG_DIR/flowmesh.conf"
export STACK_LUMILAKE_CURL_CONFIG="$STACK_CURL_CONFIG_DIR/lumilake.conf"
mkdir -p "$STACK_CURL_CONFIG_DIR"
chmod 700 "$STACK_CURL_CONFIG_DIR"
printf 'header = "Authorization: Bearer %s"\n' "$STACK_GATEWAY_TOKEN" >"$STACK_GATEWAY_CURL_CONFIG"
printf 'header = "Authorization: Bearer %s"\n' "$FLOWMESH_API_KEY" >"$STACK_FLOWMESH_CURL_CONFIG"
printf 'header = "Authorization: Bearer %s"\n' "$LUMILAKE_SERVER_API_KEY" >"$STACK_LUMILAKE_CURL_CONFIG"
chmod 600 "$STACK_GATEWAY_CURL_CONFIG" "$STACK_FLOWMESH_CURL_CONFIG" "$STACK_LUMILAKE_CURL_CONFIG"

# Encrypt the supervisor/worker gRPC channel. FlowMesh authenticates each RPC
# with a random worker token; this short-lived CA additionally prevents those
# tokens and task payloads from crossing the shared-node loopback in plaintext.
export STACK_GRPC_TLS_DIR="$STACK_RUNTIME_DIR/grpc-tls"
stack_ensure_grpc_tls() {
    local tls_ext
    command -v openssl >/dev/null 2>&1 || {
        stack_env_fail "openssl is required for per-deployment gRPC TLS"
        return 1
    }
    mkdir -p "$STACK_GRPC_TLS_DIR"
    chmod 700 "$STACK_GRPC_TLS_DIR"
    stack_lock_acquire "$STACK_RUNTIME_DIR/.grpc-tls.lock" || return 1
    if [ ! -s "$STACK_GRPC_TLS_DIR/ca.crt" ] || \
       [ ! -s "$STACK_GRPC_TLS_DIR/server.crt" ] || \
       [ ! -s "$STACK_GRPC_TLS_DIR/server.key" ]; then
        rm -f "$STACK_GRPC_TLS_DIR"/*
        tls_ext="$STACK_GRPC_TLS_DIR/server.ext"
        printf '%s\n' \
            'basicConstraints=CA:FALSE' \
            'keyUsage=digitalSignature,keyEncipherment' \
            'extendedKeyUsage=serverAuth' \
            'subjectAltName=DNS:localhost,IP:127.0.0.1' >"$tls_ext"
        openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 7 \
            -subj "/CN=cp5105-stack-${STACK_DEPLOYMENT_ID}-ca" \
            -keyout "$STACK_GRPC_TLS_DIR/ca.key" \
            -out "$STACK_GRPC_TLS_DIR/ca.crt" >/dev/null 2>&1
        openssl req -newkey rsa:2048 -nodes -sha256 \
            -subj '/CN=localhost' \
            -keyout "$STACK_GRPC_TLS_DIR/server.key" \
            -out "$STACK_GRPC_TLS_DIR/server.csr" >/dev/null 2>&1
        openssl x509 -req -sha256 -days 7 \
            -in "$STACK_GRPC_TLS_DIR/server.csr" \
            -CA "$STACK_GRPC_TLS_DIR/ca.crt" \
            -CAkey "$STACK_GRPC_TLS_DIR/ca.key" -CAcreateserial \
            -extfile "$tls_ext" \
            -out "$STACK_GRPC_TLS_DIR/server.crt" >/dev/null 2>&1
        rm -f "$STACK_GRPC_TLS_DIR/server.csr" "$tls_ext"
        chmod 600 "$STACK_GRPC_TLS_DIR"/*
    fi
    openssl verify -CAfile "$STACK_GRPC_TLS_DIR/ca.crt" \
        "$STACK_GRPC_TLS_DIR/server.crt" >/dev/null 2>&1 || {
        stack_lock_release "$STACK_RUNTIME_DIR/.grpc-tls.lock"
        stack_env_fail "generated gRPC server certificate failed verification"
        return 1
    }
    stack_lock_release "$STACK_RUNTIME_DIR/.grpc-tls.lock"
}
stack_ensure_grpc_tls || { return 1 2>/dev/null || exit 1; }
unset -f stack_ensure_grpc_tls

# Fixed loopback ports remain convenient for local clients. Startup refuses
# to adopt a listener that is not recorded as belonging to this deployment.
export STACK_PG_PORT="${STACK_PG_PORT:-15432}"
export STACK_MINIO_PORT="${STACK_MINIO_PORT:-19100}"
export STACK_MINIO_CONSOLE_PORT="${STACK_MINIO_CONSOLE_PORT:-19101}"
export STACK_REDIS_PORT="${STACK_REDIS_PORT:-16379}"
export STACK_FLOWMESH_PORT="${STACK_FLOWMESH_PORT:-8000}"
export STACK_FLOWMESH_GRPC_PORT="${STACK_FLOWMESH_GRPC_PORT:-50051}"
export STACK_LUMILAKE_PORT="${STACK_LUMILAKE_PORT:-9000}"
export STACK_GATEWAY_PORT="${STACK_GATEWAY_PORT:-9102}"

export PATH="$HOME/.local/bin:$HOME/apps/bin:$HOME/envs/services/bin:$PATH"

# PostgreSQL: the bootstrap role is used only for role/database administration;
# the gateway and health probes use the non-superuser application owner.
export STACK_PGDATA="$STACK_ROOT/pgdata"
export STACK_PGADMIN="${STACK_PGADMIN:-lumilake_admin}"
export STACK_PGUSER="${STACK_PGUSER:-lumilake}"
export STACK_PGDB="${STACK_PGDB:-lumilake}"

# MinIO and its client config are allocation-local; mc must not persist a root
# credential in ~/.mc/config.json.
export STACK_MINIO_DATA="$STACK_ROOT/minio-data"
export MC_CONFIG_DIR="$STACK_ROOT/mc"
export STACK_MINIO_ALIAS="${STACK_MINIO_ALIAS:-stackminio}"
export STACK_MINIO_ROOT_ALIAS="${STACK_MINIO_ROOT_ALIAS:-stackminioroot}"
export STACK_S3_BUCKET="${STACK_S3_BUCKET:-lumilake-private}"
export STACK_S3_PUBLIC_BUCKET="${STACK_S3_PUBLIC_BUCKET:-lumilake-public}"
STACK_MINIO_ROOT_MC_URL="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@127.0.0.1:${STACK_MINIO_PORT}"
STACK_MINIO_APP_MC_URL="http://${MINIO_APP_USER}:${MINIO_APP_PASSWORD}@127.0.0.1:${STACK_MINIO_PORT}"

# Redis ACL: the default user is disabled by stack-up.sh. Hex passwords are
# URL-safe and therefore can be embedded in client URLs without escaping.
export STACK_REDIS_USER="${STACK_REDIS_USER:-flowmesh}"
export REDIS_URL="redis://${STACK_REDIS_USER}:${STACK_REDIS_PASSWORD}@127.0.0.1:${STACK_REDIS_PORT}/0"
export REDIS_CONTROL_URL="$REDIS_URL"
export REDIS_TELEMETRY_URL="$REDIS_URL"
export REDIS_ACL_ENABLED="true"
export REDIS_USERNAME="$STACK_REDIS_USER"
export REDIS_PASSWORD="$STACK_REDIS_PASSWORD"

# lumid-gateway.
export LUMID_GATEWAY_DATABASE_URL="postgresql://${STACK_PGUSER}:${STACK_PGPASSWORD}@127.0.0.1:${STACK_PG_PORT}/${STACK_PGDB}"
export LUMID_GATEWAY_S3_ENDPOINT_URL="http://127.0.0.1:${STACK_MINIO_PORT}"
export LUMID_GATEWAY_S3_ACCESS_KEY="$MINIO_APP_USER"
export LUMID_GATEWAY_S3_SECRET_KEY="$MINIO_APP_PASSWORD"
export LUMID_GATEWAY_S3_BUCKET="$STACK_S3_BUCKET"
export LUMID_GATEWAY_S3_PUBLIC_BUCKET="$STACK_S3_PUBLIC_BUCKET"
export LUMID_GATEWAY_TOKEN="$STACK_GATEWAY_TOKEN"

# FlowMesh. FLOWMESH_REQUIRE_API_KEY is implemented by the hardened FlowMesh
# branch; setting FLOWMESH_API_KEY alone on the original upstream is not auth.
export SERVER_APP_HOST="127.0.0.1"
export SERVER_APP_PORT="$STACK_FLOWMESH_PORT"
export SERVER_GRPC_HOST="127.0.0.1"
export SERVER_GRPC_PORT="$STACK_FLOWMESH_GRPC_PORT"
export SERVER_GRPC_TLS_CA_FILE="$STACK_GRPC_TLS_DIR/ca.crt"
export SERVER_GRPC_TLS_CERT_FILE="$STACK_GRPC_TLS_DIR/server.crt"
export SERVER_GRPC_TLS_KEY_FILE="$STACK_GRPC_TLS_DIR/server.key"
export SUPERVISOR_GRPC_DISABLE_SERVER_TLS="false"
export FLOWMESH_BASE_URL="http://127.0.0.1:${STACK_FLOWMESH_PORT}"
export FLOWMESH_REQUIRE_API_KEY="true"
export ENABLE_SERVER_PORT_FORWARD="false"
export ENABLE_PERSISTENT_PORT_FORWARD="false"
export ENABLE_SERVER_SSH_PROXY="false"
export ENABLE_SERVER_SSH_CONNECTION_AUDIT="false"
export ENABLE_SERVER_SERVE_PROXY="false"
export ENABLE_SSH_BY_DEFAULT="false"
export SERVER_PORT_FORWARD_BIND_HOST="127.0.0.1"
export NODE_ALIAS="${NODE_ALIAS:-soc-lakehouse}"
export NODE_NAMESPACE="${NODE_NAMESPACE:-cp5105}"
export NODE_CLUSTER="${NODE_CLUSTER:-soc}"
export WORKER_HB_DIR="$STACK_ROOT/heartbeats"
export WORKER_RESULTS_DIR="$STACK_ROOT/results"
export RESULTS_DIR="$WORKER_RESULTS_DIR"
export HF_CACHE_DIR="$STACK_ROOT/hf-cache"
export SERVER_WORKER_CONFIG="$STACK_ROOT/worker-config.yaml"
export WORKER_CONFIG_PATH="$SERVER_WORKER_CONFIG"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# Capture the allocation tokens before any worker remaps CUDA_VISIBLE_DEVICES.
# Tokens may be physical ordinals, GPU UUIDs, or MIG UUIDs.
stack_gpu_tokens="${CUDA_VISIBLE_DEVICES:-}"
[ -n "$stack_gpu_tokens" ] || stack_gpu_tokens="${SLURM_STEP_GPUS:-${SLURM_JOB_GPUS:-}}"
export STACK_SLURM_GPU_TOKENS="${STACK_SLURM_GPU_TOKENS:-$stack_gpu_tokens}"
unset stack_gpu_tokens
export STACK_EXPECT_CPU_WORKERS="${STACK_EXPECT_CPU_WORKERS:-1}"
export STACK_EXPECT_GPU_WORKERS="${STACK_EXPECT_GPU_WORKERS:-1}"
case "$STACK_EXPECT_CPU_WORKERS:$STACK_EXPECT_GPU_WORKERS" in
    *[!0-9:]*)
        stack_env_fail "STACK_EXPECT_{CPU,GPU}_WORKERS must be non-negative integers" || {
            return 1 2>/dev/null || exit 1
        }
        ;;
esac
export FLOWMESH_READY_MIN_WORKERS="$((STACK_EXPECT_CPU_WORKERS + STACK_EXPECT_GPU_WORKERS))"
export FLOWMESH_ALLOW_PRIVILEGED_WORKER_OVERRIDES="false"
export FLOWMESH_ALLOW_DOCKER_WORKER_OVERRIDES="false"

# Lumilake server.
export LUMILAKE_SERVER_HOST="127.0.0.1"
export LUMILAKE_SERVER_PORT="$STACK_LUMILAKE_PORT"
export LUMILAKE_REQUIRE_API_KEY="true"
export LUMILAKE_API_KEY="$LUMILAKE_SERVER_API_KEY"
export LUMILAKE_SKIP_DOTENV_CHECK="1"
export LUMILAKE_RUNTIME_ORCHESTRATOR_URL="$FLOWMESH_BASE_URL"
export LUMILAKE_RUNTIME_TOKEN="$FLOWMESH_API_KEY"
export LUMID_DATA_URL="http://127.0.0.1:${STACK_GATEWAY_PORT}"
export LUMID_DATA_TOKEN="$STACK_GATEWAY_TOKEN"
export S3_DATA_PREFIX="${S3_DATA_PREFIX:-lumilake-demo}"
export S3_ARCHIVE_PREFIX="${S3_ARCHIVE_PREFIX:-lumilake-archive/artifacts}"
export LUMILAKE_DISABLE_DATA_PROFILE="1"
export LUMILAKE_FLOWMESH_OUTPUT_DESTINATION="local"
export LUMILAKE_GPU_DEVICES="${LUMILAKE_GPU_DEVICES:-0}"
export LUMILAKE_CPU_WORKER_GROUP_SIZE="$STACK_EXPECT_CPU_WORKERS"
export LUMILAKE_GPU_WORKER_GROUP_SIZE="$STACK_EXPECT_GPU_WORKERS"

# Backward-compatible name for older local commands. It is random, not a
# checked-in development value.
export STACK_TOKEN="$STACK_GATEWAY_TOKEN"
