"""
শুধু key validity check করে — কোনো model-specific call না।
GEMINI_KEYS-এ যত key দেওয়া আছে সবগুলো একে একে চেক করবে।

চালানোর নিয়ম:
    GEMINI_KEYS="key1,key2,key3" python3 check_keys.py
"""
import os
from google import genai as gai

raw = os.environ.get("GEMINI_KEYS", "")
keys = [k.strip() for k in raw.split(",") if k.strip()]

if not keys:
    print("❌ GEMINI_KEYS env var সেট নাই।")
    exit(1)

print(f"মোট {len(keys)}টা key চেক হচ্ছে...\n")

for i, key in enumerate(keys):
    try:
        client = gai.Client(api_key=key)
        models = list(client.models.list())
        count = len(models)
        print(f"✅ Key #{i+1} ({key[:15]}...): VALID — {count} model accessible")
    except Exception as e:
        err = str(e)
        if "SUSPENDED" in err.upper():
            status = "SUSPENDED"
        elif "API_KEY_INVALID" in err.upper() or "invalid" in err.lower():
            status = "INVALID KEY"
        elif "429" in err or "RESOURCE_EXHAUSTED" in err.upper():
            status = "QUOTA EXHAUSTED (key valid, just rate-limited right now)"
        else:
            status = "UNKNOWN ERROR"
        print(f"❌ Key #{i+1} ({key[:15]}...): {status} — {type(e).__name__}: {err[:150]}")

print("\nসারাংশ: VALID মানে key নিজে সুস্থ, কোনো model-এই কাজ করবে।")
print("QUOTA EXHAUSTED মানেও key সুস্থ, শুধু এই মুহূর্তে rate-limit ছুঁয়ে গেছে।")
print("SUSPENDED/INVALID KEY হলেই শুধু key replace করা লাগবে।")
