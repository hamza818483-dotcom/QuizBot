#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - Admin Handlers (/permit, /adminlist, /broadcast, /channel)"""

import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import db, Config

# ============================================================
# /permit HANDLER
# ============================================================
async def permit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin management - add/list/remove admins"""
    user_id = update.effective_user.id
    
    # Only Owner can manage admins
    if user_id != Config.OWNER_ID:
        await update.message.reply_text("❌ এই কমান্ড শুধু Bot Owner ব্যবহার করতে পারবে!")
        return
    
    args = context.args
    
    if args:
        # Add new admin
        try:
            new_admin_id = int(args[0])
            username = args[1] if len(args) > 1 else ''
            
            # Check if already admin
            existing = await db.fetchone('SELECT user_id FROM admins WHERE user_id = ?', (new_admin_id,))
            if existing:
                await update.message.reply_text(f"⚠️ এই ইউজার ইতিমধ্যেই অ্যাডমিন!")
                return
            
            await db.execute(
                'INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)',
                (new_admin_id, username, user_id)
            )
            await update.message.reply_text(f"✅ অ্যাডমিন যোগ করা হয়েছে!\n👤 User ID: `{new_admin_id}`\n👤 Username: @{username}" if username else f"✅ অ্যাডমিন যোগ করা হয়েছে!\n👤 User ID: `{new_admin_id}`", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("❌ সঠিক User ID দাও!\nযেমন: `/permit 12345678`", parse_mode=ParseMode.MARKDOWN)
    else:
        # Show admin list
        admins = await db.fetchall('SELECT user_id, username, added_at FROM admins ORDER BY added_at')
        
        if not admins:
            await update.message.reply_text("📋 কোনো অ্যাডমিন নেই!\n\n/adminlist — অ্যাডমিন লিস্ট দেখতে\n/permit <user_id> — অ্যাডমিন যোগ করতে")
            return
        
        text = "👥 *অ্যাডমিন লিস্ট:*\n\n"
        buttons = []
        
        for admin_id, username, added_at in admins:
            name = f"@{username}" if username else f"User {admin_id}"
            text += f"• `{admin_id}` — {name}\n"
            buttons.append([
                InlineKeyboardButton(f"👤 {name}", callback_data=f"admin_info_{admin_id}"),
                InlineKeyboardButton("❌ Remove", callback_data=f"admin_remove_{admin_id}")
            ])
        
        text += f"\n👑 *Owner:* `{Config.OWNER_ID}`\n📊 *Total Admins:* {len(admins)}"
        
        await update.message.reply_text(
            text, 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )


# ============================================================
# /adminlist HANDLER
# ============================================================
async def adminlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin list"""
    user_id = update.effective_user.id
    
    # Check if user is admin or owner
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনরা ব্যবহার করতে পারবে!")
        return
    
    admins = await db.fetchall('SELECT user_id, username, added_at FROM admins ORDER BY added_at')
    
    text = "👥 *অ্যাডমিন লিস্ট:*\n\n"
    text += f"👑 *Owner:* `{Config.OWNER_ID}`\n\n"
    
    if admins:
        for admin_id, username, added_at in admins:
            name = f"@{username}" if username else f"User {admin_id}"
            text += f"• `{admin_id}` — {name} (since {added_at[:10]})\n"
    else:
        text += "• কোনো অ্যাডমিন নেই\n"
    
    text += f"\n📊 *Total Admins:* {len(admins)}"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ============================================================
# /broadcast HANDLER
# ============================================================
async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast messages to users/channels"""
    user_id = update.effective_user.id
    
    # Check admin
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনরা ব্যবহার করতে পারবে!")
        return
    
    # Get stats
    users = await db.fetchone('SELECT COUNT(*) FROM bot_users')
    channels = await db.fetchone('SELECT COUNT(*) FROM channels')
    
    user_count = users[0] if users else 0
    channel_count = channels[0] if channels else 0
    total = user_count + channel_count
    
    buttons = [
        [InlineKeyboardButton(f"📤 Broadcast All ({total})", callback_data="broadcast_all")],
        [InlineKeyboardButton("🎯 Broadcast Select Channels", callback_data="broadcast_select")],
        [InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")]
    ]
    
    await update.message.reply_text(
        f"""📊 *Broadcast Stats:*

👤 *Users:* {user_count}
📢 *Channels:* {channel_count}
📦 *Total:* {total}

কোথায় পাঠাতে চাও?""",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /channel HANDLER
# ============================================================
async def channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel management - add/list/remove"""
    user_id = update.effective_user.id
    
    # Check admin
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await update.message.reply_text("❌ এই কমান্ড শুধু অ্যাডমিনরা ব্যবহার করতে পারবে!")
        return
    
    args = context.args
    
    if args:
        # Add channel
        channel_id = args[0]
        channel_name = ' '.join(args[1:]) if len(args) > 1 else channel_id
        
        # Check if already exists
        existing = await db.fetchone('SELECT id FROM channels WHERE channel_id = ?', (channel_id,))
        if existing:
            await update.message.reply_text(f"⚠️ এই চ্যানেল ইতিমধ্যেই যোগ করা আছে!")
            return
        
        await db.execute(
            'INSERT OR IGNORE INTO channels (channel_id, channel_name) VALUES (?, ?)',
            (channel_id, channel_name)
        )
        await update.message.reply_text(f"✅ চ্যানেল যোগ করা হয়েছে!\n📢 ID: `{channel_id}`\n📢 Name: {channel_name}", parse_mode=ParseMode.MARKDOWN)
    else:
        # List channels
        channels = await db.fetchall('SELECT id, channel_id, channel_name, added_at FROM channels ORDER BY added_at')
        
        if not channels:
            await update.message.reply_text(
                "📋 কোনো চ্যানেল নেই!\n\n/channel @channel_name — চ্যানেল যোগ করতে\n/channel -100xxx Name — ID দিয়ে যোগ করতে"
            )
            return
        
        text = "📢 *চ্যানেল লিস্ট:*\n\n"
        buttons = []
        
        for ch_id, ch_identifier, ch_name, added_at in channels:
            text += f"• `{ch_identifier}` — {ch_name}\n"
            buttons.append([
                InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"channel_info_{ch_id}"),
                InlineKeyboardButton("❌ Remove", callback_data=f"channel_remove_{ch_identifier}")
            ])
        
        text += f"\n📊 *Total Channels:* {len(channels)}"
        buttons.append([InlineKeyboardButton("➕ Add Channel", callback_data="channel_add")])
        
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons)
        )


# ============================================================
# ADMIN CALLBACK HANDLER
# ============================================================
async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all admin callbacks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    # Check admin for all actions
    is_admin = await db.fetchone('SELECT 1 FROM admins WHERE user_id = ?', (user_id,))
    if user_id != Config.OWNER_ID and not is_admin:
        await query.answer("❌ অ্যাডমিন না! তুমি এটা করতে পারবে না।", show_alert=True)
        return
    
    # Admin remove
    if data.startswith('admin_remove_'):
        admin_id = int(data.replace('admin_remove_', ''))
        if admin_id == Config.OWNER_ID:
            await query.answer("❌ Owner-কে রিমুভ করা যাবে না!", show_alert=True)
            return
        await db.execute('DELETE FROM admins WHERE user_id = ?', (admin_id,))
        await query.edit_message_text(f"✅ অ্যাডমিন `{admin_id}` রিমুভ করা হয়েছে!", parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith('admin_info_'):
        admin_id = int(data.replace('admin_info_', ''))
        admin = await db.fetchone('SELECT username, added_at, added_by FROM admins WHERE user_id = ?', (admin_id,))
        if admin:
            await query.answer(f"👤 ID: {admin_id}\n📅 Added: {admin[1][:10]}\n👑 By: {admin[2]}", show_alert=True)
    
    # Channel remove
    elif data.startswith('channel_remove_'):
        channel_id = data.replace('channel_remove_', '')
        await db.execute('DELETE FROM channels WHERE channel_id = ?', (channel_id,))
        await query.edit_message_text(f"✅ চ্যানেল রিমুভ করা হয়েছে!")
    
    elif data.startswith('channel_info_'):
        ch_id = int(data.replace('channel_info_', ''))
        channel = await db.fetchone('SELECT channel_id, channel_name, added_at FROM channels WHERE id = ?', (ch_id,))
        if channel:
            await query.answer(f"📢 ID: {channel[0]}\n📅 Added: {channel[2][:10]}", show_alert=True)
    
    elif data == 'channel_add':
        await query.edit_message_text("📢 চ্যানেল যোগ করতে:\n`/channel @channel_name` বা `/channel -100xxx Name`", parse_mode=ParseMode.MARKDOWN)
    
    # Broadcast
    elif data == 'broadcast_cancel':
        await query.edit_message_text("❌ Broadcast বাতিল করা হয়েছে!")
        context.user_data.pop('broadcast_mode', None)
        context.user_data.pop('broadcast_waiting', None)
    
    elif data == 'broadcast_all':
        context.user_data['broadcast_mode'] = 'all'
        context.user_data['broadcast_waiting'] = True
        await query.edit_message_text("📤 এখন Broadcast মেসেজ পাঠাও!\n\nসব ইউজার + চ্যানেলে পাঠানো হবে।")
    
    elif data == 'broadcast_select':
        channels = await db.fetchall('SELECT channel_id, channel_name FROM channels')
        buttons = []
        for ch_id, ch_name in channels:
            buttons.append([InlineKeyboardButton(f"📢 {ch_name}", callback_data=f"bcast_sel_{ch_id}")])
        buttons.append([InlineKeyboardButton("✅ Done — Send to Selected", callback_data="bcast_confirm")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")])
        
        context.user_data['broadcast_selected'] = []
        await query.edit_message_text("🎯 চ্যানেল সিলেক্ট করো (টগল):", reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data.startswith('bcast_sel_'):
        ch_id = data.replace('bcast_sel_', '')
        selected = context.user_data.get('broadcast_selected', [])
        if ch_id in selected:
            selected.remove(ch_id)
        else:
            selected.append(ch_id)
        context.user_data['broadcast_selected'] = selected
        await query.answer(f"✅ {len(selected)} টি সিলেক্টেড")
    
    elif data == 'bcast_confirm':
        selected = context.user_data.get('broadcast_selected', [])
        if not selected:
            await query.answer("❌ কোনো চ্যানেল সিলেক্ট করোনি!", show_alert=True)
            return
        context.user_data['broadcast_mode'] = 'selected'
        context.user_data['broadcast_waiting'] = True
        await query.edit_message_text(f"📤 {len(selected)} টি চ্যানেলে Broadcast মেসেজ পাঠাও!")


# ============================================================
# BROADCAST MESSAGE HANDLER
# ============================================================
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the actual broadcast message sending"""
    if not context.user_data.get('broadcast_waiting'):
        return False
    
    mode = context.user_data.get('broadcast_mode')
    msg = update.message
    
    sent = 0
    failed = 0
    
    if mode == 'all':
        # Send to all users
        users = await db.fetchall('SELECT user_id FROM bot_users')
        for (uid,) in users:
            try:
                await msg.copy(chat_id=uid)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
        
        # Send to all channels
        channels = await db.fetchall('SELECT channel_id FROM channels')
        for (ch_id,) in channels:
            try:
                await msg.copy(chat_id=ch_id)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
    
    elif mode == 'selected':
        selected = context.user_data.get('broadcast_selected', [])
        for ch_id in selected:
            try:
                await msg.copy(chat_id=ch_id)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
    
    await update.message.reply_text(f"✅ *Broadcast সম্পন্ন!*\n\n📤 Sent: {sent}\n❌ Failed: {failed}", parse_mode=ParseMode.MARKDOWN)
    
    context.user_data.pop('broadcast_mode', None)
    context.user_data.pop('broadcast_waiting', None)
    context.user_data.pop('broadcast_selected', None)
    return True
