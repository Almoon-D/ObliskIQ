#!/usr/bin/env bash
# Restore the store, the state files and the owner's settings from a backup archive:
#
#   restore.sh /var/backups/obliskiq/obliskiq-20261116T031700Z.tar.zst
#
# Fetch an off-machine archive first (rclone copy <remote>/<name> /var/backups/obliskiq/). The
# services are stopped, the archive's databases are checked with integrity_check before anything
# is replaced, and the services are started again. docs/DEPLOY.md "Restore from a backup".
#
# SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

archive=${1:?usage: restore.sh <archive.tar.zst>}
[ -f "$archive" ] || { log "no archive at $archive"; exit 2; }

"$SYSTEMCTL" stop "${UNITS[@]}"
if restore_archive "$archive"; then
    log "restored from $archive"
    status=0
else
    status=1
fi
"$SYSTEMCTL" start "${UNITS[@]}"
exit "$status"
