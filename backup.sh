#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Ensure backup drive is mounted
BACKUP_MOUNT="/media/stirunag/Mini"

if ! mountpoint -q "$BACKUP_MOUNT"; then
  echo "ERROR: Backup drive $BACKUP_MOUNT is NOT mounted at $(date)" >&2
  exit 1
fi

BACKUP_ROOT="/media/stirunag/Mini/hitl-tool"
DATE="$(date +%F)"
BACKUP_DIR="$BACKUP_ROOT/$DATE"

PROJECT_DIR="/home/stirunag/work/github/hitl-tool"
POSTGRES_CONTAINER="hitl-tool-postgres-1"
POSTGRES_USER="corpus"
POSTGRES_DB="corpusdb"

SOLR_VOLUME="hitl-tool_solrdata"
REDIS_VOLUME="hitl-tool_redis_data"

mkdir -p "$BACKUP_DIR"

echo "=== Backup started at $(date) ==="

# Postgres (logical dump)
docker exec "$POSTGRES_CONTAINER" \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  > "$BACKUP_DIR/postgres.sql"

# Solr volume
docker run --rm \
  -v "$SOLR_VOLUME":/data \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/solr_data.tar.gz /data

# Redis volume (optional but included)
docker run --rm \
  -v "$REDIS_VOLUME":/data \
  -v "$BACKUP_DIR":/backup \
  alpine \
  tar czf /backup/redis_data.tar.gz /data

# Config files
cp \
  "$PROJECT_DIR/docker-compose.yml" \
  "$PROJECT_DIR/.env" \
  "$BACKUP_DIR/"

chmod 600 "$BACKUP_DIR/.env"

echo "=== Backup completed at $(date) ==="
