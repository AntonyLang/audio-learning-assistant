#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/apps/audio-learning-assistant
backup_root=/var/backups/audio-learning-assistant
backup_dir="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
chmod 700 "$backup_root" "$backup_dir"
docker compose exec -T db pg_dump -U ala -d ala -Fc > "$backup_dir/database.dump"
docker compose exec -T api tar -C /data -czf - objects > "$backup_dir/objects.tar.gz"
cp .env compose.yaml Caddyfile "$backup_dir/"
docker compose images --format json > "$backup_dir/images.json"
sha256sum "$backup_dir/database.dump" "$backup_dir/objects.tar.gz" > "$backup_dir/SHA256SUMS"
printf 'Backup saved: %s\n' "$backup_dir"
