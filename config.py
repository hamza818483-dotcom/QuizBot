#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - Config, Database, Key Managers"""

import os
import random
import aiosqlite
import base64
import requests
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any

load_dotenv()


class Config:
    """Bot configuration"""
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    OWNER_ID = int(os.getenv('OWNER_ID', 0))
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    GEMINI_API_KEYS = os.getenv('GEMINI_API_KEYS', '').split(',')
    IMGBB_API_KEYS = os.getenv('IMGBB_API_KEYS', '').split(',')
    API_ID = os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    DB_PATH = 'data/atlas_bot.db'
    TEMP_PATH = 'data/temp'
    THUMB_PATH = 'data/thumbnails'


class Database:
    """SQLite database manager"""
    
    def __init__(self):
        self.db_path = Config.DB_PATH
        os.makedirs('data', exist_ok=True)
        os.makedirs(Config.TEMP_PATH, exist_ok=True)
        os.makedirs(Config.THUMB_PATH, exist_ok=True)
    
    async def initialize(self):
        """Create all tables"""
        async with aiosqlite.connect(self.db_path) as db:
            # Admins
            await db.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Channels
            await db.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE,
                    channel_name TEXT,
                    channel_link TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Prompts
            await db.execute('''
                CREATE TABLE IF NOT EXISTS prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    content TEXT,
                    is_active INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Explanation settings
            await db.execute('''
                CREATE TABLE IF NOT EXISTS exp_settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    mode TEXT DEFAULT 'auto',
                    custom_text TEXT,
                    tag_name TEXT
                )
            ''')
            
            # Tag settings
            await db.execute('''
                CREATE TABLE IF NOT EXISTS tag_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_type TEXT,
                    tag_name TEXT,
                    position TEXT,
                    is_active INTEGER DEFAULT 1
                )
            ''')
            
            # Thumbnail
            await db.execute('''
                CREATE TABLE IF NOT EXISTS thumbnail (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    file_id TEXT,
                    file_path TEXT
                )
            ''')
            
            # Users
            await db.execute('''
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Sessions
            await db.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    session_type TEXT,
                    session_data TEXT,
                    is_paused INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # File Store
            await db.execute('''
                CREATE TABLE IF NOT EXISTS file_store (
                    user_id INTEGER PRIMARY KEY,
                    file_data BLOB,
                    file_name TEXT,
                    file_type TEXT,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Sheet Format Settings
            await db.execute('''
                CREATE TABLE IF NOT EXISTS sheet_formats (
                    format_id TEXT PRIMARY KEY,
                    format_name TEXT,
                    is_active INTEGER DEFAULT 1
                )
            ''')
            
            await db.commit()
            
            # Insert default data
            # # await self._insert_default_prompts(db)  # Disabled - already inserted  # Disabled - already inserted
            await self._insert_default_exp(db)
            await self._insert_default_sheet_formats(db)
            await db.commit()
    
    async def _insert_default_prompts(self, db):
        """Insert 7 default prompts"""
        prompts = [
            ("Prompt-01 (Standard/Easy)", """MCQ TYPE: Standard Easy
- প্রশ্ন: ছোট, ১ লাইন
- অপশন: ৪টি, এক শব্দের ছোট
- উত্তর: ৪টির মধ্যে একটি (CSV-তে 1,2,3,4)
- ব্যাখ্যা: সঠিক উত্তর + ওই টপিকের বাকি তথ্য (Source থেকে)
- Input source থেকেই সব তথ্য
- Bengali explanation, max 165 chars
- JSON output only"""),
            
            ("Prompt-02 (ছোট প্রশ্ন, বড় অপশন)", """MCQ TYPE: Short Question, Long Options
- প্রশ্ন: ছোট, এক লাইন
- অপশন: ৪টি বড় (বাক্য বা phrase)
- উত্তর: ৪টির মধ্যে একটি (CSV-তে 1,2,3,4)
- ব্যাখ্যা: সঠিকটা কেন সঠিক + বাকিগুলো কেন ভুল (Precisely)
- Input source থেকেই সব
- Bengali, max 165 chars
- JSON output only"""),
            
            ("Prompt-03 (বড় প্রশ্ন, ছোট অপশন)", """MCQ TYPE: Long Question, Short Options
- প্রশ্ন: ২-৩ লাইন, চিন্তা করতে হয়
- অপশন: ছোট (এক শব্দ বা phrase)
- উত্তর: ৪টির মধ্যে একটি (CSV-তে 1,2,3,4)
- ব্যাখ্যা: ধাপে ধাপে কীভাবে সঠিক উত্তর পাওয়া যায়
- Input source থেকেই সব
- Bengali, max 165 chars
- JSON output only"""),
            
            ("Prompt-04 (সত্য/মিথ্যা)", """MCQ TYPE: True/False Style
প্রশ্নের ধরন (randomly mix):
- "নিচের কোনটিকে সত্য বললে ভুল হবে না?" → সত্য চাই
- "নিচের কোনটিকে সত্য বললে ভুল হবে?" → মিথ্যা চাই
- "নিচের কোনটিকে মিথ্যা বললে ভুল হবে?" → সত্য চাই
- "নিচের কোনটিকে মিথ্যা বললে ভুল হবে না?" → মিথ্যা চাই
- অপশন: ছোট বা বড় (২ টাইপই হতে পারে)
- উত্তর: ৪টির মধ্যে একটি (CSV-তে 1,2,3,4)
- ব্যাখ্যা: কোনটা সঠিক, কেন সঠিক
- Input source থেকেই সব
- Bengali, max 165 chars
- JSON output only"""),
            
            ("Prompt-05 (01+02 Mixed)", """MCQ TYPE: Mixed (01+02)
50% Prompt-01 (ছোট প্রশ্ন, ছোট অপশন)
50% Prompt-02 (ছোট প্রশ্ন, বড় অপশন)
- Input source থেকেই সব
- Bengali, max 165 chars
- JSON output only"""),
            
            ("Prompt-06 (02+03 Mixed)", """MCQ TYPE: Mixed (02+03)
50% Prompt-02 (ছোট প্রশ্ন, বড় অপশন)
50% Prompt-03 (বড় প্রশ্ন, ছোট অপশন)
- Input source থেকেই সব
- Bengali, max 165 chars
- JSON output only"""),
            
            ("Prompt-07 (01+02+03 Mixed)", """MCQ TYPE: Mixed (01+02+03)
33% Prompt-01 (ছোট প্রশ্ন, ছোট অপশন)
33% Prompt-02 (ছোট প্রশ্ন, বড় অপশন)
34% Prompt-03 (বড় প্রশ্ন, ছোট অপশন)
- Input source থেকেই সব
- Bengali, max 165 chars
- JSON output only""")
        ]
        
        for name, content in prompts:
            await db.execute(
                'INSERT OR IGNORE INTO prompts (name, content, is_active) VALUES (?, ?, ?)',
                (name, content, 1 if "Prompt-01" in name else 0)
            )
    
    async def _insert_default_exp(self, db):
        """Insert default explanation settings"""
        await db.execute(
            'INSERT OR IGNORE INTO exp_settings (id, mode, custom_text, tag_name) VALUES (1, ?, ?, ?)',
            ('auto', '', '')
        )
    
    async def _insert_default_sheet_formats(self, db):
        """Insert default sheet formats"""
        formats = [
            ('format_01', 'Practice Sheet (প্রশ্ন + উত্তর + ব্যাখ্যা)'),
            ('format_02', 'Solve Sheet (সাইডবারে উত্তর)'),
            ('format_03', 'Exam Style (Answer টেবিল)'),
            ('format_04', 'Mixed Style'),
            ('format_05', 'Summary + Answer Key')
        ]
        for fid, fname in formats:
            await db.execute(
                'INSERT OR IGNORE INTO sheet_formats (format_id, format_name, is_active) VALUES (?, ?, 1)',
                (fid, fname)
            )
    
    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute query"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor
    
    async def fetchone(self, query: str, params: tuple = ()) -> Optional[tuple]:
        """Fetch one result"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchone()
    
    async def fetchall(self, query: str, params: tuple = ()) -> List[tuple]:
        """Fetch all results"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()


class GeminiKeyManager:
    """Gemini API key rotation manager"""
    
    def __init__(self):
        raw = os.getenv('GEMINI_API_KEYS', '')
        self.keys = {
            k.strip(): {'success': 0, 'fail': 0, 'healthy': True}
            for k in raw.replace('\n', ',').split(',') if k.strip()
        }
    
    def get_healthy_key(self) -> str:
        """Get a healthy key"""
        healthy = [k for k, v in self.keys.items() if v['healthy']]
        if not healthy:
            for k in self.keys:
                self.keys[k]['healthy'] = True
            healthy = list(self.keys.keys())
        return random.choice(healthy)
    
    def record_success(self, key: str):
        """Record successful API call"""
        if key in self.keys:
            self.keys[key]['success'] += 1
            self.keys[key]['healthy'] = True
    
    def record_failure(self, key: str):
        """Record failed API call"""
        if key in self.keys:
            self.keys[key]['fail'] += 1
            if self.keys[key]['fail'] >= 3:
                self.keys[key]['healthy'] = False
    
    async def call(self, prompt: str, image=None, retries: int = 3) -> str:
        """Call Gemini API with retry"""
        for attempt in range(retries):
            key = self.get_healthy_key()
            try:
                client = genai.Client(api_key=key)
                contents = [image, prompt] if image else [prompt]
                resp = client.models.generate_content(
                    model=Config.GEMINI_MODEL,
                    contents=contents
                )
                self.record_success(key)
                return resp.text
            except Exception as e:
                self.record_failure(key)
                if attempt == retries - 1:
                    raise e
        return ""


    def get_stats(self):
        healthy = len([k for k,v in self.keys.items() if v.get('healthy')])
        return {'total': len(self.keys), 'healthy': healthy}

class ImgBBKeyManager:
    """ImgBB API key rotation manager"""
    
    def __init__(self):
        raw = os.getenv('IMGBB_API_KEYS', '')
        self.keys = [k.strip() for k in raw.split(',') if k.strip()]
        self.index = 0
    
    def upload(self, image_bytes: bytes, retries: int = 3) -> str:
        """Upload image to ImgBB"""
        b64 = base64.b64encode(image_bytes).decode('utf-8')
        for attempt in range(retries):
            key = self.keys[self.index % len(self.keys)]
            self.index += 1
            try:
                resp = requests.post(
                    'https://api.imgbb.com/1/upload',
                    data={'key': key, 'image': b64},
                    timeout=30
                )
                data = resp.json()
                if data.get('success'):
                    return data['data']['url']
            except Exception:
                if attempt == retries - 1:
                    raise
        return ""


# Global instances
gemini_manager = GeminiKeyManager()
imgbb_manager = ImgBBKeyManager()
db = Database()
