#!/bin/bash
# Atomic, versioned checkpoint/restore for PostgreSQL and both MinIO buckets.
# Redis is intentionally not restored: in-flight workflows are interrupted and
# must be reconciled/resubmitted by the caller after a new allocation starts.
# shellcheck shell=bash

checkpoint_log() { echo "[checkpoint] $*"; }

checkpoint_minio_mc() {
    env -i "HOME=$HOME" "PATH=$PATH" "MC_CONFIG_DIR=$MC_CONFIG_DIR" \
        "MC_HOST_${STACK_MINIO_ALIAS}=$STACK_MINIO_APP_MC_URL" \
        mc "$@"
}

checkpoint_require_env() {
    local name
    for name in STACK_CHECKPOINT_ROOT STACK_PG_PORT STACK_PGADMIN \
        STACK_PGADMIN_PASSWORD STACK_PGDB STACK_PGUSER STACK_MINIO_ALIAS \
        STACK_MINIO_APP_MC_URL STACK_S3_BUCKET STACK_S3_PUBLIC_BUCKET \
        STACK_RUNTIME_DIR; do
        [ -n "${!name:-}" ] || {
            echo "[checkpoint] ERROR: source stack-env.sh first ($name is unset)" >&2
            return 1
        }
    done
}

checkpoint_safe_generation() {
    case "$1" in
        ''|*[!A-Za-z0-9_.-]*|.*) return 1 ;;
        *) return 0 ;;
    esac
}

checkpoint_lock() {
    mkdir -p "$STACK_CHECKPOINT_ROOT/generations"
    chmod 700 "$STACK_CHECKPOINT_ROOT" "$STACK_CHECKPOINT_ROOT/generations"
    exec 9>"$STACK_CHECKPOINT_ROOT/.checkpoint.lock"
    flock -w "${STACK_CHECKPOINT_LOCK_TIMEOUT:-300}" 9
}

checkpoint_unlock() {
    flock -u 9 2>/dev/null || true
    exec 9>&-
}

checkpoint_latest_name() {
    local generation
    [ -r "$STACK_CHECKPOINT_ROOT/latest" ] || return 1
    IFS= read -r generation <"$STACK_CHECKPOINT_ROOT/latest" || return 1
    checkpoint_safe_generation "$generation" || {
        echo "[checkpoint] ERROR: unsafe generation name in latest" >&2
        return 1
    }
    printf '%s\n' "$generation"
}

checkpoint_validate_dir() {
    local dir="$1" expected_generation="${2:-}"
    [ -d "$dir" ] || return 1
    [ -s "$dir/manifest.txt" ] || return 1
    [ -s "$dir/checksums.sha256" ] || return 1
    [ -s "$dir/postgres.dump" ] || return 1
    [ -d "$dir/minio/$STACK_S3_BUCKET" ] || return 1
    [ -d "$dir/minio/$STACK_S3_PUBLIC_BUCKET" ] || return 1
    grep -Fqx 'format_version=1' "$dir/manifest.txt" || return 1
    grep -Fqx 'redis_restore_policy=discard_inflight' "$dir/manifest.txt" || return 1
    if [ -n "$expected_generation" ]; then
        grep -Fqx "generation=$expected_generation" "$dir/manifest.txt" || return 1
    fi
    (cd "$dir" && sha256sum -c checksums.sha256 >/dev/null) || return 1
    pg_restore --list "$dir/postgres.dump" >/dev/null 2>&1 || return 1
}

checkpoint_prune_old() {
    local keep="${STACK_CHECKPOINT_KEEP:-12}" latest generation count=0
    case "$keep" in ''|*[!0-9]*) return 1 ;; esac
    [ "$keep" -ge 2 ] || keep=2
    latest="$(checkpoint_latest_name)" || return 1
    while IFS= read -r generation; do
        checkpoint_safe_generation "$generation" || continue
        [ "$generation" = "$latest" ] && continue
        count=$((count + 1))
        # Keep N total including latest. Only validated, safely named generation
        # directories outside the retention window are eligible for deletion.
        if [ "$count" -ge "$keep" ]; then
            checkpoint_validate_dir "$STACK_CHECKPOINT_ROOT/generations/$generation" "$generation" || continue
            rm -rf -- "$STACK_CHECKPOINT_ROOT/generations/$generation"
            checkpoint_log "pruned retained generation $generation"
        fi
    done < <(
        find "$STACK_CHECKPOINT_ROOT/generations" -mindepth 1 -maxdepth 1 \
            -type d -printf '%f\n' | grep -v '^\.' | LC_ALL=C sort -r
    )
}

checkpoint_create() {
    local reason="${1:-periodic}" generation tmp final latest_tmp consistency rc=0
    checkpoint_require_env || return 1
    checkpoint_lock || {
        echo "[checkpoint] ERROR: could not acquire checkpoint lock" >&2
        return 1
    }

    # PID alone can be reused across separate invocations within the same
    # second. Add Bash's per-process random value so a valid prior generation
    # is never selected as the target of a later `mv` by name collision.
    generation="$(date -u +%Y%m%dT%H%M%S)-${SLURM_JOB_ID:-manual}-$$-${RANDOM}"
    checkpoint_safe_generation "$generation" || {
        checkpoint_unlock
        return 1
    }
    tmp="$STACK_CHECKPOINT_ROOT/generations/.tmp-$generation"
    final="$STACK_CHECKPOINT_ROOT/generations/$generation"
    mkdir -p "$tmp/minio/$STACK_S3_BUCKET" "$tmp/minio/$STACK_S3_PUBLIC_BUCKET"
    checkpoint_log "creating $generation ($reason)"
    if [ -f "$STACK_RUNTIME_DIR/writers.stopped" ]; then
        consistency=quiesced
    else
        consistency=best_effort
    fi

    if ! PGPASSWORD="$STACK_PGADMIN_PASSWORD" pg_dump \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        -d "$STACK_PGDB" --format=custom --no-owner --no-acl \
        --file="$tmp/postgres.dump"; then
        echo "[checkpoint] ERROR: PostgreSQL dump failed" >&2
        rc=1
    fi
    if [ "$rc" -eq 0 ] && ! pg_restore --list "$tmp/postgres.dump" >/dev/null 2>&1; then
        echo "[checkpoint] ERROR: PostgreSQL dump validation failed" >&2
        rc=1
    fi

    if [ "$rc" -eq 0 ] && ! checkpoint_minio_mc mirror --overwrite \
        "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET" "$tmp/minio/$STACK_S3_BUCKET"; then
        echo "[checkpoint] ERROR: private MinIO mirror failed" >&2
        rc=1
    fi
    if [ "$rc" -eq 0 ] && ! checkpoint_minio_mc mirror --overwrite \
        "$STACK_MINIO_ALIAS/$STACK_S3_PUBLIC_BUCKET" "$tmp/minio/$STACK_S3_PUBLIC_BUCKET"; then
        echo "[checkpoint] ERROR: public MinIO mirror failed" >&2
        rc=1
    fi

    if [ "$rc" -eq 0 ]; then
        {
            printf 'format_version=1\n'
            printf 'generation=%s\n' "$generation"
            printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            printf 'slurm_job_id=%s\n' "${SLURM_JOB_ID:-manual}"
            printf 'reason=%s\n' "$reason"
            printf 'consistency=%s\n' "$consistency"
            printf 'postgres_format=custom\n'
            printf 'private_bucket=%s\n' "$STACK_S3_BUCKET"
            printf 'public_bucket=%s\n' "$STACK_S3_PUBLIC_BUCKET"
            printf 'redis_restore_policy=discard_inflight\n'
        } >"$tmp/manifest.txt"
        (
            cd "$tmp"
            find . -type f ! -name checksums.sha256 -print0 |
                LC_ALL=C sort -z |
                xargs -0 sha256sum >checksums.sha256
        ) || rc=1
    fi
    if [ "$rc" -eq 0 ] && ! checkpoint_validate_dir "$tmp" "$generation"; then
        echo "[checkpoint] ERROR: complete generation validation failed" >&2
        rc=1
    fi

    if [ "$rc" -eq 0 ]; then
        mv "$tmp" "$final"
        latest_tmp="$STACK_CHECKPOINT_ROOT/.latest.$generation"
        printf '%s\n' "$generation" >"$latest_tmp"
        chmod 600 "$latest_tmp"
        # Same-filesystem rename: readers see either the old last-good or this
        # fully validated generation, never a half-written checkpoint.
        mv -f "$latest_tmp" "$STACK_CHECKPOINT_ROOT/latest"
        checkpoint_log "committed $generation as latest (consistency=$consistency)"
        checkpoint_prune_old || checkpoint_log "retention pruning skipped/failed"
    else
        rm -rf -- "$tmp"
        checkpoint_log "failed generation discarded; previous latest unchanged"
    fi

    checkpoint_unlock
    return "$rc"
}

checkpoint_reset_database() {
    # Writers are not running when this is called. The bootstrap role owns the
    # database; the gateway login remains a non-superuser runtime role.
    PGPASSWORD="$STACK_PGADMIN_PASSWORD" dropdb \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        --if-exists "$STACK_PGDB"
    PGPASSWORD="$STACK_PGADMIN_PASSWORD" createdb \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        --owner="$STACK_PGADMIN" "$STACK_PGDB"
}

checkpoint_restore_legacy() {
    local legacy_pg legacy_minio marker
    legacy_pg="$(find "$STACK_CHECKPOINT_ROOT" -maxdepth 1 -type f \
        -name 'pg-[0-9]*.sql' -print | LC_ALL=C sort | tail -n 1)"
    [ -n "$legacy_pg" ] || return 10
    tail -n 8 "$legacy_pg" | grep -Fq 'PostgreSQL database dump complete' || {
        echo "[checkpoint] ERROR: latest legacy SQL dump lacks completion marker: $legacy_pg" >&2
        return 1
    }

    checkpoint_log "restoring legacy SQL checkpoint $(basename "$legacy_pg")"
    checkpoint_reset_database || return 1
    PGPASSWORD="$STACK_PGADMIN_PASSWORD" psql \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        -d "$STACK_PGDB" -v ON_ERROR_STOP=1 --single-transaction \
        -f "$legacy_pg" >/dev/null || return 1

    # The old stack had one bucket named lumilake-demo and mirrored it into a
    # mutable directory. Preserve every object by importing that directory into
    # the new private bucket; there was no legacy public bucket to restore.
    legacy_minio="$STACK_CHECKPOINT_ROOT/minio/lumilake-demo"
    if [ -d "$legacy_minio" ]; then
        checkpoint_minio_mc mirror --overwrite --remove \
            "$legacy_minio" "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET" || return 1
    fi
    marker="$STACK_RUNTIME_DIR/restore.complete"
    printf 'legacy:%s\n' "$(basename "$legacy_pg")" >"$marker"
    chmod 600 "$marker"
    checkpoint_log "legacy restore complete (single legacy bucket mapped to private; Redis discarded)"
}

checkpoint_restore_latest() {
    local generation dir marker legacy_status
    checkpoint_require_env || return 1
    checkpoint_lock || return 1

    if ! generation="$(checkpoint_latest_name)"; then
        if [ -e "$STACK_CHECKPOINT_ROOT/latest" ] || \
           [ -L "$STACK_CHECKPOINT_ROOT/latest" ]; then
            checkpoint_unlock
            echo "[checkpoint] ERROR: latest pointer exists but is unreadable/invalid; refusing empty restore" >&2
            return 1
        fi
        if checkpoint_restore_legacy; then
            legacy_status=0
        else
            # Capture the legacy function's status inside the `else`: the
            # status of a completed `if` without this branch is not a reliable
            # copy of the failed condition command in every Bash context.
            legacy_status=$?
        fi
        checkpoint_unlock
        if [ "$legacy_status" -eq 10 ]; then
            checkpoint_log "no latest checkpoint; starting from an empty database"
        fi
        return "$legacy_status"
    fi
    dir="$STACK_CHECKPOINT_ROOT/generations/$generation"
    if ! checkpoint_validate_dir "$dir" "$generation"; then
        checkpoint_unlock
        echo "[checkpoint] ERROR: latest generation $generation is invalid; refusing to seed over it" >&2
        return 1
    fi

    checkpoint_log "restoring validated generation $generation"
    if ! checkpoint_reset_database; then
        checkpoint_unlock
        return 1
    fi
    if ! PGPASSWORD="$STACK_PGADMIN_PASSWORD" pg_restore \
        -h 127.0.0.1 -p "$STACK_PG_PORT" -U "$STACK_PGADMIN" \
        -d "$STACK_PGDB" --no-owner --no-acl --exit-on-error \
        "$dir/postgres.dump"; then
        checkpoint_unlock
        echo "[checkpoint] ERROR: PostgreSQL restore failed" >&2
        return 1
    fi
    if ! checkpoint_minio_mc mirror --overwrite --remove \
        "$dir/minio/$STACK_S3_BUCKET" "$STACK_MINIO_ALIAS/$STACK_S3_BUCKET"; then
        checkpoint_unlock
        echo "[checkpoint] ERROR: private MinIO restore failed" >&2
        return 1
    fi
    if ! checkpoint_minio_mc mirror --overwrite --remove \
        "$dir/minio/$STACK_S3_PUBLIC_BUCKET" "$STACK_MINIO_ALIAS/$STACK_S3_PUBLIC_BUCKET"; then
        checkpoint_unlock
        echo "[checkpoint] ERROR: public MinIO restore failed" >&2
        return 1
    fi

    marker="$STACK_RUNTIME_DIR/restore.complete"
    printf '%s\n' "$generation" >"$marker"
    chmod 600 "$marker"
    checkpoint_unlock
    checkpoint_log "restore complete; Redis in-flight state intentionally discarded"
}

checkpoint_validate_latest() {
    local generation dir
    checkpoint_require_env || return 1
    generation="$(checkpoint_latest_name)" || return 1
    dir="$STACK_CHECKPOINT_ROOT/generations/$generation"
    checkpoint_validate_dir "$dir" "$generation"
}

checkpoint_cli() {
    local script_dir command="${1:-}"
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # shellcheck disable=SC1091
    source "$script_dir/stack-env.sh"
    case "$command" in
        create) checkpoint_create "${2:-manual}" ;;
        restore) checkpoint_restore_latest ;;
        validate) checkpoint_validate_latest ;;
        *) echo "usage: bash checkpoint.sh {create [reason]|restore|validate}" >&2; return 2 ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -euo pipefail
    checkpoint_cli "$@"
fi
