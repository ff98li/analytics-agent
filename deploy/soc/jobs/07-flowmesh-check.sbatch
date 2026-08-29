#!/bin/bash
#SBATCH --job-name=flowmesh-check
#SBATCH --partition=normal
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=07-flowmesh-check-%j.out
#SBATCH --error=07-flowmesh-check-%j.err

# Verify the new `native` worker provider imports/validates and that the
# server worker configuration accepts its provider-specific shape.
set -Eeuo pipefail
umask 077
slurm_log_dir="${SLURM_SUBMIT_DIR:-$PWD}"
for slurm_log in \
    "$slurm_log_dir/07-flowmesh-check-${SLURM_JOB_ID}.out" \
    "$slurm_log_dir/07-flowmesh-check-${SLURM_JOB_ID}.err"; do
    [ ! -e "$slurm_log" ] || chmod 600 "$slurm_log"
done
unset slurm_log slurm_log_dir
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/src/FlowMesh"

uv run env PYTHONPATH=src python - <<'PY'
ok = True

def check(label, fn):
    global ok
    try:
        fn()
        print(f"PASS {label}")
    except Exception as exc:
        ok = False
        print(f"FAIL {label}: {type(exc).__name__}: {exc}")

check("import manager", lambda: __import__("server.supervisor.manager", fromlist=["WorkerManager"]))
check("import native adapter", lambda: __import__("server.supervisor.adapters.native", fromlist=["NativeWorkerAdapter"]))

from server.supervisor.adapters.native import NativeWorkerConfig

check("config cpu", lambda: NativeWorkerConfig.model_validate({}))
check("config gpu", lambda: NativeWorkerConfig.model_validate({"worker_type": "gpu", "cuda_devices": [0]}))
def empty_gpu_list():
    try:
        NativeWorkerConfig.model_validate({"worker_type": "gpu", "cuda_devices": []})
        raise AssertionError("expected ValueError for empty cuda_devices")
    except ValueError:
        pass
check("config rejects empty cuda_devices", empty_gpu_list)

from server.supervisor.manager import ServerWorkerConfig

check(
    "worker init config with native provider",
    lambda: ServerWorkerConfig.model_validate(
        {
            "workers": [
                {
                    "provider": "native",
                    "worker_config": {"worker_type": "cpu"},
                }
            ]
        }
    ),
)

print("IMPORT_OK" if ok else "IMPORT_FAIL")
raise SystemExit(0 if ok else 1)
PY

echo "=== existing worker-config tests (regression) ==="
# pipefail makes this pipeline return pytest's status rather than tail's.
uv run env PYTHONPATH=src pytest -q tests/server/test_worker_config.py 2>&1 | tail -8

echo "=== FLOWMESH_CHECK_DONE on $(hostname) ==="
