#!/usr/bin/env bash
# `obliskiq` on the machine: the command line in a one-off container of the image the units run,
# with the same environment file, store and owner's settings. Installed as /usr/local/bin/obliskiq
# (docs/DEPLOY.md), so every command in docs/RUNBOOK.md runs as written, without `uv run`.
#
# Commands that open a brokerage session (preflight, reconcile, flatten, run-cycle) take the
# service lock in the shared data directory, so they refuse while the service runs: stop it first.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
OBLISK_ETC=${OBLISK_ETC:-/etc/obliskiq}
OBLISK_DATA=${OBLISK_DATA:-/var/lib/obliskiq}
image=$(sed -n 's/^OBLISK_IMAGE=//p' "$OBLISK_ETC/image.env")
tag=$(sed -n 's/^OBLISK_IMAGE_TAG=//p' "$OBLISK_ETC/image.env")
tty=()
if [ -t 0 ] && [ -t 1 ]; then
    tty=(-it)
fi
exec docker run --rm "${tty[@]}" \
    --env-file "$OBLISK_ETC/obliskiq.env" \
    --volume "$OBLISK_DATA:/var/lib/obliskiq" \
    --volume "$OBLISK_ETC/owner:/etc/obliskiq/owner" \
    --shm-size 256m \
    "$image:$tag" "$@"
