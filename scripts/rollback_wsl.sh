#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s --dry-run|--execute\n' "$0"
}

if [[ $# -ne 1 || ( "$1" != "--dry-run" && "$1" != "--execute" ) ]]; then
  usage >&2
  exit 2
fi

mode="$1"
if [[ ! -r /proc/version ]] || ! grep -qi microsoft /proc/version; then
  printf 'Refusing: this rollback must run inside WSL.\n' >&2
  exit 1
fi

home_real="$(realpath -e "$HOME")"
backup_dir="$home_real/outlook-triage-rollback-backups"
relative_targets=(
  "outlook-triage"
  ".config/outlook-triage"
  ".local/share/outlook-triage"
  ".local/state/outlook-triage"
)

existing_targets=()
for relative in "${relative_targets[@]}"; do
  candidate="$home_real/$relative"
  if [[ -e "$candidate" ]]; then
    resolved="$(realpath -e "$candidate")"
    case "$resolved" in
      "$home_real"/*) ;;
      *) printf 'Refusing unsafe target outside HOME: %s\n' "$resolved" >&2; exit 1 ;;
    esac
    existing_targets+=("$relative")
  fi
done

if [[ ${#existing_targets[@]} -eq 0 ]]; then
  printf 'No WSL Outlook-triage installation was found under %s.\n' "$home_real"
  exit 0
fi

printf 'Mode: %s\nTargets:\n' "$mode"
for relative in "${existing_targets[@]}"; do
  printf '  %s/%s\n' "$home_real" "$relative"
done
printf 'Backup directory: %s\n' "$backup_dir"

if [[ "$mode" == "--dry-run" ]]; then
  printf 'Dry run complete. Nothing was archived or removed.\n'
  exit 0
fi

if pgrep -af '[o]utlook-triage (sync|retry-failed|digest|classify-one)' >/dev/null 2>&1; then
  printf 'Refusing: an outlook-triage process is running.\n' >&2
  exit 1
fi
if [[ -e "$home_real/.local/state/outlook-triage/sync.lock" ]]; then
  printf 'Refusing: the synchronization lock exists. Resolve it before rollback.\n' >&2
  exit 1
fi

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp="$(date +%Y%m%d-%H%M%S)"
archive="$backup_dir/outlook-triage-wsl-$stamp.tar.gz"
manifest="$archive.contents.txt"
checksum="$archive.sha256"

tar -czf "$archive" \
  --exclude='outlook-triage/.venv' \
  --exclude='*/__pycache__' \
  --exclude='*/.pytest_cache' \
  --exclude='*/build' \
  --exclude='*/dist' \
  --exclude='*/htmlcov' \
  --exclude='*/.coverage' \
  -C "$home_real" "${existing_targets[@]}"

tar -tzf "$archive" > "$manifest"
sha256sum "$archive" > "$checksum"
(cd "$backup_dir" && sha256sum -c "$(basename "$checksum")")
chmod 600 "$archive" "$manifest" "$checksum"

for relative in "${existing_targets[@]}"; do
  target="$home_real/$relative"
  resolved="$(realpath -e "$target")"
  case "$resolved" in
    "$home_real"/*) rm -rf -- "$resolved" ;;
    *) printf 'Refusing unsafe removal target: %s\n' "$resolved" >&2; exit 1 ;;
  esac
done

printf 'WSL installation archived and removed.\n'
printf 'Archive: %s\n' "$archive"
printf 'Restore with:\n  tar -xzf %q -C %q\n' "$archive" "$home_real"

