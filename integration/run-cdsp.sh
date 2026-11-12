#!/bin/sh
# Compatibility entrypoint; implementation belongs to the installed session package.
set -eu
source_entry=$(dirname -- "$(readlink -f -- "$0")")/../src/run-cdsp.sh
if [ -f "$source_entry" ]; then exec "$source_entry" "$@"; fi
exec /usr/lib/xr/session-manager/run-cdsp.sh "$@"
