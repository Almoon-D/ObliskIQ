# Shared by deploy/backup.sh, deploy/restore.sh and deploy/update.sh: where things are on the
# machine, the owner's alert, and how an archive is made and put back.
#
# Every location and command can be overridden from the environment, which is how the tests run
# these scripts against stub docker, systemctl, rclone and curl.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

OBLISK_ETC=${OBLISK_ETC:-/etc/obliskiq}
OBLISK_DATA=${OBLISK_DATA:-/var/lib/obliskiq}
OBLISK_BACKUP_DIR=${OBLISK_BACKUP_DIR:-/var/backups/obliskiq}
OBLISK_BACKUP_KEEP=${OBLISK_BACKUP_KEEP:-14}
OBLISK_UID=${OBLISK_UID:-10001}
DOCKER=${DOCKER:-docker}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
RCLONE=${RCLONE:-rclone}
CURL=${CURL:-curl}
SQLITE3=${SQLITE3:-sqlite3}
ZSTD=${ZSTD:-zstd}

#: The databases a backup copies online; the small state files it copies as they are.
DATABASES=(obliskiq.db research.db)
STATE_FILES=(settings-audit.jsonl settings-applied.json alert-state.json KILL_SWITCH \
    FORCED_SHADOW PAUSED telegram-state.json manual-incidents.yaml board.json)
UNITS=(obliskiq.service obliskiq-cockpit.service)

log() { echo "obliskiq-deploy: $*" >&2; }

# One variable from the machine's environment file, without sourcing it.
env_value() {
    local file="$OBLISK_ETC/obliskiq.env"
    [ -r "$file" ] || return 0
    sed -n "s/^$1=//p" "$file" | tail -n 1
}

# Tell the owner: the journal always, Telegram when its token and chat are set. Never fails.
alert() {
    log "ALERT: $*"
    command -v logger >/dev/null 2>&1 && logger -t obliskiq-deploy "ALERT: $*" || true
    local token chat
    token=$(env_value TELEGRAM_BOT_TOKEN)
    chat=$(env_value TELEGRAM_CHAT_ID)
    if [ -n "$token" ] && [ -n "$chat" ]; then
        "$CURL" -fsS -m 10 -o /dev/null \
            --data-urlencode "chat_id=$chat" \
            --data-urlencode "text=ObliskIQ deploy: $*" \
            "https://api.telegram.org/bot$token/sendMessage" || log "the Telegram alert failed"
    fi
    return 0
}

# Make an archive of the store and the state: each database copied online with SQLite's backup,
# checked with integrity_check, then everything compressed with zstd. Prints the archive's path.
make_archive() {
    local stamp work archive db result
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    work=$(mktemp -d)
    mkdir -p "$OBLISK_BACKUP_DIR" "$work/data" "$work/owner"
    for db in "${DATABASES[@]}"; do
        [ -f "$OBLISK_DATA/$db" ] || continue
        "$SQLITE3" "$OBLISK_DATA/$db" ".backup '$work/data/$db'"
        result=$("$SQLITE3" "$work/data/$db" "PRAGMA integrity_check;")
        if [ "$result" != "ok" ]; then
            rm -rf "$work"
            alert "the backup of $db failed its integrity check: $result"
            return 1
        fi
    done
    for name in "${STATE_FILES[@]}"; do
        [ -e "$OBLISK_DATA/$name" ] && cp -p "$OBLISK_DATA/$name" "$work/data/"
    done
    [ -f "$OBLISK_ETC/owner/owner.yaml" ] && cp -p "$OBLISK_ETC/owner/owner.yaml" "$work/owner/"
    archive="$OBLISK_BACKUP_DIR/obliskiq-$stamp.tar.zst"
    tar -C "$work" -cf - data owner | "$ZSTD" -q -T0 -o "$archive"
    rm -rf "$work"
    chmod 600 "$archive"
    echo "$archive"
}

# Put an archive back: its databases (checked first) and state files replace the data
# directory's, and its owner settings the machine's. The services must be stopped.
restore_archive() {
    local archive=$1 work db result name
    work=$(mktemp -d)
    "$ZSTD" -q -d -c "$archive" | tar -C "$work" -xf -
    for db in "${DATABASES[@]}"; do
        [ -f "$work/data/$db" ] || continue
        result=$("$SQLITE3" "$work/data/$db" "PRAGMA integrity_check;")
        if [ "$result" != "ok" ]; then
            rm -rf "$work"
            alert "the archive $archive holds a damaged $db: nothing was restored"
            return 1
        fi
    done
    for db in "${DATABASES[@]}"; do
        rm -f "$OBLISK_DATA/$db-wal" "$OBLISK_DATA/$db-shm"
        if [ -f "$work/data/$db" ]; then
            cp -p "$work/data/$db" "$OBLISK_DATA/$db"
        fi
    done
    for name in "${STATE_FILES[@]}"; do
        if [ -e "$work/data/$name" ]; then
            cp -p "$work/data/$name" "$OBLISK_DATA/$name"
        else
            rm -f "$OBLISK_DATA/$name"
        fi
    done
    if [ -f "$work/owner/owner.yaml" ]; then
        mkdir -p "$OBLISK_ETC/owner"
        cp -p "$work/owner/owner.yaml" "$OBLISK_ETC/owner/owner.yaml"
    fi
    rm -rf "$work"
    chown -R "$OBLISK_UID:$OBLISK_UID" "$OBLISK_DATA" "$OBLISK_ETC/owner" 2>/dev/null || true
}
