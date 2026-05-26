#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Services - Gemini, ImgBB, utilities"""

import re
import asyncio
from typing import Dict, List

def clean_text(text: str) -> str:
    """Clean and sanitize text"""
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove special chars but keep Bengali
    text = re.sub(r'[^\w\s\u0980-\u09FF.,;:?!(){}[\]"\'=-]', '', text)
    return text.strip()


def format_progress(current: int, total: int, prefix: str = "Progress") -> str:
    """Format progress message"""
    percentage = int((current / total) * 100) if total > 0 else 0
    bar_length = 20
    filled = int(bar_length * current / total) if total > 0 else 0
    bar = '█' * filled + '░' * (bar_length - filled)
    
    return f"""{prefix}
{bar} {percentage}%
📊 {current}/{total}"""


def estimate_time_remaining(current: int, total: int, elapsed_seconds: float) -> str:
    """Estimate time remaining"""
    if current == 0:
        return "calculating..."
    
    rate = current / elapsed_seconds
    remaining = total - current
    seconds_left = remaining / rate if rate > 0 else 0
    
    if seconds_left < 60:
        return f"~{int(seconds_left)} seconds"
    elif seconds_left < 3600:
        return f"~{int(seconds_left / 60)} minutes"
    else:
        return f"~{int(seconds_left / 3600)} hours"


class ProgressTracker:
    """Track progress of long operations"""
    
    def __init__(self, total: int, message_func):
        self.total = total
        self.current = 0
        self.message_func = message_func
        self.start_time = asyncio.get_event_loop().time()
        self.last_update = 0
    
    async def update(self, increment: int = 1):
        """Update progress"""
        self.current += increment
        current_time = asyncio.get_event_loop().time()
        
        # Update every 2 seconds
        if current_time - self.last_update >= 2:
            elapsed = current_time - self.start_time
            remaining = estimate_time_remaining(self.current, self.total, elapsed)
            
            progress_text = format_progress(self.current, self.total, "⏳ প্রসেসিং চলছে...")
            progress_text += f"\n⏱️ বাকি: {remaining}"
            
            try:
                await self.message_func(progress_text)
                self.last_update = current_time
            except:
                pass


def parse_json_from_text(text: str) -> List[Dict]:
    """Extract JSON from text response"""
    # Try to find JSON array
    json_match = re.search(r'\[.*\]', text, re.DOTALL)
    if json_match:
        import json
        try:
            return json.loads(json_match.group())
        except:
            pass
    return []


def is_valid_mcq(mcq: Dict) -> bool:
    """Validate MCQ structure"""
    required = ['question', 'options', 'answer']
    if not all(k in mcq for k in required):
        return False
    
    if not isinstance(mcq['options'], dict):
        return False
    
    if len(mcq['options']) != 4:
        return False
    
    if mcq['answer'] not in mcq['options']:
        return False
    
    return True


def sanitize_filename(filename: str) -> str:
    """Sanitize filename"""
    # Remove invalid chars
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Limit length
    if len(filename) > 200:
        name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        filename = name[:195] + ('.' + ext if ext else '')
    return filename


async def batch_process(items: List, batch_size: int, process_func, delay: float = 0):
    """Process items in batches"""
    results = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batch_results = await asyncio.gather(*[process_func(item) for item in batch])
        results.extend(batch_results)
        if delay > 0 and i + batch_size < len(items):
            await asyncio.sleep(delay)
    return results
