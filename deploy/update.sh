#!/usr/bin/env bash
# Update the machine to a release, or roll back by itself (MASTER_PLAN §5.20):
#
#   update.sh 0.28.0        (or v0.28.0)
#
# 1. pull ghcr.io/almoon-d/obliskiq:<tag>; nothing else changes if the pull fails;
# 2. read the kill switch's state and back up the store (deploy/backup.sh --local-only);
# 3. stop the service and the cockpit, point /etc/obliskiq/image.env at the new tag and start
#    them (the store's migrations run on start);
# 4. wait up to OBLISK_UPDATE_TIMEOUT seconds for health: a heartbeat written after the update
#    began (`obliskiq health --since`), `obliskiq status` succeeding, the kill switch as it was;
# 5. on failure: stop them, restore the backup, start the previous tag and alert the owner.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

OBLISK_UPDATE_TIMEOUT=${OBLISK_UPDATE_TIMEOUT:-900}
OBLISK_UPDATE_POLL=${OBLISK_UPDATE_POLL:-15}
#: The service's container (deploy/obliskiq.service names it).
CONTAINER=obliskiq
IMAGE_ENV="$OBLISK_ETC/image.env"

tag=${1:?usage: update.sh <release version, e.g. 0.28.0>}
tag=${tag#v}
[ -f "$IMAGE_ENV" ] || { log "no $IMAGE_ENV: install first (docs/DEPLOY.md)"; exit 2; }
image=$(sed -n 's/^OBLISK_IMAGE=//p' "$IMAGE_ENV")
previous=$(sed -n 's/^OBLISK_IMAGE_TAG=//p' "$IMAGE_ENV")
[ -n "$image" ] && [ -n "$previous" ] || { log "$IMAGE_ENV names no image and tag"; exit 2; }
if [ "$tag" = "$previous" ]; then
    log "already on $tag"
    exit 0
fi

write_tag() {
    printf 'OBLISK_IMAGE=%s\nOBLISK_IMAGE_TAG=%s\n' "$image" "$1" > "$IMAGE_ENV.tmp"
    mv "$IMAGE_ENV.tmp" "$IMAGE_ENV"
}

# "kill switch ON" or "kill switch OFF", from the running container or, when it is down, from a
# one-off container of the tag running now.
kill_state() {
    local found
    if found=$("$DOCKER" exec "$CONTAINER" obliskiq kill status 2>/dev/null); then
        echo "$found" | head -n 1
        return 0
    fi
    "$DOCKER" run --rm --env-file "$OBLISK_ETC/obliskiq.env" \
        -v "$OBLISK_DATA:/var/lib/obliskiq" -v "$OBLISK_ETC/owner:/etc/obliskiq/owner" \
        "$image:$previous" kill status | head -n 1
}

# Healthy within the timeout: a fresh heartbeat, a status that reads, the kill switch kept.
wait_healthy() {
    local since=$1 kill_before=$2 deadline now kill_now
    deadline=$(( $(date +%s) + OBLISK_UPDATE_TIMEOUT ))
    while :; do
        if "$DOCKER" exec "$CONTAINER" obliskiq health --since "$since" >/dev/null 2>&1 \
            && "$DOCKER" exec "$CONTAINER" obliskiq status >/dev/null 2>&1; then
            kill_now=$("$DOCKER" exec "$CONTAINER" obliskiq kill status 2>/dev/null | head -n 1)
            if [ "$kill_now" = "$kill_before" ]; then
                return 0
            fi
            why="the kill switch was '$kill_before' and is '$kill_now'"
            return 1
        fi
        now=$(date +%s)
        if [ "$now" -ge "$deadline" ]; then
            why="no fresh, healthy heartbeat within ${OBLISK_UPDATE_TIMEOUT} s"
            return 1
        fi
        sleep "$OBLISK_UPDATE_POLL"
    done
}

if ! "$DOCKER" pull "$image:$tag"; then
    alert "update to $tag refused: $image:$tag could not be pulled; still on $previous"
    exit 1
fi
kill_before=$(kill_state)
backup=$("$HERE/backup.sh" --local-only) || {
    alert "update to $tag refused: the backup failed; still on $previous"
    exit 1
}
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
log "updating $previous -> $tag (backup $backup, $kill_before)"
"$SYSTEMCTL" stop "${UNITS[@]}"
write_tag "$tag"
"$SYSTEMCTL" start "${UNITS[@]}"

why=""
if wait_healthy "$started" "$kill_before"; then
    log "updated to $tag"
    exit 0
fi

"$SYSTEMCTL" stop "${UNITS[@]}"
restored="restored $backup"
restore_archive "$backup" || restored="the backup $backup could NOT be restored"
write_tag "$previous"
"$SYSTEMCTL" start "${UNITS[@]}"
alert "update to $tag failed ($why): rolled back to $previous; $restored"
exit 1
