import re, sys

def parse(text):
    lines = [l for l in text.splitlines() if "🚫" in l and "banned" in l]
    reasons = {}
    ages = []
    for l in lines:
        if "deleted or disabled" in l:
            reasons["service_account_disabled"] = reasons.get("service_account_disabled", 0) + 1
        elif "suspended" in l:
            reasons["consumer_suspended"] = reasons.get("consumer_suspended", 0) + 1
        elif "API key not valid" in l:
            reasons["invalid_key"] = reasons.get("invalid_key", 0) + 1
        else:
            reasons["other"] = reasons.get("other", 0) + 1
        m = re.search(r"key was ([\d.]+)d old", l)
        if m:
            ages.append(float(m.group(1)))
    print(f"Total banned keys parsed: {len(lines)}")
    for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {r}: {c}")
    if ages:
        print(f"Age range: {min(ages)}d - {max(ages)}d, avg {sum(ages)/len(ages):.2f}d")

if __name__ == "__main__":
    data = sys.stdin.read()
    parse(data)
