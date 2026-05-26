#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Admin Handlers - /permit, /broadcast, /channel"""

import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import db, Config

async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/permit - Admin management"""
    if update.message.from_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Owner only!")
        return
    
    if context.args:
        # Add admin
        user_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else ''
        
        await db.execute(
            'INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)',
            (user_id, username, update.message.from_user.id)
        )
        await update.message.reply_text(f"✅ Admin added: {user_id}")
    else:
        # List admins
        admins = await db.fetchall('SELECT user_id, username FROM admins')
        
        if not admins:
            await update.message.reply_text("📋 No admins")
            return
        
        buttons = []
        for user_id, username in admins:
            buttons.append([
                InlineKeyboardButton(
                    f"👤 {username or user_id}",
                    callback_data=f"admin_view_{user_id}"
                ),
                InlineKeyboardButton("❌", callback_data=f"admin_remove_{user_id}")
            ])
        
        await update.message.reply_text(
            "👥 Admin List:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast - Broadcast messages"""
    if update.message.from_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Owner only!")
        return
    
    # Get stats
    users = await db.fetchall('SELECT COUNT(*) FROM bot_users')
    channels = await db.fetchall('SELECT COUNT(*) FROM channels')
    
    user_count = users[0][0] if users else 0
    channel_count = channels[0][0] if channels else 0
    total = user_count + channel_count
    
    buttons = [
        [InlineKeyboardButton("📤 Broadcast All", callback_data="broadcast_all")],
        [InlineKeyboardButton("🎯 Broadcast Select", callback_data="broadcast_select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")]
    ]
    
    await update.message.reply_text(
        f"""📊 Broadcast Stats:
👤 Users: {user_count}
📢 Channels: {channel_count}
📦 Total: {total}""",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/channel - Channel management"""
    # Check admin
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (update.message.from_user.id,))
    if not is_admin and update.message.from_user.id != Config.OWNER_ID:
        await update.message.reply_text("❌ Admin only!")
        return
    
    if context.args:
        # Add channel
        channel_id = context.args[0]
        channel_name = ' '.join(context.args[1:]) if len(context.args) > 1 else channel_id
        
        await db.execute(
            'INSERT OR IGNORE INTO channels (channel_id, channel_name) VALUES (?, ?)',
            (channel_id, channel_name)
        )
        await update.message.reply_text(f"✅ Channel added: {channel_name}")
    else:
        # List channels
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        
        if not channels:
            await update.message.reply_text("📋 No channels")
            return
        
        buttons = []
        for ch_id, ch_name in channels:
            buttons.append([
                InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"channel_view_{ch_id}"),
                InlineKeyboardButton("❌", callback_data=f"channel_remove_{ch_id}")
            ])
        
        buttons.append([InlineKeyboardButton("➕ Add Channel", callback_data="channel_add")])
        
        await update.message.reply_text(
            "📢 Channel List:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith('admin_remove_'):
        user_id = int(data.replace('admin_remove_', ''))
        await db.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
        await query.edit_message_text(f"✅ Admin removed: {user_id}")
    
    elif data.startswith('channel_remove_'):
        channel_id = data.replace('channel_remove_', '')
        await db.execute('DELETE FROM channels WHERE channel_id = ?', (channel_id,))
        await query.edit_message_text(f"✅ Channel removed")
    
    elif data == 'broadcast_cancel':
        await query.edit_message_text("❌ Broadcast cancelled")
    
    elif data == 'broadcast_all':
        await query.edit_message_text("📤 এখন broadcast message পাঠাও...")
        context.user_data['broadcast_mode'] = 'all'
        context.user_data['broadcast_waiting'] = True
    
    elif data == 'broadcast_select':
        # Get channels
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        buttons = []
        for ch_id, ch_name in channels:
            buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"bcast_sel_{ch_id}")])
        buttons.append([InlineKeyboardButton("✅ Broadcast to Selected", callback_data="bcast_confirm")])
        
        context.user_data['broadcast_selected'] = []
        await query.edit_message_text("🎯 Channels select করো:", reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data.startswith('bcast_sel_'):
        channel_id = data.replace('bcast_sel_', '')
        selected = context.user_data.get('broadcast_selected', [])
        if channel_id in selected:
            selected.remove(channel_id)
        else:
            selected.append(channel_id)
        context.user_data['broadcast_selected'] = selected
        await query.answer(f"✅ {len(selected)} selected")
    
    elif data == 'bcast_confirm':
        selected = context.user_data.get('broadcast_selected', [])
        if not selected:
            await query.answer("❌ কোন channel select করা হয়নি")
            return
        await query.edit_message_text(f"📤 {len(selected)} channels এ broadcast message পাঠাও...")
        context.user_data['broadcast_mode'] = 'selected'
        context.user_data['broadcast_waiting'] = True


async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle message during broadcast"""
    if not context.user_data.get('broadcast_waiting'):
        return False
    
    mode = context.user_data.get('broadcast_mode')
    msg = update.message
    
    if mode == 'all':
        # Broadcast to all
        users = await db.fetchall('SELECT user_id FROM bot_users')
        channels = await db.fetchall('SELECT channel_id FROM channels')
        
        sent = 0
        failed = 0
        
        # Send to users
        for (user_id,) in users:
            try:
                await msg.copy(chat_id=user_id)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
        
        # Send to channels
        for (channel_id,) in channels:
            try:
                await msg.copy(chat_id=channel_id)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
        
        await update.message.reply_text(f"✅ Broadcast সম্পন্ন!\n📤 Sent: {sent}\n❌ Failed: {failed}")
    
    elif mode == 'selected':
        selected = context.user_data.get('broadcast_selected', [])
        sent = 0
        failed = 0
        
        for channel_id in selected:
            try:
                await msg.copy(chat_id=channel_id)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
        
        await update.message.reply_text(f"✅ Broadcast সম্পন্ন!\n📤 Sent: {sent}/{len(selected)}")
    
    context.user_data['broadcast_waiting'] = False
    return True
