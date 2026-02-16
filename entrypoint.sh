#!/bin/bash
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [ "$(id -u appuser)" != "$PUID" ] || [ "$(id -g appuser)" != "$PGID" ]; then
    groupmod -o -g "$PGID" appuser
    usermod -o -u "$PUID" appuser
fi

chown appuser:appuser /data

exec gosu appuser "$@"
