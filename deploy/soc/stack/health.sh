#!/bin/bash
# Layered health checks:
#   L0 -- cheap process/listener liveness
#   L1 -- authenticated dependency readiness and storage policy checks
#   L2 -- FlowMesh/Lumilake worker readiness + Slurm GPU allocation invariants
# A separate e2e-smoke.sh exercises an actual DB -> gateway -> MinIO result.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stack-env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/process-lib.sh"

level=2
if [ "${1:-}" = "--level" ]; then
    level="${2:-}"
fi
case "$level" in 0|1|2) ;; *) echo "usage: bash health.sh [--level 0|1|2]" >&2; exit 2 ;; esac

ok=0
fail=0
check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        printf 'OK   L%s %s\n' "$level" "$name"
        ok=$((ok + 1))
    else
        printf 'FAIL L%s %s\n' "$level" "$name"
        fail=$((fail + 1))
    fi
}

curl_probe() {
    # A wedged local listener must not hold health/cleanup indefinitely.
    curl --connect-timeout 2 --max-time 10 "$@"
}

redis_cli() {
    if [ "$STACK_REDIS_AUTH_MODE" = "acl" ]; then
        env -i "HOME=$HOME" "PATH=$PATH" \
            "REDISCLI_AUTH=$STACK_REDIS_PASSWORD" redis-cli \
            --no-auth-warning --user "$STACK_REDIS_USER" \
            -h 127.0.0.1 -p "$STACK_REDIS_PORT" "$@"
    else
        env -i "HOME=$HOME" "PATH=$PATH" \
            "REDISCLI_AUTH=$STACK_REDIS_PASSWORD" redis-cli \
            -h 127.0.0.1 -p "$STACK_REDIS_PORT" "$@"
    fi
}

minio_app_mc() {
    env -i "HOME=$HOME" "PATH=$PATH" "MC_CONFIG_DIR=$MC_CONFIG_DIR" \
        "MC_HOST_${STACK_MINIO_ALIAS}=$STACK_MINIO_APP_MC_URL" \
        mc "$@"
}

postgres_livez() {
    pg_isready -q -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGUSER"
}

postgres_ready() {
    local answer
    answer="$(PGPASSWORD="$STACK_PGPASSWORD" psql \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGUSER" \
        -d "$STACK_PGDB" -v ON_ERROR_STOP=1 -Atqc \
        "SELECT (to_regclass('lumilake_demo.instrument_profile') IS NOT NULL AND NOT rolsuper)::int FROM pg_roles WHERE rolname = current_user")"
    [ "$answer" = "1" ]
}

postgres_hba_ready() {
    grep -Eq '^[[:space:]]*(local|host)[[:space:]].*[[:space:]]scram-sha-256([[:space:]]|$)' \
        "$STACK_PGDATA/pg_hba.conf" &&
        ! grep -Eq '^[[:space:]]*(local|host)[[:space:]].*[[:space:]]trust([[:space:]]|$)' \
            "$STACK_PGDATA/pg_hba.conf"
}

redis_livez() {
    [ "$(redis_cli ping 2>/dev/null)" = "PONG" ]
}

redis_ready() {
    local key value got
    value="redis-health-$STACK_DEPLOYMENT_ID-$$"
    key="cp5105:health:${STACK_DEPLOYMENT_ID}:$$"
    redis_cli set "$key" "$value" EX 30 >/dev/null
    got="$(redis_cli get "$key")"
    redis_cli del "$key" >/dev/null
    [ "$got" = "$value" ]
}

redis_auth_enforced() {
    local unauthenticated
    unauthenticated="$(
        env -i "HOME=$HOME" "PATH=$PATH" redis-cli \
            -h 127.0.0.1 -p "$STACK_REDIS_PORT" ping 2>/dev/null || true
    )"
    case "$unauthenticated" in
        *NOAUTH*|*WRONGPASS*) ;;
        *) return 1 ;;
    esac
    if [ "$STACK_REDIS_AUTH_MODE" = "acl" ]; then
        [ "$(redis_cli ACL WHOAMI 2>/dev/null)" = "$STACK_REDIS_USER" ]
    fi
}

minio_ready_and_policies() {
    local key_private key_public body private_status public_status put_status rc=0
    key_private="health/$STACK_DEPLOYMENT_ID-$$-private.txt"
    key_public="health/$STACK_DEPLOYMENT_ID-$$-public.txt"
    body="minio-health-$STACK_DEPLOYMENT_ID-$$"

    minio_app_mc stat "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET" >/dev/null || return 1
    minio_app_mc stat "$STACK_MINIO_ALIAS/$STACK_S3_PUBLIC_BUCKET" >/dev/null || return 1
    printf '%s' "$body" | minio_app_mc pipe "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET/$key_private" >/dev/null || return 1
    printf '%s' "$body" | minio_app_mc pipe "$STACK_MINIO_ALIAS/$STACK_S3_PUBLIC_BUCKET/$key_public" >/dev/null || {
        minio_app_mc rm --force "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET/$key_private" >/dev/null 2>&1 || true
        return 1
    }

    private_status="$(curl_probe -sS -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:$STACK_MINIO_PORT/$STACK_S3_BUCKET/$key_private")" || rc=1
    public_status="$(curl_probe -sS -o "$STACK_RUNTIME_DIR/minio-health-body.$$" -w '%{http_code}' \
        "http://127.0.0.1:$STACK_MINIO_PORT/$STACK_S3_PUBLIC_BUCKET/$key_public")" || rc=1
    put_status="$(curl_probe -sS -o /dev/null -w '%{http_code}' -X PUT --data 'unauthorized' \
        "http://127.0.0.1:$STACK_MINIO_PORT/$STACK_S3_PUBLIC_BUCKET/$key_public")" || rc=1

    [ "$private_status" = "403" ] || rc=1
    [ "$public_status" = "200" ] || rc=1
    [ "$put_status" = "403" ] || rc=1
    [ "$(cat "$STACK_RUNTIME_DIR/minio-health-body.$$" 2>/dev/null)" = "$body" ] || rc=1

    minio_app_mc rm --force "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET/$key_private" >/dev/null 2>&1 || rc=1
    minio_app_mc rm --force "$STACK_MINIO_ALIAS/$STACK_S3_PUBLIC_BUCKET/$key_public" >/dev/null 2>&1 || rc=1
    rm -f "$STACK_RUNTIME_DIR/minio-health-body.$$"
    return "$rc"
}

gateway_catalog_ready() {
    curl_probe -fsS --config "$STACK_GATEWAY_CURL_CONFIG" \
        "http://127.0.0.1:$STACK_GATEWAY_PORT/catalog/tables/lumilake_demo/instrument_profile" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("columns")'
}

flowmesh_auth_ready() {
    local unauth
    unauth="$(curl_probe -sS -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:$STACK_FLOWMESH_PORT/api/v1/workers")" || return 1
    [ "$unauth" = "401" ] || return 1
    curl_probe -fsS --config "$STACK_FLOWMESH_CURL_CONFIG" \
        "http://127.0.0.1:$STACK_FLOWMESH_PORT/api/v1/workers" |
        python3 -c 'import json,sys; assert isinstance(json.load(sys.stdin), list)'
}

lumilake_worker_proxy_ready() {
    local unauth
    unauth="$(curl_probe -sS -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:$STACK_LUMILAKE_PORT/api/v1/workers")" || return 1
    [ "$unauth" = "401" ] || return 1
    curl_probe -fsS --config "$STACK_LUMILAKE_CURL_CONFIG" \
        "http://127.0.0.1:$STACK_LUMILAKE_PORT/api/v1/workers" |
        python3 -c 'import json,sys; assert isinstance(json.load(sys.stdin), list)'
}

worker_allocation_ready() {
    local json_file="$STACK_RUNTIME_DIR/health-workers.$$.json" rc=0
    curl_probe -fsS --config "$STACK_FLOWMESH_CURL_CONFIG" \
        "http://127.0.0.1:$STACK_FLOWMESH_PORT/api/v1/workers" >"$json_file" || return 1
    python3 - "$json_file" "$STACK_EXPECT_CPU_WORKERS" "$STACK_EXPECT_GPU_WORKERS" \
        "$STACK_SLURM_GPU_TOKENS" "$STACK_RUNTIME_DIR/gpu-allocation-evidence.json" <<'PY' || rc=$?
import json
import subprocess
import sys

path, expected_cpu_raw, expected_gpu_raw, allocation_raw, evidence_path = sys.argv[1:]
expected_cpu = int(expected_cpu_raw)
expected_gpu = int(expected_gpu_raw)
workers = json.load(open(path, encoding="utf-8"))
assert isinstance(workers, list), "worker response is not a list"

cpu = []
gpu = []
uuids = []
for worker in workers:
    status = str(worker.get("status", "")).upper()
    assert status in {"IDLE", "BUSY"}, f"worker not ready: {status}"
    assert worker.get("stale") is not True, "stale worker"
    hardware = worker.get("hardware") or {}
    devices = ((hardware.get("gpu") or {}).get("devices") or [])
    if devices:
        gpu.append(worker)
        indices = [int(device["index"]) for device in devices]
        assert indices == list(range(len(indices))), (
            f"GPU indices are not allocation-relative: {indices}"
        )
        for device in devices:
            uuid = str(device.get("uuid", ""))
            assert uuid, "GPU UUID missing"
            uuids.append(uuid)
    else:
        cpu.append(worker)

assert len(cpu) == expected_cpu, f"expected {expected_cpu} CPU workers, got {len(cpu)}"
assert len(gpu) == expected_gpu, f"expected {expected_gpu} GPU workers, got {len(gpu)}"
assert len(uuids) == len(set(uuids)), "same allocated GPU reported more than once"

tokens = [token.strip() for token in allocation_raw.split(",") if token.strip()]
if tokens and expected_gpu == 1:
    # This stack starts one GPU worker and asks it to consume the allocation.
    assert len(uuids) == len(tokens), (
        f"worker reported {len(uuids)} GPUs but Slurm allocated {len(tokens)}"
    )
    expected_uuids = []
    for token in tokens:
        if token.startswith(("GPU-", "MIG-")):
            expected_uuids.append(token)
            continue
        assert token.isdigit(), f"unsupported Slurm GPU token: {token!r}"
        probe = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                token,
                "--query-gpu=uuid",
                "--format=csv,noheader",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        resolved = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        assert len(resolved) == 1, f"could not uniquely resolve GPU token {token}"
        expected_uuids.append(resolved[0])
    evidence = {
        "slurm_tokens": tokens,
        "resolved_expected_uuids": expected_uuids,
        "reported_worker_uuids": uuids,
        "reported_logical_indices": [
            device["index"]
            for worker in gpu
            for device in ((worker.get("hardware") or {}).get("gpu") or {}).get("devices", [])
        ],
    }
    with open(evidence_path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2, sort_keys=True)
        handle.write("\n")
    assert {item.lower() for item in uuids} == {
        item.lower() for item in expected_uuids
    }, "reported GPU UUID set does not match the Slurm allocation"
elif expected_gpu:
    assert uuids, "GPU worker advertised no GPU"
PY
    [ ! -e "$STACK_RUNTIME_DIR/gpu-allocation-evidence.json" ] || \
        chmod 600 "$STACK_RUNTIME_DIR/gpu-allocation-evidence.json"
    rm -f "$json_file"
    return "$rc"
}

# L0: cheap liveness only.
check "postgres listener :$STACK_PG_PORT" postgres_livez
check "minio live :$STACK_MINIO_PORT" curl_probe -fsS "http://127.0.0.1:$STACK_MINIO_PORT/minio/health/live"
check "redis authenticated ping :$STACK_REDIS_PORT" redis_livez
check "gateway process/live :$STACK_GATEWAY_PORT" curl_probe -fsS "http://127.0.0.1:$STACK_GATEWAY_PORT/livez"
check "flowmesh process/live :$STACK_FLOWMESH_PORT" curl_probe -fsS "http://127.0.0.1:$STACK_FLOWMESH_PORT/livez"
check "lumilake process/live :$STACK_LUMILAKE_PORT" curl_probe -fsS "http://127.0.0.1:$STACK_LUMILAKE_PORT/livez"

if [ "$level" -ge 1 ]; then
    check "postgres SELECT/schema/non-superuser" postgres_ready
    check "postgres pg_hba SCRAM/no-trust" postgres_hba_ready
    check "redis authenticated TTL roundtrip" redis_ready
    check "redis rejects unauthenticated clients" redis_auth_enforced
    check "minio auth + private/public policy roundtrip" minio_ready_and_policies
    check "gateway dependency readiness" curl_probe -fsS "http://127.0.0.1:$STACK_GATEWAY_PORT/readyz"
    check "gateway authenticated catalog" gateway_catalog_ready
    check "flowmesh dependency readiness" curl_probe -fsS --config "$STACK_FLOWMESH_CURL_CONFIG" \
        "http://127.0.0.1:$STACK_FLOWMESH_PORT/readyz"
    check "flowmesh API-key enforcement" flowmesh_auth_ready
    check "lumilake dependency readiness" curl_probe -fsS --config "$STACK_LUMILAKE_CURL_CONFIG" \
        "http://127.0.0.1:$STACK_LUMILAKE_PORT/readyz"
    check "lumilake -> flowmesh worker proxy" lumilake_worker_proxy_ready
fi

if [ "$level" -ge 2 ]; then
    # Registered, non-stale workers prove the token-authenticated TLS gRPC path
    # completed registration/heartbeat; a TCP-only probe would not.
    check "workers + gRPC heartbeat + Slurm GPU subset" worker_allocation_ready
    check "tracked process ownership" required_stack_processes_alive
fi

echo "health L$level: $ok ok, $fail failed"
[ "$fail" -eq 0 ]
