#!/bin/bash
cd ~/AtlasMasterBot
git pull origin main
sed -i '/from google import genai/d' config.py services.py 2>/dev/null
sed -i '/from playwright/d' services.py 2>/dev/null
pkill -f "python bot.py" 2>/dev/null
sleep 2
python bot.py &
echo "✅ Bot Updated"
cp data/backup_newest.db data/atlas_bot.db 2>/dev/null
