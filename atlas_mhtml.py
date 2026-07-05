# ============================================================
# atlas_mhtml.py
# ATLAS BOT - MHTML/HTML -> CSV Handler (ported from AtlasMasterBot)
# Supports: Chorcha.net + Testmoz sources
# Full LaTeX/math cleanup + imgbb image upload -> <img> tag embed
# Output CSV schema: questions,option1..5,answer,explanation,type,section
# ============================================================
import os
import re
import io
import gc
import time
import base64
import uuid
import logging
import urllib.parse

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from PIL import Image

logger = logging.getLogger("atlas.mhtml")
_http_client = httpx.Client(timeout=30)

# ============================================================
# IMGBB UPLOAD (key rotation, sync — called via asyncio.to_thread)
# ============================================================
# ============================================================
# IMGBB UPLOAD (health-tracked key rotation — always prefers a healthy key)
# Env var: IMGBB_API_KEYS (comma-separated)
# ============================================================
class ImgBBKeyManager:
    """
    ImgBB API key rotation manager with health tracking.
    - Always picks a currently-healthy key first (round-robin among healthy ones)
    - A key gets marked unhealthy after 3 consecutive failures
    - If ALL keys are unhealthy, auto-resets everyone to healthy (avoids permanent lockout
      from a transient outage) and tries again
    - record_success() resets a key's failure streak back to 0 (so one bad attempt doesn't
      permanently penalize an otherwise-fine key)
    """

    def __init__(self):
        raw = os.environ.get("IMGBB_API_KEYS", "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        self.stats = {k: {"success": 0, "fail": 0, "healthy": True} for k in keys}
        self.index = 0

    @property
    def keys(self):
        return list(self.stats.keys())

    def _healthy_keys(self):
        healthy = [k for k, v in self.stats.items() if v["healthy"]]
        if not healthy and self.stats:
            # All keys down — reset everyone, better to retry than permanently fail
            logger.warning("[ImgBB] All keys unhealthy — resetting all to healthy")
            for k in self.stats:
                self.stats[k]["healthy"] = True
                self.stats[k]["fail"] = 0
            healthy = list(self.stats.keys())
        return healthy

    def _next_key(self):
        healthy = self._healthy_keys()
        if not healthy:
            return None
        key = healthy[self.index % len(healthy)]
        self.index += 1
        return key

    def record_success(self, key):
        if key in self.stats:
            self.stats[key]["success"] += 1
            self.stats[key]["fail"] = 0
            self.stats[key]["healthy"] = True

    def record_failure(self, key):
        if key in self.stats:
            self.stats[key]["fail"] += 1
            if self.stats[key]["fail"] >= 3:
                self.stats[key]["healthy"] = False
                logger.warning(f"[ImgBB] Key ...{key[-6:]} marked unhealthy after 3 failures")

    def get_stats(self):
        return {
            "total": len(self.stats),
            "healthy": len([k for k, v in self.stats.items() if v["healthy"]]),
            "keys": {
                f"key_{i+1}": {"success": v["success"], "fail": v["fail"], "healthy": v["healthy"]}
                for i, (k, v) in enumerate(self.stats.items())
            }
        }

    def upload(self, image_bytes: bytes, retries: int = 3) -> str:
        if not self.stats:
            return ""
        tried = set()
        for attempt in range(max(retries, len(self.stats))):
            key = self._next_key()
            if not key or key in tried and len(tried) >= len(self.stats):
                break
            tried.add(key)
            try:
                b64 = base64.b64encode(image_bytes).decode("utf-8")
                resp = _http_client.post(
                    "https://api.imgbb.com/1/upload",
                    data={"key": key, "image": b64},
                )
                data = resp.json()
                if data.get("success"):
                    self.record_success(key)
                    return data["data"]["url"]
                self.record_failure(key)
            except Exception as e:
                logger.warning(f"[ImgBB] Upload attempt failed on key ...{key[-6:]}: {e}")
                self.record_failure(key)
        return ""


imgbb_manager = ImgBBKeyManager()


def compress_image(b64_str):
    try:
        img_data = base64.b64decode(b64_str)
        img = Image.open(io.BytesIO(img_data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        out_buffer = io.BytesIO()
        img.save(out_buffer, format="JPEG", optimize=True, quality=70)
        return base64.b64encode(out_buffer.getvalue()).decode("utf-8")
    except Exception:
        return b64_str


_upload_cache = {}


def upload_to_imgbb(b64):
    """
    Supabase Storage-এ image upload করে permanent public URL রিটার্ন করে।
    imgbb-এর বদলে Supabase Storage — কোনো key rotation লাগে না, permanent, free tier যথেষ্ট।
    Env vars: SUPABASE_URL, SUPABASE_KEY (আগে থেকেই bot-এ সেট আছে)
    একই base64 image দ্বিতীয়বার এলে cache থেকে URL রিটার্ন করে (duplicate image reupload skip)।
    """
    if not b64:
        return ""
    cache_key = b64[:64] + str(len(b64))
    if cache_key in _upload_cache:
        return _upload_cache[cache_key]
    try:
        compressed = compress_image(b64)
        img_bytes = base64.b64decode(compressed)
        supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        supabase_key = os.environ.get("SUPABASE_KEY", "")
        if not supabase_url or not supabase_key:
            return imgbb_manager.upload(img_bytes)  # fallback যদি supabase env না থাকে

        bucket = "quiz-images"
        filename = f"{uuid.uuid4().hex}.jpg"
        resp = _http_client.post(
            f"{supabase_url}/storage/v1/object/{bucket}/{filename}",
            headers={
                "Authorization": f"Bearer {supabase_key}",
                "apikey": supabase_key,
                "Content-Type": "image/jpeg",
            },
            content=img_bytes,
        )
        if resp.status_code in (200, 201):
            url = f"{supabase_url}/storage/v1/object/public/{bucket}/{filename}"
            _upload_cache[cache_key] = url
            return url
        logger.warning(f"[SupabaseStorage] Upload failed {resp.status_code}: {resp.text[:200]}")
        return imgbb_manager.upload(img_bytes)  # fallback to imgbb on failure
    except Exception as e:
        logger.warning(f"[SupabaseStorage] Exception: {e} — falling back to imgbb")
        try:
            return imgbb_manager.upload(base64.b64decode(compress_image(b64)))
        except Exception:
            return ""


# ============================================================
# UNICODE MAPS
# ============================================================
SUB_MAP = str.maketrans("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")
SUP_MAP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₌⁽⁾ⁿ")
SUP_TO_NORMAL = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

LATEX_SYMBOLS = {
    r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
    r'\epsilon': 'ε', r'\zeta': 'ζ', r'\eta': 'η', r'\theta': 'θ',
    r'\iota': 'ι', r'\kappa': 'κ', r'\lambda': 'λ', r'\mu': 'μ',
    r'\nu': 'ν', r'\xi': 'ξ', r'\pi': 'π', r'\rho': 'ρ',
    r'\sigma': 'σ', r'\tau': 'τ', r'\phi': 'φ', r'\chi': 'χ',
    r'\psi': 'ψ', r'\omega': 'ω', r'\Gamma': 'Γ', r'\Delta': 'Δ',
    r'\Theta': 'Θ', r'\Lambda': 'Λ', r'\Pi': 'Π', r'\Sigma': 'Σ',
    r'\Phi': 'Φ', r'\Psi': 'Ψ', r'\Omega': 'Ω',
    r'\infty': '∞', r'\times': '×', r'\div': '÷', r'\pm': '±',
    r'\mp': '∓', r'\leq': '≤', r'\geq': '≥', r'\neq': '≠',
    r'\approx': '≈', r'\equiv': '≡', r'\propto': '∝',
    r'\sqrt': '√', r'\int': '∫', r'\oint': '∮', r'\iint': '∬',
    r'\sum': '∑', r'\prod': '∏', r'\partial': '∂', r'\nabla': '∇',
    r'\rightarrow': '→', r'\leftarrow': '←', r'\leftrightarrow': '↔',
    r'\Rightarrow': '⇒', r'\Leftarrow': '⇐', r'\Leftrightarrow': '⇔',
    r'\uparrow': '↑', r'\downarrow': '↓',
    r'\sin': 'sin', r'\cos': 'cos', r'\tan': 'tan',
    r'\cot': 'cot', r'\sec': 'sec', r'\csc': 'csc',
    r'\log': 'log', r'\ln': 'ln', r'\lim': 'lim',
    r'\cdot': '·', r'\bullet': '•', r'\circ': '°',
    r'\therefore': '∴', r'\because': '∵',
    r'\in': '∈', r'\notin': '∉', r'\subset': '⊂', r'\supset': '⊃',
    r'\cup': '∪', r'\cap': '∩', r'\emptyset': '∅',
    r'\forall': '∀', r'\exists': '∃',
}


def convert_to_english_numbers(text):
    return text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


def aggressive_clean(text):
    if not text:
        return ""
    text = convert_to_english_numbers(text)

    text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
    text = re.sub(r'\\frac\s*(\S+)\s*(\S+)', r'\1/\2', text)

    for latex, uni in LATEX_SYMBOLS.items():
        text = text.replace(latex, uni)

    text = re.sub(r'_\{\s*([^}]+)\s*\}', lambda m: m.group(1).translate(SUB_MAP), text)
    text = re.sub(r'\^\{\s*([^}]+)\s*\}', lambda m: m.group(1).translate(SUP_MAP), text)

    text = re.sub(r'_([0-9a-zA-Z+\-]+)', lambda m: m.group(1).translate(SUB_MAP), text)
    text = re.sub(r'\^([0-9a-zA-Z+\-]+)', lambda m: m.group(1).translate(SUP_MAP), text)

    text = re.sub(r'([⁰¹²³⁴⁵⁶⁷⁸⁹]+)°', lambda m: m.group(1).translate(SUP_TO_NORMAL) + '°', text)
    text = text.replace('^\\circ', '°').replace('^{\\circ}', '°').replace('∘', '°')
    text = text.replace('° C', '°C').replace('^ C', '°C')

    text = text.translate(str.maketrans("ₐₑₒₓₕₖₗₘₙₚₛₜ", "aeoxhklmnpst"))

    text = re.sub(r'\s+([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+)', r'\1', text)
    text = text.replace('₍', '(').replace('₎', ')')
    text = re.sub(r'(?<=[A-Za-z])\s+(?=[a-z](?:\s|$|[^a-zA-Z]))', '', text)
    text = re.sub(r'(?<=\d)\s+(?=[A-Z])', '', text)
    text = re.sub(r'(?<=[A-Z])\s+(?=[A-Z])', '', text)
    text = re.sub(r'(?<=[A-Z])\s+(?=[a-z](?:\s|$|[^a-zA-Z]))', '', text)
    text = re.sub(r'(?<=[A-Z][a-z])\s+(?=[A-Z])', '', text)
    text = re.sub(r'(?<=[₀-₉⁰-⁹])\s+(?=[A-Z])', '', text)
    text = text.replace(' . ', '.').replace(' .', '.').replace('. ', '.')

    text = re.sub(r'\\[a-zA-Z]+\s*\{?', ' ', text)
    text = re.sub(r'([A-Z][a-z]?)\s+([₀-₉⁰-⁹⁺⁻])', r'\1\2', text)

    units = r'(mL|L|m³|cm³|g|kg|mol|M|Pa|atm|J|K|V|A|W|N|C|Hz|eV|nm|mm|cm|m)'
    text = re.sub(r'(\d+)\s*' + units + r'\b', r'\1 \2', text)

    text = re.sub(r'[\{\}]', '', text)

    text = text.replace('\ufeff', '').replace('\u200b', '').replace('\u200c', '')

    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def format_content(element, img_map):
    if not element:
        return ""

    for hidden in element.find_all(['annotation', 'script', 'mjx-assistive-mathml']):
        hidden.decompose()
    for hidden in element.find_all('span', class_=['katex-html', 'MJX_Assistive_MathML', 'MathJax_Preview']):
        hidden.decompose()

    for mfrac in element.find_all('mfrac'):
        contents = mfrac.find_all(recursive=False)
        if len(contents) == 2:
            num = contents[0].get_text(strip=True)
            den = contents[1].get_text(strip=True)
            mfrac.replace_with(f"{num}/{den}")

    for sub in element.find_all(['sub', 'msub']):
        sub.replace_with(sub.get_text(strip=True).translate(SUB_MAP))
    for sup in element.find_all(['sup', 'msup']):
        sup.replace_with(sup.get_text(strip=True).translate(SUP_MAP))

    for img in element.find_all('img'):
        src = img.get('src', '') or img.get('data-src', '')
        if not src:
            img.decompose()
            continue

        url = ""
        b64 = ""
        if src.startswith('http'):
            url = src
        elif src.startswith('data:image'):
            try:
                if 'base64,' in src:
                    b64 = src.split('base64,')[1]
            except Exception:
                pass
        else:
            decoded_src = urllib.parse.unquote(src)
            b64 = img_map.get(src) or img_map.get(decoded_src) or ""

        if b64 and not url:
            url = upload_to_imgbb(b64)

        if url:
            img.replace_with(f" img_s{url}img_e ")
        else:
            img.decompose()

    raw_text = element.get_text(separator=" ", strip=True)
    img_markers = []

    def img_repl(match):
        img_markers.append(match.group(0))
        return f" ZZZIMG{len(img_markers)-1}ZZZ "

    raw_text = re.sub(r'img_s.*?img_e', img_repl, raw_text)
    cleaned_text = aggressive_clean(raw_text)

    for i, marker in enumerate(img_markers):
        cleaned_text = cleaned_text.replace(f"ZZZIMG{i}ZZZ", marker)

    return re.sub(r'img_s(.*?)img_e', r'<img class="qimg" src="\1">', cleaned_text)


def post_process(results: list) -> list:
    results = [r for r in results if r.get('questions', '').strip()]
    seen = set()
    unique = []
    for r in results:
        key = r.get('questions', '').strip()[:120]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ============================================================
# MAIN PARSE FUNCTION (sync, run via asyncio.to_thread)
# Returns dict: {"source": "Chorcha.net"|"Testmoz"|None, "results": [...]}
# ============================================================
def parse_mhtml_to_mcqs(file_bytes: bytes, file_name: str, progress_cb=None) -> dict:
    """
    progress_cb(done:int, total:int) — optional callback, called after each
    question is parsed, for live progress/ETA reporting by the caller.
    """
    import email
    from email import policy as _policy

    img_map, html_body = {}, ""

    if file_name.lower().endswith(('.mhtml', '.mht')):
        msg = email.message_from_bytes(file_bytes, policy=_policy.default)
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                html_body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or 'utf-8', errors='ignore')
            elif part.get_content_type().startswith('image/'):
                loc, raw = part.get('Content-Location', ''), part.get_payload(decode=True)
                if loc and raw:
                    b64_data = base64.b64encode(raw).decode('utf-8')
                    img_map[loc] = b64_data
                    img_map[urllib.parse.unquote(loc)] = b64_data
    else:
        html_body = file_bytes.decode('utf-8', errors='ignore')

    soup = BeautifulSoup(html_body, 'lxml')

    # ============================================================
    # CHORCHA.NET
    # ============================================================
    chorcha_cards = soup.find_all('div', class_=lambda x: x and 'p-5' in x and 'rounded-xl' in x)

    if chorcha_cards:
        results = []
        ans_map = {'ক': '1', 'খ': '2', 'গ': '3', 'ঘ': '4'}
        _total_cards = len(chorcha_cards)

        for _ci, card in enumerate(chorcha_cards, 1):
            q_div = card.find('div', class_=lambda x: x and 'font-medium' in x)
            if not q_div:
                continue
            q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*', '', format_content(q_div, img_map))
            if not q_text.strip():
                continue

            options, ans_idx = [], "1"
            for i, btn in enumerate(card.find_all('button', class_=lambda x: x and 'p-2' in x), 1):
                lbl = btn.find('span', class_=lambda x: x and 'rounded-full' in x)
                opt_content = btn.find('div', class_='flex-1')
                if opt_content:
                    options.append(format_content(opt_content, img_map))
                    if any(c in str(btn) for c in ['#017A47', 'border-[#017A47]', '#E2A03F', '#F59E0B', 'border-[#F59E0B]']):
                        ans_idx = ans_map.get(lbl.get_text(strip=True) if lbl else "", str(i))

            while len(options) < 5:
                options.append("")
            if options[4].strip() and ans_idx == "5":
                options[3], ans_idx = options[4], "4"

            exp_div = card.find('div', class_=lambda x: x and 'prose' in x)
            exp_text = format_content(exp_div, img_map) if exp_div else ""

            results.append({"questions": q_text, "option1": options[0], "option2": options[1],
                             "option3": options[2], "option4": options[3], "option5": "",
                             "answer": ans_idx, "explanation": exp_text, "type": 1, "section": 1})

            if progress_cb:
                try:
                    progress_cb(_ci, _total_cards)
                except Exception:
                    pass

        results = post_process(results)
        gc.collect()
        return {"source": "Chorcha.net", "results": results}

    # ============================================================
    # TESTMOZ
    # ============================================================
    cards = soup.find_all('div', class_=lambda x: x and 'rounded-lg' in x and 'shadow-md' in x)
    results = []
    _total_cards = len(cards)

    for _ci, card in enumerate(cards, 1):
        q_p = card.find('p', class_='text-[17px]')
        q_text = re.sub(r'^\s*[0-9০-৯]+\s*[\.\)\-ঃ:]\s*',
                         '', format_content(q_p, img_map)) if q_p else ""
        if not q_text.strip():
            continue

        opt_divs = card.find_all('div', class_=lambda x: x and 'cursor-pointer' in x and 'col-span-2' in x)
        exp_div = card.find('div', class_=lambda x: x and 'col-span-2' in x
                             and 'font-semibold' in x and 'cursor-pointer' not in x)

        for img in card.find_all('img'):
            if q_p and img in q_p.descendants:
                continue
            in_opt = any(img in opt.descendants for opt in opt_divs)
            in_exp = exp_div and img in exp_div.descendants
            if not in_opt and not in_exp:
                dummy = BeautifulSoup(str(img), 'html.parser')
                q_text += " " + format_content(dummy, img_map)

        options, ans_idx = [], "1"
        for i, opt in enumerate(opt_divs, 1):
            text_sm = opt.find('div', class_='text-sm')
            opt_text = format_content(text_sm, img_map) if text_sm else ""
            for img in opt.find_all('img'):
                if text_sm and img not in text_sm.descendants:
                    dummy = BeautifulSoup(str(img), 'html.parser')
                    opt_text += " " + format_content(dummy, img_map)
            options.append(opt_text)
            if opt.find('div', class_=lambda x: x and 'bg-green-500' in x) or opt.find('svg'):
                ans_idx = str(i)

        while len(options) < 5:
            options.append("")
        if options[4].strip() and ans_idx == "5":
            options[3], ans_idx = options[4], "4"

        exp_text = format_content(exp_div, img_map) if exp_div else ""
        results.append({"questions": q_text, "option1": options[0], "option2": options[1],
                         "option3": options[2], "option4": options[3], "option5": "",
                         "answer": ans_idx, "explanation": exp_text, "type": 1, "section": 1})

        if progress_cb:
            try:
                progress_cb(_ci, _total_cards)
            except Exception:
                pass

    results = post_process(results)
    gc.collect()
    return {"source": "Testmoz" if results else None, "results": results}


def results_to_csv_bytes(results: list) -> bytes:
    df = pd.DataFrame(results)
    csv_buf = io.BytesIO()
    df.to_csv(csv_buf, index=False, encoding='utf-8-sig')
    return csv_buf.getvalue()
