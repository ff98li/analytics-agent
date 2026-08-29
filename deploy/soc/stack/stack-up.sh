#!/bin/bash
# Bring up one authenticated, allocation-scoped lakehouse stack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stack-env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/process-lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/checkpoint.sh"

mkdir -p "$STACK_ROOT/logs" "$WORKER_HB_DIR" "$WORKER_RESULTS_DIR" \
    "$HF_CACHE_DIR" "$STACK_MINIO_DATA" "$MC_CONFIG_DIR"
chmod 700 "$STACK_ROOT" "$STACK_ROOT/logs" "$MC_CONFIG_DIR"

log() { echo "[stack-up] $*"; }
die() { echo "[stack-up] ERROR: $*" >&2; exit 1; }

SERVICE_ENV_DIR="$STACK_RUNTIME_DIR/service-env"
mkdir -p "$SERVICE_ENV_DIR"
chmod 700 "$SERVICE_ENV_DIR"

COMMON_SERVICE_ENV_NAMES=(
    HOME PATH USER LOGNAME SHELL LANG LC_ALL LC_CTYPE TZ TMPDIR TMP TEMP
    LD_LIBRARY_PATH LIBRARY_PATH CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH
    PKG_CONFIG_PATH VIRTUAL_ENV CONDA_PREFIX UV_CACHE_DIR XDG_CACHE_HOME
    SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE HTTP_PROXY HTTPS_PROXY
    NO_PROXY CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES CUDA_HOME
    SLURM_JOB_ID SLURM_JOB_GPUS SLURM_STEP_GPUS SLURM_CPUS_PER_TASK
    SLURM_MEM_PER_NODE SLURM_GPUS_ON_NODE STACK_DEPLOYMENT_ID STACK_RUNTIME_DIR
)

write_service_env() {
    local service="$1" variable value file
    shift
    file="$SERVICE_ENV_DIR/$service.env"
    : >"$file"
    for variable in "${COMMON_SERVICE_ENV_NAMES[@]}" "$@"; do
        if [[ -v "$variable" ]]; then
            value="${!variable}"
            printf 'export %s=%q\n' "$variable" "$value" >>"$file"
        fi
    done
    chmod 600 "$file"
    SERVICE_ENV_FILE="$file"
}

clean_service_command() {
    # Usage: clean_service_command env-file working-dir command...
    # Only non-secret paths/ids are present in env(1)'s argv; the service's
    # private values are loaded by run-service.sh from the 0600 file.
    local env_file="$1" working_dir="$2"
    shift 2
    env -i \
        "HOME=$HOME" "PATH=$PATH" \
        "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID" \
        "STACK_RUNTIME_DIR=$STACK_RUNTIME_DIR" \
        bash "$SCRIPT_DIR/run-service.sh" "$env_file" "$working_dir" "$@"
}

require_commands() {
    local command
    for command in initdb pg_ctl psql createdb pg_dump pg_restore minio mc \
        redis-server redis-cli curl setsid flock sha256sum uv python3; do
        command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
    done
}

validate_pg_identifier() {
    case "$1" in
        ''|[0-9]*|*[!A-Za-z0-9_]*) die "unsafe PostgreSQL identifier: $1" ;;
    esac
}

wait_postgres() {
    local attempt
    for attempt in $(seq 1 60); do
        if PGPASSWORD="$STACK_PGADMIN_PASSWORD" psql \
            -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
            -d postgres -v ON_ERROR_STOP=1 -Atqc 'SELECT 1' >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_http() {
    local url="$1" curl_config="${2:-}" attempt
    for attempt in $(seq 1 120); do
        if [ -n "$curl_config" ]; then
            curl --connect-timeout 2 --max-time 10 -fsS \
                --config "$curl_config" "$url" >/dev/null 2>&1 && return 0
        else
            curl --connect-timeout 2 --max-time 10 -fsS \
                "$url" >/dev/null 2>&1 && return 0
        fi
        sleep 1
    done
    return 1
}

redis_cli() {
    env -i "HOME=$HOME" "PATH=$PATH" \
        "REDISCLI_AUTH=$STACK_REDIS_PASSWORD" redis-cli \
        --no-auth-warning --user "$STACK_REDIS_USER" \
        -h 127.0.0.1 -p "$STACK_REDIS_PORT" "$@"
}

minio_root_mc() {
    env -i "HOME=$HOME" "PATH=$PATH" "MC_CONFIG_DIR=$MC_CONFIG_DIR" \
        "MC_HOST_${STACK_MINIO_ROOT_ALIAS}=$STACK_MINIO_ROOT_MC_URL" \
        mc "$@"
}

minio_app_mc() {
    env -i "HOME=$HOME" "PATH=$PATH" "MC_CONFIG_DIR=$MC_CONFIG_DIR" \
        "MC_HOST_${STACK_MINIO_ALIAS}=$STACK_MINIO_APP_MC_URL" \
        mc "$@"
}

ensure_runtime_role() {
    # Password arrives on psql stdin, never in argv or xtrace. Both identifiers
    # are validated and generated passwords are lowercase hexadecimal.
    {
        printf 'DO $$ BEGIN\n'
        printf "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '%s') THEN\n" "$STACK_PGUSER"
        printf '    CREATE ROLE "%s" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '\''%s'\'';\n' \
            "$STACK_PGUSER" "$STACK_PGPASSWORD"
        printf '  ELSE\n'
        printf '    ALTER ROLE "%s" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '\''%s'\'';\n' \
            "$STACK_PGUSER" "$STACK_PGPASSWORD"
        printf '  END IF;\nEND $$;\n'
    } | PGPASSWORD="$STACK_PGADMIN_PASSWORD" psql \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        -d postgres -v ON_ERROR_STOP=1 >/dev/null
}

ensure_database() {
    if ! PGPASSWORD="$STACK_PGADMIN_PASSWORD" psql \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" -d postgres \
        -Atqc "SELECT 1 FROM pg_database WHERE datname = '$STACK_PGDB'" |
        grep -qx 1; then
        PGPASSWORD="$STACK_PGADMIN_PASSWORD" createdb \
            -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
            --owner="$STACK_PGADMIN" "$STACK_PGDB"
    fi
}

grant_runtime_privileges() {
    {
        # Legacy dumps owned demo objects by the runtime login. Move ownership
        # back to the bootstrap role before granting only DML/catalog access.
        printf 'REASSIGN OWNED BY "%s" TO "%s";\n' "$STACK_PGUSER" "$STACK_PGADMIN"
        printf 'REVOKE CREATE ON DATABASE "%s" FROM PUBLIC;\n' "$STACK_PGDB"
        printf 'GRANT CONNECT ON DATABASE "%s" TO "%s";\n' "$STACK_PGDB" "$STACK_PGUSER"
        printf 'REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n'
        printf 'GRANT USAGE ON SCHEMA lumilake_demo TO "%s";\n' "$STACK_PGUSER"
        printf 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA lumilake_demo TO "%s";\n' "$STACK_PGUSER"
        printf 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA lumilake_demo TO "%s";\n' "$STACK_PGUSER"
        printf 'ALTER DEFAULT PRIVILEGES FOR ROLE "%s" IN SCHEMA lumilake_demo GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "%s";\n' \
            "$STACK_PGADMIN" "$STACK_PGUSER"
        printf 'ALTER DEFAULT PRIVILEGES FOR ROLE "%s" IN SCHEMA lumilake_demo GRANT USAGE, SELECT ON SEQUENCES TO "%s";\n' \
            "$STACK_PGADMIN" "$STACK_PGUSER"
    } | PGPASSWORD="$STACK_PGADMIN_PASSWORD" psql \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        -d "$STACK_PGDB" -v ON_ERROR_STOP=1 >/dev/null
}

require_commands
validate_pg_identifier "$STACK_PGADMIN"
validate_pg_identifier "$STACK_PGUSER"
validate_pg_identifier "$STACK_PGDB"
if [ "$STACK_EXPECT_GPU_WORKERS" -gt 0 ] && [ -z "$STACK_SLURM_GPU_TOKENS" ]; then
    die "GPU workers requested but no CUDA_VISIBLE_DEVICES/Slurm GPU allocation tokens are present"
fi

# PostgreSQL -----------------------------------------------------------------
if [ ! -f "$STACK_PGDATA/PG_VERSION" ]; then
    log "initializing PostgreSQL with SCRAM auth"
    pg_pwfile="$STACK_RUNTIME_DIR/.pg-init-password"
    printf '%s\n' "$STACK_PGADMIN_PASSWORD" >"$pg_pwfile"
    chmod 600 "$pg_pwfile"
    if ! initdb -D "$STACK_PGDATA" -U "$STACK_PGADMIN" \
        --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
        --pwfile="$pg_pwfile" --encoding=UTF8 \
        >"$STACK_ROOT/logs/initdb.log" 2>&1; then
        rm -f "$pg_pwfile"
        die "initdb failed; see $STACK_ROOT/logs/initdb.log"
    fi
    rm -f "$pg_pwfile"
fi

# Reconcile pg_hba.conf on every start so an allocation-local data directory
# created by an older script cannot silently retain loopback/local `trust`.
pg_hba_tmp="$STACK_PGDATA/.pg_hba.conf.$$"
{
    printf '# Managed by CP5105 stack-up.sh; do not add trust rules.\n'
    printf 'local all all scram-sha-256\n'
    printf 'host all all 127.0.0.1/32 scram-sha-256\n'
    printf 'host all all ::1/128 scram-sha-256\n'
} >"$pg_hba_tmp"
chmod 600 "$pg_hba_tmp"
mv "$pg_hba_tmp" "$STACK_PGDATA/pg_hba.conf"

if pg_ctl -D "$STACK_PGDATA" status >/dev/null 2>&1; then
    log "PostgreSQL already running from this deployment's PGDATA"
    pg_ctl -D "$STACK_PGDATA" reload >/dev/null
else
    is_port_open "$STACK_PG_PORT" && die "PostgreSQL port $STACK_PG_PORT belongs to another process/job"
    write_service_env postgres
    postgres_env="$SERVICE_ENV_FILE"
    clean_service_command "$postgres_env" "$STACK_ROOT" \
        pg_ctl -D "$STACK_PGDATA" -l "$STACK_ROOT/logs/postgres.log" \
        -o "-p $STACK_PG_PORT -c listen_addresses=127.0.0.1 -c password_encryption=scram-sha-256" start
fi
wait_postgres || die "PostgreSQL did not accept authenticated connections"
ensure_runtime_role
ensure_database
log "PostgreSQL ready (bootstrap and non-superuser runtime roles separated)"

# MinIO ----------------------------------------------------------------------
assert_port_free_or_owned minio "$STACK_MINIO_PORT"
if ! tracked_process_alive minio; then
    is_port_open "$STACK_MINIO_CONSOLE_PORT" && die "MinIO console port $STACK_MINIO_CONSOLE_PORT belongs to another process/job"
    write_service_env minio MINIO_ROOT_USER MINIO_ROOT_PASSWORD
    minio_env="$SERVICE_ENV_FILE"
    start_tracked_service minio "$STACK_ROOT/logs/minio.log" \
        env -i "HOME=$HOME" "PATH=$PATH" \
        "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID" "STACK_RUNTIME_DIR=$STACK_RUNTIME_DIR" \
        bash "$SCRIPT_DIR/run-service.sh" "$minio_env" "$STACK_ROOT" \
        minio server "$STACK_MINIO_DATA" \
        --address "127.0.0.1:$STACK_MINIO_PORT" \
        --console-address "127.0.0.1:$STACK_MINIO_CONSOLE_PORT"
fi
wait_http "http://127.0.0.1:$STACK_MINIO_PORT/minio/health/ready" || die "MinIO did not become ready"
minio_root_mc mb --ignore-existing "$STACK_MINIO_ROOT_ALIAS/$STACK_S3_BUCKET" >/dev/null
minio_root_mc mb --ignore-existing "$STACK_MINIO_ROOT_ALIAS/$STACK_S3_PUBLIC_BUCKET" >/dev/null

# Create a non-root gateway/checkpoint principal scoped to these two buckets.
# The access/secret keys are fed to mc on stdin rather than exposed in argv.
minio_policy="$STACK_RUNTIME_DIR/minio-gateway-policy.json"
cat >"$minio_policy" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::$STACK_S3_BUCKET", "arn:aws:s3:::$STACK_S3_PUBLIC_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::$STACK_S3_BUCKET/*", "arn:aws:s3:::$STACK_S3_PUBLIC_BUCKET/*"]
    }
  ]
}
EOF
chmod 600 "$minio_policy"
if ! minio_root_mc admin user info "$STACK_MINIO_ROOT_ALIAS" "$MINIO_APP_USER" >/dev/null 2>&1; then
    printf '%s\n%s\n' "$MINIO_APP_USER" "$MINIO_APP_PASSWORD" |
        minio_root_mc admin user add "$STACK_MINIO_ROOT_ALIAS" >/dev/null
fi
if ! minio_root_mc admin policy info "$STACK_MINIO_ROOT_ALIAS" cp5105-gateway >/dev/null 2>&1; then
    minio_root_mc admin policy create "$STACK_MINIO_ROOT_ALIAS" cp5105-gateway "$minio_policy" >/dev/null
fi
minio_root_mc admin policy attach "$STACK_MINIO_ROOT_ALIAS" cp5105-gateway \
    --user "$MINIO_APP_USER" >/dev/null
minio_app_mc stat "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET" >/dev/null
minio_app_mc stat "$STACK_MINIO_ALIAS/$STACK_S3_PUBLIC_BUCKET" >/dev/null

# Restore only once per ephemeral deployment. A validated last-good is restored
# before schema reconciliation, seed, Redis, gateway, FlowMesh, or Lumilake.
if [ ! -f "$STACK_RUNTIME_DIR/database.ready" ]; then
    if checkpoint_restore_latest; then
        restore_status=0
    else
        restore_status=$?
    fi
    case "$restore_status" in
        0) log "restored last-good PostgreSQL and both MinIO buckets" ;;
        10) log "no checkpoint exists; initializing a fresh database" ;;
        *) die "checkpoint restore failed; last-good was not modified" ;;
    esac

    # The seed file is transactional and idempotent. Running it after restore
    # supplies schema migration/constraints without duplicating rows.
    PGPASSWORD="$STACK_PGADMIN_PASSWORD" psql \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        -d "$STACK_PGDB" -v ON_ERROR_STOP=1 \
        -f "$SCRIPT_DIR/seed-demo-data.sql" >>"$STACK_ROOT/logs/seed.log" 2>&1
    grant_runtime_privileges
    printf '%s\n' "${restore_status}" >"$STACK_RUNTIME_DIR/database.ready"
    chmod 600 "$STACK_RUNTIME_DIR/database.ready"
fi

# Policies are applied after restore. Private is credential-only; public allows
# anonymous GET but never anonymous PUT.
minio_root_mc anonymous set none "$STACK_MINIO_ROOT_ALIAS/$STACK_S3_BUCKET" >/dev/null
minio_root_mc anonymous set download "$STACK_MINIO_ROOT_ALIAS/$STACK_S3_PUBLIC_BUCKET" >/dev/null
log "MinIO ready (private=$STACK_S3_BUCKET, public-read-only=$STACK_S3_PUBLIC_BUCKET)"

# Redis ----------------------------------------------------------------------
redis_config="$STACK_RUNTIME_DIR/redis.conf"
{
    printf 'bind 127.0.0.1\n'
    printf 'protected-mode yes\n'
    printf 'port %s\n' "$STACK_REDIS_PORT"
    printf 'save ""\n'
    printf 'appendonly no\n'
    printf 'user default off\n'
    printf 'user %s on >%s ~* &* +@all\n' "$STACK_REDIS_USER" "$STACK_REDIS_PASSWORD"
} >"$redis_config"
chmod 600 "$redis_config"
assert_port_free_or_owned redis "$STACK_REDIS_PORT"
if ! tracked_process_alive redis; then
    write_service_env redis
    redis_env="$SERVICE_ENV_FILE"
    start_tracked_service redis "$STACK_ROOT/logs/redis.log" \
        env -i "HOME=$HOME" "PATH=$PATH" \
        "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID" "STACK_RUNTIME_DIR=$STACK_RUNTIME_DIR" \
        bash "$SCRIPT_DIR/run-service.sh" "$redis_env" "$STACK_ROOT" \
        redis-server "$redis_config"
fi
for _ in $(seq 1 30); do
    redis_cli ping 2>/dev/null | grep -qx PONG && break
    sleep 1
done
redis_cli ping 2>/dev/null | grep -qx PONG || die "Redis ACL authentication failed"
log "Redis ready (ACL user=$STACK_REDIS_USER; in-flight state is ephemeral)"

# Python services ------------------------------------------------------------
rm -f "$STACK_RUNTIME_DIR/writers.stopped"
cp "$SCRIPT_DIR/worker-config.yaml" "$SERVER_WORKER_CONFIG"
chmod 600 "$SERVER_WORKER_CONFIG"

assert_port_free_or_owned gateway "$STACK_GATEWAY_PORT"
if ! tracked_process_alive gateway; then
    write_service_env gateway \
        LUMID_GATEWAY_DATABASE_URL LUMID_GATEWAY_S3_ENDPOINT_URL \
        LUMID_GATEWAY_S3_ACCESS_KEY LUMID_GATEWAY_S3_SECRET_KEY \
        LUMID_GATEWAY_S3_BUCKET LUMID_GATEWAY_S3_PUBLIC_BUCKET \
        LUMID_GATEWAY_S3_REGION LUMID_GATEWAY_TOKEN \
        LUMID_GATEWAY_MAX_BLOB_BYTES LUMID_GATEWAY_MAX_RESULT_BYTES \
        LUMID_GATEWAY_HEALTH_TIMEOUT_SECONDS
    gateway_env="$SERVICE_ENV_FILE"
    start_tracked_service gateway "$STACK_ROOT/logs/gateway.log" \
        env -i "HOME=$HOME" "PATH=$PATH" \
        "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID" "STACK_RUNTIME_DIR=$STACK_RUNTIME_DIR" \
        bash "$SCRIPT_DIR/run-service.sh" "$gateway_env" "$HOME/src/analytics-agent" \
        env PYTHONPATH=src uv run uvicorn \
        analytics_agent.lumid_gateway.app:create_app --factory \
        --host 127.0.0.1 --port "$STACK_GATEWAY_PORT"
fi
wait_http "http://127.0.0.1:$STACK_GATEWAY_PORT/readyz" || die "gateway did not become ready"
log "lumid-gateway ready"

assert_port_free_or_owned flowmesh "$STACK_FLOWMESH_PORT"
if ! tracked_process_alive flowmesh; then
    is_port_open "$STACK_FLOWMESH_GRPC_PORT" && die "FlowMesh gRPC port $STACK_FLOWMESH_GRPC_PORT belongs to another process/job"
    flowmesh_env_names=()
    while IFS= read -r variable; do
        case "$variable" in
            FLOWMESH_*|SERVER_*|REDIS_*|NODE_*|WORKER_*|RESULTS_DIR|HF_CACHE_DIR|LOG_*|ENABLE_*|SSH_*|SUPERVISOR_*|LUMID_DATA_*|S3_*|OPENAI_*|AZURE_*|GOOGLE_*|HF_TOKEN|UTU_*|SERPER_*|JINA_*|NEBULA_*|MODEL_*|PREDOWNLOAD_*)
                flowmesh_env_names+=("$variable")
                ;;
        esac
    done < <(compgen -e)
    write_service_env flowmesh "${flowmesh_env_names[@]}"
    flowmesh_env="$SERVICE_ENV_FILE"
    start_tracked_service flowmesh "$STACK_ROOT/logs/flowmesh.log" \
        env -i "HOME=$HOME" "PATH=$PATH" \
        "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID" "STACK_RUNTIME_DIR=$STACK_RUNTIME_DIR" \
        bash "$SCRIPT_DIR/run-service.sh" "$flowmesh_env" "$HOME/src/FlowMesh" \
        env PYTHONPATH=src CUDA_HOME=/usr/local/cuda-12.9 \
        uv run python -m server.main
fi
wait_http "http://127.0.0.1:$STACK_FLOWMESH_PORT/readyz" "$STACK_FLOWMESH_CURL_CONFIG" || die "FlowMesh did not become ready"
log "FlowMesh ready (API key required, native workers managed in-process)"

assert_port_free_or_owned lumilake "$STACK_LUMILAKE_PORT"
if ! tracked_process_alive lumilake; then
    write_service_env lumilake \
        LUMILAKE_SERVER_HOST LUMILAKE_SERVER_PORT LUMILAKE_REQUIRE_API_KEY \
        LUMILAKE_SERVER_API_KEY LUMILAKE_API_KEY LUMILAKE_SKIP_DOTENV_CHECK \
        LUMILAKE_RUNTIME_ORCHESTRATOR_URL LUMILAKE_RUNTIME_TOKEN \
        LUMID_DATA_URL LUMID_DATA_TOKEN S3_DATA_PREFIX S3_ARCHIVE_PREFIX \
        LUMILAKE_DISABLE_DATA_PROFILE LUMILAKE_FLOWMESH_OUTPUT_DESTINATION \
        LUMILAKE_GPU_DEVICES LUMILAKE_CPU_WORKER_GROUP_SIZE \
        LUMILAKE_GPU_WORKER_GROUP_SIZE LOG_LEVEL
    lumilake_env="$SERVICE_ENV_FILE"
    start_tracked_service lumilake "$STACK_ROOT/logs/lumilake.log" \
        env -i "HOME=$HOME" "PATH=$PATH" \
        "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID" "STACK_RUNTIME_DIR=$STACK_RUNTIME_DIR" \
        bash "$SCRIPT_DIR/run-service.sh" "$lumilake_env" "$HOME/src/Lumilake" \
        env PYTHONPATH=src uv run python -m lumilake_server
fi
wait_http "http://127.0.0.1:$STACK_LUMILAKE_PORT/readyz" || die "Lumilake did not become ready"
log "Lumilake ready"

bash "$SCRIPT_DIR/health.sh" --level 2
log "STACK_UP_OK on $(hostname) deployment=$STACK_DEPLOYMENT_ID"
