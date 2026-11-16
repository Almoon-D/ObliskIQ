#!/usr/bin/env bash
# The nightly backup (obliskiq-backup.timer): an online copy of each database (SQLite's backup,
# so the running service is never stopped), checked with integrity_check, the state files and the
# owner's settings beside them, compressed with zstd, pushed off the machine with rclone to
# OBLISK_BACKUP_REMOTE (any rclone remote: an S3 bucket, B2, Google Drive, SFTP), and the
# newest OBLISK_BACKUP_KEEP archives kept here. A failure alerts the owner and exits non-zero.
#
#   backup.sh              back up and push off the machine
#   backup.sh --local-only back up here only (deploy/update.sh uses it before an update)
#
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

local_only=false
if [ "${1:-}" = "--local-only" ]; then
    local_only=true
fi

archive=$(make_archive) || exit 1
log "backed up to $archive"

if ! $local_only; then
    remote=${OBLISK_BACKUP_REMOTE:-$(env_value OBLISK_BACKUP_REMOTE)}
    if [ -z "$remote" ]; then
        alert "the backup $archive stayed on the machine: OBLISK_BACKUP_REMOTE is not set"
        exit 1
    fi
    if ! "$RCLONE" copy "$archive" "$remote"; then
        alert "the backup $archive could not be pushed to $remote"
        exit 1
    fi
    log "pushed to $remote"
fi

# Keep the newest archives here; the remote keeps its own (an rclone lifecycle rule).
mapfile -t archives < <(ls -1t "$OBLISK_BACKUP_DIR"/obliskiq-*.tar.zst 2>/dev/null)
if [ "${#archives[@]}" -gt "$OBLISK_BACKUP_KEEP" ]; then
    rm -f -- "${archives[@]:$OBLISK_BACKUP_KEEP}"
fi
echo "$archive"
