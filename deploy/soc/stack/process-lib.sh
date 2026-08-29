# Exact process ownership and lifecycle helpers for the stack.
# shellcheck shell=bash

if [ -z "${STACK_RUNTIME_DIR:-}" ]; then
    echo "[process-lib] source stack-env.sh first" >&2
    return 1 2>/dev/null || exit 1
fi

export STACK_PID_DIR="${STACK_PID_DIR:-$STACK_RUNTIME_DIR/pids}"
mkdir -p "$STACK_PID_DIR"
chmod 700 "$STACK_PID_DIR"

process_log() { echo "[process] $*"; }

process_name_is_safe() {
    case "$1" in
        ''|*[!A-Za-z0-9_.-]*) return 1 ;;
        *) return 0 ;;
    esac
}

process_start_time() {
    local pid="$1"
    [ -r "/proc/$pid/stat" ] || return 1
    # Stack service comm values (python, minio, redis-server, bash) contain no
    # spaces, so field 22 is stable here. Deployment-id verification below is
    # the authoritative ownership check.
    awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

process_pgid() {
    ps -o pgid= -p "$1" 2>/dev/null | tr -d ' '
}

process_state() {
    [ -r "/proc/$1/stat" ] || return 1
    awk '{print $3}' "/proc/$1/stat" 2>/dev/null
}

process_identity_alive() {
    local pid="$1" expected_start="$2" actual_start state
    kill -0 "$pid" 2>/dev/null || return 1
    actual_start="$(process_start_time "$pid")" || return 1
    [ "$actual_start" = "$expected_start" ] || return 1
    state="$(process_state "$pid")" || return 1
    [ "$state" != "Z" ]
}

process_has_deployment() {
    local pid="$1"
    [ -r "/proc/$pid/environ" ] || return 1
    tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null |
        grep -Fqx "STACK_DEPLOYMENT_ID=$STACK_DEPLOYMENT_ID"
}

write_process_record() {
    local name="$1" pid="$2" start pgid tmp value
    process_name_is_safe "$name" || return 1
    start="$(process_start_time "$pid")" || return 1
    pgid="$(process_pgid "$pid")"
    for value in "$pid" "$pgid" "$start"; do
        case "$value" in ''|*[!0-9]*) return 1 ;; esac
    done
    tmp="$STACK_PID_DIR/.${name}.pid.$$"
    printf '%s %s %s %s\n' "$pid" "$pgid" "$start" "$STACK_DEPLOYMENT_ID" >"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$STACK_PID_DIR/$name.pid"
}

read_process_record() {
    local name="$1" value
    process_name_is_safe "$name" || return 1
    [ -r "$STACK_PID_DIR/$name.pid" ] || return 1
    read -r STACK_RECORD_PID STACK_RECORD_PGID STACK_RECORD_START STACK_RECORD_DEPLOYMENT \
        <"$STACK_PID_DIR/$name.pid" || return 1
    for value in "$STACK_RECORD_PID" "$STACK_RECORD_PGID" "$STACK_RECORD_START"; do
        case "$value" in ''|*[!0-9]*) return 1 ;; esac
    done
    [ "$STACK_RECORD_DEPLOYMENT" = "$STACK_DEPLOYMENT_ID" ]
}

tracked_process_alive() {
    local name="$1" actual_start actual_pgid
    read_process_record "$name" || return 1
    process_identity_alive "$STACK_RECORD_PID" "$STACK_RECORD_START" || return 1
    actual_start="$(process_start_time "$STACK_RECORD_PID")" || return 1
    actual_pgid="$(process_pgid "$STACK_RECORD_PID")"
    [ "$actual_start" = "$STACK_RECORD_START" ] || return 1
    [ "$actual_pgid" = "$STACK_RECORD_PGID" ] || return 1
    process_has_deployment "$STACK_RECORD_PID"
}

process_identity_conclusively_gone() {
    # Return success only when the recorded Linux process identity is known to
    # be dead (including zombie) or the PID has been reused. A transient /proc
    # read, environ, or PGID verification failure is deliberately *not* stale
    # evidence and must not authorize deleting the ownership record.
    local pid="$1" expected_start="$2" actual_start state
    [ -e "/proc/$pid" ] || return 0
    actual_start="$(process_start_time "$pid")" || return 1
    [ "$actual_start" != "$expected_start" ] && return 0
    state="$(process_state "$pid")" || return 1
    [ "$state" = "Z" ]
}

remove_conclusively_stale_record() {
    local name="$1" record="$STACK_PID_DIR/$name.pid"
    [ -e "$record" ] || return 0
    if ! read_process_record "$name"; then
        echo "[process] ERROR: retaining unreadable/untrusted $name pid record; manual inspection required" >&2
        return 1
    fi
    if process_identity_conclusively_gone "$STACK_RECORD_PID" "$STACK_RECORD_START"; then
        process_log "removing conclusively stale $name pid record; no process was signalled"
        rm -f "$record" "$STACK_PID_DIR/$name.descendants"
        return 0
    fi
    echo "[process] ERROR: $name record still names a live identity but ownership verification failed; record retained and no process was signalled" >&2
    return 1
}

is_port_open() {
    local port="$1"
    (exec 7<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && {
        exec 7>&- 7<&-
        return 0
    }
    return 1
}

assert_port_free_or_owned() {
    local name="$1" port="$2"
    if ! is_port_open "$port"; then
        return 0
    fi
    if tracked_process_alive "$name"; then
        return 0
    fi
    echo "[process] ERROR: port $port is occupied by a process not owned by deployment $STACK_DEPLOYMENT_ID" >&2
    return 1
}

start_tracked_service() {
    local name="$1" logfile="$2" pid
    shift 2
    process_name_is_safe "$name" || {
        echo "[process] invalid service name: $name" >&2
        return 1
    }
    if tracked_process_alive "$name"; then
        process_log "$name already running as pid $STACK_RECORD_PID"
        return 0
    fi
    remove_conclusively_stale_record "$name" || return 1
    mkdir -p "$(dirname "$logfile")"
    process_log "starting $name"
    setsid "$@" >>"$logfile" 2>&1 &
    pid=$!
    sleep 0.2
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || true
        echo "[process] ERROR: $name exited during startup; see $logfile" >&2
        return 1
    fi
    process_has_deployment "$pid" || {
        echo "[process] ERROR: $name pid $pid did not inherit STACK_DEPLOYMENT_ID" >&2
        kill "$pid" 2>/dev/null || true
        return 1
    }
    write_process_record "$name" "$pid" || {
        echo "[process] ERROR: could not record $name pid $pid" >&2
        kill "$pid" 2>/dev/null || true
        return 1
    }
}

collect_descendants() {
    local parent="$1" child child_start child_pgid
    command -v pgrep >/dev/null 2>&1 || return 0
    while read -r child; do
        [ -n "$child" ] || continue
        child_start="$(process_start_time "$child")" || {
            collect_descendants "$child"
            continue
        }
        child_pgid="$(process_pgid "$child")"
        case "$child:$child_start:$child_pgid" in
            *[!0-9:]*|*::*|:*|*:) collect_descendants "$child"; continue ;;
        esac
        printf '%s %s %s\n' "$child" "$child_start" "$child_pgid"
        collect_descendants "$child"
    done < <(pgrep -P "$parent" 2>/dev/null || true)
}

signal_owned_pid_group() {
    local pid="$1" signal="$2" expected_start="${3:-}" expected_pgid="${4:-}" pgid shell_pgid
    kill -0 "$pid" 2>/dev/null || return 0
    if [ -n "$expected_start" ]; then
        process_identity_alive "$pid" "$expected_start" || return 0
    fi
    process_has_deployment "$pid" || {
        echo "[process] refusing to signal unverified pid $pid" >&2
        return 1
    }
    pgid="$(process_pgid "$pid")"
    shell_pgid="$(process_pgid $$)"
    case "$pgid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ -n "$expected_pgid" ] && [ "$pgid" != "$expected_pgid" ]; then
        echo "[process] refusing to signal pid $pid after process-group identity changed" >&2
        return 1
    fi
    [ "$pgid" != "$shell_pgid" ] || {
        echo "[process] refusing to signal current shell process group $pgid" >&2
        return 1
    }
    kill -s "$signal" -- "-$pgid" 2>/dev/null || true
}

stop_tracked_service() {
    local name="$1" timeout="${2:-20}" pid recorded_start recorded_pgid \
        descendants_file deadline child child_start child_pgid
    process_name_is_safe "$name" || return 1
    if ! tracked_process_alive "$name"; then
        remove_conclusively_stale_record "$name"
        return $?
    fi

    pid="$STACK_RECORD_PID"
    recorded_start="$STACK_RECORD_START"
    recorded_pgid="$STACK_RECORD_PGID"
    descendants_file="$STACK_RUNTIME_DIR/.${name}.descendants.$$"
    {
        printf '%s %s %s\n' "$pid" "$recorded_start" "$recorded_pgid"
        collect_descendants "$pid"
    } >"$descendants_file"
    process_log "stopping $name pid $pid (TERM, ${timeout}s grace)"
    signal_owned_pid_group "$pid" TERM "$recorded_start" "$recorded_pgid" || {
        rm -f "$descendants_file"
        return 1
    }

    deadline=$((SECONDS + timeout))
    while process_identity_alive "$pid" "$recorded_start" && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 1
    done

    # Native workers create their own sessions. They should exit during the
    # supervisor's graceful shutdown; only descendants captured before TERM
    # are eligible for the exact fallback below.
    if process_identity_alive "$pid" "$recorded_start"; then
        process_log "$name exceeded grace period; killing verified process groups"
    fi
    while read -r child child_start child_pgid; do
        [ -n "$child" ] || continue
        if process_identity_alive "$child" "$child_start" && \
           [ "$(process_pgid "$child")" = "$child_pgid" ] && \
           process_has_deployment "$child"; then
            signal_owned_pid_group "$child" KILL "$child_start" "$child_pgid" || true
        fi
    done <"$descendants_file"

    if process_identity_alive "$pid" "$recorded_start"; then
        sleep 1
    fi
    if process_identity_alive "$pid" "$recorded_start"; then
        # Keep the trusted record and captured descendants so a later cleanup
        # can investigate/retry; never discard the only exact ownership proof.
        mv "$descendants_file" "$STACK_PID_DIR/$name.descendants" 2>/dev/null || true
        echo "[process] ERROR: verified $name pid $pid survived KILL; pid record retained" >&2
        return 1
    fi
    rm -f "$descendants_file" "$STACK_PID_DIR/$name.descendants" "$STACK_PID_DIR/$name.pid"
    return 0
}

required_stack_processes_alive() {
    local service
    for service in minio redis gateway flowmesh lumilake; do
        tracked_process_alive "$service" || {
            echo "[process] required service $service is not owned/alive" >&2
            return 1
        }
    done
    [ -f "$STACK_PGDATA/PG_VERSION" ] && pg_ctl -D "$STACK_PGDATA" status >/dev/null 2>&1
}
