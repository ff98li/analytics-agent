# analytics-agent (Student C, CP5105)

Our plug point in the local lakehouse agent. Phase 1 ships the first component:

## `lumid_gateway`
A lumid-data-app-compatible data-plane service for the local lakehouse (PostgreSQL + MinIO),
reimplemented because the upstream app ships only as a Docker image and the SoC cluster has no
container runtime (see `../cluster/SoC-cluster-deploy-plan.md`).

Phase-1 endpoints (contract extracted from `Lumilake/.../lumid_data_client.py` and
`FlowMesh/.../lumid_data_connector.py`):

- `POST /retrieve` `{sql, output_format="jsonl"|"csv"}` → `{materialized_uri, output_format,
  rowcount, size_bytes, access_chain, run_id}`; `GET <materialized_uri>` serves the rows.
- `GET /blobs?prefix=&delimiter=&limit=` → `{objects:[{key,size}], truncated}`
- `PUT /blobs/<key>` / `GET /blobs/<key>` (404 when missing; 413 over quota)
- `GET /catalog/tables/<schema>/<table>` → `{columns:[{name}]}` (404 when absent)
- `GET /healthz`

Deferred: `POST /profile` (data profiling — `LUMILAKE_DISABLE_DATA_PROFILE=1` in Phase 1) and
`POST /agent/v1` (NL→SQL agent — Phase 2, dovetails with our `nl2workflow`).

Run: `uv run uvicorn analytics_agent.lumid_gateway.app:create_app --factory --host 127.0.0.1 --port 9102`

Config (env): `LUMID_GATEWAY_DATABASE_URL`, `LUMID_GATEWAY_S3_ENDPOINT_URL`,
`LUMID_GATEWAY_S3_ACCESS_KEY`, `LUMID_GATEWAY_S3_SECRET_KEY`, `LUMID_GATEWAY_S3_BUCKET`,
`LUMID_GATEWAY_S3_REGION`, `LUMID_GATEWAY_TOKEN` (optional; unset = no auth, local mode),
`LUMID_GATEWAY_MAX_BLOB_BYTES` (default 1 GiB), `LUMID_GATEWAY_MAX_RESULT_BYTES` (default 512 MiB).
