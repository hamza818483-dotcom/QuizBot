#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATLAS BOT - MCQ Cache Handler (100% Safe)"""

import hashlib, json
from config import db


async def get_cached_mcqs(file_bytes: bytes):
    """Check if MCQ exists in cache"""
    try:
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        row = await db.fetchone("SELECT mcqs FROM mcq_cache WHERE file_hash = ?", (file_hash,))
        if row:
            return json.loads(row[0])
    except:
        pass
    return None


async def save_mcq_cache(file_bytes: bytes, mcqs: list):
    """Save MCQ to cache"""
    try:
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        await db.execute("INSERT OR REPLACE INTO mcq_cache (file_hash, mcqs) VALUES (?, ?)",
                         (file_hash, json.dumps(mcqs, ensure_ascii=False)))
    except:
        pass
