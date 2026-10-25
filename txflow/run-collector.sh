#!/usr/bin/env bash
# Run collect_flow.py inside the postfiatd container's network namespace (the admin WebSocket
# listens on the container's own 127.0.0.1:6006, which docker-proxy cannot reach), as an
# unprivileged user: nsenter enters only the net namespace, then drops to RUN_UID/RUN_GID.
# Usage: sudo RUN_UID=1000 RUN_GID=1000 bash run-collector.sh --out DIR [--log FILE] [--hours 24]
# If the container restarts, its namespace goes away, the stream errors out and this script
# exits non-zero once the collector notices; systemd (Restart=on-failure) re-runs it with the new PID.
set -euo pipefail
CONTAINER="${CONTAINER:-postfiatd}"
: "${RUN_UID:?set RUN_UID}" "${RUN_GID:?set RUN_GID}"
here="$(cd "$(dirname "$0")" && pwd)"
pid="$(docker inspect -f '{{.State.Pid}}' "$CONTAINER")"
[ "$pid" -gt 0 ] || { echo "container $CONTAINER not running" >&2; exit 1; }
exec nsenter -t "$pid" -n --setuid "$RUN_UID" --setgid "$RUN_GID" -- /usr/bin/python3 "$here/collect_flow.py" "$@"
