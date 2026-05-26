#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS MCQ BOT v3.0 — Main Entry Point"""

import asyncio
import logging
import os
import sys
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# Config
from config import Config, db, gemini_manager, imgbb_manager

# Core Handlers
from core_handlers import (
    start_handler, img_handler, txt_handler, prompt_handler,
    handle_core_callbacks, handle_edit_message
)

# Admin Handlers
from admin_handlers import (
    permit_handler, adminlist_handler, broadcast_handler, channel_handler,
    handle_admin_callbacks, handle_broadcast_message
)

# Tools Handlers
from tools_handlers import (
    split_handler, merge_handler, convert_handler, rename_handler,
    watermark_handler, exp_handler, tag_handler, thumb_handler,
    sheet_handler, ping_handler, error_handler_cmd, logs_handler,
    collect_handler, done_handler, status_handler, cancel_collection_handler,
    pause_handler, resume_handler, restart_handler,
    handle_tools_callbacks, handle_settings_message
)

# PDF Handler
from pdf_handler import (
    pdfm_handler, qbm_handler, handle_pdf_callbacks
)

# MHTML Handler
from mhtml_handler import mhtml_handler, queue_mhtml, mhtml_worker

# CSV Poll Handler
from csv_poll_handler import (
    csv_handler, csvs_handler, csvi_handler, csvis_handler,
    handle_csv_callbacks
)

# CSV Sheet Handler
from csv_sheet_handler import (
    sheet_handler as csv_sheet_handler,
    handle_sheet_callbacks, handle_sheet_title
)

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    format='%(asctime)s | %(levelname)s | %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('data/bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

BOT_START_TIME = datetime.now()

# ============================================================
# POST INIT
# ============================================================
async def post_init(app: Application):
    """Initialize database and services after bot starts"""
    await db.initialize()
    
    # Start MHTML worker
    asyncio.create_task(mhtml_worker())
    
    # Create data folders
    os.makedirs('data/temp', exist_ok=True)
    os.makedirs('data/thumbnails', exist_ok=True)
    
    logger.info("✅ ATLAS Bot Database & Services Ready")
    logger.info(f"🤖 Gemini Keys: {len(gemini_manager.keys)} loaded")
    logger.info(f"🖼️ ImgBB Keys: {len(imgbb_manager.keys)} loaded")
    logger.info(f"🚀 Bot Started at {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")


# ============================================================
# CALLBACK ROUTER
# ============================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callbacks to appropriate handlers"""
    data = update.callback_query.data
    
    # Core callbacks (/start info, /img MCQ edit, /prompt)
    if data.startswith(('info_', 'mcq_', 'prompt_', 'start_')):
        await handle_core_callbacks(update, context)
    
    # Admin callbacks (/permit, /broadcast, /channel)
    elif data.startswith(('admin_', 'broadcast_', 'channel_', 'bcast_')):
        await handle_admin_callbacks(update, context)
    
    # Tools callbacks (/exp, /tag, /sheet format toggle)
    elif data.startswith(('exp_', 'tag_', 'sheet_toggle', 'sheet_select', 'sheet_generate', 'sheet_cancel')):
        if data.startswith('sheet_'):
            await handle_sheet_callbacks(update, context)
        else:
            await handle_tools_callbacks(update, context)
    
    # PDF callbacks (/pdfm, /qbm)
    elif data.startswith(('pdfm_', 'qbm_', 'pdf_')):
        await handle_pdf_callbacks(update, context)
    
    # CSV Poll callbacks (/csv, /csvS, /csvI, /csvIS)
    elif data.startswith(('poll_', 'csvs_', 'inline_', 'csvis_', 'iq_', 'retake_', 'result_')):
        await handle_csv_callbacks(update, context)


# ============================================================
# MESSAGE ROUTER
# ============================================================
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route text messages to appropriate handlers"""
    
    # Check if waiting for broadcast message
    if context.user_data.get('broadcast_waiting'):
        handled = await handle_broadcast_message(update, context)
        if handled:
            return
    
    # Check if waiting for sheet title
    if context.user_data.get('waiting_sheet_title'):
        handled = await handle_sheet_title(update, context)
        if handled:
            return
    
    # Check if editing MCQ or settings
    if context.user_data.get('editing_field') or \
       context.user_data.get('editing_prompt') or \
       context.user_data.get('adding_prompt') or \
       context.user_data.get('setting_custom_exp') or \
       context.user_data.get('setting_tag_name') or \
       context.user_data.get('editing_tag') or \
       context.user_data.get('adding_tag_name'):
        await handle_settings_message(update, context)
        return
    
    # Check settings message
    await handle_settings_message(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler"""
    error = context.error
    
    logger.error(f"❌ Bot Error: {error}")
    
    # Log to file
    try:
        with open('data/error.log', 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now()}] {str(error)}\n")
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__, file=f)
            f.write("\n" + "="*50 + "\n")
    except:
        pass
    
    # Notify user if possible
    try:
        if update and hasattr(update, 'effective_message'):
            await update.effective_message.reply_text(
                f"❌ *একটি ত্রুটি ঘটেছে!*\n\n`{str(error)[:200]}`\n\n🔄 আবার চেষ্টা করুন।",
                parse_mode='Markdown'
            )
    except:
        pass


# ============================================================
# MAIN FUNCTION
# ============================================================
def main():
    """Initialize and run the bot"""
    logger.info("🚀 ATLAS MCQ BOT v3.0 Starting...")
    
    # Check token
    if not Config.TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set!")
        sys.exit(1)
    
    # Build application
    app = Application.builder() \
        .token(Config.TELEGRAM_BOT_TOKEN) \
        .post_init(post_init) \
        .connect_timeout(30) \
        .read_timeout(30) \
        .write_timeout(30) \
        .build()
    
    # ============================================================
    # REGISTER COMMAND HANDLERS
    # ============================================================
    
    # Core
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("img", img_handler))
    app.add_handler(CommandHandler("txt", txt_handler))
    app.add_handler(CommandHandler("prompt", prompt_handler))
    
    # Admin
    app.add_handler(CommandHandler("permit", permit_handler))
    app.add_handler(CommandHandler("adminlist", adminlist_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("channel", channel_handler))
    
    # Tools
    app.add_handler(CommandHandler("split", split_handler))
    app.add_handler(CommandHandler("merge", merge_handler))
    app.add_handler(CommandHandler("convert", convert_handler))
    app.add_handler(CommandHandler("rename", rename_handler))
    app.add_handler(CommandHandler("watermark", watermark_handler))
    app.add_handler(CommandHandler("exp", exp_handler))
    app.add_handler(CommandHandler("tag", tag_handler))
    app.add_handler(CommandHandler("thumb", thumb_handler))
    app.add_handler(CommandHandler("sheet", csv_sheet_handler))
    app.add_handler(CommandHandler("ping", ping_handler))
    app.add_handler(CommandHandler("error", error_handler_cmd))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CommandHandler("collect", collect_handler))
    app.add_handler(CommandHandler("done", done_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("cancel", cancel_collection_handler))
    app.add_handler(CommandHandler("pause", pause_handler))
    app.add_handler(CommandHandler("resume", resume_handler))
    app.add_handler(CommandHandler("restart", restart_handler))
    
    # PDF
    app.add_handler(CommandHandler("pdfm", pdfm_handler))
    app.add_handler(CommandHandler("qbm", qbm_handler))
    
    # CSV Poll
    app.add_handler(CommandHandler("csv", csv_handler))
    app.add_handler(CommandHandler("csvS", csvs_handler))
    app.add_handler(CommandHandler("csvI", csvi_handler))
    app.add_handler(CommandHandler("csvIS", csvis_handler))
    
    # ============================================================
    # REGISTER MESSAGE HANDLERS
    # ============================================================
    
    # MHTML/HTML files
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("mhtml") | 
        filters.Document.FileExtension("mht") | 
        filters.Document.FileExtension("html"),
        queue_mhtml
    ))
    
    # Text messages (for editing, settings, broadcast)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        message_router
    ))
    
    # ============================================================
    # REGISTER CALLBACK HANDLER
    # ============================================================
    app.add_handler(CallbackQueryHandler(callback_router))
    
    # ============================================================
    # REGISTER ERROR HANDLER
    # ============================================================
    app.add_error_handler(error_handler)
    
    # ============================================================
    # START BOT
    # ============================================================
    logger.info("✅ All handlers registered!")
    logger.info("🚀 Bot is now online and polling...")
    
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
