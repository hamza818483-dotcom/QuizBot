#!/bin/bash
cd ~/AtlasMasterBot
git add .
git commit -m "Auto-sync: $(date)" 2>/dev/null
git push origin main 2>/dev/null
echo "$(date): Auto-push done" >> ~/auto_push.log
