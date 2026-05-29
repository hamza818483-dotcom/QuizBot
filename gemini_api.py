import requests
import os
import random
import base64
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEYS = [k.strip() for k in os.getenv('GEMINI_API_KEYS', '').split(',') if k.strip()]
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

def call_gemini(prompt, image_bytes=None):
    """Call Gemini REST API with key rotation"""
    for attempt in range(3):
        key = random.choice(GEMINI_API_KEYS)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
        
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode()
            payload["contents"][0]["parts"].append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
        
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            if 'quota' in str(e).lower():
                quota_keys.add(key)
                continue
            continue
    return "[]"

def get_healthy_key():
    return random.choice(GEMINI_API_KEYS) if GEMINI_API_KEYS else ""
