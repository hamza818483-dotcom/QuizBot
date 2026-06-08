#!/bin/bash
CF_TOKEN="cfut_FGraQfSZRQyYYrFicvB6uPOBRuIkmAf9goNL04Qw12e3e776"
CF_ACCOUNT="acb7a8f8fc00546ef672527a692d89fc"
WORKER_NAME="atlas-mcq-worker"
SCRIPT="$HOME/cloudflare-worker/worker.js"

echo "🚀 Deploying Worker..."
curl -s -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/workers/scripts/$WORKER_NAME" \
  -H "Authorization: Bearer $CF_TOKEN" \
  -F "metadata={\"main_module\":\"worker.js\",\"bindings\":[{\"type\":\"d1\",\"name\":\"DB\",\"database_id\":\"9b866cf1-d4e8-440c-a2f8-33f2b826e383\"},{\"type\":\"queue\",\"name\":\"NEXT_QUEUE\",\"queue_name\":\"next-queue\"}]};type=application/json" \
  -F "worker.js=@$SCRIPT;type=application/javascript+module" | grep -E '"success"|"message"'

echo ""
echo "🔗 Setting Webhook..."
curl -s "https://api.telegram.org/bot8672553290:AAGVPBir4iqGFi5NEQeIHd5-rYto82XQ4jU/setWebhook?url=https://atlas-mcq-worker.hamza818483.workers.dev/webhook"

echo ""
echo "✅ Done!"
