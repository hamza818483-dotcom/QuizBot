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


def _try_supabase_upload(supabase_url, supabase_key, img_bytes):
    """One Supabase Storage upload attempt + reachability verify. Returns url or ''."""
    try:
        bucket = "quiz-images"
        filename = f"{uuid.uuid4().hex}.jpg"

        def _do_post():
            return _http_client.post(
                f"{supabase_url}/storage/v1/object/{bucket}/{filename}",
                headers={
                    "Authorization": f"Bearer {supabase_key}",
                    "apikey": supabase_key,
                    "Content-Type": "image/jpeg",
                },
                content=img_bytes,
            )

        resp = _do_post()
        if resp.status_code not in (200, 201):
            body = resp.text[:200]
            if resp.status_code in (400, 404) and "not found" in body.lower():
                # Bucket doesn't exist on this project yet — create it public, retry once.
                try:
                    _http_client.post(
                        f"{supabase_url}/storage/v1/bucket",
                        headers={
                            "Authorization": f"Bearer {supabase_key}",
                            "apikey": supabase_key,
                            "Content-Type": "application/json",
                        },
                        json={"id": bucket, "name": bucket, "public": True},
                    )
                    resp = _do_post()
                except Exception as e:
                    logger.warning(f"[SupabaseStorage] Bucket auto-create failed: {e}")
            if resp.status_code not in (200, 201):
                logger.warning(f"[SupabaseStorage] Upload failed {resp.status_code}: {resp.text[:200]}")
                return ""
        url = f"{supabase_url}/storage/v1/object/public/{bucket}/{filename}"
        check = None
        for _attempt in range(2):
            check = _http_client.head(url, timeout=8)
            if check.status_code == 200:
                return url
            time.sleep(0.6)
        logger.warning(f"[SupabaseStorage] Public URL unreachable ({check.status_code if check else '?'}): {url}")
        return ""
    except Exception as e:
        logger.warning(f"[SupabaseStorage] Exception: {e}")
        return ""


def upload_to_imgbb(b64):
    """
    Image upload: imgbb (primary) -> Supabase Storage S1 -> S2 (last-resort fallback only).
    Env vars: SUPABASE_URL/SUPABASE_KEY (S1), SB2_URL/SB2_KEY (S2, same as core.py's DB fallback account).
    একই base64 image দ্বিতীয়বার এলে cache থেকে URL রিটার্ন করে।
    """
    if not b64:
        return ""
    cache_key = b64[:64] + str(len(b64))
    if cache_key in _upload_cache:
        return _upload_cache[cache_key]
    try:
        compressed = compress_image(b64)
        img_bytes = base64.b64decode(compressed)

        primary_url = imgbb_manager.upload(img_bytes)
        if primary_url:
            _upload_cache[cache_key] = primary_url
            return primary_url

        s1_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        s1_key = os.environ.get("SUPABASE_KEY", "")
        s2_url = os.environ.get("SB2_URL", "https://xnkuuzstschdovcyomfk.supabase.co").rstrip("/")
        s2_key = os.environ.get("SB2_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhua3V1enN0c2NoZG92Y3lvbWZrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI3NTI3NzUsImV4cCI6MjA5ODMyODc3NX0.rD6p4U1fdqnM2M6t7wA3qsMY1p3KEFD2S1WzSIZehW4")

        for s_url, s_key in ((s1_url, s1_key), (s2_url, s2_key)):
            if not s_url or not s_key:
                continue
            url = _try_supabase_upload(s_url, s_key, img_bytes)
            if url:
                _upload_cache[cache_key] = url
                return url
        return ""
    except Exception as e:
        logger.warning(f"[ImageUpload] Exception: {e} — retrying imgbb")
        try:
            fb = imgbb_manager.upload(base64.b64decode(compress_image(b64)))
            if fb:
                _upload_cache[cache_key] = fb
            return fb
        except Exception:
            return ""


# ============================================================
# UNICODE MAPS
# ============================================================
SUB_MAP = str.maketrans("0123456789+\u2212\u2013\u2014-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₋₋₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")
SUP_MAP = str.maketrans("0123456789+\u2212\u2013\u2014-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁻⁻₌⁽⁾ⁿ")
SUP_TO_NORMAL = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")

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

# Reverse of LATEX_SYMBOLS, used to rebuild valid LaTeX source for an
# exponent/subscript group that contains a symbol with no Unicode
# superscript/subscript form (e.g. γ, θ) -- by this point in the pipeline
# LATEX_SYMBOLS has already turned '\gamma' into 'γ', so this converts it
# back inside the specific fallback branch that needs real LaTeX syntax.
UNICODE_TO_LATEX = {v: k for k, v in LATEX_SYMBOLS.items() if len(v) == 1 or v.isalpha()}


def convert_to_english_numbers(text):
    return text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


def aggressive_clean(text):
    if not text:
        return ""
    text = convert_to_english_numbers(text)

    # Protect already-final $^{...}$ / $_{...}$ / $\frac{...}{...}$ LaTeX
    # that callers such as chorcha_parser._latex_to_text() may have already
    # built (e.g. from a KaTeX <annotation> source, where a fraction-in-
    # exponent or word subscript was correctly preserved as real LaTeX).
    # This MUST run before any other regex below -- including this
    # function's own \frac-rebuilding step further down -- because that
    # step would otherwise find the literal \frac{...}{...} INSIDE an
    # already-final $^{...}$ block and wrap it in its own separate $...$,
    # corrupting valid "$^{\frac{a}{b}}$" into broken nested
    # "$^{$\frac{a}{b}$}$".
    _pre_final_markers = []
    def _pre_final_protect(m):
        _pre_final_markers.append(m.group(0))
        return f"ZZZPREFINAL{len(_pre_final_markers)-1}ZZZ"
    text = re.sub(r'\$[_^]\{.*?\}\$', _pre_final_protect, text)
    text = re.sub(r'\$\\d?frac\{.*?\}\{.*?\}\$', _pre_final_protect, text)
    text = re.sub(r'\$\\sqrt\{.*?\}\$', _pre_final_protect, text)

    # Vector arrow fix: get_text(separator=" ", ...) inserts a space at every
    # inline-tag boundary, so a letter followed by a combining "arrow above"
    # (U+20D7, e.g. rendering as V⃗ for vector V) ends up as "V ⃗" -- the
    # arrow floats beside the letter instead of sitting directly above it,
    # since a combining character must immediately follow its base letter
    # with no space between them. Also handle the standalone (non-combining)
    # right-arrow U+2192 used the same way in some source markup.
    text = re.sub(r'([^\s\u0300-\u036F\u20D0-\u20FF])\s+([\u20D0-\u20FF])', r'\1\2', text)
    text = re.sub(r'([A-Za-z\u0980-\u09FF])\s+(\u2192)(?!\w)', lambda m: m.group(1) + '\u20D7', text)
    # Unit-vector "hat" notation written as a literal ASCII caret after the
    # letter with a space (e.g. "i ^", "j ^") -- not a Unicode combining
    # character itself, so the arrow-diacritic regex above doesn't catch it.
    # Convert to the proper combining circumflex (U+0302) directly on the
    # letter, same reasoning as the vector-arrow fix above. Negative
    # lookahead avoids eating a real exponent caret ("x^2", "a^{b}").
    text = re.sub(r'([A-Za-z])\s*\^(?!\{|\(|\w)', lambda m: m.group(1) + '\u0302', text)
    # A number (coefficient) must sit directly against a following
    # vector-marked letter (one carrying the arrow diacritic U+20D7 or the
    # hat/circumflex U+0302 from the two fixes above) -- standard math
    # notation never puts a space between a coefficient and its vector,
    # e.g. "2 î" must become "2î", "2 3 ĵ" (a coefficient plus a
    # mid-broken vector letter) becomes "23ĵ".
    text = re.sub(
        r'(\d(?:\s*\d)*)\s+([A-Za-z\u0980-\u09FF][\u0300-\u036F\u20D0-\u20FF])',
        lambda m: re.sub(r'\s+', '', m.group(1)) + m.group(2),
        text,
    )

    # \frac{num}{den} -> num/den, but a NESTED \frac inside num or den
    # (fraction-of-a-fraction) can't be flattened to "a/b" without losing
    # the grouping -- "[^}]+" only matches up to the first "}", so it was
    # truncating at the inner fraction's closing brace and emitting garbage
    # (e.g. "\frac{\frac{1}{2}}{3}" -> "1/23/"). Detect a nested \frac and
    # keep the whole thing as LaTeX ($\frac{...}{...}$) instead.
    def _frac_repl(text):
        out = []
        i = 0
        pat = re.compile(r'\\frac\s*')
        while i < len(text):
            m = pat.match(text, i)
            if not m:
                out.append(text[i])
                i += 1
                continue

            def _read_group(pos):
                if pos >= len(text) or text[pos] != '{':
                    m2 = re.match(r'\S+', text[pos:])
                    return (m2.group(0), pos + len(m2.group(0))) if m2 else (None, pos)
                depth = 0
                start = pos
                for j in range(pos, len(text)):
                    if text[j] == '{':
                        depth += 1
                    elif text[j] == '}':
                        depth -= 1
                        if depth == 0:
                            return text[start+1:j], j + 1
                return None, pos

            p = m.end()
            num, p = _read_group(p)
            if num is None:
                out.append(text[i]); i += 1; continue
            while p < len(text) and text[p] == ' ':
                p += 1
            den, p2 = _read_group(p)
            if den is None:
                out.append(text[i]); i += 1; continue
            # Every \frac now becomes real $\frac{}{}$ LaTeX, unconditionally
            # (not just complex/multi-term ones) -- ensures consistent,
            # properly-rendered fraction display everywhere once run through
            # KaTeX/MathJax, instead of some fractions being plain "a/b" text
            # and others being LaTeX depending on term complexity.
            out.append(f"$\\dfrac{{{num}}}{{{den}}}$")
            i = p2
        return ''.join(out)
    text = _frac_repl(text)

    # Protect nested-fraction LaTeX ($\frac{...}{...}$) just created above
    # from this function's own later brace-stripping (r'[\{\}]') and
    # backslash-command-stripping (r'\\[a-zA-Z]+...') regexes, which would
    # otherwise destroy the LaTeX we deliberately kept.
    _nfrac_markers = []
    def _nfrac_protect(m):
        _nfrac_markers.append(m.group(0))
        return f"ZZZNFRACLATEX{len(_nfrac_markers)-1}ZZZ"
    text = re.sub(r'\$\\d?frac\{.*?\}\{.*?\}\$', _nfrac_protect, text)

    # Protect $\xrightarrow{...}$/$\xleftarrow{...}$ LaTeX (built above by
    # the <mover> handler for an arrow with a label stacked over it) using
    # the same marker technique as \frac just above -- without this, the
    # generic backslash-command-stripping regex further down (r'\\[a-zA-Z]+
    # \{?' -> ' ') would eat the "\xrightarrow{" literally, since it has no
    # concept of this command and treats it like any other unknown one,
    # leaving the label floating with no arrow and no visual link to it.
    text = re.sub(r'\$\\x(?:right|left)arrow\{.*?\}\$', _nfrac_protect, text)

    # Protect $\overline{...}$ LaTeX (built above by the <mover> handler
    # for a bar spanning a multi-letter base, e.g. line-segment notation
    # AB with a full overline) the same way -- otherwise the generic
    # backslash-command-stripper below eats "\overline{" since it has no
    # special-case for it either, same failure mode as \xrightarrow above.
    text = re.sub(r'\$\\overline\{.*?\}\$', _nfrac_protect, text)
    text = re.sub(r'\$\\bar\{.*?\}\$', _nfrac_protect, text)

    # \sqrt[n]{...} nth-root form was NOT handled at all here -- only plain
    # \sqrt{...} was, so any literal "\sqrt[3]{x}" text (e.g. AI-generated
    # explanation using LaTeX-ish syntax) fell through completely
    # unconverted, leaving raw backslash-LaTeX visible to the user. Handle
    # this BEFORE the plain \sqrt{...} regex so it isn't half-matched.
    def _nroot_repl(m):
        idx = m.group(1).strip()
        inner = m.group(2).strip()
        if re.fullmatch(r'[A-Za-z0-9]+', inner):
            radicand = inner
        else:
            radicand = '(' + inner + ')'
        # Small index -> unicode superscript prefix (²√, ³√, ...);
        # anything else -> keep the index explicit so it's never silently
        # dropped ("[n]" would otherwise just vanish).
        if re.fullmatch(r'[0-9]+', idx):
            sup_idx = idx.translate(SUP_MAP)
        else:
            sup_idx = f"({idx})"
        return f"{sup_idx}√{radicand}"
    text = re.sub(r'\\sqrt\s*\[([^\[\]]+)\]\s*\{([^{}]+)\}', _nroot_repl, text)

    # \sqrt{...}: multi-term contents must keep grouping as √(...), since
    # the generic '{}' strip further down would otherwise fuse a multi-term
    # radicand into the surrounding expression with no boundary at all
    # (e.g. "√{a+b}" -> "√a+b", silently changing what's under the root).
    # Single-token contents (plain numbers/letters) don't need parens.
    def _sqrt_repl(m):
        inner = m.group(1).strip()
        if re.fullmatch(r'[A-Za-z0-9]+', inner):
            return '√' + inner
        return '√(' + inner + ')'
    text = re.sub(r'\\sqrt\s*\{([^{}]+)\}', _sqrt_repl, text)
    # \sqrt without braces followed directly by a token, e.g. "\sqrt2", "\sqrt x"
    text = re.sub(r'\\sqrt\s*([A-Za-z0-9]+)', r'√\1', text)

    for latex, uni in LATEX_SYMBOLS.items():
        text = text.replace(latex, uni)

    _SUB_SAFE_LETTERS = "aeoxhklmnpst"
    _SUP_SAFE_LETTERS = "n"
    _UNSAFE_SUB_CHARS = re.compile(r'[^0-9' + _SUB_SAFE_LETTERS + r'+\u2212\u2013\u2014\-=()]')
    _UNSAFE_SUP_CHARS = re.compile(r'[^0-9' + _SUP_SAFE_LETTERS + r'+\u2212\u2013\u2014\-=()]')

    def _read_braced_group(text, pos):
        """Depth-aware {...} reader -- handles nested braces correctly,
        unlike a naive '[^}]+' regex which stops at the first '}' and
        corrupts anything with a nested group inside (e.g. x^{y^{z}})."""
        if pos >= len(text) or text[pos] != '{':
            return None, pos
        depth = 0
        start = pos
        for j in range(pos, len(text)):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    return text[start+1:j], j + 1
        return None, pos

    def _read_paren_group(text, pos):
        """Depth-aware (...) reader, same logic as _read_braced_group but
        for parenthesized exponents/subscripts (e.g. AI output 'P^(a/b)'
        instead of 'P^{a/b}') -- both forms are common AI-generated syntax
        and must be treated identically."""
        if pos >= len(text) or text[pos] != '(':
            return None, pos
        depth = 0
        start = pos
        for j in range(pos, len(text)):
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
                if depth == 0:
                    return text[start+1:j], j + 1
        return None, pos

    def _script_repl_scan(text, marker_char, repl_fn):
        """Scan for marker_char ('_' or '^') followed by a {..} or (..)
        group (depth-aware) or a bare token, replacing each via repl_fn(inner)."""
        out = []
        i = 0
        while i < len(text):
            if text[i] == marker_char and i + 1 < len(text) and text[i+1] == '{':
                inner, p = _read_braced_group(text, i + 1)
                if inner is not None:
                    out.append(repl_fn(inner))
                    i = p
                    continue
            if text[i] == marker_char and i + 1 < len(text) and text[i+1] == '(':
                inner, p = _read_paren_group(text, i + 1)
                if inner is not None:
                    out.append(repl_fn(inner))
                    i = p
                    continue
            out.append(text[i])
            i += 1
        return ''.join(out)

    def _bare_frac_to_latex(inner):
        """If inner contains a top-level bare '/' (division AI wrote as
        plain text instead of \\frac{}{}, e.g. 'T\\gamma/1-\\gamma' or
        'Tγ/1-γ'), rebuild it as a real \\frac{num}{den}. Only the FIRST
        top-level '/' (outside any nested (), {}) is treated as the
        fraction bar -- this matches how these expressions are always
        written (single ratio, not nested divisions). Returns None if no
        top-level '/' is found so the caller can fall back to its normal
        handling."""
        depth = 0
        for idx, ch in enumerate(inner):
            if ch in '({':
                depth += 1
            elif ch in ')}':
                depth -= 1
            elif ch == '/' and depth == 0:
                num, den = inner[:idx].strip(), inner[idx+1:].strip()
                if num and den:
                    return f"\\dfrac{{{_to_latex_inner(num)}}}{{{_to_latex_inner(den)}}}"
                return None
        return None

    def _sub_repl_inner(inner):
        inner = inner.strip()
        _frac_marker_match = re.fullmatch(r'ZZZNFRACLATEX(\d+)ZZZ', inner)
        if _frac_marker_match:
            # inner is a placeholder for an already-built $\frac{...}{...}$ --
            # combine into one valid $_{\frac{...}{...}}$ instead of nesting
            # a second $...$ pair around the placeholder (which corrupts to
            # unbalanced-$ LaTeX once the marker gets restored later).
            _raw_frac = _nfrac_markers[int(_frac_marker_match.group(1))].strip('$')
            return f"$_{{{_raw_frac}}}$"
        _bare_frac = _bare_frac_to_latex(inner)
        if _bare_frac is not None:
            return f"$_{{{_bare_frac}}}$"
        if _UNSAFE_SUB_CHARS.search(inner):
            return f"$_{{{_to_latex_inner(inner)}}}$"
        return inner.translate(SUB_MAP)

    def _sup_repl_inner(inner):
        inner = inner.strip()
        _frac_marker_match = re.fullmatch(r'ZZZNFRACLATEX(\d+)ZZZ', inner)
        if _frac_marker_match:
            _raw_frac = _nfrac_markers[int(_frac_marker_match.group(1))].strip('$')
            return f"$^{{{_raw_frac}}}$"
        _bare_frac = _bare_frac_to_latex(inner)
        if _bare_frac is not None:
            return f"$^{{{_bare_frac}}}$"
        if _UNSAFE_SUP_CHARS.search(inner):
            return f"$^{{{_to_latex_inner(inner)}}}$"
        return inner.translate(SUP_MAP)

    def _to_latex_inner(inner: str) -> str:
        # rebuild any bare Unicode symbol (γ, θ, ×, etc) back into its LaTeX
        # command so the resulting $...$ is valid, render-able LaTeX source.
        # If a nested _{...}/^{...} exists inside (e.g. x^{y^{z}}), don't
        # attempt to recursively re-wrap it in its own $...$ (that produces
        # unbalanced-$ output) -- just leave it as raw LaTeX, which is still
        # valid once the whole thing gets one outer $...$ wrap by the caller.
        out = []
        for ch in inner:
            out.append(UNICODE_TO_LATEX.get(ch, ch))
        return ''.join(out)

    def _sub_repl(m):
        return _sub_repl_inner(m.group(1))

    def _sup_repl(m):
        return _sup_repl_inner(m.group(1))

    text = _script_repl_scan(text, '^', _sup_repl_inner)
    text = _script_repl_scan(text, '_', _sub_repl_inner)
    # Bare underscore/caret fallback (no {}/() group) must only grab the
    # IMMEDIATE simple subscript/exponent token (digits and/or letters) --
    # previously the char class also included '+'/'-', so a multi-term
    # expression like "I_1-90I_g-32I_2=0" (no braces around each index)
    # had its greedy match swallow "1-90I" as ONE subscript, corrupting
    # the literal minus between terms into a subscript-minus glyph and
    # eating the next term's base letter too. Real negative-subscript
    # cases are rare and already handled via the braced/paren form
    # (_{-1}), so bare form is restricted to plain alnum only.
    text = re.sub(r'_([0-9a-zA-Z]+)', _sub_repl, text)
    text = re.sub(r'\^([0-9a-zA-Z]+)', _sup_repl, text)

    # Protect the $^{...}$ / $_{...}$ LaTeX fallback just created above from
    # this function's own later brace-stripping and backslash-command
    # stripping regexes (same reasoning as the nested-\frac protection).
    _script_latex_markers = []
    def _script_latex_protect(m):
        _script_latex_markers.append(m.group(0))
        return f"ZZZSCRIPTLATEX{len(_script_latex_markers)-1}ZZZ"
    text = re.sub(r'\$[_^]\{.*?\}\$', _script_latex_protect, text)

    # Universal degree-number fix: superscript digits right before "°" must
    # become plain digits (e.g. "⁶⁷°" -> "67°"). The DOM-text extraction's
    # get_text(separator=" ", ...) sometimes inserts a stray space between
    # individual superscript digits themselves (not just around other tags),
    # producing "⁶ ⁷°" or "⁶ ⁷ °" -- the non-spaced version below wouldn't
    # match that. Strip any spaces within a run of superscript digits (and
    # between the run and a following °) before doing the digit conversion,
    # so it's covered regardless of where the stray space landed.
    # Also covers decimal points and superscript minus signs mixed into the
    # run (e.g. "⁰.⁰³°" -> "0.03°", "⁻⁴¹⁹.⁴°" -> "-419.4°") -- previously
    # only bare superscript digit runs matched, so any negative or decimal
    # value before ° leaked through as raw superscript unicode.
    text = re.sub(
        r'((?:[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]|\.(?=[⁰¹²³⁴⁵⁶⁷⁸⁹]))\s*)+°',
        lambda m: m.group(0)[:-1].replace(' ', '').translate(SUP_TO_NORMAL) + '°',
        text,
    )
    text = text.replace('^\\circ', '°').replace('^{\\circ}', '°').replace('∘', '°')
    text = text.replace('° C', '°C').replace('^ C', '°C')
    text = re.sub(r'(\d)\s+°', r'\1°', text)

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

    # --- Scientific-notation / ion-notation cleanup -----------------
    # Some source pages already contain literal Unicode sub/superscript
    # characters (⁰-⁹, ₀-₉, ⁺⁻₊₋) but with (a) the wrong minus glyph
    # (U+2212 "−" instead of the proper superscript/subscript minus) and
    # (b) stray spaces the original author/AI-generator left between a
    # base symbol and its sub/superscript, e.g. "NH 4 +" instead of
    # "NH₄⁺", or "SO 4 2 −" instead of "SO₄²⁻". These are unambiguous,
    # safe-to-fix spacing/glyph issues (no chemistry knowledge needed):

    # 1) Convert a bare "−" that is directly touching an existing sub/sup
    #    digit into the matching sub/sup minus, so mixed runs like "¹³−"
    #    become fully "¹³⁻" instead of a garbled mix of styles. Also
    #    handles the case where a space sits between the digit and the
    #    minus (source often renders base/charge/sign as separate nodes,
    #    e.g. sup"2" + text"−" -> "² −" instead of "²⁻").
    #    IMPORTANT: this must only fire for a TRAILING charge/exponent sign
    #    (nothing after it, or immediately another sub/sup char/closing
    #    bracket) -- NOT for ordinary subtraction that happens to follow a
    #    subscripted variable, e.g. "T₂ - ln T₁" or "ms(ln T₂ - ln T₁)".
    #    Without this guard, a real minus between two terms was wrongly
    #    swallowed into a subscript minus (T₂ - -> T₂₋), corrupting the
    #    arithmetic operator and losing the space that should stay before
    #    the next term.
    text = re.sub(r'([⁰¹²³⁴⁵⁶⁷⁸⁹])\s*[\u2212\u2013\u2014](?=\s*(?:[⁰¹²³⁴⁵⁶⁷⁸⁹]|[)\]]|$))', r'\1⁻', text)
    text = re.sub(r'([₀₁₂₃₄₅₆₇₈₉])\s*[\u2212\u2013\u2014](?=\s*(?:[₀₁₂₃₄₅₆₇₈₉]|[)\]]|$))', r'\1₋', text)
    text = re.sub(r'[\u2212\u2013\u2014]\s*([⁰¹²³⁴⁵⁶⁷⁸⁹])', r'⁻\1', text)
    text = re.sub(r'[\u2212\u2013\u2014]\s*([₀₁₂₃₄₅₆₇₈₉])', r'₋\1', text)

    # 2) "× 10" scientific notation: if the digits right after "×" were
    #    wrongly superscripted along with the real exponent (source HTML
    #    put the whole "10-13" inside one <sup>), the base "10" should
    #    read as normal text -- only the true exponent stays raised.
    #    "×¹⁰⁻¹³" -> "×10⁻¹³", "×¹⁰⁶" -> "×10⁶".
    text = re.sub(
        r'×\s*¹⁰([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]*)',
        lambda m: '×10' + m.group(1),
        text,
    )

    # 3) Squeeze whitespace that shouldn't be there: between a normal
    #    letter/digit/bracket and an adjacent sub/superscript run, and
    #    inside bracket pairs used for complex ions e.g. "[ PO 4 3 − ]²".
    supsub_chars = "⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉⁺⁻₊₋ⁿ"
    text = re.sub(rf'([A-Za-z\]\)])\s+(?=[{supsub_chars}])', r'\1', text)
    text = re.sub(rf'([{supsub_chars}])\s+(?=[A-Za-z0-9\[\(])', r'\1', text)
    text = re.sub(rf'([{supsub_chars}])\s+(?=[{supsub_chars}])', r'\1', text)
    text = re.sub(r'\[\s+', '[', text)
    text = re.sub(r'\s+\]', ']', text)
    # A bare digit immediately before a sub/superscript run inside the
    # same "word" (e.g. "PO 4 3 −" -> "PO4" + "3−") still needs the
    # digit-to-digit space closed once the run above has been squeezed.
    text = re.sub(rf'(\d)\s+(?=[{supsub_chars}])', r'\1', text)

    # A unicode superscript digit run directly followed (no gap) by a plain
    # digit reads as one merged number -- "2³4" looks like the power is
    # "34" when it's actually 2^3 followed by a separate "4". Insert a
    # thin space (U+2009) so the superscript visually ends before the
    # plain digit starts, without disturbing spacing anywhere else.
    text = re.sub(r'([⁰¹²³⁴⁵⁶⁷⁸⁹])(?=\d)', '\\1\u2009', text)

    text = text.replace('\ufeff', '').replace('\u200b', '').replace('\u200c', '')

    # --- Universal bracket-spacing squeeze (chemical formulas ONLY) --
    # e.g. "Al ( OH) 3" -> "Al(OH)3", "Ca ( NO3 )2" -> "Ca(NO3)2".
    # Scoped to formula-like runs (element symbols/digits/brackets only,
    # no lowercase words) so normal English parentheticals like
    # "this is (a bird)" are never touched.
    _formula_run = re.compile(
        r'(?:[A-Z][a-z]?\d*\s*){1,}'
        r'(?:\(\s*(?:[A-Za-z]{1,2}\d*\s*)+\)\s*\d*\s*)+'
        r'(?:[A-Z][a-z]?\d*\s*)*'
    )

    def _squeeze_formula(m):
        s = m.group(0)
        s = re.sub(r'\(\s+', '(', s)
        s = re.sub(r'\s+\)', ')', s)
        s = re.sub(r'([A-Za-z0-9\)])\s+\(', r'\1(', s)
        s = re.sub(r'\)\s+(?=\d)', ')', s)
        return s.rstrip() + (' ' if m.group(0).endswith(' ') else '')

    text = _formula_run.sub(_squeeze_formula, text)

    # Generic "( x )" -> "(x)" tightening for short parenthesized math
    # terms -- source DOM often has each token (open-paren, letter,
    # operator, close-paren) as a SEPARATE MathML node, and
    # get_text(separator=" ") inserts a space at every one of those node
    # boundaries, so a simple "(r-1)" comes out as "( r - 1 )". Scoped to
    # parens whose entire inside is short math-only content (letters,
    # digits, +-−=, no spaces-separated words) so real prose parentheticals
    # like "(for example)" are never touched.
    text = re.sub(
        r'\(\s*([A-Za-z0-9+\u2212\u2013\u2014\-=\u00b1 ]{1,12}?)\s*\)',
        lambda m: '(' + re.sub(r'\s+', '', m.group(1)) + ')'
        if re.fullmatch(r'[A-Za-z0-9+\u2212\u2013\u2014\-=\u00b1\s]+', m.group(1)) and not re.search(r'[A-Za-z]{4,}', m.group(1))
        else m.group(0),
        text,
    )

    # Collapse horizontal whitespace runs (spaces/tabs) but keep newlines
    # intact -- numbered/roman-numeral sub-point lists rely on them to
    # render as separate lines instead of one run-on line.
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    for _i, _marker in enumerate(_nfrac_markers):
        text = text.replace(f"ZZZNFRACLATEX{_i}ZZZ", _marker)
    for _i, _marker in enumerate(_script_latex_markers):
        text = text.replace(f"ZZZSCRIPTLATEX{_i}ZZZ", _marker)
    for _i, _marker in enumerate(_pre_final_markers):
        text = text.replace(f"ZZZPREFINAL{_i}ZZZ", _marker)


    # Two side-by-side fractions with nothing but whitespace between them
    # (e.g. source math "1/x  1/y" meaning "(1/x) times (1/y)") must not be
    # left as bare "1/x 1/y" -- that reads as two separate unrelated
    # fractions sitting next to each other with no operator, ambiguous/
    # wrong. Wrap each side in parens and make the implied multiplication
    # explicit: "(1/x)×(1/y)". Runs while changes are found so a chain of
    # 3+ adjacent fractions ("1/x 1/y 1/z") gets every gap fixed, not just
    # the first pair.
    _adj_plain_frac = re.compile(
        r'(?<![/\w])([A-Za-z0-9]+/[A-Za-z0-9]+)[ \t]+([A-Za-z0-9]+/[A-Za-z0-9]+)(?![/\w(])'
    )
    while True:
        new_text = _adj_plain_frac.sub(r'(\1)×(\2)', text)
        if new_text == text:
            break
        text = new_text

    # Same fix for two adjacent already-built $\dfrac{}{}$ LaTeX fractions
    # (from real \frac source commands) sitting next to each other with
    # only whitespace between -- same ambiguity, same implied-multiplication
    # fix, just wrapping the LaTeX blocks in literal parens instead of
    # rebuilding them as plain text.
    _adj_latex_frac = re.compile(
        r'(\$\\d?frac\{[^{}$]*\}\{[^{}$]*\}\$)[ \t]+(\$\\d?frac\{[^{}$]*\}\{[^{}$]*\}\$)'
    )
    while True:
        new_text = _adj_latex_frac.sub(r'(\1)×(\2)', text)
        if new_text == text:
            break
        text = new_text

    # Every remaining bare fraction "a/b" (not already inside brackets/LaTeX)
    # must be wrapped in parens regardless of what's next to it -- not just
    # when two fractions sit side by side. "x + a/b = 5" -> "x + (a/b) = 5",
    # "ফলাফল a/b হবে" -> "ফলাফল (a/b) হবে". Runs after the adjacent-fraction
    # pass above so "(1/x)×(1/y)" is untouched (already bracketed) and any
    # single leftover fraction still gets wrapped.
    _bare_frac = re.compile(r'(?<![/\w(])([A-Za-z0-9]+/[A-Za-z0-9]+)(?![/\w)])')
    text = _bare_frac.sub(r'(\1)', text)

    return text.strip()


def format_content(element, img_map):
    text = _format_content_inner(element, img_map)
    # Final safety-net: run the universal degree/superscript/spacing fixes
    # from aggressive_clean() on every format_content() output too -- the
    # per-<sup> sibling-based ° check above only catches the case where the
    # ° sits in the immediate next sibling text node with no gap; if the
    # source markup has any intervening whitespace-only node (or the ° is
    # further down after other inline tags), that check misses it and a
    # raw '⁴°C' style artifact would otherwise reach the user untouched.
    #
    # BUT by this point _format_content_inner already returned a finished
    # <img class="qimg" src="...URL..."> tag with the real image URL
    # embedded verbatim and UNPROTECTED -- running aggressive_clean() a
    # second time here has no img-marker protection, so any underscore in
    # the URL's filename (chorcha's asset URLs commonly contain one, e.g.
    # ".../rP7HceGc-bQaU_F3W0ozj.png") gets misread by aggressive_clean's
    # bare "_word" subscript-rebuild regex and turned into broken LaTeX
    # (".../rP7HceGc-bQaU$_{F3W0ozj}$.png"), corrupting the URL so the
    # image can never load. Re-protect any <img ...> tag before this
    # second pass, same marker technique already used inside
    # _format_content_inner's own aggressive_clean call.
    if not text:
        return text
    img_tag_markers = []
    def _img_tag_repl(match):
        img_tag_markers.append(match.group(0))
        return f" ZZZIMGTAG{len(img_tag_markers)-1}ZZZ "
    protected = re.sub(r'<img\b[^>]*>', _img_tag_repl, text)
    cleaned = aggressive_clean(protected)
    for i, marker in enumerate(img_tag_markers):
        cleaned = cleaned.replace(f"ZZZIMGTAG{i}ZZZ", marker)
    return cleaned


def _format_content_inner(element, img_map):
    if not element:
        return ""

    for hidden in element.find_all(['annotation', 'script', 'mjx-assistive-mathml']):
        hidden.decompose()
    for hidden in element.find_all('span', class_=['katex-html', 'MJX_Assistive_MathML', 'MathJax_Preview']):
        hidden.decompose()
    # Tailwind-style visually-hidden duplicate content -- e.g. an
    # accessibility-only "sr-only"/"visually-hidden" span that repeats the
    # SAME text as the visible element right next to it. If left in place,
    # get_text() concatenates the visible copy + this hidden duplicate (and
    # sometimes a third invisible katex-mathml copy already handled above),
    # producing tripled option text like "0.2 A0.2 A0.2 A" instead of
    # "0.2 A". These classes carry no visible content, only screen-reader
    # duplicates, so they're always safe to drop entirely.
    for hidden in element.find_all(class_=lambda c: c and any(
        cls in ('sr-only', 'visually-hidden', 'visuallyhidden', 'screen-reader-only', 'hidden')
        for cls in (c if isinstance(c, list) else c.split())
    )):
        hidden.decompose()

    for mfrac in element.find_all('mfrac'):
        # An <mfrac> nested directly inside <msup>/<msub>/<msubsup> as the
        # exponent/subscript (e.g. TP^((1-γ)/γ) written as
        # <msup><mrow>TP</mrow><mfrac>...</mfrac></msup>) must NOT be
        # flattened here -- doing so replaces it with a bare string, so
        # msup.find_all(recursive=False) below sees only 1 remaining child
        # instead of 2, base_text detection fails, and the base ("TP") gets
        # silently swallowed into sup_text instead of kept separate,
        # producing "$^{TP}$" with the fraction AND base both lost/merged.
        # Let the msup/msub loops handle their own direct-child mfrac.
        if mfrac.parent and mfrac.parent.name in ('msup', 'msub', 'msubsup'):
            continue
        contents = mfrac.find_all(recursive=False)
        if len(contents) == 2:
            num = contents[0].get_text(strip=True)
            den = contents[1].get_text(strip=True)
            mfrac.replace_with(f"{num}/{den}")

    # <msubsup> (base with BOTH a subscript AND superscript at once, e.g.
    # R with sub 1 and sup 2 -- MathML: <msubsup><mi>R</mi><mn>1</mn><mn>2</mn></msubsup>)
    # was never handled at all -- it doesn't match 'sub'/'msub' OR
    # 'sup'/'msup', so it fell all the way through to the final
    # get_text(separator=" ") flatten untouched, producing broken output
    # like "R 1 2" (base + sub + sup as three space-separated plain-text
    # tokens) instead of "R₁²". Convert its 3 children (base, sub, sup)
    # into text with SUB_MAP/SUP_MAP applied to the right piece, same
    # unsafe-char LaTeX-fallback logic as the msub/msup blocks below.
    for msubsup in element.find_all('msubsup'):
        kids = msubsup.find_all(recursive=False)
        if len(kids) == 3:
            base_t = kids[0].get_text(strip=True)
            sub_t = kids[1].get_text(strip=True)
            sup_t = kids[2].get_text(strip=True)
            sub_out = f"$_{{{sub_t}}}$" if re.search(r'[^0-9+\u2212\u2013\u2014\-=()aeoxhklmnpst]', sub_t) else sub_t.translate(SUB_MAP)
            sup_out = f"$^{{{sup_t}}}$" if re.search(r'[^0-9+\u2212\u2013\u2014\-=()n]', sup_t) else sup_t.translate(SUP_MAP)
            msubsup.replace_with(base_t + sub_out + sup_out)
        else:
            msubsup.replace_with(msubsup.get_text(strip=True))

    # <msqrt> (plain square root, MathML) and <mroot> (nth root, MathML)
    # were NEVER handled anywhere in this function -- unlike mfrac/msub/
    # msup/msubsup above, there was no branch for them at all, so a real √
    # symbol in the source math silently VANISHED entirely during
    # extraction (the radicand's plain text survived via the final
    # get_text() flatten, but the √ sign itself, which lives only in the
    # tag semantics, was lost with no trace). Handle both aggressively so
    # a root is never dropped, only ever rendered.
    for msqrt in element.find_all('msqrt'):
        inner = msqrt.get_text(strip=True)
        if re.fullmatch(r'[A-Za-z0-9]+', inner):
            msqrt.replace_with('√' + inner)
        else:
            msqrt.replace_with('√(' + inner + ')')
    for mroot in element.find_all('mroot'):
        # <mroot> has exactly 2 children: [radicand, index] e.g.
        # <mroot><mi>x</mi><mn>3</mn></mroot> = cube root of x.
        kids = mroot.find_all(recursive=False)
        if len(kids) == 2:
            radicand = kids[0].get_text(strip=True)
            idx = kids[1].get_text(strip=True)
            rad_out = radicand if re.fullmatch(r'[A-Za-z0-9]+', radicand) else f"({radicand})"
            idx_out = idx.translate(SUP_MAP) if re.fullmatch(r'[0-9]+', idx) else f"({idx})"
            mroot.replace_with(f"{idx_out}√{rad_out}")
        else:
            # Malformed/unexpected structure -- still surface the raw text
            # rather than silently dropping the whole root.
            mroot.replace_with('√(' + mroot.get_text(strip=True) + ')')

    # <mover> (base with a symbol stacked directly above it) was never
    # handled -- most commonly this is an arrow with a condition/label
    # above it, e.g. MathML <mover><mo>→</mo><mi>Z</mi></mover> for
    # \xrightarrow{Z}. Left unhandled, get_text() flattens base+overlay
    # as two space-separated tokens with no indication one sits above the
    # other (e.g. "→ Z"), losing the stacked relationship entirely.
    # Detect the arrow-with-label case specifically and emit real LaTeX
    # \xrightarrow{...}/\xleftarrow{...} so it can be rendered back
    # correctly later; any other <mover> combination still gets a safe
    # fallback of "base(overlay)" rather than silently vanishing.
    ARROW_MOVER_MAP = {
        '→': r'\xrightarrow', '⟶': r'\xrightarrow',
        '←': r'\xleftarrow', '⟵': r'\xleftarrow',
    }
    # Overline bar over a letter/base (e.g. mean/average notation Ā, or a
    # repeating-decimal vinculum) is ALSO a <mover>, with the overlay being
    # a bar character ('‾' U+203E, '¯' U+00AF, or '-'/'−' used as a bar in
    # some source markup) instead of an arrow. This must not fall into the
    # generic "base(overlay)" fallback below (which would wrongly produce
    # "A(‾)" -- a stray character in parentheses, not a bar over the
    # letter). Emit the real combining overline U+0305 directly on the
    # base so it sits properly stacked, same reasoning as the existing
    # vector-arrow (U+20D7) combining-mark fix a few lines below.
    OVERLINE_CHARS = {'‾', '¯', '-', '−', '—'}
    for mover in element.find_all('mover'):
        kids = mover.find_all(recursive=False)
        if len(kids) == 2:
            base_t = kids[0].get_text(strip=True)
            over_t = kids[1].get_text(strip=True)
            if base_t in ARROW_MOVER_MAP:
                if over_t:
                    mover.replace_with(f"${ARROW_MOVER_MAP[base_t]}{{{over_t}}}$")
                else:
                    mover.replace_with(base_t)
            elif over_t in OVERLINE_CHARS and base_t:
                # A combining overline (U+0305) directly on the base
                # letter was tried first, but that's inconsistent across
                # fonts/devices (some Android/mobile fonts fail to shape
                # it correctly) -- same class of problem as the vector
                # arrow combining-mark issue this file already works
                # around elsewhere. Use explicit LaTeX instead, which
                # MathJax renders identically everywhere: \bar{} for a
                # single letter/digit, \overline{} for a multi-character
                # base (line-segment notation like AB, or any expression)
                # since \bar only ever sits over one character in LaTeX.
                if len(base_t) == 1:
                    mover.replace_with(f"$\\bar{{{base_t}}}$")
                else:
                    mover.replace_with(f"$\\overline{{{base_t}}}$")
            else:
                mover.replace_with(f"{base_t}({over_t})" if over_t else base_t)
        else:
            mover.replace_with(mover.get_text(strip=True))

    for sub in element.find_all(['sub', 'msub']):
        if sub.find('img'):
            continue
        # A real HTML <sub> tag contains ONLY the subscript part, so
        # get_text() on the whole element is correct. But MathML <msub>
        # contains TWO children -- [base, subscript] (e.g.
        # <msub><mn>10</mn><mn>16</mn></msub> for "10 with subscript 16")
        # -- so get_text() on the whole element wrongly concatenates base+
        # script into one string (e.g. "1016") and translates BOTH through
        # SUB_MAP, corrupting a numeric base like "10" into subscript
        # digits too (e.g. "10¹⁶"-style bug but for subscripts: base "10"
        # became "₁₀" instead of staying plain). Only the LAST child of an
        # <msub> is the actual script; keep the base untouched.
        if sub.name == 'msub':
            kids = sub.find_all(recursive=False)
            base_text = kids[0].get_text(strip=True) if len(kids) >= 2 else ""
            sub_text = kids[-1].get_text(strip=True) if kids else sub.get_text(strip=True)
        else:
            base_text = ""
            sub_text = sub.get_text(strip=True)
        # Fraction inside a subscript -- unicode has no fraction-in-subscript,
        # so emit LaTeX instead of mangling it.
        if re.search(r'/', sub_text) and len(sub_text) > 2:
            sub.replace_with(f"{base_text}$_{{{sub_text}}}$")
        else:
            sub.replace_with(base_text + sub_text.translate(SUB_MAP))
    for sup in element.find_all(['sup', 'msup']):
        if sup.find('img'):
            continue
        # Same base-vs-script split as msub above -- an <msup> like
        # <msup><mn>10</mn><mn>16</mn></msup> ("10^16") must keep its base
        # "10" as plain text and only superscript the "16" exponent;
        # get_text() on the whole element previously concatenated both
        # into "1016" and superscripted everything, producing the broken
        # "¹⁰¹⁶" instead of the correct "10¹⁶".
        if sup.name == 'msup':
            kids = sup.find_all(recursive=False)
            base_text = kids[0].get_text(strip=True) if len(kids) >= 2 else ""
            if kids and kids[-1].name == 'mfrac':
                # Exponent is itself a fraction (e.g. TP^((1-γ)/γ)) -- read
                # it as num/den, not raw get_text() which would concatenate
                # numerator+denominator with no "/" between them.
                frac_kids = kids[-1].find_all(recursive=False)
                if len(frac_kids) == 2:
                    num_t = frac_kids[0].get_text(strip=True)
                    den_t = frac_kids[1].get_text(strip=True)
                    if re.search(r'[+\-−]', num_t):
                        num_t = f"({num_t})"
                    if re.search(r'[+\-−]', den_t):
                        den_t = f"({den_t})"
                    sup_text = f"{num_t}/{den_t}"
                else:
                    sup_text = kids[-1].get_text(strip=True)
            else:
                sup_text = kids[-1].get_text(strip=True) if kids else sup.get_text(strip=True)
        else:
            base_text = ""
            sup_text = sup.get_text(strip=True)
        # Unit-vector "hat" notation on the exponent's trailing letter(s)
        # (e.g. exponent node text "5j^" or "5jk^-1" from source markup that
        # nests the hat-marked unit inside the same <msup> as the numeric
        # exponent) must NOT be treated as part of the exponent -- it's a
        # separate unit multiplied after 10^5, not "10 to the power of
        # 5jk^-1". Split it off before the exponent is classified, so a
        # purely-numeric exponent like "5" isn't wrongly routed into the
        # LaTeX $^{...}$ fallback just because a trailing hat-unit was
        # glued onto its text. The split-off tail is re-appended as plain
        # text (aggressive_clean()'s hat/vector regexes convert it to a
        # proper combining circumflex later) after the exponent itself has
        # been rendered.
        _hat_tail_m = re.match(r'^([0-9+\u2212\u2013\u2014\-=()n]*)([A-Za-z]\^?.*)$', sup_text) if sup_text else None
        _hat_tail = ""
        if _hat_tail_m and _hat_tail_m.group(1) and ('^' in _hat_tail_m.group(2) or _hat_tail_m.group(2)):
            # Only split when there's a genuine numeric-exponent prefix
            # followed by trailing unit letters -- avoids touching real
            # letter-only exponents like x^n or x^m which must stay as-is.
            _digits_prefix = _hat_tail_m.group(1)
            _tail = _hat_tail_m.group(2)
            if _digits_prefix and re.fullmatch(r'[0-9+\u2212\u2013\u2014\-=()n]+', _digits_prefix):
                sup_text = _digits_prefix
                _hat_tail = _tail
        # A <sup>/<msup> whose own text ends in "°", OR whose very next
        # sibling text starts with "°" (source markup sometimes wraps
        # just the number in <sup> right before a separate "°C"/"° C"
        # node), is a degree-temperature value (e.g. 20°C) -- not a math
        # exponent. Keep those digits as plain text so "20°C" never
        # becomes "²⁰°C".
        next_sib = sup.next_sibling
        next_text = next_sib.strip() if isinstance(next_sib, str) else (next_sib.get_text() if next_sib else "")
        # KaTeX's invisible MathML tree (<msup><mn>98</mn><mo>∘</mo></msup>)
        # uses U+2218 RING OPERATOR for the degree symbol, NOT U+00B0
        # DEGREE SIGN -- this check only matched U+00B0, so a temperature
        # like 98°C coming from the MathML branch never matched the
        # degree-exception here, fell through to SUP_MAP translation (which
        # doesn't cover U+2218 either, silently dropping it), and produced
        # "৯৮" -> "⁹⁸" with the ° stripped entirely and the surrounding
        # space lost -- e.g. "বিন্দু 98°C" became "বিন্দু98°C" with no gap.
        # aggressive_clean() converts ∘->° later, but by then the sup tag
        # is already gone and the joined-text spacing can't be recovered.
        _is_deg = sup_text.rstrip().endswith(('°', '∘')) or (next_text or "").lstrip().startswith(('°', '∘'))
        if _is_deg:
            sup.replace_with(base_text + sup_text.replace('∘', '°') + _hat_tail)
        elif re.search(r'/', sup_text) and len(sup_text) > 2:
            # Fraction-in-superscript (e.g. "(1-γ)/γ") -- unicode superscript
            # can't represent a fraction, so emit LaTeX instead of mangling
            # it into broken/unreadable unicode chars.
            sup.replace_with(f"{base_text}$^{{{sup_text}}}${_hat_tail}")
        elif sup_text and not all(c in "0123456789+\u2212\u2013\u2014-=()n" for c in sup_text):
            # SUP_MAP only has glyphs for digits/+-=()n -- any other
            # character (e.g. m, x, γ, or any letter besides n) has no
            # unicode superscript equivalent, so .translate() below would
            # silently leave it as plain text, indistinguishable from the
            # base ("a^n b^m" -> "aⁿbm", the second exponent invisible as
            # a power). Emit LaTeX ^{...} instead so it's unambiguous.
            sup.replace_with(f"{base_text}$^{{{sup_text}}}${_hat_tail}")
        else:
            sup.replace_with(base_text + sup_text.translate(SUP_MAP) + _hat_tail)

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

    # Insert real newlines at list/block-item boundaries (li, br, p) BEFORE
    # flattening to text, so numbered sub-point stems like
    # "1. আকারে ছোট  2. কান্ড ও পাতা রসালো  3. ত্বকে কিউটিকল অনুপস্থিত"
    # or roman-numeral variants ("i. ... ii. ... iii. ...") come out on
    # separate lines in the CSV, matching how the source page displays
    # them, instead of being joined into one run-on line.
    for br in element.find_all('br'):
        br.replace_with("\n")

    # numbered/roman-numeral sub-point stems (e.g. "1. ... 2. ... 3. ...")
    # render as <li> items in the source markup. Build text by walking
    # direct text runs and only breaking to a new line at <li>/<p>
    # boundaries -- everything else (inline spans, bold, sub/sup) stays
    # joined with spaces exactly as before, so this only affects list
    # items and doesn't fragment normal running prose.
    line_tags_raw = element.find_all(['li', 'p'])
    # Skip a <p> that is nested inside an <li> we've already matched --
    # e.g. <li><p>চিরসবুজ বৃক্ষ</p></li> -- otherwise find_all(['li','p'])
    # matches BOTH tags and the same text gets collected twice, producing
    # duplicated lines like "চিরসবুজ বৃক্ষ\nচিরসবুজ বৃক্ষ" in the output.
    line_tags = [
        lt for lt in line_tags_raw
        if not (lt.name == 'p' and lt.find_parent('li') in line_tags_raw)
    ]
    if line_tags:
        parts = []
        for lt in line_tags:
            t = lt.get_text(separator=" ", strip=True)
            if not t:
                continue
            if lt.name == 'li':
                parent = lt.find_parent(['ol', 'ul'])
                if parent is not None and parent.name == 'ol':
                    # number relative to this <li>'s position among its
                    # OWN <ol> siblings (not the flat line_tags list),
                    # so nested/adjacent lists each number from 1.
                    siblings = [c for c in parent.find_all('li', recursive=False)]
                    try:
                        n = siblings.index(lt) + 1
                        # ZZZOLDOTZZZ protects the number-marker's period
                        # from aggressive_clean's generic ". "->"." collapse
                        # (meant for decimal numbers), which would otherwise
                        # turn "1. বোটানিক্যাল" into "1.বোটানিক্যাল".
                        t = f"{n}ZZZOLDOTZZZ {t}"
                    except ValueError:
                        pass
                elif parent is not None and parent.name == 'ul':
                    t = f"• {t}"
            parts.append(t)
        # anything outside of li/p (e.g. the "নিচের কোনটি সঠিক?" tail
        # text, or the whole stem when there's no list at all) still
        # needs to be captured -- fall back to the full element text if
        # the li/p-only join loses content (e.g. because the list items
        # are nested inside a <ul> that's itself inside a <p>, causing
        # double-counting, or because most of the text is NOT in li/p).
        joined = "\n".join(parts)
        full_flat = element.get_text(separator=" ", strip=True)
        # Heuristic: only prefer the line-broken join when it actually
        # captured most of the same content (avoids silently dropping
        # text that lives outside li/p tags).
        if joined and len(joined.replace("\n", " ")) >= len(full_flat) * 0.6:
            raw_text = joined
        else:
            raw_text = full_flat
    else:
        raw_text = element.get_text(separator=" ", strip=True)
    img_markers = []

    def img_repl(match):
        img_markers.append(match.group(0))
        return f" ZZZIMG{len(img_markers)-1}ZZZ "

    raw_text = re.sub(r'img_s.*?img_e', img_repl, raw_text)

    # Protect fraction-in-sup/sub LaTeX spans (e.g. "$^{(1-γ)/γ}$") from
    # aggressive_clean's own \^{...}/\_{...} stripping regexes below --
    # otherwise it would immediately unpack what we just wrapped in LaTeX.
    latex_markers = []
    def _latex_repl(match):
        latex_markers.append(match.group(0))
        return f" ZZZLATEX{len(latex_markers)-1}ZZZ "
    raw_text = re.sub(r'\$[\^_]\{[^}]+\}\$', _latex_repl, raw_text)

    cleaned_text = aggressive_clean(raw_text)
    cleaned_text = cleaned_text.replace("ZZZOLDOTZZZ", ".")

    for i, marker in enumerate(img_markers):
        cleaned_text = cleaned_text.replace(f"ZZZIMG{i}ZZZ", marker)
    for i, marker in enumerate(latex_markers):
        cleaned_text = cleaned_text.replace(f"ZZZLATEX{i}ZZZ", marker).replace(f"ZZZLATEX{i} ZZZ", marker)

    return re.sub(r'img_s(.*?)img_e', r'<img class="qimg" src="\1">', cleaned_text)


def post_process(results: list) -> list:
    # Page-এ যতগুলো MCQ card আছে ততগুলোই রাখা হয় — কোনো dedup/filter না,
    # exact same question hubohu দুইবার থাকলেও (ভিন্ন university tag-এ
    # legit repeat entry) সবগুলো output-এ থাকবে।
    return [r for r in results if r.get('questions', '').strip()]


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
    chorcha_cards = soup.find_all('div', class_=lambda x: x and 'rounded-xl' in x and ('p-5' in x or 'pb-6' in x))

    if chorcha_cards:
        results = []
        ans_map = {'ক': '1', 'খ': '2', 'গ': '3', 'ঘ': '4'}
        _total_cards = len(chorcha_cards)

        pending_context = ""  # image/text from a parent card with no options, to prepend to child MCQs
        skipped_info = []  # [(card_index, reason), ...] -- surfaced to the caller for diagnostics
        context_only_count = 0  # parent/passage cards with no options -- expected to produce 0 results, not a gap
        for _ci, card in enumerate(chorcha_cards, 1):
            q_div = card.find('div', class_=lambda x: x and 'font-medium' in x)
            if not q_div:
                skipped_info.append((_ci, "no question-text div found in this card"))
                continue
            q_text_raw = format_content(q_div, img_map)
            q_num_match = re.match(r'^\s*([0-9০-৯]+(?:\.[0-9০-৯]+)?)\s*[\.\)\-ঃ:]', q_text_raw)
            q_text = re.sub(r'^\s*[0-9০-৯]+(?:\.[0-9০-৯]+)?\s*[\.\)\-ঃ:]\s*', '', q_text_raw)
            if not q_text.strip() and not q_text_raw.strip():
                skipped_info.append((_ci, "question text was completely empty"))
                continue

            nested_cards = card.find_all('div', class_=lambda x: x and 'rounded-xl' in x and ('p-5' in x or 'pb-6' in x))
            own_buttons = [
                btn for btn in card.find_all('button', class_=lambda x: x and 'p-2' in x)
                if not any(btn in nc.find_all('button') for nc in nested_cards)
            ]
            has_options_probe = bool(own_buttons)
            is_nested = bool(q_num_match and '.' in q_num_match.group(1))

            if not has_options_probe:
                # Parent/context-only card (image or passage, no MCQ here) --
                # this card is EXPECTED to produce zero results; its content
                # gets folded into whichever nested child question(s) follow.
                # Tracked separately (not as a "skip") so total_cards_seen
                # accounting stays accurate: total_cards_seen == len(results)
                # + context_only_count + len(skipped_info), always.
                context_only_count += 1
                # A NEW top-level (non-nested) number resets any pending
                # context from an earlier, unrelated group. Strip the
                # parent's own leading number ("1. ") -- it belongs to the
                # parent, not to the child MCQs that will use this context.
                q_text_context = re.sub(r'^\s*[0-9০-৯]+(?:\.[0-9০-৯]+)?\s*[\.\)\-ঃ:]\s*', '', q_text_raw).strip()
                if not is_nested:
                    pending_context = q_text_context
                elif pending_context:
                    pending_context = (pending_context + "\n" + q_text_context).strip()
                continue

            if is_nested and pending_context:
                q_text = (pending_context + "\n" + q_text).strip()
            elif not is_nested:
                pending_context = ""  # own top-level question stands alone; clear stale context
            if not q_text.strip():
                skipped_info.append((_ci, "has options but question text ended up empty after context-merge"))
                continue

            options, ans_idx = [], "1"
            for i, btn in enumerate(own_buttons, 1):
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

            # chorcha.net-এ দুই ধরনের ব্যাখ্যা section থাকতে পারে: সাধারণ
            # "ব্যাখ্যা" (pre-rendered hidden div, class="whitespace-pre-line")
            # এবং "AI ব্যাখ্যা" (lazy -- button click করলে DOM-এ content
            # আসে)। AI-এর populated content wrapper সবসময় একই
            # "whitespace-pre-line" class ব্যবহার নাও করতে পারে (React
            # lazy-render আলাদা markup দিতে পারে), তাই primary পাস তা মিস
            # করলে সেই section-এর নিজস্ব টেক্সট (button-এর নিজের label বাদ
            # দিয়ে) fallback হিসেবে নেওয়া হয় -- এতে AI ব্যাখ্যা কখনো silently
            # blank থেকে যায় না।
            exp_parts = []
            matched_sections = set()
            for exp_div in card.find_all('div', class_=lambda x: x and 'whitespace-pre-line' in x):
                t = format_content(exp_div, img_map)
                if t.strip():
                    exp_parts.append(t.strip())
                    parent_section = exp_div.find_parent('section')
                    if parent_section is not None:
                        matched_sections.add(id(parent_section))

            for sec in card.find_all('section'):
                if id(sec) in matched_sections:
                    continue  # already got its content via whitespace-pre-line above
                btns = sec.find_all('button')
                if not btns:
                    continue
                btn_label = btns[0].get_text(strip=True)
                if 'AI' not in btn_label and 'ব্যাখ্যা' not in btn_label:
                    continue  # not an explanation section at all
                full_text = format_content(sec, img_map)
                # Subtract EVERY button's own rendered text (not just the
                # first) -- the section can have the top toggle button,
                # a thumbs up/down feedback control, AND a separate
                # "AI Explanation" source-link button below the answer
                # text, all of which are UI controls, not explanation
                # content, and must not leak into the remainder.
                for b in btns:
                    b_text = format_content(b, img_map)
                    if b_text:
                        full_text = full_text.replace(b_text, '', 1)
                remainder = full_text.strip()
                if remainder and remainder not in ('ব্যাখ্যা', 'AI ব্যাখ্যা'):
                    exp_parts.append(remainder)

            exp_text = "\n\n".join(exp_parts)

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
        return {
            "source": "Chorcha.net",
            "results": results,
            "total_cards_seen": _total_cards,
            "skipped": skipped_info,
            "context_only_count": context_only_count,
        }

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
