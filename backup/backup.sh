#!/bin/sh
# n8n backup into restic: the db plus the encryption key, credentials are useless without it
#
#   backup.sh schedule        every day at $BACKUP_AT, time zone $TZ (the container's command)
#   backup.sh run             one backup now, only counts after the dump restores into a scratch server
#   backup.sh restore [ID]    snapshot ID (latest by default) into the empty db $PGDATABASE on $PGHOST,
#                             encryption key goes to /restore
#
# every run pushes n8n_backup_last_run_success (1 or 0) to the Pushgateway, on success also
# n8n_backup_last_success_timestamp_seconds, n8n_backup_duration_seconds and n8n_backup_dump_bytes
set -eu
PGPASSWORD=$(cat /run/secrets/postgres_password)
export PGPASSWORD
dir=/var/backup/n8n  # the same path in every snapshot

report() {
    if [ "$1" = 0 ]; then
        metrics="n8n_backup_last_run_success 1
n8n_backup_last_success_timestamp_seconds $(date +%s)
n8n_backup_duration_seconds $(($(date +%s) - started))
n8n_backup_dump_bytes $(stat -c %s "$dir/n8n.dump")"
    else
        metrics="n8n_backup_last_run_success 0"
    fi
    echo "$metrics" | curl -fsS --data-binary @- "$PUSHGATEWAY/metrics/job/n8n_backup" || echo "could not push metrics" >&2
}

count() {  # rows in table $1, the rest are psql args
    t=$1
    shift
    psql -X -At "$@" -c "SELECT count(*) FROM $t"
}

verify() {  # if the dump doesn't restore it's not a backup
    scratch="-h /tmp/verify -U postgres -d postgres"
    gosu postgres pg_ctl -D /tmp/verify/data -m immediate stop >/dev/null 2>&1 || true
    rm -rf /tmp/verify && mkdir /tmp/verify && chown postgres /tmp/verify
    gosu postgres initdb -D /tmp/verify/data -U postgres -A trust >/dev/null
    gosu postgres pg_ctl -D /tmp/verify/data -l /tmp/verify/log -w -o "-c listen_addresses='' -k /tmp/verify" start >/dev/null
    gosu postgres pg_restore $scratch --no-owner --exit-on-error <"$1"
    for t in workflow_entity credentials_entity execution_entity; do
        restored=$(count $t $scratch)
        live=$(count $t)
        echo "$t: $restored rows restored, $live live"
        # executions keep coming while the dump runs, workflows and credentials barely change
        [ "$restored" = "$live" ] || [ $t = execution_entity ] || { echo "$t differs" >&2; exit 1; }
    done
    gosu postgres pg_ctl -D /tmp/verify/data -m fast stop >/dev/null
    rm -rf /tmp/verify
}

run() {
    started=$(date +%s)
    trap 'report $?' EXIT
    rm -rf "$dir" && mkdir -p "$dir"
    # no compression here so restic can dedup against last night's dump, it compresses anyway
    pg_dump -Fc -Z0 -f "$dir/n8n.dump"
    cp /run/secrets/n8n_encryption_key "$dir/"
    verify "$dir/n8n.dump"
    restic cat config >/dev/null 2>&1 || restic init
    restic unlock  # locks left by a run that was killed
    restic backup --host n8n --tag n8n "$dir"
    restic forget --host n8n --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
    if [ "$(date +%u)" = 7 ]; then
        restic check --read-data-subset=10%
    fi
}

restore() {
    if [ "$(psql -X -At -c "SELECT to_regclass('workflow_entity') IS NULL")" != t ]; then
        echo "$PGDATABASE on $PGHOST already holds n8n: restore into an empty database" >&2
        exit 1
    fi
    rm -rf /tmp/restore
    restic restore "$1" --host n8n --target /tmp/restore
    pg_restore --no-owner --exit-on-error -d "$PGDATABASE" "/tmp/restore$dir/n8n.dump"
    install -m 644 "/tmp/restore$dir/n8n_encryption_key" /restore/
    rm -rf /tmp/restore
    echo "restored snapshot $1 into $PGDATABASE on $PGHOST, the encryption key into /restore"
}

case "${1:-}" in
schedule)
    echo "backing up every day at $BACKUP_AT ($TZ)"
    while :; do
        now=$(date +%s)
        at=$(date -d "$BACKUP_AT" +%s)
        [ "$at" -gt "$now" ] || at=$((at + 86400))
        sleep $((at - now))
        "$0" run || true  # a failed run shows up in the metrics
    done
    ;;
run) run ;;
restore) restore "${2:-latest}" ;;
*)
    echo "usage: backup.sh schedule | run | restore [snapshot]" >&2
    exit 2
    ;;
esac
