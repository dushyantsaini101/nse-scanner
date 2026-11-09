"""
@dushyantsainibot  -  Telegram desk bot (v2, zero cost)

Kaam:
  * Har ghante (8 AM - 8 PM IST, roz) 11 channels ke NAYE messages padhta hai
  * Faltu cheezein chhodta hai (sticker, poll, greeting, ads, chhote chat)
  * PDF / screenshot ka text free mein nikalta hai (pdftotext + tesseract)
  * Tag lagata hai: Broker report / Sector report / Result / Other
  * Sab kuch Google Sheet ("Telegram" tab) mein bhejta hai (Apps Script telegram_add)
  * Aapke holding wale stock ki nayi post par 1 alert bhejta hai
  * Commands sirf: /status aur /run

Claude API, Zerodha aur Google Drive OAuth ka koi code ab nahi hai.
.env mein chahiye:  BOT_TOKEN, TG_API_ID, TG_API_HASH, WL_URL, WL_KEY
"""
import asyncio
import base64
import csv
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
IST = ZoneInfo("Asia/Kolkata")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TG_API_ID = int(os.getenv("TG_API_ID", "0") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "")
WL_URL = os.getenv("WL_URL") or os.getenv("BASE_URL") or ""
WL_KEY = os.getenv("WL_KEY") or os.getenv("SHEET_KEY") or ""
SCAN_URL = os.getenv(
    "SCAN_URL",
    "https://raw.githubusercontent.com/dushyantsaini101/nse-scanner/main/data/scan_full.csv")

OWNER_FILE = os.path.join(BASE, "owner.txt")
STATE_FILE = os.path.join(BASE, "state.json")
CHATS_FILE = os.path.join(BASE, "chats.json")
SCAN_FILE = os.path.join(BASE, "scan_full.csv")

FIRST_RUN_HOURS = 24          # naye channel par pichhle 24 ghante tak ki posts
MAX_FILE_MB = 10              # isse badi file Drive mein nahi bhejte
MAX_DOWNLOAD_MB = 25          # isse badi file download hi nahi karte
DUP_DAYS = 3                  # ek hi post alag channel mein aaye to 3 din tak dobara nahi
ROW_TEXT_MAX = 1400
HEADLINE_MAX = 200
ALERT_MAX_AGE_H = 12          # purani post par holding alert nahi
BATCH_ROWS = 40               # bina file wali rows ek request mein

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("desk")
logging.getLogger("httpx").setLevel(logging.WARNING)

tg_user = None                # Telethon client (event loop ke andar banta hai)
RUN_LOCK = None               # asyncio.Lock, loop ke andar banta hai


# =====================================================================
#  chhote helpers
# =====================================================================
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def owner_id():
    try:
        return int(open(OWNER_FILE).read().strip())
    except Exception:
        return None


def squash(s):
    return re.sub(r"[ \t ]+", " ", re.sub(r"\r", "", s or "")).strip()


def one_line(s, n):
    return re.sub(r"\s+", " ", s or "").strip()[:n]


# =====================================================================
#  stock list (scanner CSV) - naam / symbol pehchanne ke liye
# =====================================================================
DROP_WORDS = {"LTD", "LIMITED", "PVT", "PRIVATE", "THE", "CO", "COMPANY"}
STRIP_WORDS = {"INDUSTRIES", "INDUSTRY", "INDIA", "CORPORATION", "CORP"}
COMMON_WORDS = {  # aise naam jo aam English shabd bhi hain - akele par match nahi
    "ADVANCE", "UNIVERSAL", "NATIONAL", "GENERAL", "STANDARD", "CENTURY", "PIONEER",
    "GLOBAL", "SUPREME", "EASTERN", "WESTERN", "NORTHERN", "SOUTHERN", "CAPITAL",
    "FINANCE", "ELECTRIC", "SPECTRUM", "PRECISION", "VISION", "PRIME", "ROYAL",
    "SUPER", "SUNRISE", "SILVER", "GOLDEN", "BRIGHT", "FUTURE", "EXCEL", "ANSHUNI",
    "BHARAT", "INDIAN", "INFRA", "POWER", "STEEL", "ENERGY", "FOODS", "TRADING",
    "GROWTH", "PROFIT", "SHARES", "MARKET", "SECTOR", "RESULT", "RESULTS", "REPORT",
    "ENTERPRISE", "ENTERPRISES", "HOLDINGS", "INVESTMENT", "INVESTMENTS", "TECHNOLOGY",
    "TECHNOLOGIES", "SOLUTIONS", "SERVICES", "SYSTEMS", "PRODUCTS", "MATERIALS",
    "CHEMICALS", "PHARMA", "HEALTHCARE", "MEDIA", "TEXTILES", "CEMENT", "MOTORS",
}
THREE_LETTER_OK = {"BEL", "HAL", "TCS", "ITC", "DLF", "PFC", "REC", "IOC", "IEX", "ABB",
                   "UPL", "MRF", "NBCC", "NTPC", "ONGC", "SAIL", "GAIL", "BHEL", "IRFC",
                   "RVNL", "NMDC", "BPCL", "HPCL", "CAMS", "CDSL", "LTIM", "LICI"}
SYMBOL_STOP = {
    "BUY", "SELL", "HOLD", "TARGET", "PRICE", "NIFTY", "SENSEX", "BANK", "EBITDA", "YOY",
    "QOQ", "RESULT", "RESULTS", "STOCK", "STOCKS", "NEWS", "ORDER", "ORDERS", "INDIA",
    "LIMITED", "SECTOR", "REPORT", "UPDATE", "ALERT", "CALL", "CALLS", "BREAKOUT",
    "VOLUME", "STOPLOSS", "PROFIT", "MARKET", "TODAY", "WATCH", "LIVE", "FREE", "JOIN",
    "VIEW", "NOTE", "OPEN", "HIGH", "LOW", "CLOSE", "GLOBAL", "INDEX", "FUTURES",
    "OPTIONS", "SHARE", "SHARES", "TRADE", "TRADING", "DAILY", "WEEKLY", "MONTHLY",
    "QUARTER", "EARNINGS", "GROWTH", "OUTLOOK", "RATING", "ACCUMULATE", "REDUCE",
    "NEUTRAL", "OUTPERFORM", "UNDERPERFORM", "INITIATING", "COVERAGE", "BUYBACK",
    "DIVIDEND", "SPLIT", "BONUS", "RIGHTS", "ISSUE", "LISTING", "GMP", "NSE", "BSE",
    "SEBI", "RBI", "FIIS", "DIIS", "NIFTY50", "USDINR", "CRUDE", "GOLD", "SILVER",
}


def norm(s):
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class StockIndex:
    def __init__(self):
        self.symbols = {}   # token -> output token  (NSE symbol ya "BSE:code")
        self.aliases = {}   # normalised naam -> output token
        self.sym_of = {}    # output token ("BSE:543xxx") -> asli symbol (holding match ke liye)
        self.ok = False

    def load(self, path):
        try:
            rows = list(csv.DictReader(open(path, encoding="utf-8")))
        except Exception as e:
            log.warning("scanner csv nahi padh paya: %s", e)
            return
        seen_isin = set()
        rows.sort(key=lambda r: 0 if r.get("Exchange") == "NSE" else 1)  # NSE pehle
        for r in rows:
            isin = r.get("ISIN") or ""
            sym = (r.get("Symbol") or "").strip().upper()
            if r.get("Exchange") == "BSE":
                if isin and isin in seen_isin:
                    continue
                code = (r.get("BSE_Code") or "").strip()
                out = ("BSE:" + code) if code else sym
            else:
                out = sym
            if not out:
                continue
            if isin:
                seen_isin.add(isin)
            if sym:
                self.symbols.setdefault(sym, out)
                self.sym_of.setdefault(out, sym)
            words = [w for w in norm(r.get("Name")).split() if w not in DROP_WORDS]
            if not words:
                continue
            full = " ".join(words[:4])
            self._add_alias(full, out)
            stripped = [w for w in words if w not in STRIP_WORDS]
            if stripped and len(stripped) != len(words):
                self._add_alias(" ".join(stripped[:4]), out, strict=True)
        self.ok = bool(self.symbols)
        log.info("stock index: %d symbols, %d naam", len(self.symbols), len(self.aliases))

    def _add_alias(self, alias, out, strict=False):
        if len(alias) < 6:
            return
        if " " not in alias and alias in COMMON_WORDS:
            return
        if strict and len(alias) < 8:
            return
        self.aliases.setdefault(alias, out)

    def find(self, text, limit=6):
        """text mein kaun se stocks hain (order of appearance)."""
        if not self.ok or not text:
            return []
        found = []

        def add(tok):
            if tok and tok not in found:
                found.append(tok)

        # 1) hashtag / $tag / NSE:SYMBOL
        for m in re.finditer(r"(?:[#$]|\b(?:NSE|BSE)\s?:\s?)([A-Za-z0-9&\-]{2,20})", text):
            add(self.symbols.get(m.group(1).upper()))
        # 2) CAPS symbol
        for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9&\-]{2,19})(?![A-Za-z0-9])", text):
            t = m.group(1)
            if t in SYMBOL_STOP:
                continue
            if len(t) == 3 and t not in THREE_LETTER_OK:
                continue
            add(self.symbols.get(t))
        # 3) company naam (1-4 shabd)
        toks = norm(text).split()
        i = 0
        while i < len(toks):
            hit = None
            for n in (4, 3, 2, 1):
                if i + n <= len(toks):
                    out = self.aliases.get(" ".join(toks[i:i + n]))
                    if out:
                        hit = (n, out)
                        break
            if hit:
                add(hit[1])
                i += hit[0]
            else:
                i += 1
        return found[:limit]


STOCKS = StockIndex()


def refresh_stock_index():
    """scanner CSV roz ek baar (20 ghante purani ho to) dobara laata hai."""
    try:
        fresh = os.path.exists(SCAN_FILE) and time.time() - os.path.getmtime(SCAN_FILE) < 20 * 3600
        if not fresh:
            r = httpx.get(SCAN_URL, timeout=60, follow_redirects=True)
            r.raise_for_status()
            if len(r.content) > 100000:
                tmp = SCAN_FILE + ".tmp"
                open(tmp, "wb").write(r.content)
                os.replace(tmp, SCAN_FILE)
        if not STOCKS.ok or not fresh:
            fresh_idx = StockIndex()
            fresh_idx.load(SCAN_FILE)
            if fresh_idx.ok:
                STOCKS.symbols, STOCKS.aliases, STOCKS.sym_of, STOCKS.ok = (
                    fresh_idx.symbols, fresh_idx.aliases, fresh_idx.sym_of, True)
    except Exception as e:
        log.warning("scanner csv refresh fail: %s", e)
        if not STOCKS.ok and os.path.exists(SCAN_FILE):
            STOCKS.load(SCAN_FILE)


# =====================================================================
#  text nikalna (free): PDF -> pdftotext, image -> tesseract
# =====================================================================
_OCR_LANG = None


def ocr_lang():
    global _OCR_LANG
    if _OCR_LANG is None:
        try:
            out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                                 text=True, timeout=20).stdout
            _OCR_LANG = "eng+hin" if re.search(r"^hin$", out, re.M) else "eng"
        except Exception:
            _OCR_LANG = ""
    return _OCR_LANG


def ocr_image(path):
    lang = ocr_lang()
    if not lang:
        return ""
    try:
        r = subprocess.run(["tesseract", path, "stdout", "-l", lang, "--psm", "6"],
                           capture_output=True, text=True, errors="replace", timeout=90)
        return clean_extracted(r.stdout)
    except Exception as e:
        log.warning("ocr fail: %s", e)
        return ""


def pdf_text(path):
    try:
        r = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", "4", path, "-"],
                           capture_output=True, text=True, errors="replace", timeout=60)
        txt = clean_extracted(r.stdout)
    except Exception as e:
        log.warning("pdftotext fail: %s", e)
        txt = ""
    if len(txt) < 100:  # scanned PDF - pehla page OCR
        try:
            d = tempfile.mkdtemp(prefix="pdfocr_")
            subprocess.run(["pdftoppm", "-r", "130", "-f", "1", "-l", "1", "-png", path,
                            os.path.join(d, "p")], capture_output=True, timeout=90)
            pngs = sorted(f for f in os.listdir(d) if f.endswith(".png"))
            if pngs:
                txt = ocr_image(os.path.join(d, pngs[0])) or txt
            shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            log.warning("pdf ocr fail: %s", e)
    return txt


def _junk_line(ln):
    """OCR ka kachra: symbols ki bharmar, ya sirf 1-2 akshar ke tukde."""
    alnum = sum(ch.isalnum() for ch in ln)
    if alnum < 3 or alnum / max(len(ln), 1) < 0.55:
        return True
    toks = ln.split()
    if len(toks) >= 4 and sum(len(w) <= 2 for w in toks) / len(toks) > 0.7:
        return True
    return False


def clean_extracted(s):
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (s or "").splitlines()]
    lines = [ln for ln in lines if len(ln) >= 3 and not _junk_line(ln)]
    return "\n".join(lines)


# =====================================================================
#  tag lagana
# =====================================================================
HOUSES = [
    ("Motilal Oswal", r"motilal\s+oswal|\bMOSL\b|\bMOFSL\b"),
    ("ICICI Securities", r"icici\s+sec|icici\s+direct|icici\s+institutional"),
    ("Kotak", r"kotak\s+(?:institutional|securities|equities)|kotak\s+inst"),
    ("Axis Securities", r"axis\s+(?:securities|capital|direct)"),
    ("HDFC Securities", r"hdfc\s+sec"),
    ("Nuvama", r"nuvama"),
    ("Edelweiss", r"edelweiss"),
    ("Emkay", r"emkay"),
    ("JM Financial", r"\bjm\s+financial"),
    ("Jefferies", r"jefferies"),
    ("Goldman Sachs", r"goldman"),
    ("Morgan Stanley", r"morgan\s+stanley"),
    ("CLSA", r"\bclsa\b"),
    ("Citi", r"\bciti(?:group)?\b(?:\s+research)?"),
    ("Nomura", r"nomura"),
    ("UBS", r"\bUBS\b"),
    ("Macquarie", r"macquarie"),
    ("Bernstein", r"bernstein"),
    ("BofA", r"\bbofa\b|bank\s+of\s+america|\bBOFA\s+securities"),
    ("Systematix", r"systematix"),
    ("PL Capital", r"prabhudas|\bPL\s+capital"),
    ("Sharekhan", r"sharekhan"),
    ("Anand Rathi", r"anand\s+rathi"),
    ("Antique", r"antique\s+stock"),
    ("Ambit", r"\bambit\b"),
    ("Elara", r"elara"),
    ("Equirus", r"equirus"),
    ("Dolat", r"dolat"),
    ("SBI Securities", r"sbi\s*(?:cap|securities)|sbicap"),
    ("Centrum", r"centrum"),
    ("Choice", r"choice\s+(?:equity|broking|institutional)"),
    ("Geojit", r"geojit"),
    ("Arihant", r"arihant\s+capital"),
    ("InCred", r"incred"),
    ("Nirmal Bang", r"nirmal\s+bang"),
    ("YES Securities", r"yes\s+securities"),
    ("BOB Capital", r"bob\s+cap"),
    ("Ventura", r"ventura\s+sec"),
    ("Prabhudas Lilladher", r"lilladher"),
    ("Phillip Capital", r"phillip\s*capital"),
    ("IDBI Capital", r"idbi\s+capital"),
    ("Asian Markets", r"asian\s+markets\s+sec|\bamsec\b"),
    ("Monarch", r"monarch\s+networth"),
    ("Religare", r"religare"),
    ("IIFL", r"\biifl\b"),
    ("Investec", r"investec"),
    ("Dam Capital", r"dam\s+capital"),
]
HOUSE_RES = [(n, re.compile(p, re.I)) for n, p in HOUSES]

RATING_RE = re.compile(
    r"\b(strong\s+buy|buy|accumulate|add|reduce|sell|hold|neutral|outperform|underperform|"
    r"overweight|underweight|trim|reiterate\s+buy)\b", re.I)
TARGET_RE = re.compile(
    r"(?:target\s*price|price\s*target|\bTP\b|\bPT\b|target|\btgt\b)\s*(?:of|at|to|:|-|=)?\s*"
    r"(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)
SECTOR_RE = re.compile(
    r"sector\s+(?:update|report|outlook|view|note|strategy|thematic)|industry\s+(?:report|update|outlook)|"
    r"sectoral|channel\s+check|monthly\s+update|thematic|\bQ[1-4]\s?FY\s?\d{2}\s+(?:preview|review)|"
    r"sector\s+(?:top\s+picks|picks)", re.I)
RESULT_PATTERNS = [
    re.compile(r"\bq[1-4]\s?-?fy\s?'?\d{2}\b", re.I), re.compile(r"\bpat\b", re.I),
    re.compile(r"net\s+profit", re.I), re.compile(r"\bebitda\b", re.I),
    re.compile(r"\byo\s?y\b|y-o-y", re.I), re.compile(r"\brevenue\b|net\s+sales", re.I),
    re.compile(r"\bresults?\b", re.I), re.compile(r"\bmargins?\b", re.I),
    re.compile(r"\bqoq\b|q-o-q", re.I),
]
GREETING_RE = re.compile(
    r"^\W*(good\s*(morning|evening|night|afternoon)|gm\b|gn\b|jai\s+(shri|shree|sri)|"
    r"happy\s+\w+|shubh|namaste|ram\s+ram|thank|thanks|welcome|ok\b|okay|yes\b|no\b|hi\b|hello|"
    r"sab\s+ko|subh\s|नमस्|शुभ|सुप्रभात|धन्यवाद)", re.I)
PROMO_RE = re.compile(
    r"(join|subscribe|follow|register|offer|discount|demat|open\s+account|premium|paid|"
    r"whatsapp|contact|call\s+us|dm\s+me|link\s+in\s+bio|t\.me/\+|telegram\.me/)", re.I)


def find_house(text):
    for name, rx in HOUSE_RES:
        if rx.search(text):
            return name
    return ""


def find_rating(text):
    m = RATING_RE.search(text)
    if not m:
        return ""
    r = re.sub(r"\s+", " ", m.group(1)).title()
    return "Buy" if r.lower() == "reiterate buy" else r


def find_target(text):
    for m in TARGET_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 1 <= v <= 1000000:
            return int(v) if v == int(v) else round(v, 2)
    return None


def result_hits(text):
    return sum(1 for rx in RESULT_PATTERNS if rx.search(text))


SINGLE_CO_RE = re.compile(
    r"initiat|company\s+update|result\s+(?:review|update)|results?\s+update|earnings\s+update|"
    r"conference\s+call|concall|management\s+meet|q[1-4]\s?-?fy\s?'?\d{2}\s+(?:result|update)", re.I)
RECO_RE = re.compile(r"\breco\b|\brecommendation\b|\bcmp\b|target\s*price|\btgt\b|\bupside\b", re.I)
TIP_RE = re.compile(r"intraday|buy\s+above|sell\s+below|\bsl\b|stop\s*loss|\bbtst\b|swing\s+call|trade\s+call|\bcalls?\b", re.I)


def classify(text, fname, stocks, house, rating, target, is_pdf):
    sector = bool(SECTOR_RE.search(text)) or "sector" in (fname or "").lower()
    single_company = bool(SINGLE_CO_RE.search(text))
    if sector and (len(stocks) != 1 or not single_company):
        return "Sector report"
    if house and (rating or target):
        return "Broker report"
    reco_words = {m.group(0).lower() for m in RECO_RE.finditer(text)}
    if len(stocks) <= 1 and target and (len(reco_words) >= 2 or (rating and not TIP_RE.search(text))):
        return "Broker report"          # "Reco / Target Price / CMP" post, broker ka naam na ho tab bhi
    if house and is_pdf:
        return "Broker report"
    if sector and not (single_company and len(stocks) == 1):
        return "Sector report"
    if result_hits(text) >= 3:
        return "Result"
    return "Other"


def _post_hash(row):
    import hashlib
    base = re.sub(r"[^a-z0-9]+", " ", ((row.get("headline") or "") + " " + (row.get("text") or "")[:300]).lower()).strip()
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


def tg_link(entity, msg_id):
    uname = getattr(entity, "username", None)
    if uname:
        return "https://t.me/%s/%d" % (uname, msg_id)
    cid = str(getattr(entity, "id", "")).replace("-100", "", 1)
    return "https://t.me/c/%s/%d" % (cid, msg_id)


def key_lines(text):
    """target / rating / CMP wali lines aage rakhte hain taaki 1400 char mein aa jayen."""
    keep = []
    for ln in text.splitlines():
        if re.search(r"target|\bTP\b|\bCMP\b|upside|rating|recommend|\bbuy\b|\bsell\b|"
                     r"accumulate|\bPAT\b|ebitda|revenue", ln, re.I):
            keep.append(ln.strip())
        if len(keep) >= 8:
            break
    return "\n".join(keep)


DATE_LINE_RE = re.compile(
    r"^\W*(?:(?:date|dated|time|posted|as\s+on)\s*[:\-]\s*)?"
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?"
    r"(?:\d{1,2}(?:st|nd|rd|th)?[\s/\-.]+(?:\d{1,2}|[a-z]{3,9})[\s/\-.,']*\d{2,4}"
    r"|[a-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4})"
    r"\W*(?:\d{1,2}[:.]\d{2}\s*(?:am|pm)?)?\W*$", re.I)


def pick_headline(msg_text, extracted, fname):
    for src in (msg_text, extracted):
        for ln in (src or "").splitlines():
            ln = re.sub(r"[*_`#>]+", "", ln).strip()
            if DATE_LINE_RE.match(ln) or re.fullmatch(r"[\d\W]+", ln):
                continue                    # sirf date / numbers wali line headline nahi banti
            if len(ln) >= 12 and not re.match(r"^(https?://|www\.)", ln):
                return one_line(ln, HEADLINE_MAX)
    if fname:
        return one_line(os.path.splitext(fname)[0].replace("_", " "), HEADLINE_MAX)
    return one_line(msg_text, HEADLINE_MAX)


def is_junk_message(m, text):
    """Download se pehle ka sasta filter. True = chhod do."""
    if getattr(m, "action", None):
        return "service"
    if getattr(m, "sticker", None) or getattr(m, "poll", None) or getattr(m, "voice", None) \
            or getattr(m, "video_note", None) or getattr(m, "gif", None) or getattr(m, "contact", None):
        return "sticker/poll/voice"
    has_media = bool(getattr(m, "photo", None) or getattr(m, "document", None)
                     or getattr(m, "video", None))
    if getattr(m, "video", None) and not getattr(m, "photo", None) and not _is_pdf_or_img(m):
        return "video"
    if not text and not has_media:
        return "empty"
    if not has_media:
        if len(text) < 40:
            return "tiny"
        if len(text) < 150 and GREETING_RE.search(text):
            return "greeting"
        if len(text) < 500 and PROMO_RE.search(text) and not find_house(text) \
                and not find_target(text) and result_hits(text) < 2:
            return "promo"
    return ""


def _is_pdf_or_img(m):
    f = getattr(m, "file", None)
    mime = (getattr(f, "mime_type", "") or "").lower()
    name = (getattr(f, "name", "") or "").lower()
    return mime == "application/pdf" or name.endswith(".pdf") or mime.startswith("image/")


# =====================================================================
#  ek message ko row banana
# =====================================================================
async def build_row(m, entity, chan_name, holdings, tmpdir):
    """Returns (row, skip_reason). row=None to skip."""
    text = squash(getattr(m, "message", "") or "")
    why = is_junk_message(m, text)
    if why:
        return None, why

    f = getattr(m, "file", None)
    mime = (getattr(f, "mime_type", "") or "").lower() if f else ""
    fname = (getattr(f, "name", "") or "") if f else ""
    size = int(getattr(f, "size", 0) or 0) if f else 0
    is_pdf = bool(f) and (mime == "application/pdf" or fname.lower().endswith(".pdf"))
    is_img = bool(getattr(m, "photo", None)) or (bool(f) and mime.startswith("image/"))

    extracted, local_path, upload_name, upload_mime = "", None, "", ""
    if (is_pdf or is_img) and size <= MAX_DOWNLOAD_MB * 1024 * 1024:
        try:
            ext = ".pdf" if is_pdf else (os.path.splitext(fname)[1] or ".jpg")
            path = os.path.join(tmpdir, "m%d%s" % (m.id, ext))
            got = await m.download_media(file=path)
            if got:
                local_path = got
                if is_pdf:
                    extracted = await asyncio.to_thread(pdf_text, local_path)
                    upload_mime = "application/pdf"
                else:
                    extracted = await asyncio.to_thread(ocr_image, local_path)
                    low = local_path.lower()
                    upload_mime = ("image/png" if low.endswith(".png") else
                                   "image/webp" if low.endswith(".webp") else
                                   "image/gif" if low.endswith(".gif") else "image/jpeg")
                upload_name = fname or ("%s_%d%s" % (re.sub(r"\W+", "_", chan_name)[:30], m.id, ext))
        except Exception as e:
            log.warning("download fail %s/%s: %s", chan_name, m.id, e)

    # photo/screenshot jisme na caption, na padhne layak text
    if is_img and not text and len(extracted) < 40:
        return None, "image bina text"
    if not text and not extracted and not local_path:
        return None, "empty"

    blob = "\n".join(x for x in (text, fname, extracted) if x)
    head_blob = blob[:2500]
    stocks = STOCKS.find(head_blob)
    if not stocks:                       # shuru mein naam nahi: poore text mein, sirf 1-2 stock mile to
        wide = STOCKS.find(blob[:8000], limit=6)
        if 1 <= len(wide) <= 2:
            stocks = wide
    house = find_house(blob[:4000])
    rating = find_rating(head_blob)
    target = find_target(head_blob) if len(stocks) <= 1 else None
    typ = classify(blob[:4000], fname, stocks, house, rating, target, is_pdf)

    if typ == "Other" and len(blob) < 120 and not stocks and not local_path:
        return None, "chhota/kaam ka nahi"
    if typ == "Other" and not stocks and len(blob) < 200:
        return None, "chhota/kaam ka nahi"

    holding = bool(stocks) and any(
        s in holdings or STOCKS.sym_of.get(s, s.replace("BSE:", "")) in holdings for s in stocks)
    highlight = (typ in ("Sector report", "Result")
                 or (typ == "Broker report" and bool(target))) or holding

    body = text
    if extracted:
        kl = key_lines(extracted)
        body = "\n".join(x for x in (text, kl, extracted) if x)
    body = body[:ROW_TEXT_MAX]

    when = m.date.astimezone(IST) if m.date.tzinfo else m.date.replace(tzinfo=dt.timezone.utc).astimezone(IST)
    row = {
        "time": when.strftime("%Y-%m-%d %H:%M"),
        "channel": chan_name[:100],
        "type": typ,
        "stocks": stocks,
        "house": house,
        "rating": rating,
        "headline": pick_headline(text, extracted, fname),
        "text": body,
        "link": tg_link(entity, m.id),
        "highlight": highlight,
        "holding": holding,
    }
    if target:
        row["target"] = target

    # file sirf kaam ki posts ke saath (Other ki file Drive mein nahi)
    if local_path and typ != "Other" and os.path.getsize(local_path) <= MAX_FILE_MB * 1024 * 1024:
        with open(local_path, "rb") as fh:
            row["file"] = {"name": upload_name, "mime": upload_mime,
                           "data": base64.b64encode(fh.read()).decode("ascii")}
    row["_age_h"] = (dt.datetime.now(dt.timezone.utc) - m.date.astimezone(dt.timezone.utc)).total_seconds() / 3600
    return row, ""


# =====================================================================
#  Google Sheet (Apps Script) se baat
# =====================================================================
def _post(rows):
    body = json.dumps({"key": WL_KEY, "action": "telegram_add", "rows": rows}, ensure_ascii=False)
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(WL_URL, content=body.encode("utf-8"),
                           headers={"Content-Type": "text/plain;charset=utf-8"},
                           timeout=180, follow_redirects=True)
            data = r.json()
            return data
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("Sheet se connect nahi hua: %s" % last)


def send_rows(rows):
    """rows -> har row ka status list: added / duplicate / error / failed"""
    statuses = [None] * len(rows)
    groups = []
    plain = [i for i, r in enumerate(rows) if "file" not in r]
    for k in range(0, len(plain), BATCH_ROWS):
        groups.append(plain[k:k + BATCH_ROWS])
    for i, r in enumerate(rows):
        if "file" in r:
            groups.append([i])
    for g in groups:
        payload = [{k: v for k, v in rows[i].items() if not k.startswith("_")} for i in g]
        try:
            res = _post(payload)
        except Exception as e:
            log.error("%s", e)
            for i in g:
                statuses[i] = "failed"
            continue
        if not res.get("ok"):
            log.error("Sheet ne request mana ki: %s", res.get("error"))
            for i in g:
                statuses[i] = "failed"
            continue
        results = res.get("results") or []
        for j, i in enumerate(g):
            st = (results[j] if j < len(results) else {}).get("status", "error")
            statuses[i] = st if st in ("added", "duplicate") else "error"
            if st == "error":
                log.warning("row error: %s", (results[j] if j < len(results) else {}).get("error"))
    return statuses


def fetch_holdings():
    r = httpx.get(WL_URL, params={"key": WL_KEY, "action": "holdings"}, timeout=60,
                  follow_redirects=True)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("error", "holdings nahi mili"))
    return {str(h["symbol"]).upper() for h in d.get("holdings", []) if h.get("symbol")}


# =====================================================================
#  collect: ek poora round
# =====================================================================
async def collect(bot=None, manual=False):
    """Sab channels ke naye messages -> Sheet. Summary dict return karta hai."""
    global RUN_LOCK
    if RUN_LOCK is None:
        RUN_LOCK = asyncio.Lock()
    if RUN_LOCK.locked():
        return {"busy": True}
    async with RUN_LOCK:
        return await _collect_inner(bot, manual)


async def _collect_inner(bot, manual):
    t0 = time.time()
    state = load_json(STATE_FILE, {})
    last_ids = state.setdefault("last_ids", {})
    alerted = state.setdefault("alerted", [])
    now_ts = time.time()
    seen = state.setdefault("seen", {})
    for h in [h for h, ts in seen.items() if now_ts - ts > DUP_DAYS * 86400]:
        del seen[h]
    chats = load_json(CHATS_FILE, [])
    summary = {"chats": len(chats), "fetched": 0, "skipped": 0, "sent": 0, "added": 0,
               "duplicate": 0, "errors": 0, "failed": 0, "alerts": 0, "problems": [],
               "skip_reasons": {}, "manual": manual}
    if not chats:
        summary["problems"].append("chats.json khaali hai (setup_chats.py chalaiye)")
    if not (WL_URL and WL_KEY):
        summary["problems"].append("WL_URL / WL_KEY .env mein nahi hain")
        return _finish(state, summary, t0)

    await asyncio.to_thread(refresh_stock_index)
    try:
        holdings = await asyncio.to_thread(fetch_holdings)
        state["holdings"] = sorted(holdings)
    except Exception as e:
        holdings = set(state.get("holdings", []))
        summary["problems"].append("holdings nahi aayi (purani cache use ki): %s" % e)

    tmpdir = tempfile.mkdtemp(prefix="deskbot_")
    try:
        for c in chats:
            cid = str(c["id"])
            name = c.get("name") or cid
            try:
                entity = await tg_user.get_entity(c["id"])
                msgs = await _new_messages(c["id"], last_ids.get(cid))
            except Exception as e:
                summary["problems"].append("%s: padh nahi paya (%s)" % (name, one_line(str(e), 80)))
                continue
            if not msgs:
                continue
            summary["fetched"] += len(msgs)
            max_id = max(m.id for m in msgs)
            rows = []
            for m in msgs:
                try:
                    row, why = await build_row(m, entity, name, holdings, tmpdir)
                except Exception as e:
                    log.exception("message %s/%s fail", name, m.id)
                    row, why = None, "error"
                    summary["problems"].append("%s #%s: %s" % (name, m.id, one_line(str(e), 60)))
                if row is None:
                    summary["skipped"] += 1
                    summary["skip_reasons"][why] = summary["skip_reasons"].get(why, 0) + 1
                else:
                    rows.append(row)
                for fn in os.listdir(tmpdir):  # disk saaf
                    try:
                        os.remove(os.path.join(tmpdir, fn))
                    except OSError:
                        pass
            if rows:
                kept, batch_seen = [], set()
                for r in rows:
                    h = _post_hash(r)
                    if h in batch_seen or (h in seen and now_ts - seen[h] < DUP_DAYS * 86400):
                        summary["skipped"] += 1
                        summary["skip_reasons"]["duplicate post"] = summary["skip_reasons"].get("duplicate post", 0) + 1
                        continue
                    batch_seen.add(h)
                    kept.append(r)
                rows = kept
            if rows:
                statuses = await asyncio.to_thread(send_rows, rows)
                if "failed" in statuses:
                    summary["failed"] += statuses.count("failed")
                    summary["problems"].append("%s: Sheet tak nahi pahunch paya, agle round mein dobara" % name)
                    continue  # last id aage nahi badhate -> retry (duplicate safe)
                summary["sent"] += len(rows)
                for r in rows:               # sirf Sheet tak pahunchne ke baad "dekha hua" mano
                    seen[_post_hash(r)] = now_ts
                for row, st in zip(rows, statuses):
                    if st == "added":
                        summary["added"] += 1
                    elif st == "duplicate":
                        summary["duplicate"] += 1
                    else:
                        summary["errors"] += 1
                    if st == "added" and row.get("holding") and row["link"] not in alerted \
                            and row.get("_age_h", 0) <= ALERT_MAX_AGE_H:
                        if await _send_alert(bot, row):
                            alerted.append(row["link"])
                            summary["alerts"] += 1
            last_ids[cid] = max_id
            state["alerted"] = alerted[-600:]
            save_json(STATE_FILE, state)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return _finish(state, summary, t0)


def _finish(state, summary, t0):
    summary["seconds"] = round(time.time() - t0, 1)
    summary["at"] = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    state["last_run"] = summary
    save_json(STATE_FILE, state)
    log.info("round done: %s", {k: v for k, v in summary.items() if k != "problems"})
    return summary


async def _new_messages(chat_id, last_id):
    msgs = []
    if last_id:
        async for m in tg_user.iter_messages(chat_id, min_id=int(last_id), limit=300, reverse=True):
            msgs.append(m)
        return msgs
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=FIRST_RUN_HOURS)
    async for m in tg_user.iter_messages(chat_id, limit=120):
        if m.date < since:
            break
        msgs.append(m)
    msgs.reverse()
    return msgs


async def _send_alert(bot, row):
    oid = owner_id()
    if not (bot and oid):
        return False
    stocks = ", ".join(row.get("stocks") or [])
    txt = "🔔 %s: %s\n%s\n%s" % (stocks or "Holding", row["headline"], row["channel"], row["link"])
    try:
        await bot.send_message(oid, txt[:3900], disable_web_page_preview=True)
        return True
    except Exception as e:
        log.warning("alert nahi gaya: %s", e)
        return False


# =====================================================================
#  bot: sirf /status aur /run
# =====================================================================
def summary_text(s):
    if s.get("busy"):
        return "⏳ Ek round pehle se chal raha hai. Thodi der baad /status dekhiye."
    lines = ["%s | %s sec" % (s.get("at", "?"), s.get("seconds", "?")),
             "Channels: %s | naye messages: %s | chhode: %s" % (s.get("chats"), s.get("fetched"), s.get("skipped")),
             "Sheet mein gaye: %s (nayi %s, pehle se %s, error %s)" % (
                 s.get("sent"), s.get("added"), s.get("duplicate"), s.get("errors")),
             "Holding alerts: %s" % s.get("alerts")]
    if s.get("failed"):
        lines.append("⚠️ Sheet tak nahi pahunche: %s" % s["failed"])
    for p in s.get("problems", [])[:6]:
        lines.append("⚠️ " + p)
    return "\n".join(lines)


async def guard(update):
    oid = owner_id()
    if oid is not None and update.effective_user and update.effective_user.id == oid:
        return True
    await update.message.reply_text("Ye ek private bot hai.")
    return False


async def cmd_start(update, ctx):
    if owner_id() is None:
        open(OWNER_FILE, "w").write(str(update.effective_user.id))
        await update.message.reply_text("✅ Aap is bot ke owner set ho gaye.")
    if await guard(update):
        await update.message.reply_text(
            "Commands:\n/status – bot ka haal\n/run – abhi ek round chalao\n\n"
            "Bot roz 8 AM–8 PM har ghante khud chalta hai.")


async def cmd_status(update, ctx):
    if not await guard(update):
        return
    state = load_json(STATE_FILE, {})
    s = state.get("last_run")
    checks = []
    checks.append("pdftotext: " + ("✅" if shutil.which("pdftotext") else "❌ (apt install poppler-utils)"))
    checks.append("tesseract: " + ("✅" if shutil.which("tesseract") else "❌ (apt install tesseract-ocr)"))
    checks.append("Sheet: " + ("✅ key mili" if (WL_URL and WL_KEY) else "❌ WL_URL/WL_KEY nahi"))
    checks.append("Stock list: %s naam" % len(STOCKS.aliases) if STOCKS.ok else "Stock list: ❌")
    checks.append("Holdings cache: %s stock" % len(state.get("holdings", [])))
    txt = "📟 Bot chal raha hai.\n" + "\n".join(checks)
    txt += "\n\nPichhla round:\n" + (summary_text(s) if s else "abhi tak koi round nahi hua.")
    nxt = _next_run_text()
    await update.message.reply_text(txt + "\n\nAgla round: " + nxt)


def _next_run_text():
    now = dt.datetime.now(IST)
    cand = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while not (8 <= cand.hour <= 20):
        cand += dt.timedelta(hours=1)
    return cand.strftime("%H:%M")


async def cmd_run(update, ctx):
    if not await guard(update):
        return
    note = await update.message.reply_text("⏳ Round chal raha hai (channels padh raha hoon)...")
    try:
        s = await collect(ctx.bot, manual=True)
        txt = "✅ Round poora\n" + summary_text(s)
    except Exception as e:
        log.exception("manual run fail")
        txt = "❌ Dikkat aayi: %s" % e
    try:
        await note.delete()
    except Exception:
        pass
    await ctx.bot.send_message(update.effective_chat.id, txt[:3900])


async def job_hourly(ctx):
    try:
        await collect(ctx.bot)
    except Exception:
        log.exception("hourly round fail")


async def post_init(app):
    global tg_user, RUN_LOCK
    from telethon import TelegramClient
    RUN_LOCK = asyncio.Lock()
    tg_user = TelegramClient(os.path.join(BASE, "user"), TG_API_ID, TG_API_HASH)
    await tg_user.connect()
    if not await tg_user.is_user_authorized():
        log.error("Telegram user login nahi hai - pehle setup_chats.py chalaiye.")
    else:
        await tg_user.get_dialogs()
    await asyncio.to_thread(refresh_stock_index)
    for h in range(8, 21):   # 8:00 ... 20:00 IST, roz
        app.job_queue.run_daily(job_hourly, dt.time(h, 0, tzinfo=IST), name="hourly_%02d" % h)
    log.info("Bot chal raha hai (v2).")


def main():
    from telegram.ext import Application, CommandHandler
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.run_polling()


if __name__ == "__main__":
    main()
