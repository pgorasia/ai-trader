#!/usr/bin/env bash
set -euo pipefail

project=/home/ubuntu/projects/ai-trader
destination=/home/ubuntu/ai-trader-backups
retention_days=30
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$destination/ai-trader-safe-state-$timestamp.tar.gz"

mkdir -p "$destination"
tar --create --gzip --file "$archive" --directory "$project" --exclude='state/heartbeat.json' --exclude='*.tmp' --exclude='*cache*' state reports logs
contents="$(tar --list --gzip --file "$archive")"
if grep -Eiq '(^|/)(auth\.json|\.venv|oauth|token|cache)(/|$)' <<<"$contents"; then
  echo "Backup validation rejected a credential/cache path" >&2
  rm -f "$archive"
  exit 2
fi
find "$destination" -maxdepth 1 -type f -name 'ai-trader-safe-state-*.tar.gz' -mtime +"$retention_days" -delete
chmod 600 "$archive"
printf '%s\n' "$archive"
