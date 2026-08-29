#!/bin/bash
#SBATCH --job-name=lumilake-groups
#SBATCH --partition=normal
#SBATCH --cpus-per-task=8
#SBATCH --mem=12G
#SBATCH --time=00:45:00
#SBATCH --output=05d-lumilake-groups-%j.out
#SBATCH --error=05d-lumilake-groups-%j.err

# Lumilake installs no default deps; the server runtime lives in the
# `server-runtime` dependency-group and the CLI in the `cli` extra.
set -Eeuo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/src/Lumilake" || exit 1

uv sync --python 3.12 --group server-runtime --extra cli --extra hook || exit 1

uv run python -c "import fastapi, uvicorn, pandas, lumid_hooks, flowmesh, lumilake_hook; print('server-runtime OK')" || exit 1
source "$HOME/lakehouse/stack/stack-env.sh"
uv run env PYTHONPATH=src python -c "import lumilake_server.main; print('lumilake_server import OK')" || exit 1
uv run lumilake --help 2>&1 | head -5

echo "=== LUMILAKE_GROUPS_OK on $(hostname) ==="
