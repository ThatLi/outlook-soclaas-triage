#!/usr/bin/env bash
set -euo pipefail

rollback_script="$1"
fake_home="$(mktemp -d)"
restore_dir="$(mktemp -d)"
trap 'rm -rf "$fake_home" "$restore_dir"' EXIT

mkdir -p "$fake_home/outlook-triage/.venv" \
         "$fake_home/outlook-triage/src" \
         "$fake_home/.config/outlook-triage" \
         "$fake_home/.local/share/outlook-triage/reports" \
         "$fake_home/.local/state/outlook-triage"
printf source > "$fake_home/outlook-triage/src/app.py"
printf disposable > "$fake_home/outlook-triage/.venv/python"
printf secret > "$fake_home/.config/outlook-triage/secrets.env"
printf database > "$fake_home/.local/share/outlook-triage/triage.db"
printf report > "$fake_home/.local/share/outlook-triage/reports/report.md"
printf log > "$fake_home/.local/state/outlook-triage/app.log"

HOME="$fake_home" bash "$rollback_script" --dry-run
test -f "$fake_home/outlook-triage/src/app.py"
test ! -e "$fake_home/outlook-triage-rollback-backups"

HOME="$fake_home" bash "$rollback_script" --execute
test ! -e "$fake_home/outlook-triage"
test ! -e "$fake_home/.config/outlook-triage"
test ! -e "$fake_home/.local/share/outlook-triage"
test ! -e "$fake_home/.local/state/outlook-triage"

archive="$(find "$fake_home/outlook-triage-rollback-backups" -name '*.tar.gz' -type f)"
test -n "$archive"
archive_listing="$fake_home/archive-contents.txt"
tar -tzf "$archive" > "$archive_listing"
grep -q 'outlook-triage/src/app.py' "$archive_listing"
if grep -q 'outlook-triage/.venv' "$archive_listing"; then
  exit 1
fi
(cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256")
tar -xzf "$archive" -C "$restore_dir"
test -f "$restore_dir/outlook-triage/src/app.py"
test -f "$restore_dir/.config/outlook-triage/secrets.env"
test -f "$restore_dir/.local/share/outlook-triage/triage.db"

