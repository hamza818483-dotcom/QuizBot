# 🤖 ATLAS BOT - Complete Setup Guide

## 📋 Features
- ✅ Image MCQ Generation (/img)
- ✅ Text MCQ Generation (/txt)
- ✅ 7 Prompt Templates
- ✅ CSV to Poll (/csv, /csvS)
- ✅ PDF MCQ Extraction (/pdfm, /qbm)
- ✅ 4 PDF Formats
- ✅ MHTML to CSV Export
- ✅ Admin & Channel Management
- ✅ Broadcast System
- ✅ Gemini & ImgBB Key Rotation

---

## 📁 File Structure

```
atlas-bot/
├── bot.py                  # Main entry
├── config.py               # Config + DB + Key managers
├── csv_handler.py          # CSV to Poll (complete)
├── pdf_handler.py          # All 4 PDF formats + /pdfm + /qbm
├── mhtml_handler.py        # MHTML/HTML to CSV
├── handlers_core.py        # Core commands
├── handlers_admin.py       # Admin commands
├── handlers_tools.py       # Tool commands
├── services.py             # Utilities
├── .env                    # Credentials
├── requirements.txt        # Dependencies
└── README.md              # This file
```

---

## 🚀 Installation

### VPS Setup (Ubuntu/Debian)

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python
sudo apt install python3 python3-pip python3-venv -y

# Create directory
mkdir atlas-bot && cd atlas-bot

# Upload all files here

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright
playwright install chromium
playwright install-deps chromium

# Create data directories
mkdir -p data/temp data/thumbnails

# Run bot
python bot.py
```

---

## ⚙️ Configuration

### .env File
Already created with your credentials. Edit if needed:

```env
TELEGRAM_BOT_TOKEN=your_token_here
OWNER_ID=your_user_id
GEMINI_API_KEYS=key1,key2,key3...
IMGBB_API_KEYS=key1,key2,key3...
GEMINI_MODEL=gemini-2.0-flash-exp
```

---

## 🔧 Run as 24/7 Service

### Create systemd service:

```bash
sudo nano /etc/systemd/system/atlas-bot.service
```

### Paste this:

```ini
[Unit]
Description=ATLAS Telegram Bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/atlas-bot
ExecStart=/root/atlas-bot/venv/bin/python bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable atlas-bot
sudo systemctl start atlas-bot
sudo systemctl status atlas-bot
```

### Control commands:

```bash
sudo systemctl stop atlas-bot     # Stop
sudo systemctl restart atlas-bot  # Restart
sudo journalctl -u atlas-bot -f   # View logs
```

---

## 📱 Commands

### Core Commands:
- `/start` - Welcome message with all features
- `/img` - Generate MCQs from image
- `/txt` - Generate MCQs from text
- `/prompt` - Manage 7 prompt templates
- `/exp` - Explanation settings (Auto/Custom/Tag)
- `/tag` - Tag position settings

### CSV & Poll:
- `/csv` - Upload CSV and send polls
- `/csvS [batch] [channel] [topic]` - Send in batches

### PDF:
- `/pdfm -p 1-10 -m "Title"` - Create MCQs from PDF
- `/qbm` - Extract existing questions from PDF

### Admin:
- `/permit [user_id]` - Add admin
- `/broadcast` - Broadcast messages
- `/channel` - Manage channels

### Tools:
- `/thumb` - Set thumbnail
- `/ping` - Bot status
- `/error` - Health check
- `/collect` - Start CSV collection

---

## 🗄️ Database

SQLite database created automatically at `data/atlas_bot.db`

Tables:
- admins
- channels
- prompts (7 default prompts)
- exp_settings
- tag_settings
- thumbnail
- bot_users
- sessions

---

## 🔑 Key Rotation

### Gemini API:
- 11 keys configured
- Auto-rotation on failure
- 3 failures = key marked unhealthy

### ImgBB API:
- 7 keys configured
- Round-robin rotation
- Auto-retry on failure

---

## 📊 Poll Format

### Header:
```
🌟Important Poll Solve By ATLAS
🔥Topic Name: [topic]
✅প্রশ্ন সংখ্যা: [count]
```

### Ending Message:
```
🎉 ধন্যবাদ প্রিয় শিক্ষার্থী!
📊 মোট পোল: [count]
⁉️তোমার স্কোর কত? 🤔
```

---

## 🐛 Troubleshooting

### Bot not starting?
```bash
# Check logs
sudo journalctl -u atlas-bot -n 50

# Check Python
python3 --version

# Reinstall dependencies
pip install -r requirements.txt --force-reinstall
```

### Playwright errors?
```bash
# Reinstall Playwright
playwright install chromium
playwright install-deps chromium
```

### Database errors?
```bash
# Delete and recreate
rm data/atlas_bot.db
python bot.py  # Will auto-create
```

---

## 📞 Support

- Owner ID: 5341425626
- Bot Token: 8951742110:AAF7...

---

## 🎯 Version: 2.0

Complete feature bot with all functionalities!

---

**Made with ❤️ for educational purposes**
