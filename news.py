"""
News tab data for the scanner page.

Sources (free, official first):
  NSE corporate announcements (mainboard + SME), board meetings, corporate actions,
  SAST (Reg 29) and insider (PIT) disclosures, bulk / block deals, results calendar;
  BSE corporate announcements RSS (used for BSE-only stocks; NSE already has the rest);
  Google News RSS per stock (headline, source, time, link only).

Runs every 30 minutes on market days (.github/workflows/news.yml). Every source is optional:
if one fails it is skipped and the rest still update.

Writes data/news/:
  YYYY-MM-DD.json   one file per IST day, newest item first, 30 days kept
  index.json        updated time, source status, days available, upcoming results, today's news per stock
  khaas.json        "Aaj ki khaas khabar" for the daily email (Apps Script reads it)
  state.json        Google News rotation (which stocks were searched when)
Item = [id, time "YYYY-MM-DDTHH:MM" IST, stock key, symbol, categories, headline, link, source, extra]
Categories: R results, O order/contract, D dividend/bonus/split, B board meeting, F fund raise,
            P promoter/insider, K bulk/block deal, M media news.
"""
import csv, hashlib, io, json, os, re, sys, time
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.environ.get("NEWS_DIR") or os.path.join(DATA, "news")
KEEP_DAYS = 30
MEDIA_MAX = int(os.environ.get("NEWS_MEDIA_MAX", "45"))   # Google News searches per run
FORCE = os.environ.get("FORCE") == "1"
NOW = datetime.now()                                       # the workflow sets TZ=Asia/Kolkata
TODAY = NOW.date()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
NSE_REF = {"Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements"}
status = {}


def log(*a):
    print(*a, flush=True)


def nse_prime():
    for u in ("https://www.nseindia.com/", "https://www.nseindia.com/companies-listing/corporate-filings-announcements"):
        try:
            S.get(u, timeout=20)
        except requests.RequestException:
            pass


def get(url, headers=None, tries=3, kind="json"):
    """GET with retries. Returns parsed JSON / text, or None."""
    for i in range(tries):
        try:
            r = S.get(url, headers=headers, timeout=40)
            if r.status_code == 200 and r.content:
                if kind == "json":
                    return r.json()
                return r.text
            if r.status_code in (401, 403) and "nseindia" in url:
                nse_prime()
            if r.status_code == 404:
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2 * (i + 1))
    return None


def mark(src, ok, n=0, note=""):
    status[src] = ["ok" if ok else "fail", n, NOW.strftime("%H:%M"), note]
    log(f"  {src}: {'ok' if ok else 'FAILED'} {n} {note}")


def dmy(d):
    return d.strftime("%d-%m-%Y")


def iso_t(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse_dt(s, fmts=("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y, %H-%M", "%d-%m-%Y %H:%M:%S",
                      "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y")):
    s = (s or "").strip()
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def hid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def nkey(sym):
    return "N_" + re.sub(r"[^A-Za-z0-9_-]", "_", sym)


def clean(t, n=240):
    t = re.sub(r"\s+", " ", (t or "")).strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


# ---------------------------------------------------------------- stocks on the page (scores, names, lists)

def load_stocks():
    """key -> {sym, name, score, chg}, from the scanner's daily data/scan_full.csv."""
    out, bse_only = {}, {}
    path = os.path.join(DATA, "scan_full.csv")
    if not os.path.exists(path):
        return out, bse_only
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["Exchange"] == "BSE":
                k = "B_" + re.sub(r"[^A-Za-z0-9_-]", "_", r["BSE_Code"] or r["Symbol"])
                bse_only[r["BSE_Code"]] = k
            else:
                k = nkey(r["Symbol"])
            try:
                sc = int(r.get("Momentum_Score") or 0)
            except ValueError:
                sc = 0
            out[k] = {"sym": r["Symbol"], "name": r["Name"], "score": sc,
                      "chg": float(r["Chg_1D"]) if r.get("Chg_1D") not in ("", None) else None}
    return out, bse_only


def load_lists():
    try:
        return json.load(open(os.path.join(DATA, "fixed_lists.json"), encoding="utf-8")).get("lists", {})
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- categories

DROP_DESC = re.compile(r"newspaper|investor meet|con\. call|investor presentation|monitoring agency|statement of deviation"
                       r"|trading window|loss of share|duplicate share|certificate under|esop|esos|esps|credit rating"
                       r"|change in (director|management|auditor|company secretary)|appointment|resignation|cessation"
                       r"|retirement|book closure|reg\. 74", re.I)
RX = {
    "B_notice": re.compile(r"(meeting of the board|board meeting|board of directors).{0,120}(scheduled|to be held|will be held|is to be held|"
                           r"proposed to be held|intimation)|to consider and approve|prior intimation", re.I),
    "R": re.compile(r"financial results?|un-?audited results|audited results|quarterly results|results? for the (quarter|half|year|period)", re.I),
    "O": re.compile(r"\b(bagging|receiv\w*|bag(ged)?|secur\w*|won|win\w*|award\w*|bags)\b.{0,80}\b(orders?|contracts?|work orders?|letter of award|"
                    r"loa|letter of intent|loi|purchase orders?|tenders?)\b|\b(orders?|contracts?)\b.{0,40}\b(worth|valued|aggregating|of rs|of inr|of ₹)", re.I),
    "D": re.compile(r"\bdividend\b|\bbonus\b|sub-?division|stock split|split of (the )?(equity )?shares|face value split", re.I),
    "F": re.compile(r"\bqip\b|qualified institutions? placement|preferential (issue|allotment|basis)|rights issue|fund ?-?rais|raising of funds|"
                    r"raise funds|issue of (equity shares|securities|warrants|convertible)|\bwarrants\b|non-convertible debentures?|\bncds?\b|"
                    r"private placement|\bfccbs?\b", re.I),
    "P": re.compile(r"takeover regulations|\bsast\b|regulation 29|regulation 31|reg\.? ?29|insider trading|regulation 7\s*\(2\)|"
                    r"promoter.{0,60}(acqui|purchas|bought|sold|sale of|sell|pledge|invocation|release)|open offer|encumbrance|pledge", re.I),
}


def categorize(desc, text):
    d, t = (desc or "").strip(), (text or "").strip()
    if d and DROP_DESC.search(d):
        return ""
    both = d + " · " + t
    c = ""
    notice = bool(re.search(r"board meeting intimation|prior intimation", d, re.I)) or (
        "outcome" not in d.lower() and RX["B_notice"].search(t) and not re.search(r"outcome|approved|has approved|submitted", t, re.I))
    if notice:
        return "B"
    if RX["R"].search(both) and not re.search(r"newspaper|publication", both, re.I):
        c += "R"
    if re.search(r"orders?/contracts", d, re.I) or RX["O"].search(both):
        c += "O"
    if RX["D"].search(both) and not re.search(r"dividend distribution policy|unclaimed dividend|iepf|tax deduct|\btds\b|withholding tax", both, re.I):
        c += "D"
    if re.search(r"rights issue|issue of securities|fund raising|qip", d, re.I) or RX["F"].search(both):
        c += "F"
    if re.search(r"takeover|insider|open offer|pledge|encumbrance", d, re.I) or RX["P"].search(both):
        c += "P"
    return c


BOILER = re.compile(r"^.{0,160}?\b(has|have)\s+(informed|submitted to|intimated)\s+(the\s+)?(exchange|bse)\s*,?\s*(about|that|regarding|of)?\s*", re.I)


def headline(desc, text):
    t = BOILER.sub("", clean(text, 400)).strip()
    t = re.sub(r"^(the|a|an)\s+", "", t, flags=re.I).strip(" '\"")
    if not t:
        return clean(desc)
    t = t[0].upper() + t[1:]
    if desc and desc.lower()[:20] not in t.lower():
        t = f"{desc}: {t}"
    return clean(t, 240)


CR = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?|million|mn|billion|bn)\b", re.I)


def crores(text):
    """Largest rupee amount in the text, in crores (orders: 'Rs 250 crore')."""
    best = None
    for m in CR.finditer(text or ""):
        v = float(m.group(1).replace(",", ""))
        u = m.group(2).lower()
        v = v if u.startswith("cr") else v / 100 if u.startswith("la") else v / 10 if u in ("million", "mn") else v * 100
        best = v if best is None else max(best, v)
    return round(best, 1) if best is not None else None


# ---------------------------------------------------------------- sources

def nse_announcements(days):
    """NSE filings (mainboard + SME) for the given IST dates."""
    items, ok_any, fails = [], False, 0
    for idx in ("equities", "sme"):
        for d in days:
            data = get(f"https://www.nseindia.com/api/corporate-announcements?index={idx}&from_date={dmy(d)}&to_date={dmy(d)}", NSE_REF)
            if not isinstance(data, list):
                fails += 1
                continue
            ok_any = True
            for a in data:
                sym, desc, text = a.get("symbol") or "", a.get("desc") or "", a.get("attchmntText") or ""
                cats = categorize(desc, text)
                dt = parse_dt(a.get("an_dt")) or parse_dt(a.get("sort_date"))
                if not cats or not sym or not dt:
                    continue
                url = a.get("attchmntFile") or "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
                f = {}
                if "O" in cats and crores(text) is not None:
                    f["cr"] = crores(text)
                items.append([hid("nse", a.get("seq_id") or (sym + desc + text)), iso_t(dt), nkey(sym), sym, cats,
                              headline(desc, text), url, "NSE" + (" SME" if idx == "sme" else ""), f or 0])
            time.sleep(0.4)
    mark("nse_filings", ok_any, len(items), f"{fails} requests failed" if fails else "")
    return items


def bse_announcements(bse_only):
    """BSE announcements RSS (latest ~2 days). Only BSE-only stocks; NSE-listed companies file the same on NSE."""
    txt = get("https://www.bseindia.com/data/xml/announcements.xml", {"Referer": "https://www.bseindia.com/"}, kind="text")
    if not txt:
        mark("bse_filings", False)
        return [], []
    try:
        root = ET.fromstring(txt.encode("utf-8"))
    except ET.ParseError:
        mark("bse_filings", False, note="bad xml")
        return [], []
    items, cal = [], []
    for it in root.iter("item"):
        code = (it.findtext("scripcode") or "").strip()
        k = bse_only.get(code)
        if not k:
            continue
        title, text = it.findtext("title") or "", it.findtext("description") or ""
        dt = parse_dt(it.findtext("pubDate"))
        name = re.sub(r"\s*\(\d+\)\s*$", "", title).strip()
        text = re.sub(r"^" + re.escape(name) + r"\s*", "", text.strip()).strip()   # "XYZ Ltdhas informed BSE …"
        cats = categorize("", text)
        if not cats or not dt:
            continue
        if "B" in cats and re.search(r"result", text, re.I):
            m = re.search(r"scheduled on (\d{2}/\d{2}/\d{4})", text)
            md = parse_dt(m.group(1)) if m else None
            if md:
                cal.append([md.date().isoformat(), k, name[:40], "Financial Results"])
        f = {"cr": crores(text)} if "O" in cats and crores(text) is not None else 0
        items.append([hid("bse", it.findtext("link"), code, text[:80]), iso_t(dt), k, code, cats,
                      headline("", "has informed BSE that " + text if not text.lower().startswith("has") else text),
                      it.findtext("link") or "https://www.bseindia.com/corporates/ann.html", "BSE", f])
    mark("bse_filings", True, len(items))
    return items, cal


def nse_board_meetings():
    """Board meeting intimations (category B) and the results calendar (next 7 days)."""
    items, cal, ok = [], [], False
    for idx in ("equities", "sme"):
        data = get(f"https://www.nseindia.com/api/corporate-board-meetings?index={idx}&from_date={dmy(TODAY - timedelta(days=2))}"
                   f"&to_date={dmy(TODAY + timedelta(days=21))}", NSE_REF)
        if not isinstance(data, list):
            continue
        ok = True
        for b in data:
            sym, md, ts = b.get("bm_symbol") or "", parse_dt(b.get("bm_date")), parse_dt(b.get("bm_timestamp"))
            if not sym or not md:
                continue
            purpose, desc = b.get("bm_purpose") or "", b.get("bm_desc") or ""
            if re.search(r"result", purpose + " " + desc, re.I) and TODAY <= md.date() <= TODAY + timedelta(days=7):
                cal.append([md.date().isoformat(), nkey(sym), sym, "Financial Results"])
            if not ts or purpose.lower().startswith("board meeting intimation"):
                continue    # the same meeting also comes with its real purpose
            what = purpose if purpose and purpose.lower() not in ("others", "other") else clean(desc, 120)
            items.append([hid("bm", sym, b.get("bm_date"), purpose), iso_t(ts), nkey(sym), sym, "B",
                          f"Board meeting {md:%d %b}: {clean(what, 160)}",
                          b.get("ixbrl") or b.get("attachment") or "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
                          "NSE", 0])
        time.sleep(0.4)
    ev = get("https://www.nseindia.com/api/event-calendar?index=equities", NSE_REF)
    if isinstance(ev, list):
        ok = True
        for e in ev:
            d = parse_dt(e.get("date"))
            if d and e.get("symbol") and re.search(r"result", e.get("purpose") or "", re.I) and TODAY <= d.date() <= TODAY + timedelta(days=7):
                cal.append([d.date().isoformat(), nkey(e["symbol"]), e["symbol"], "Financial Results"])
    mark("nse_board_meetings", ok, len(items), f"{len(cal)} results dates")
    return items, cal


def nse_corporate_actions():
    """Upcoming dividend / bonus / split ex-dates (first time we see them)."""
    items, ok = [], False
    for idx in ("equities", "sme"):
        data = get(f"https://www.nseindia.com/api/corporates-corporateActions?index={idx}&from_date={dmy(TODAY)}"
                   f"&to_date={dmy(TODAY + timedelta(days=30))}", NSE_REF)
        if not isinstance(data, list):
            continue
        ok = True
        for a in data:
            sym, subj = a.get("symbol") or "", a.get("subject") or ""
            if not sym or not RX["D"].search(subj):
                continue
            items.append([hid("ca", sym, a.get("exDate"), subj), iso_t(NOW), nkey(sym), sym, "D",
                          f"Ex-date {a.get('exDate')}: {clean(subj, 160)}" + (f" (record date {a['recDate']})" if a.get("recDate") not in (None, "-", a.get("exDate")) else ""),
                          "https://www.nseindia.com/companies-listing/corporate-filings-actions", "NSE", 0])
        time.sleep(0.4)
    mark("nse_corp_actions", ok, len(items))
    return items


def nse_insider():
    """SAST Reg 29 (promoter / large holder buy-sell) and PIT (insider trades)."""
    items, ok = [], False
    data = get("https://www.nseindia.com/api/corporate-sast-reg29?index=equities", NSE_REF)
    if isinstance(data, dict):
        ok = True
        for a in data.get("data") or []:
            sym, ts = a.get("symbol") or "", parse_dt(a.get("timestamp")) or parse_dt(a.get("sysTime"))
            if not sym or not ts:
                continue
            buy = (a.get("acqSaleType") or "").lower().startswith("acq")
            prom = (a.get("promoterType") or "").upper() == "Y"
            who = clean(a.get("acquirerName"), 60)
            n = a.get("noOfShareAcq") if buy else a.get("noOfShareSale")
            after = a.get("totAftShare")
            items.append([hid("sast", a.get("application_no") or (sym + who + str(ts))), iso_t(ts), nkey(sym), sym, "P",
                          f"{'Promoter' if prom else 'Holder'} {'acquisition' if buy else 'sale'} ({a.get('regType') or 'SAST'}): {who}"
                          + (f" · {int(float(n)):,} shares" if n not in (None, "", "-") and re.match(r"^[\d.]+$", str(n)) else "")
                          + (f" · {a.get('acquisitionMode')}" if a.get("acquisitionMode") else "")
                          + (f" · holding now {after}%" if after not in (None, "", "-") else ""),
                          a.get("attachement") or "https://www.nseindia.com/companies-listing/corporate-filings-regulation-29",
                          "NSE", {"pb": 1} if prom and buy else 0])
    pit = get(f"https://www.nseindia.com/api/corporates-pit?index=equities&from_date={dmy(TODAY - timedelta(days=1))}&to_date={dmy(TODAY)}",
              {"Referer": "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"})
    if isinstance(pit, dict):
        ok = True
        for a in pit.get("data") or []:
            sym = a.get("symbol") or ""
            ts = parse_dt(a.get("date")) or parse_dt(a.get("intimDt")) or NOW
            cat = a.get("personCategory") or ""
            typ = (a.get("tdpTransactionType") or a.get("acqMode") or "").strip()
            buy = typ.lower().startswith(("buy", "acq", "market purchase"))
            prom = "promoter" in cat.lower()
            val = a.get("secVal")
            items.append([hid("pit", sym, a.get("acqName"), a.get("secAcq"), a.get("date"), typ), iso_t(ts), nkey(sym), sym, "P",
                          f"Insider {typ or 'trade'}: {clean(a.get('acqName'), 60)} ({clean(cat, 30)})"
                          + (f" · {int(float(a['secAcq'])):,} shares" if re.match(r"^[\d.]+$", str(a.get("secAcq") or "")) else "")
                          + (f" · ₹{float(val) / 1e7:,.2f} Cr" if re.match(r"^[\d.]+$", str(val or "")) else ""),
                          a.get("xbrl") or "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading",
                          "NSE", {"pb": 1} if prom and buy else 0])
    mark("nse_insider", ok, len(items))
    return items


def lakh(q):
    q = float(q)
    return f"{q / 1e7:,.2f} Cr" if q >= 1e7 else f"{q / 1e5:,.1f} L" if q >= 1e5 else f"{int(q):,}"


def group_deals(rows, kind):
    """rows: (date, sym, client, side, qty, price). One item per stock per day per kind."""
    by = {}
    for d, sym, client, side, qty, price in rows:
        try:
            qty, price = float(qty), float(price)
        except (TypeError, ValueError):
            continue
        by.setdefault((d, sym), []).append((client, side.upper()[:1], qty, price))
    items = []
    label = "Bulk deal" if kind == "bulk" else "Block deal"
    for (d, sym), deals in by.items():
        buy = sum(q * p for _, s, q, p in deals if s == "B") / 1e7
        sell = sum(q * p for _, s, q, p in deals if s == "S") / 1e7
        top = sorted(deals, key=lambda x: -x[2] * x[3])[:3]
        who = "; ".join(f"{clean(c, 40)} {'BUY' if s == 'B' else 'SELL'} {lakh(q)} @ ₹{p:,.2f}" for c, s, q, p in top)
        more = f" +{len(deals) - 3} more" if len(deals) > 3 else ""
        side = " · ".join(x for x in (f"BUY ₹{buy:,.1f} Cr" if buy else "", f"SELL ₹{sell:,.1f} Cr" if sell else "") if x)
        items.append([hid(kind, d.isoformat(), sym), f"{d.isoformat()}T17:00", nkey(sym), sym, "K",
                      f"{label}s: {side} — {who}{more}",
                      "https://www.nseindia.com/report-detail/display-bulk-and-block-deals", "NSE",
                      {"buy": round(buy, 2), "sell": round(sell, 2), "upd": 1}])
    return items


def nse_deals(backfill):
    rows = {"bulk": [], "block": []}
    ok = False
    snap = get("https://www.nseindia.com/api/snapshot-capital-market-largedeal", NSE_REF)
    if isinstance(snap, dict):
        ok = True
        for kind, field in (("bulk", "BULK_DEALS_DATA"), ("block", "BLOCK_DEALS_DATA")):
            for a in snap.get(field) or []:
                d = parse_dt(a.get("date"))
                if d and a.get("symbol"):
                    rows[kind].append((d.date(), a["symbol"], a.get("clientName") or "", a.get("buySell") or "", a.get("qty"), a.get("watp")))
    else:   # the plain CSV files (today's deals only)
        for kind in ("bulk", "block"):
            txt = get(f"https://nsearchives.nseindia.com/content/equities/{kind}.csv", kind="text")
            if not txt:
                continue
            ok = True
            for r in csv.reader(io.StringIO(txt)):
                if len(r) >= 7 and parse_dt(r[0].title()):
                    rows[kind].append((parse_dt(r[0].title()).date(), r[1], r[3], r[4], r[5], r[6]))
    if backfill:
        for kind in ("bulk", "block"):
            for w in range(0, KEEP_DAYS, 7):
                a, b = TODAY - timedelta(days=w + 6), TODAY - timedelta(days=w)
                h = get(f"https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType={kind}_deals"
                        f"&from={dmy(a)}&to={dmy(b)}", NSE_REF)
                for x in (h.get("data") or []) if isinstance(h, dict) else []:
                    d = parse_dt((x.get("BD_DT_DATE") or "").title())
                    if d and x.get("BD_SYMBOL"):
                        rows[kind].append((d.date(), x["BD_SYMBOL"], x.get("BD_CLIENT_NAME") or "", x.get("BD_BUY_SELL") or "",
                                           x.get("BD_QTY_TRD"), x.get("BD_TP_WATP")))
                time.sleep(0.4)
    seen, items = set(), []
    for kind in ("bulk", "block"):
        uniq = []
        for r in rows[kind]:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        items += group_deals(uniq, kind)
    mark("nse_deals", ok, len(items))
    return items


def holiday_today():
    data = get("https://www.nseindia.com/api/holiday-master?type=trading", NSE_REF)
    if not isinstance(data, dict):
        return False
    return any(parse_dt(h.get("tradingDate")) and parse_dt(h.get("tradingDate")).date() == TODAY for h in data.get("CM") or [])


# ---------------------------------------------------------------- Google News

JUNK = re.compile(r"share price (today|live)|stock price (today|live)|live updates|live blog|stocks? to (watch|buy)|"
                  r"top (gainers|losers)|stock market (today|live)|market live|price target today", re.I)


def company_names():
    names = {}
    for url in ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"):
        txt = get(url, kind="text")
        for r in csv.reader(io.StringIO(txt or "")):
            if len(r) > 2 and r[0] != "SYMBOL":
                names[nkey(r[0].strip())] = r[1].strip()
    return names


def short_name(n):
    n = re.sub(r"\s+", " ", n or "").strip()
    n = re.sub(r"\b(limited|ltd\.?|ltd|pvt|private)\b\.?$", "", n, flags=re.I).strip(" .,-")
    n = re.sub(r"\(india\)$", "", n, flags=re.I).strip(" .,-")
    return n


def google_news(keys, names, stocks):
    items, done, fails = [], [], 0
    for k in keys:
        nm = short_name(names.get(k) or (stocks.get(k) or {}).get("name") or "")
        if len(nm) < 3:
            continue
        q = requests.utils.quote(f'"{nm}" when:1d')
        txt = None
        try:
            r = S.get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en", timeout=25)
            if r.status_code == 200:
                txt = r.text
            elif r.status_code in (429, 503):
                fails += 3
        except requests.RequestException:
            pass
        if not txt:
            fails += 1
            if fails >= 4:
                log("  Google News stopped answering — skipping the rest this run")
                break
            continue
        fails = 0
        done.append(k)
        try:
            root = ET.fromstring(txt.encode("utf-8"))
        except ET.ParseError:
            continue
        first = nm.split()[0].lower()
        n = 0
        for it in root.iter("item"):
            title, link, src = it.findtext("title") or "", it.findtext("link") or "", it.findtext("source") or ""
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            if not link or JUNK.search(title) or first not in title.lower() and stocks.get(k, {}).get("sym", "~").lower() not in title.lower():
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone().replace(tzinfo=None)
            except (TypeError, ValueError):
                continue
            if dt < NOW - timedelta(hours=36):
                continue
            items.append([hid("gn", k, title.lower()), iso_t(dt), k, (stocks.get(k) or {}).get("sym") or k[2:], "M",
                          clean(title, 200), link, clean(src, 40) or "Google News", 0])
            n += 1
            if n >= 3:
                break
        time.sleep(0.8)
    mark("google_news", bool(done) or not keys, len(items), f"{len(done)} stocks searched")
    return items, done


def media_keys(state, stocks, lists, filed):
    """Which stocks to search on Google News this run: today's filings first, then a rotation over
    Nifty 500 + F&O + IPO + the 150 best-scored stocks (each searched again after 3 hours at the earliest)."""
    last = state.setdefault("gq", {})
    cutoff = iso_t(NOW - timedelta(hours=3))
    fresh = lambda k: last.get(k, "") < cutoff
    by_score = lambda k: -(stocks.get(k) or {}).get("score", 0)
    pri = sorted({k for k in filed if fresh(k) and k in stocks}, key=by_score)[: MEDIA_MAX // 2]
    uni = set()
    for lid in ("n500", "fo", "ipo"):
        uni |= set((lists.get(lid) or {}).keys())
    uni |= set(sorted(stocks, key=by_score)[:150])
    uni = sorted(uni, key=lambda k: (by_score(k), k))
    if not uni:
        return pri
    cur = state.get("cursor", 0) % len(uni)
    rot = []
    for i in range(len(uni)):
        k = uni[(cur + i) % len(uni)]
        if fresh(k) and k not in pri:
            rot.append(k)
            if len(pri) + len(rot) >= MEDIA_MAX:
                state["cursor"] = (cur + i + 1) % len(uni)
                break
    else:
        state["cursor"] = cur
    return pri + rot


# ---------------------------------------------------------------- store

def day_path(d):
    return os.path.join(OUT, f"{d}.json")


def load_store():
    store = {}
    if os.path.isdir(OUT):
        for fn in os.listdir(OUT):
            if re.match(r"\d{4}-\d{2}-\d{2}\.json$", fn):
                try:
                    for it in json.load(open(os.path.join(OUT, fn), encoding="utf-8"))["items"]:
                        store[it[0]] = it
                except (OSError, ValueError, KeyError):
                    pass
    return store


def khaas(today_items, stocks):
    """Aaj ki khaas khabar: results / big orders / promoter buying / bulk-block buying, for well-scored stocks."""
    out = []
    for it in today_items:
        k, cats, f = it[2], it[4], it[8] or {}
        st = stocks.get(k) or {}
        sc = st.get("score", 0)
        why = ""
        if "P" in cats and f.get("pb"):
            why = "Promoter buying"
        elif "O" in cats and (f.get("cr") or 0) >= 500:
            why = "Bada order"
        elif "O" in cats and sc >= 60:
            why = "Order / contract"
        elif "R" in cats and sc >= 60:
            why = "Results"
        elif "K" in cats and sc >= 60 and (f.get("buy") or 0) >= 1 and f["buy"] > 1.5 * (f.get("sell") or 0):   # not same-day in-and-out trades
            why = "Bulk/Block buying"
        if why:
            out.append({"why": why, "k": k, "sym": it[3], "score": sc, "chg": st.get("chg"), "t": it[1],
                        "title": it[5], "url": it[6], "src": it[7]})
    order = {"Promoter buying": 0, "Bada order": 1, "Results": 2, "Order / contract": 3, "Bulk/Block buying": 4}
    out.sort(key=lambda x: (order[x["why"]], -x["score"], x["t"]))
    seen, res = set(), []
    for x in out:
        if (x["k"], x["why"]) not in seen:
            seen.add((x["k"], x["why"]))
            res.append(x)
    return res[:60]


def main():
    os.makedirs(OUT, exist_ok=True)
    log(f"News update {NOW:%d %b %Y %H:%M}")
    nse_prime()
    if not FORCE and TODAY.weekday() < 5 and holiday_today():
        log("NSE holiday today — nothing to do")
        return
    stocks, bse_only = load_stocks()
    lists = load_lists()
    store = load_store()
    first_run = not store
    try:
        state = json.load(open(os.path.join(OUT, "state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    try:
        old_index = json.load(open(os.path.join(OUT, "index.json"), encoding="utf-8"))
    except (OSError, ValueError):
        old_index = {}

    days = [TODAY - timedelta(days=i) for i in range(KEEP_DAYS if first_run else 2)]
    new = []
    steps = [lambda: nse_announcements(days), lambda: nse_corporate_actions(), lambda: nse_insider(),
             lambda: nse_deals(first_run)]
    for step in steps:
        try:
            new += step()
        except Exception as e:              # one broken source must not stop the others
            log(f"  source error: {e!r}")
    cal = []
    for fn in (lambda: nse_board_meetings(), lambda: bse_announcements(bse_only)):
        try:
            a, c = fn()
            new += a
            cal += c
        except Exception as e:
            log(f"  source error: {e!r}")

    today_iso = TODAY.isoformat()
    filed = {it[2] for it in new if it[1][:10] == today_iso and set(it[4]) & set("ROFPK")}
    try:
        names = company_names()
        mk = media_keys(state, stocks, lists, filed)
        media, done = google_news(mk, names, stocks)
        new += media
        for k in done:
            state["gq"][k] = iso_t(NOW)
    except Exception as e:
        log(f"  Google News error: {e!r}")
        mark("google_news", False, note=repr(e)[:80])

    added = 0
    for it in new:
        old = store.get(it[0])
        if old is None:
            store[it[0]] = it
            added += 1
        elif isinstance(it[8], dict) and it[8].get("upd") and old[5] != it[5]:
            it[1] = old[1]
            store[it[0]] = it               # more deals came in for the same stock and day
    oldest = (TODAY - timedelta(days=KEEP_DAYS - 1)).isoformat()
    by_day = {}
    for it in store.values():
        if oldest <= it[1][:10] <= today_iso:
            by_day.setdefault(it[1][:10], []).append(it)
    for d, its in by_day.items():
        its.sort(key=lambda x: (x[1], x[0]), reverse=True)
        body = json.dumps({"date": d, "items": its}, ensure_ascii=False, separators=(",", ":"))
        p = day_path(d)
        if not os.path.exists(p) or open(p, encoding="utf-8").read() != body:
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
    for fn in os.listdir(OUT):
        if re.match(r"\d{4}-\d{2}-\d{2}\.json$", fn) and fn[:10] not in by_day:
            os.remove(os.path.join(OUT, fn))

    # results calendar: fresh list if a source answered, else the earlier one (only dates still ahead)
    if not cal:
        cal = [c for c in old_index.get("cal", []) if c[0] >= today_iso]
    cal = sorted({(c[0], c[1]): c for c in cal if today_iso <= c[0] <= (TODAY + timedelta(days=7)).isoformat()}.values())
    today_items = by_day.get(today_iso, [])
    per = {}
    for it in today_items:
        e = per.setdefault(it[2], [0, ""])
        e[0] += 1
        e[1] = "".join(sorted(set(e[1] + it[4])))
    for s, v in (old_index.get("status") or {}).items():
        if s not in status:
            status[s] = v
    index = {"updated": iso_t(NOW), "days": sorted(by_day, reverse=True), "counts": {d: len(v) for d, v in by_day.items()},
             "status": status, "cal": cal, "todayDate": today_iso, "today": per}
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    kh = {"date": today_iso, "updated": iso_t(NOW), "final": NOW.hour >= 18,
          "site": "https://dushyantsaini101.github.io/nse-scanner/", "items": khaas(today_items, stocks)}
    with open(os.path.join(OUT, "khaas.json"), "w", encoding="utf-8") as f:
        json.dump(kh, f, ensure_ascii=False, indent=0)
    cut = iso_t(NOW - timedelta(days=2))
    state["gq"] = {k: v for k, v in state.get("gq", {}).items() if v >= cut}
    with open(os.path.join(OUT, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)
    log(f"Done: {added} new items, {len(today_items)} today, {sum(len(v) for v in by_day.values())} in {len(by_day)} days, "
        f"{len(cal)} upcoming results, {len(kh['items'])} khaas")


if __name__ == "__main__":
    main()
