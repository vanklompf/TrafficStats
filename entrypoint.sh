#!/bin/bash
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [ "$(id -u appuser)" != "$PUID" ] || [ "$(id -g appuser)" != "$PGID" ]; then
    groupmod -o -g "$PGID" appuser
    usermod -o -u "$PUID" appuser
fi

chown appuser:appuser /data

if [ -c /dev/dri/renderD128 ]; then
    RENDER_GID=$(stat -c '%g' /dev/dri/renderD128)
    if ! getent group "$RENDER_GID" > /dev/null 2>&1; then
        groupadd -g "$RENDER_GID" render_host
    fi
    usermod -aG "$RENDER_GID" appuser
fi

exec gosu appuser "$@"
