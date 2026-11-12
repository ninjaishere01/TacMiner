#!/bin/sh
set -e

cd /opt/tacminer

DB_NAME=$(grep '^DB_NAME=' .env | cut -d= -f2- | tr -d "'\"")
DB_USER=$(grep '^DB_USER=' .env | cut -d= -f2- | tr -d "'\"")
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2- | tr -d "'\"")
BACKUP_CHAT=$(grep '^BACKUP_CHAT_ID=' .env | cut -d= -f2- | tr -d "'\"")

if [ -z "$DB_NAME" ] || [ -z "$DB_USER" ] || [ -z "$BOT_TOKEN" ] || [ -z "$BACKUP_CHAT" ]; then
    echo "backup: missing DB_NAME/DB_USER/TELEGRAM_BOT_TOKEN/BACKUP_CHAT_ID in .env" >&2
    exit 1
fi

mkdir -p backups
FILE="backups/db-$(date +%F-%H%M).sql.gz"
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "$FILE"

if [ ! -s "$FILE" ]; then
    echo "backup: dump file is empty" >&2
    exit 1
fi

# keep the last 7 backups on disk
ls -1t backups/db-*.sql.gz | tail -n +8 | xargs -r rm -f

# send a private copy to the owner's chat with the bot
curl -sf -F "chat_id=$BACKUP_CHAT" \
    -F "caption=نسخة احتياطية لقاعدة بيانات TAC Miner - $(date +%F)" \
    -F "document=@$FILE" \
    "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" > /dev/null

echo "backup: sent $FILE"
