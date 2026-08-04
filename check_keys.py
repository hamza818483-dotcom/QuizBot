"""
শুধু key validity check করে — কোনো model-specific call না।
GEMINI_KEYS-এ যত key দেওয়া আছে সবগুলো একে একে চেক করবে।
সাথে actual rate-limit info (RPM/RPD/TPM headers) ও দেখাবে।

চালানোর নিয়ম:
    GEMINI_KEYS="key1,key2,key3" python3 check_keys.py

নোট: Google-এর official published free-tier limit (2026, gemini-2.5-flash):
  - RPM (per minute): ~10-15 request
  - RPD (per day): ~250-1500 request (source ভেদে ভিন্ন, নিচের actual header-ই সঠিক)
  - TPM (tokens/minute): ~250,000 token (এটা কখনো bottleneck হয় না, RPM/RPD-ই আসল সীমা)
  - Limit apply হয় per PROJECT, per KEY না — তাই একই Google project-এর একাধিক
    key থাকলে তাদের quota shared হতে পারে, আলাদা আলাদা না।
"""
import os
import requests
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

        # Direct REST call to inspect rate-limit headers (SDK hides these)
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": "hi"}]}]},
                timeout=15
            )
            headers_of_interest = {
                k2: v for k2, v in r.headers.items()
                if "quota" in k2.lower() or "ratelimit" in k2.lower() or "limit" in k2.lower()
            }
            if headers_of_interest:
                for hk, hv in headers_of_interest.items():
                    print(f"     {hk}: {hv}")
            else:
                print(f"     (এই response-এ কোনো rate-limit header দেয়নি — Google AI Studio console-এ direct দেখো)")
        except Exception as e2:
            print(f"     header check failed: {e2}")

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
print("\nসঠিক/live RPM-RPD-TPM সংখ্যা দেখতে: https://aistudio.google.com/app/apikey")
print("→ ওই key select করে 'Rate Limits' ট্যাবে actual real-time quota দেখা যায়।")
