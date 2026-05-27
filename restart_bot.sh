#!/bin/bash
pkill -9 -f "python bot.py"
sleep 2
cd ~/AtlasMasterBot
python bot.py &
