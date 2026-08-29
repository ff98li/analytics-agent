#!/bin/bash
# Small state-changing smoke: PostgreSQL -> authenticated gateway -> private
# MinIO materialization. It also exercises Lumilake -> authenticated FlowMesh
# worker enumeration through health L2. It does not submit a user workflow.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stack-env.sh"

minio_app_mc() {
    env -i "HOME=$HOME" "PATH=$PATH" "MC_CONFIG_DIR=$MC_CONFIG_DIR" \
        "MC_HOST_${STACK_MINIO_ALIAS}=$STACK_MINIO_APP_MC_URL" \
        mc "$@"
}

bash "$SCRIPT_DIR/health.sh" --level 2

response="$STACK_RUNTIME_DIR/e2e-response.$$.json"
result="$STACK_RUNTIME_DIR/e2e-result.$$.jsonl"
cleanup() {
    local object_key="${1:-}"
    if [ -n "$object_key" ]; then
        minio_app_mc rm --force "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET/$object_key" >/dev/null 2>&1 || true
    fi
    rm -f "$response" "$result"
}
trap 'cleanup "${object_key:-}"' EXIT

curl --connect-timeout 2 --max-time 30 -fsS --config "$STACK_GATEWAY_CURL_CONFIG" \
    -H 'Content-Type: application/json' \
    --data '{"sql":"SELECT 1 AS probe","output_format":"jsonl"}' \
    "http://127.0.0.1:$STACK_GATEWAY_PORT/retrieve" >"$response"

materialized_uri="$(python3 - "$response" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["rowcount"] == 1
uri = payload["materialized_uri"]
assert uri.startswith("/materialized/")
print(uri)
PY
)"
object_key="${materialized_uri#/}"

curl --connect-timeout 2 --max-time 30 -fsS --config "$STACK_GATEWAY_CURL_CONFIG" \
    "http://127.0.0.1:$STACK_GATEWAY_PORT$materialized_uri" >"$result"
python3 - "$result" <<'PY'
import json
import sys

line = open(sys.argv[1], encoding="utf-8").readline()
assert json.loads(line)["probe"] == 1
PY

echo "E2E_SMOKE_OK (DB -> gateway -> private MinIO; no user workflow submitted)"
