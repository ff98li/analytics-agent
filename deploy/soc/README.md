# SoC deployment assets

This directory is the Git source of truth for the CP5105 single-node Slurm
stack. The working copies under `cluster/` and the deployed copies under
`~/lakehouse/stack/` and `~/slurm/` must be byte-for-byte mirrors of these
files; runtime secrets, logs, PID records, certificates, and checkpoints must
never be copied back into Git.

## Layout

- `stack/`: authenticated PostgreSQL, MinIO, Redis, gateway, FlowMesh, and
  Lumilake lifecycle; atomic checkpoint/restore; layered health and E2E smoke.
  Redis authentication is version-aware: Redis 6.2+ uses a named ACL user,
  while older Redis uses a random `requirepass`; neither mode permits an
  unauthenticated listener.
- `jobs/`: focused install/check templates, including the corrected FlowMesh
  heredoc and Lumilake exit-code checks.
- `workflows/`: the minimal SQL smoke workflow used by the project.
- `../../vendor/flowmesh/`: signed patch series for the native-provider base
  and its control-plane/GPU hardening.

The matching product branches are:

- FlowMesh `ff98li/FlowMesh:codex/native-provider-hardening`, commit
  `0b8be48e711515e566a5125ff56b67166a997473`.
- Lumilake `ff98li/Lumilake:codex/readiness-hardening`, commit
  `38ba3e503f4e131c29a2c8e8ee510108f0f59af3`.

## Safe deployment

Before overwriting a running host, confirm that no stack job is active and
create a timestamped backup on the receiver. Sync without `--delete`; this
avoids removing unrelated operator files.

```bash
ssh xlogin2.comp.nus.edu.sg 'test -z "$(squeue -h -u "$USER" -n lumilake-stack)"'
ssh xlogin2.comp.nus.edu.sg 'stamp=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$HOME/deploy-backups/$stamp"; cp -a "$HOME/lakehouse/stack" "$HOME/slurm/05d-lumilake-groups.sbatch" "$HOME/slurm/07-flowmesh-check.sbatch" "$HOME/slurm/09-lumilake-dryrun.sbatch" "$HOME/deploy-backups/$stamp/"'
rsync -a deploy/soc/stack/ xlogin2.comp.nus.edu.sg:~/lakehouse/stack/
rsync -a deploy/soc/jobs/ xlogin2.comp.nus.edu.sg:~/slurm/
```

Product source changes are deployed from the two Git commits above, not by
copying an unreviewed working tree. For a checkout based on the native-provider
base, the equivalent FlowMesh patch series is:

```bash
git am vendor/flowmesh/0001-native-worker-provider.patch
git am vendor/flowmesh/0002-fix-harden-worker-control-plane.patch
git am vendor/flowmesh/0003-fix-redact-redis-connection-failures.patch
git am vendor/flowmesh/0004-fix-redact-redis-lifecycle-logs.patch
```

## Validation boundary

Every audit/test Slurm job must have a wall-clock limit of at most one hour.
The full restore rehearsal is:

```bash
ssh xlogin2.comp.nus.edu.sg 'cd "$HOME/slurm" && sbatch --time=01:00:00 --export=ALL,STACK_TEST_MODE=1 "$HOME/lakehouse/stack/slurm-stack.sbatch"'
```

`STACK_TEST_MODE=1` starts the complete stack, runs L2/E2E checks, creates and
validates a quiesced checkpoint, stops the stores, restores within the same
allocation, reruns health/E2E, then exercises final cleanup. Do not use the
file's three-day operational default for an audit run.
