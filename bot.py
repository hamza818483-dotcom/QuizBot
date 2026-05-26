#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT v2.0 - Main Entry"""

import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from config import Config, Database
from handlers_core import *
from handlers_admin import *
from handlers_tools import *
from csv_handler import csv_handler, csvs_handler, handle_csv_callbacks
from pdf_handler import pdfm_handler, qbm_handler, handle_pdf_callbacks
from mhtml_handler import mhtml_handler

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

async def post_init(app: Application):
    db = Database()
    await db.initialize()
    logger.info("✅ ATLAS Bot Ready")

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith(('prompt_', 'exp_', 'tag_', 'start_')):
        await handle_core_callbacks(update, context)
    elif data.startswith(('admin_', 'broadcast_', 'channel_')):
        await handle_admin_callbacks(update, context)
    elif data.startswith(('csv_', 'csvs_', 'poll_')):
        await handle_csv_callbacks(update, context)
    elif data.startswith(('pdf_', 'qbm_', 'format_')):
        await handle_pdf_callbacks(update, context)
    elif data.startswith(('tool_', 'thumb_', 'sheet_')):
        await handle_tools_callbacks(update, context)

async def error_callback(update, context):
    logger.error(f"Error: {context.error}")

def main():
    config = Config()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("img", img_handler))
    app.add_handler(CommandHandler("txt", txt_handler))
    app.add_handler(CommandHandler("prompt", prompt_handler))
    app.add_handler(CommandHandler("exp", exp_handler))
    app.add_handler(CommandHandler("tag", tag_handler))
    app.add_handler(CommandHandler("permit", admin_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("channel", channel_handler))
    app.add_handler(CommandHandler("csv", csv_handler))
    app.add_handler(CommandHandler("csvS", csvs_handler))
    app.add_handler(CommandHandler("pdfm", pdfm_handler))
    app.add_handler(CommandHandler("qbm", qbm_handler))
    app.add_handler(CommandHandler("split", split_handler))
    app.add_handler(CommandHandler("convert", convert_handler))
    app.add_handler(CommandHandler("thumb", thumb_handler))
    app.add_handler(CommandHandler("ping", ping_handler))
    app.add_handler(CommandHandler("error", error_handler))
    app.add_handler(CommandHandler("collect", collect_handler))
    app.add_handler(CommandHandler("done", done_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("cancel", cancel_collection_handler))
    app.add_handler(CommandHandler("pause", pause_handler))
    app.add_handler(CommandHandler("resume", resume_handler))
    app.add_handler(CommandHandler("restart", restart_handler))
    app.add_handler(CommandHandler("logs", logs_handler))
    app.add_handler(CommandHandler("sheet", sheet_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, mhtml_handler))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(error_callback)
    
    logger.info("🚀 Starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
