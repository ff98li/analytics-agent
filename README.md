# analytics-agent (CP5105)

The **analytics-agent plug point** of the local lakehouse agent, built on Lumilake OSS: natural
language in, an analytics workflow DAG over a local lakehouse out, with private data and locally
derived insights staying local.

Four pluggable functions:

| Function | Status |
|---|---|
| `nl2workflow(nl_request, context) -> workflow_dag` | **baseline shipped** — rule-based; the `context` seam is reserved for memory/inference |
| `execute(workflow_dag) -> {results, provenance}` | exercised through Lumilake's runtime path onto FlowMesh |
| `learn(outcome) -> updated_policy` | not started (wk 3–5) |
| `publish_gate(item) -> {private\|public}` | not started (wk 4–6); the storage partition it attaches to is in place |

**Phase 1 (wk 1–2) is complete.** Lumilake runs a Q1–Q6 workflow end-to-end on a local lakehouse
(PostgreSQL + MinIO), and the baseline `nl2workflow` emits DAGs that both score against a
ground-truth set and run on the live stack.

## Layout

```
src/analytics_agent/
  lumid_gateway/     lumid-data-app-compatible data plane (PostgreSQL + MinIO)
  nl2workflow/       baseline NL -> DAG generator + structural scorer
nl-requests/         ground-truth NL-request set
scripts/             workflow-YAML emitter
tests/               pytest suite
vendor/flowmesh/     patch adding a container-less worker provider to FlowMesh
```

Cluster deployment scripts, sbatch jobs and the phase reports live outside this repository.

## `lumid_gateway`

A lumid-data-app-compatible data-plane service for the local lakehouse, reimplemented because the
upstream app ships only as a Docker image and the deployment target — the NUS SoC cluster — has no
container runtime available (rootless user namespaces are blocked, and the proot/podman fallbacks
fail too). With this gateway plus a native FlowMesh worker provider, the entire stack runs as plain
processes: no Docker anywhere.

Phase-1 endpoints, with the wire contract extracted from `lumid_data_client.py` (Lumilake, server
side) and `lumid_data_connector.py` (FlowMesh, worker side):

- `POST /retrieve` `{sql, output_format="jsonl"|"csv"}` → `{materialized_uri, output_format,
  rowcount, size_bytes, access_chain, run_id}`; `GET <materialized_uri>` serves the rows.
  Read-only SQL is enforced; results are materialized to object storage.
- `GET /blobs?prefix=&delimiter=&limit=` → `{objects:[{key,size}], truncated}`
- `PUT /blobs/<key>` / `GET /blobs/<key>` (404 when missing; 413 over quota)
- `GET /catalog/tables/<schema>/<table>` → `{columns:[{name}]}` (404 when absent)
- `GET /healthz`

Bucket routing implements the two-tier storage partition the privacy gate will build on: a
public-read-only bucket and a private-read-write bucket, routed on the first key segment.

Deferred: `POST /profile` (data profiling — `LUMILAKE_DISABLE_DATA_PROFILE=1` in Phase 1) and
`POST /agent/v1` (NL→SQL agent — dovetails with `nl2workflow`, so it lands with that work).

Run:

```
uv run uvicorn analytics_agent.lumid_gateway.app:create_app --factory --host 127.0.0.1 --port 9102
```

Config (env): `LUMID_GATEWAY_DATABASE_URL`, `LUMID_GATEWAY_S3_ENDPOINT_URL`,
`LUMID_GATEWAY_S3_ACCESS_KEY`, `LUMID_GATEWAY_S3_SECRET_KEY`, `LUMID_GATEWAY_S3_BUCKET`,
`LUMID_GATEWAY_S3_REGION`, `LUMID_GATEWAY_TOKEN` (optional; unset = no auth, local mode),
`LUMID_GATEWAY_MAX_BLOB_BYTES` (default 1 GiB), `LUMID_GATEWAY_MAX_RESULT_BYTES` (default 512 MiB).

## `nl2workflow`

```python
from analytics_agent.nl2workflow import nl2workflow, workflow_to_yaml

workflow = nl2workflow("Should I buy AAPL right now?")
print(workflow_to_yaml(workflow))
```

**Generator (`baseline.py`).** Deterministic symbol detection and intent classification over six
canonical Lumilake-format DAG templates (profile, news, fundamentals, market, trading, compare).
The trading intent emits the full 11-operator structure with its data wiring — prompt templates,
`aggregate_table`, `structural_outputs` — not a stub. Generated DAGs have been dispatched to the
live stack and returned data-grounded results, so "runnable" here means executed, not just
well-formed.

**Scorer (`scoring.py`).** Structural accuracy against a ground-truth DAG:
`0.5 × op-set Jaccard + 0.5 × edge-set Jaccard`, where edges are operator inputs plus output
references, with an `exact` flag for a full structural match. `evaluate_set` reports over a whole
request set.

**Ground truth (`nl-requests/ground_truth.json`).** Eight NL requests with reference DAGs; Lumilake
ships none, so this set is purpose-built. The baseline reproduces these structures by construction
(8/8 exact) — it is the zero point of the improvement curve, not a result. Out-of-set requests are
what will exercise the loop.

Emit submission-ready YAML:

```
uv run python scripts/gen_nl2workflow_yaml.py generated-workflows/
```

## FlowMesh native worker provider

FlowMesh spawns workers as Docker containers. `vendor/flowmesh/0001-native-worker-provider.patch`
adds a `native` provider that spawns them as processes instead, plus a host `nvidia-smi` fallback
for GPU enumeration, a `FLOWMESH_COLLECT_GPU` toggle so CPU workers report no GPU (Lumilake groups
workers by hardware, and data-retrieval operators need a CPU-group worker), and defensive provider
loading, since `docker.from_env()` raises eagerly on hosts without Docker.

Applied upstream-style on a fork branch:
<https://github.com/ff98li/FlowMesh/tree/feat/native-worker-provider> (4 files, +449/−49;
`mlsys-io/FlowMesh` itself is untouched).

## Tests

```
uv sync --group dev
uv run pytest
```

Requires Python ≥ 3.12. The gateway tests stub PostgreSQL and object storage, so they need no live
services.