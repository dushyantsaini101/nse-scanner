#!/usr/bin/env python3
"""
Indian Stock Scanner — NSE + BSE (Mainboard and SME)
----------------------------------------------------
Downloads the official end-of-day bhavcopy files from NSE and BSE for the last
~1 year, plus NSE index constituent lists, and builds an interactive scanner
page with:

  * % change for 1 Day / 1 Week / 1 Month / 3 Months / 6 Months / 1 Year
    (prices adjusted for splits and bonuses)
  * Universe: NSE Mainboard, NSE SME, BSE SME, BSE-only stocks, Nifty indices
  * Setups: Darvas box breakout, in Darvas box near top, new 52-week high,
    near 52-week high, volume spike, IPO base breakout
  * Sort by any column, sector / price / value / volume filters, CSV export

Usage:
    pip install requests
    python nse_scanner.py            # latest available trading day
    python nse_scanner.py 2026-10-21 # as of a specific date

The first run downloads ~1 year of files (5-10 minutes); later runs only fetch
the new day. Downloads are cached in ./nse_cache.
"""

import bisect, csv, gzip, io, json, os, re, sys, time, zipfile, webbrowser
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("Please install requests first:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("NSE_CACHE") or os.path.join(HERE, "nse_cache")
OUT_DIR = os.environ.get("OUT_DIR")          # set by the GitHub workflow
OUT = os.path.join(OUT_DIR, "index.html") if OUT_DIR else os.path.join(HERE, "scanner.html")
DATA_DIR = os.path.join(HERE, "data") if OUT_DIR else HERE
IN_CI = bool(os.environ.get("CI"))
os.makedirs(CACHE, exist_ok=True)
if OUT_DIR:
    os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

HISTORY_DAYS = 375                 # calendar days of history to keep
NSE_MAIN = {"EQ", "BE", "BZ"}
NSE_SME = {"SM", "ST"}
BSE_SME = {"M", "MT", "MS"}
BSE_SKIP_SERIES = {"IF", "G"}            # REITs / InvITs, government securities
DEBT_NAME = re.compile(r"\d%|BONDS?\b|\bNCDS?\b|TAX ?FREE|-PVT\b|-ZC-|DEBENTURE", re.I)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
NSE_HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
               "Referer": "https://www.nseindia.com/"}
BSE_HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
               "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}

PERIODS = [  # key, label, calendar days back
    ("1D", "1 Day", None),
    ("1W", "1 Week", 7),
    ("1M", "1 Month", 30),
    ("3M", "3 Months", 91),
    ("6M", "6 Months", 182),
    ("1Y", "1 Year", 365),
]

TRADING_BACK = [2, 3, 4, 5, 10, 21, 63]   # sessions back for the page's "s.tb" closes (keep in sync with TPER in the page)
INDICES = [  # label, NSE file name
    ("Nifty 50", "ind_nifty50list.csv"),
    ("Nifty Next 50", "ind_niftynext50list.csv"),
    ("Nifty 100", "ind_nifty100list.csv"),
    ("Nifty 200", "ind_nifty200list.csv"),
    ("Nifty 500", "ind_nifty500list.csv"),
    ("Nifty Midcap 150", "ind_niftymidcap150list.csv"),
    ("Nifty Smallcap 250", "ind_niftysmallcap250list.csv"),
    ("Nifty Microcap 250", "ind_niftymicrocap250_list.csv"),
    ("Nifty Bank", "ind_niftybanklist.csv"),
    ("Nifty Financial Services", "ind_niftyfinancelist.csv"),
    ("Nifty IT", "ind_niftyitlist.csv"),
    ("Nifty Pharma", "ind_niftypharmalist.csv"),
    ("Nifty Healthcare", "ind_niftyhealthcarelist.csv"),
    ("Nifty Auto", "ind_niftyautolist.csv"),
    ("Nifty FMCG", "ind_niftyfmcglist.csv"),
    ("Nifty Metal", "ind_niftymetallist.csv"),
    ("Nifty Realty", "ind_niftyrealtylist.csv"),
    ("Nifty Energy", "ind_niftyenergylist.csv"),
    ("Nifty PSU Bank", "ind_niftypsubanklist.csv"),
    ("Nifty Media", "ind_niftymedialist.csv"),
    ("Nifty CPSE", "ind_niftycpselist.csv"),
    ("Nifty Infrastructure", "ind_niftyinfralist.csv"),
    ("Nifty Consumer Durables", "ind_niftyconsumerdurableslist.csv"),
    ("Nifty Oil & Gas", "ind_niftyoilgaslist.csv"),
    ("Nifty India Defence", "ind_niftyindiadefence_list.csv"),
]

session = requests.Session()
session.headers.update(NSE_HEADERS)
_primed = set()


def prime_cookies(home):
    if home not in _primed:
        try:
            session.get(home, timeout=15)
        except Exception:
            pass
        _primed.add(home)


def fetch(url, headers=None, tries=3):
    """GET with retries. Returns (bytes or None, status) — status: ok / missing / error."""
    for i in range(tries):
        try:
            r = session.get(url, timeout=30, headers=headers)
            if r.status_code == 200 and r.content:
                return r.content, "ok"
            if r.status_code == 404:
                return None, "missing"
            if r.status_code in (401, 403):
                prime_cookies("https://www.bseindia.com/" if "bseindia" in url
                              else "https://www.nseindia.com/")
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None, "error"


# ---------------------------------------------------------------- bhavcopies

def nse_url(d):
    return ("https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")


def bse_url(d):
    return ("https://www.bseindia.com/download/BhavCopy/Equity/"
            f"BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV")


last_status = {}


def get_raw(ex, d):
    """Return the raw CSV bytes of exchange `ex` ('nse'/'bse') for date d, or None."""
    gz = os.path.join(CACHE, f"{ex}_{d:%Y%m%d}.csv.gz")
    miss = os.path.join(CACHE, f"{ex}_{d:%Y%m%d}.missing")
    if os.path.exists(gz):
        with gzip.open(gz, "rb") as f:
            return f.read()
    if ex == "nse":  # files cached by the earlier version of this script
        legacy = os.path.join(CACHE, f"bhav_{d:%Y%m%d}.csv")
        if os.path.exists(legacy):
            with open(legacy, "rb") as f:
                return f.read()
        if os.path.exists(legacy + ".missing") and d < date.today():
            return None
    if os.path.exists(miss) and d < date.today():
        return None
    if d.weekday() >= 5:
        return None

    if ex == "nse":
        blob, st = fetch(nse_url(d))
        raw = None
        if blob:
            try:
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    raw = z.read(z.namelist()[0])
            except zipfile.BadZipFile:
                st = "error"
    else:
        blob, st = fetch(bse_url(d), headers=BSE_HEADERS)
        raw = blob
        if blob and (b"TradDt" not in blob[:300] or b"<html" in blob[:300].lower()):
            raw, st = None, "error"   # an error page, not the file
    time.sleep(0.25)
    last_status[ex] = st
    if raw:
        with gzip.open(gz, "wb") as f:
            f.write(raw)
        return raw
    if st == "missing" and d < date.today():
        open(miss, "w").close()
    return None


def num(x):
    try:
        return float(str(x).strip().replace(",", ""))
    except ValueError:
        return None


def parse(raw, ex):
    """Return {key: row} for equity rows. NSE keys are symbols, BSE keys are 'B:<scrip code>'."""
    text = raw.decode("utf-8-sig", errors="ignore")
    rows = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items() if k}
        series = r.get("SctySrs", "")
        isin = r.get("ISIN", "")
        if ex == "nse":
            if series not in NSE_MAIN and series not in NSE_SME:
                continue
            if isin and not isin.startswith("INE"):      # ETFs and funds (INF…)
                continue
            key = sym = r.get("TckrSymb", "")
        else:
            if r.get("FinInstrmTp", "STK") not in ("STK", "") or not isin.startswith("INE"):
                continue
            if series in BSE_SKIP_SERIES or DEBT_NAME.search(r.get("FinInstrmNm", "")):
                continue
            code = r.get("FinInstrmId", "") or r.get("TckrSymb", "")
            key, sym = "B:" + code, r.get("TckrSymb", "") or code
        close = num(r.get("ClsPric"))
        if not key or not close:
            continue
        rows[key] = {
            "sym": sym, "code": r.get("FinInstrmId", ""), "name": r.get("FinInstrmNm", ""),
            "series": series, "isin": isin,
            "o": num(r.get("OpnPric")) or close, "h": num(r.get("HghPric")) or close,
            "l": num(r.get("LwPric")) or close, "c": close, "prev": num(r.get("PrvsClsgPric")),
            "vol": num(r.get("TtlTradgVol")) or 0, "val": num(r.get("TtlTrfVal")) or 0,
        }
    return rows


# ---------------------------------------------------------------- indices

def load_index(fname):
    cache_file = os.path.join(CACHE, fname)
    fresh = os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) < 7 * 86400
    if fresh:
        with open(cache_file, "rb") as f:
            raw = f.read()
    else:
        raw = (fetch(f"https://nsearchives.nseindia.com/content/indices/{fname}")[0]
               or fetch(f"https://www.niftyindices.com/IndexConstituent/{fname}")[0])
        if not raw:
            if not os.path.exists(cache_file):
                return None
            with open(cache_file, "rb") as f:
                raw = f.read()
        else:
            with open(cache_file, "wb") as f:
                f.write(raw)
    out = {}
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="ignore"))):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("Symbol"):
            out[r["Symbol"]] = r.get("Industry", "")
    return out


def load_fo(nse_symbols):
    """Stocks in NSE's official F&O securities list (fo_mktlots.csv), refreshed daily, cached copy as fallback."""
    cache_file = os.path.join(CACHE, "fo_mktlots.csv")
    fresh = os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) < 20 * 3600
    raw = None
    if not fresh:
        raw, _ = fetch("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv")
        if raw and b"SYMBOL" in raw[:400].upper():
            with open(cache_file, "wb") as f:
                f.write(raw)
        else:
            raw = None
    if raw is None and os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            raw = f.read()
    if not raw:
        return []
    out = []
    for row in csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="ignore"))):
        sym = row[1].strip().upper() if len(row) > 1 else ""
        if sym in nse_symbols and sym not in out:      # skips the header and index rows (NIFTY, BANKNIFTY…)
            out.append(sym)
    return sorted(out)


# ---------------------------------------------------------------- analytics

def build_series(days, keys):
    """days: [(date, rows)] ascending. Returns {key: [[date,o,h,l,c,prev,vol], ...]}."""
    ser = {k: [] for k in keys}
    for d, rows in days:
        for k in keys:
            r = rows.get(k)
            if r:
                ser[k].append([d, r["o"], r["h"], r["l"], r["c"], r["prev"], r["vol"]])
    return ser


# price factors produced by splits, bonuses and both together (e.g. 10:1 split + 1:2 bonus = 1/15)
_SPLITS = [1, 2, 4, 5, 10, 20, 25, 50, 100]
_BONUS = [1, 2, 1.5, 4 / 3, 5 / 4, 3, 5 / 3, 4, 5, 6]
BIG_RATIOS = sorted({1 / (x * y) for x in _SPLITS for y in _BONUS if x * y >= 1.8})
BONUS_RATIOS = [2 / 3, 3 / 4, 3 / 5]
CONSOLIDATION = [float(n) for n in range(2, 11)]


def snap(g, ratios, tol):
    k = min(ratios, key=lambda k: abs(g / k - 1))
    return k if abs(g / k - 1) <= tol else None


def split_factor(last, o, c, prev, consecutive):
    """Detect a split / bonus / consolidation between two trades of a stock. Returns factor or None.
    Exchange circuit limits stop normal stocks falling ~45% overnight, so a drop that size is a
    corporate action; smaller bonus-sized drops must match the ratio closely."""
    if not last or not o:
        return None
    g = o / last
    if prev:  # exchange already adjusted the previous close, and the price confirms it
        fp = prev / last
        if abs(fp - 1) > 0.02 and abs(g / fp - 1) < 0.08:
            return fp
    x = g if g < 1 else min(g, c / last if c else g)
    if x < 0.55 and (consecutive or x < 0.3):
        return snap(x, BIG_RATIOS, 0.06) or x
    if not consecutive:
        return None
    if x < 0.78:
        return snap(x, BONUS_RATIOS, 0.03)
    if g > 1.8:
        return snap(g, CONSOLIDATION, 0.06)
    return None


def adjust(recs, sess_idx):
    """Adjust earlier prices and volumes for splits, bonuses and consolidations.
    Returns the list of (date, factor) adjustments made."""
    n = len(recs)
    factors = [1.0] * n
    events = []
    for j in range(1, n):
        consecutive = sess_idx.get(recs[j][0], -9) - sess_idx.get(recs[j - 1][0], -99) == 1
        f = split_factor(recs[j - 1][4], recs[j][1], recs[j][4], recs[j][5], consecutive)
        if f:
            factors[j] = f
            events.append((recs[j][0], f))
    mult = 1.0
    for j in range(n - 1, -1, -1):
        if mult != 1.0:
            r = recs[j]
            r[1] *= mult; r[2] *= mult; r[3] *= mult; r[4] *= mult; r[6] /= mult
        mult *= factors[j]
    return events


def darvas(H, L, C):
    """Classic Darvas box: a top holds for 3 days, then a bottom holds for 3 days.
    Returns (status, top, bottom, sessions_since_box, sessions_since_box_start) — status: bo / box / form."""
    n = len(H)
    if n < 8:
        return None
    state, top, top_i, cnt = 0, H[0], 0, 0
    bot = bcnt = 0
    box_t = box_b = box_i = box_s = None
    bo_box = None
    for i in range(1, n):
        h, l, c = H[i], L[i], C[i]
        if state == 2:
            if c > box_t:
                if i == n - 1:
                    bo_box = (box_t, box_b, i - box_i, i - box_s)
                state, top, top_i, cnt = 0, h, i, 0
            elif c < box_b:
                state, top, top_i, cnt = 0, h, i, 0
            continue
        if state == 0:
            if h > top:
                top, top_i, cnt = h, i, 0
            else:
                cnt += 1
                if cnt >= 3:
                    seg = L[top_i + 1:i + 1]
                    bot = min(seg)
                    bcnt = i - (top_i + 1 + seg.index(bot))
                    state = 1
                    if bcnt >= 3:
                        state, box_t, box_b, box_i, box_s = 2, top, bot, i, top_i
            continue
        if state == 1:
            if h > top:
                state, top, top_i, cnt = 0, h, i, 0
                continue
            if l < bot:
                bot, bcnt = l, 0
            else:
                bcnt += 1
            if bcnt >= 3:
                state, box_t, box_b, box_i, box_s = 2, top, bot, i, top_i
    if bo_box:
        return ("bo",) + bo_box
    if state == 2:
        return ("box", box_t, box_b, n - 1 - box_i, n - 1 - box_s)
    return ("form", None, None, None, None)


def r2(x):
    return None if x is None else round(x, 2)


def analyse(recs, latest_day, sessions, periods):
    """Compute changes and setups from one stock's adjusted daily records (last = today)."""
    dates = [r[0] for r in recs]
    today = recs[-1]
    c = today[4]
    out = {"ch": {}}
    out["ch"]["1D"] = r2((c / today[5] - 1) * 100) if today[5] else None
    for key, _, days in periods:
        if not days:
            continue
        target = latest_day - timedelta(days=days)
        i = bisect.bisect_right(dates, target) - 1
        if i >= 0 and dates[0] <= target and recs[i][4]:
            out["ch"][key] = r2((c / recs[i][4] - 1) * 100)
        else:
            out["ch"][key] = None
    # closes N trading sessions back (Watchlist "change period" dropdown: 2/3/4 din, 1-2 hafte, 1/3 mahine)
    out["tb"] = []
    for n in TRADING_BACK:
        i = bisect.bisect_right(dates, sessions[-1 - n]) - 1 if len(sessions) > n else -1
        out["tb"].append(round(recs[i][4], 2) if i >= 0 and recs[i][4] else None)

    # 52-week high
    start52 = latest_day - timedelta(days=365)
    j52 = bisect.bisect_left(dates, start52)
    highs = [r[2] for r in recs[j52:]]
    h52 = max(highs)
    prior_high = max(highs[:-1]) if len(highs) > 1 else None
    full_year = dates[0] <= start52 + timedelta(days=10)
    out["h52"] = r2(h52)
    out["f52"] = r2((c / h52 - 1) * 100) if h52 else None
    out["nh"] = bool(full_year and prior_high and today[2] > prior_high)

    # volume vs 50-session average
    vols = [r[6] for r in recs[-51:-1] if r[6]]
    out["vx"] = r2(today[6] / (sum(vols) / len(vols))) if len(vols) >= 10 and today[6] else None
    vols20 = [r[6] for r in recs[-21:-1] if r[6]]
    out["v20"] = r2(today[6] / (sum(vols20) / len(vols20))) if len(vols20) >= 10 and today[6] else None
    closes = [r[4] for r in recs]
    out["d50"] = r2(sum(closes[-50:]) / 50) if len(closes) >= 50 else None
    out["d200"] = r2(sum(closes[-200:]) / 200) if len(closes) >= 200 else None

    # Darvas box on the last year of sessions
    sl = recs[j52:]
    dv = darvas([r[2] for r in sl], [r[3] for r in sl], [r[4] for r in sl])
    out["dv"] = ({"st": dv[0], "t": r2(dv[1]), "b": r2(dv[2]), "d": dv[3], "s": dv[4]}
                 if dv and dv[0] != "form" else ({"st": "form"} if dv else None))

    # recent listing (appeared after the start of history and trades regularly)
    out["ipo"] = None
    out["ib"] = False
    since = len(sessions) - bisect.bisect_left(sessions, dates[0])
    if dates[0] > sessions[0] + timedelta(days=7) and len(recs) >= 0.9 * since:
        age = len(recs)
        out["ipo"] = age
        if age >= 10 and prior_high is not None and c > max(r[2] for r in recs[:-1]):
            out["ib"] = True
    return out


# ---------------------------------------------------------------- main

def main():
    as_of = (datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
             if len(sys.argv) > 1 else date.today())

    # ---- NSE: latest day + 1 year of history
    print("NSE: finding latest trading day…")
    latest_day, latest_raw = None, None
    for k in range(10):
        d = as_of - timedelta(days=k)
        latest_raw = get_raw("nse", d)
        if latest_raw:
            latest_day = d
            break
    if not latest_day:
        sys.exit("Could not download the latest NSE bhavcopy. Check your internet, "
                 "or run after 7 PM IST on a trading day.")
    print(f"  latest: {latest_day:%d %b %Y}")

    print(f"NSE: loading {HISTORY_DAYS} days of history (first run takes a few minutes)…")
    nse_days = []
    d = latest_day
    start = latest_day - timedelta(days=HISTORY_DAYS)
    n_checked = 0
    while d >= start:
        if d.weekday() < 5:
            raw = latest_raw if d == latest_day else get_raw("nse", d)
            if raw:
                nse_days.append((d, parse(raw, "nse")))
            n_checked += 1
            if n_checked % 50 == 0:
                print(f"  …{n_checked} weekdays checked, {len(nse_days)} sessions")
        d -= timedelta(days=1)
    nse_days.reverse()
    sessions = [d for d, _ in nse_days]
    latest = nse_days[-1][1]
    print(f"  {len(sessions)} sessions from {sessions[0]:%d %b %Y}")

    # ---- BSE: only stocks not listed on NSE
    print("BSE: loading…")
    nse_isins = {r["isin"] for r in latest.values() if r["isin"]}
    first_seen = {}
    for d, rows in nse_days:
        for k in rows:
            first_seen.setdefault(k, d)
    late_isin = {latest[k]["isin"]: k for k, d0 in first_seen.items()
                 if k in latest and latest[k]["isin"] and d0 > sessions[0] + timedelta(days=7)}
    late_hist = {}   # NSE key -> {date: BSE row} for stocks that traded on BSE before NSE
    bse_days, bse_status, bse_series = [], "", {}
    bse_code_of = {}   # ISIN -> BSE scrip code, so NSE stocks can be found by their BSE code too
    raw = get_raw("bse", latest_day)
    if not raw:
        bse_status = f"unavailable ({last_status.get('bse', 'not found')})"
        print(f"  BSE file for {latest_day:%d %b %Y} {bse_status} — continuing with NSE only")
    else:
        today_bse = parse(raw, "bse")
        bse_code_of = {r["isin"]: r["code"] for r in today_bse.values() if r["isin"] and r["code"]}
        for r in today_bse.values():
            bse_series[r["series"]] = bse_series.get(r["series"], 0) + 1
        bse_only = {k: r for k, r in today_bse.items() if r["isin"] not in nse_isins}
        errors = 0
        for d in sessions[:-1]:
            raw_d = get_raw("bse", d)
            if raw_d:
                rows = parse(raw_d, "bse")
                bse_days.append((d, {k: rows[k] for k in bse_only if k in rows}))
                for r in rows.values():
                    nk = late_isin.get(r["isin"])
                    if nk and d < first_seen[nk]:
                        late_hist.setdefault(nk, {})[d] = r
                errors = 0
            elif last_status.get("bse") == "error":
                errors += 1
                if errors >= 8:
                    print("  BSE stopped responding — using the history fetched so far")
                    break
        bse_days.append((latest_day, bse_only))
        bse_days.sort(key=lambda x: x[0])
        bse_status = (f"ok ({len(bse_only)} BSE-only stocks, {len(bse_days)} sessions, "
                      f"{len(late_hist)} stocks with earlier BSE history)")
        print(f"  {bse_status}")

    # ---- indices
    print("Index lists…")
    index_members, sector = {}, {}
    for label, fname in INDICES:
        m = load_index(fname)
        if m:
            index_members[label] = sorted(m.keys())
            for s, ind in m.items():
                if ind and s not in sector:
                    sector[s] = ind
    print(f"  {len(index_members)} indices")
    fo_list = load_fo({r["sym"] for r in latest.values()})
    print(f"  F&O stocks: {len(fo_list)}")

    # ---- analytics
    print("Calculating…")
    stocks, adj_log, charts = [], [], {}
    sess_idx = {d: i for i, d in enumerate(sessions)}
    prev_day = sessions[-2] if len(sessions) > 1 else None
    prev_sessions = sessions[:-1]
    groups = [("nse", nse_days, latest)]
    if bse_days:
        groups.append(("bse", bse_days, bse_days[-1][1]))
    for ex, days, today_rows in groups:
        ser = build_series(days, list(today_rows.keys()))
        for key, recs in ser.items():
            if not recs or recs[-1][0] != latest_day:
                continue
            if ex == "nse" and key in late_hist:      # traded on BSE before it listed on NSE
                early = [[d, b["o"], b["h"], b["l"], b["c"], b["prev"], b["vol"]]
                         for d, b in sorted(late_hist[key].items()) if d < recs[0][0]]
                if early:
                    recs[0][5] = None                 # NSE's first "previous close" is not a BSE close
                    recs = early + recs
            r = today_rows[key]
            for d, f in adjust(recs, sess_idx):
                adj_log.append((d, r["sym"], "BSE" if ex == "bse" else "NSE",
                                r["code"] if ex == "bse" else "", round(f, 5)))
            cid = chart_id(ex, r)
            charts[cid] = recs
            a = analyse(recs, latest_day, sessions, PERIODS)
            # the same numbers as of the previous session, for yesterday's momentum rank
            a_prev = (analyse(recs[:-1], prev_day, prev_sessions, PERIODS)
                      if prev_day and len(recs) > 1 and recs[-2][0] == prev_day else None)
            if a_prev:
                a_prev["c"] = recs[-2][4]
            if ex == "nse":
                seg = "NSE SME" if r["series"] in NSE_SME else "NSE"
            else:
                seg = "BSE SME" if r["series"] in BSE_SME else "BSE"
            stocks.append({
                "s": r["sym"], "n": r["name"], "seg": seg, "sr": r["series"], "k": cid, "i": r["isin"],
                "x": "B" if ex == "bse" else "N", "code": r["code"] if ex == "bse" else "",
                "sec": sector.get(r["sym"], "") if ex == "nse" else "",
                "c": r["c"], "v": int(r["vol"]), "tv": round(r["val"] / 1e7, 2),
                **a, "_prev": a_prev,
                **({"bc": bse_code_of[r["isin"]]} if ex == "nse" and r["isin"] in bse_code_of else {}),
            })

    print("Momentum scores…")
    mom_prev, bench = add_momentum(stocks, index_members)
    print(f"  benchmark (Nifty 50 equal-weight) 3M: today {bench[0]}%, previous session {bench[1]}%")

    print("Cap class…")
    add_mcap(stocks, index_members, latest_day)

    print("Fixed lists…")
    fixed, ipo_info = build_fixed(latest_day, sessions, nse_days, bse_days, index_members, fo_list, charts)
    lists, changes = track_changes(fixed, ipo_info, latest_day, nse_days, bse_days)

    counts = {}
    for s in stocks:
        counts[s["seg"]] = counts.get(s["seg"], 0) + 1
    meta = {
        "asOf": latest_day.strftime("%d %b %Y"),
        "generated": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "periodDates": {k: (latest_day - timedelta(days=dd)).strftime("%d %b %Y")
                        for k, _, dd in PERIODS if dd},
        "periods": [[k, l] for k, l, _ in PERIODS],
        "counts": counts,
        "sessions": len(sessions),
        "historyFrom": sessions[0].strftime("%d %b %Y"),
        "bse": bse_status,
        "bseSeries": bse_series,
        "adjustments": len(adj_log),
        "prevAsOf": prev_day.strftime("%d %b %Y") if prev_day else "",
        "bench3M": bench[0],
        "asOfISO": latest_day.isoformat(),
    }

    html = TEMPLATE.replace("/*__DATA__*/", json.dumps(
        {"meta": meta, "stocks": stocks, "indices": index_members, "mprev": mom_prev,
         "lists": lists, "ipo": ipo_info, "chg": changes},
        separators=(",", ":")))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    write_app_files(os.path.dirname(OUT))
    write_charts(os.path.dirname(OUT), charts)
    write_csv(stocks, index_members, meta)
    write_email_summary(stocks, lists, changes, mom_prev, meta)
    with open(os.path.join(DATA_DIR, "adjustments.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Symbol", "Exchange", "BSE_Code", "Price_factor"])
        for row in sorted(adj_log, key=lambda x: (x[0], x[1]), reverse=True):
            w.writerow([row[0].isoformat()] + list(row[1:]))

    print(f"\nDone: {len(stocks):,} stocks as of {meta['asOf']}  {counts}")
    print(f"BSE: {bse_status}")
    print(f"Scanner saved to {OUT}")
    if not IN_CI:
        try:
            webbrowser.open("file://" + OUT)
        except Exception:
            pass


# ---------------------------------------------------------------- momentum score

def _lin(x, lo, hi, pts):
    """0 points at `lo` or below, full points at `hi` or above, straight line in between."""
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * pts


def momentum(a, c, bench3m):
    """Momentum Score 0-100 from one stock's analysed numbers. Returns (score, parts, rs).
    Keep in step with momParts() / MOM_PARTS in the page template."""
    ch = a["ch"]
    dv = a.get("dv") or {}
    if dv.get("st") == "bo":
        pdv = 10
    elif dv.get("st") == "box" and dv.get("t"):
        gap = (dv["t"] - c) / dv["t"] * 100
        pdv = 7 if gap <= 3 else 4 if gap <= 7 else 2
    else:
        pdv = 0
    rs = None if ch.get("3M") is None or bench3m is None else ch["3M"] - bench3m
    up_day = (ch.get("1D") or 0) >= 0          # heavy volume only counts on a green day
    parts = [
        _lin(a.get("f52"), -25, 0, 20),        # closeness to 52-week high
        _lin(ch.get("1W"), -5, 10, 10),        # 1 week %
        _lin(ch.get("1M"), -10, 20, 10),       # 1 month %
        _lin(ch.get("3M"), -15, 40, 15),       # 3 months %
        _lin(a.get("v20"), 0.5, 2.5, 10) if up_day else 0.0,   # volume vs 20-day average
        pdv,                                   # Darvas box
        7.5 if a.get("d50") and c > a["d50"] else 0.0,          # above 50 DMA
        7.5 if a.get("d200") and c > a["d200"] else 0.0,        # above 200 DMA
        _lin(rs, -10, 25, 10),                 # 3M return vs Nifty 50
    ]
    total = sum(parts)
    if a.get("ipo") is not None:               # recent listing: judge only on the parts it has data for
        mx = [20, 10 if ch.get("1W") is not None else 0, 10 if ch.get("1M") is not None else 0,
              15 if ch.get("3M") is not None else 0, 10 if a.get("v20") is not None else 0, 10,
              7.5 if a.get("d50") else 0, 7.5 if a.get("d200") else 0, 10 if rs is not None else 0]
        if sum(mx) < 100:
            total = total / sum(mx) * 100
    return int(total + 0.5), [round(p, 1) for p in parts], r2(rs)   # half-up, same as the page


def add_momentum(stocks, index_members):
    """Adds ms (score) and mi ([vol/20d avg, 50 DMA, 200 DMA]) to every stock; the page rebuilds the parts
    from these with the same formula (momParts). Returns ({key: yesterday's score}, benchmarks).
    Benchmark = equal-weight average 3M return of the Nifty 50 stocks (the bhavcopy has no index values)."""
    n50 = set(index_members.get("Nifty 50", []))

    def bench(get):
        pool = [s for s in stocks if s["x"] == "N" and s["s"] in n50]
        if len(pool) < 25:                     # index list missing: fall back to all NSE mainboard stocks
            pool = [s for s in stocks if s["seg"] == "NSE"]
            vals = sorted(v for v in map(get, pool) if v is not None)
            return r2(vals[len(vals) // 2]) if vals else None
        vals = [v for v in map(get, pool) if v is not None]
        return r2(sum(vals) / len(vals)) if vals else None

    b_now = bench(lambda s: s["ch"].get("3M"))
    b_prev = bench(lambda s: s["_prev"]["ch"].get("3M") if s["_prev"] else None)
    prev = {}
    for s in stocks:
        s["ms"] = momentum(s, s["c"], b_now)[0]
        s["mi"] = [s.pop("v20"), s.pop("d50"), s.pop("d200")]
        p = s.pop("_prev")
        if p:
            prev[s["k"]] = momentum(p, p["c"], b_prev)[0]
    return prev, (b_now, b_prev)


# ---------------------------------------------------------------- fixed lists + change log

FIXED = [("n50", "Nifty 50"), ("n500", "Nifty 500"), ("fo", "F&O"),
         ("nsme", "NSE SME"), ("bsme", "BSE SME"), ("ipo", "IPO"), ("msh", "Mainboard Shift")]
RECENT = 10          # a stock belongs to the SME lists if it traded in the last 10 sessions (SME stocks skip days)
CHANGE_LOG = "list_changes.csv"
LIST_STATE = "fixed_lists.json"


def load_nse_ipos():
    """NSE past issues (mainboard + SME): {symbol: (listing date, issue price)}. Cached copy if NSE refuses."""
    cache_file = os.path.join(CACHE, "nse_past_issues.json")
    data = None
    if not (os.path.exists(cache_file) and time.time() - os.path.getmtime(cache_file) < 20 * 3600):
        raw, _ = fetch("https://www.nseindia.com/api/public-past-issues",
                       headers={**NSE_HEADERS, "Accept": "application/json, text/plain, */*"})
        try:
            data = json.loads(raw) if raw else None
            if isinstance(data, list) and len(data) > 100:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            else:
                data = None
        except ValueError:
            data = None
    if data is None and os.path.exists(cache_file):
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
    if not data:
        return None
    out = {}
    for x in data:
        if str(x.get("securityType", "")).strip() not in ("EQ", "BE", "BZ", "SME"):
            continue
        try:
            ld = datetime.strptime(str(x.get("listingDate", "")).strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        sym = str(x.get("symbol", "")).strip().upper()
        price = num(str(x.get("issuePrice", "")).replace("Rs.", ""))
        if sym and (sym not in out or ld > out[sym][0]):
            out[sym] = (ld, price)
    return out


def build_fixed(latest_day, sessions, nse_days, bse_days, index_members, fo_list, charts):
    """Today's members of the fixed lists: {list: {key: symbol}} (None = source unavailable today),
    and IPO details {key: [listing date, issue price or None, listing-day open]}."""
    latest = nse_days[-1][1]
    nkey = lambda sym: "N_" + re.sub(r"[^A-Za-z0-9_-]", "_", sym)
    fixed = {}
    for lid, label in (("n50", "Nifty 50"), ("n500", "Nifty 500")):
        syms = index_members.get(label)
        fixed[lid] = {nkey(s): s for s in syms if s in latest} if syms else None
    fixed["fo"] = {nkey(s): s for s in fo_list} if fo_list else None
    nsme = {}
    for _, rows in nse_days[-RECENT:]:
        for r in rows.values():
            if r["series"] in NSE_SME:
                nsme[chart_id("nse", r)] = r["sym"]
    fixed["nsme"] = nsme or None
    bsme = {}
    for _, rows in bse_days[-RECENT:]:
        for r in rows.values():
            if r["series"] in BSE_SME:
                bsme[chart_id("bse", r)] = r["sym"]
    fixed["bsme"] = bsme or None

    # IPOs listed in the last 365 days: NSE's official list; BSE-only listings from their first trading day
    cutoff = latest_day - timedelta(days=365)
    recent = set()
    for _, rows in nse_days[-RECENT:]:
        recent.update(chart_id("nse", r) for r in rows.values())
    for _, rows in bse_days[-RECENT:]:
        recent.update(chart_id("bse", r) for r in rows.values())
    first = {}                                # key -> (first trading day, open that day, symbol)
    for ex, days in (("nse", nse_days), ("bse", bse_days)):
        for d, rows in days:
            for r in rows.values():
                k = chart_id(ex, r)
                if k not in first:
                    first[k] = (d, r["o"], r["sym"])
    def split_factor_since_listing(k):
        """Prices on the page are adjusted for splits/bonuses; scale the issue price the same way."""
        f0, recs = first.get(k), charts.get(k)
        if f0 and recs and recs[0][0] == f0[0] and f0[1]:
            return recs[0][1] / f0[1]
        return 1.0

    # SME -> mainboard migrations of the last year: seen on an SME series, now on a mainboard series
    sme_seen, main_since = {}, {}
    for d, rows in nse_days:
        for r in rows.values():
            k = chart_id("nse", r)
            if r["series"] in NSE_SME:
                sme_seen[k] = d
                main_since.pop(k, None)
            elif r["series"] in NSE_MAIN and k in sme_seen and k not in main_since:
                main_since[k] = d
    main_now_k = {chart_id("nse", r) for r in latest.values() if r["series"] in NSE_MAIN}
    fixed["nsme"] = {k: v for k, v in nsme.items() if k not in main_now_k} or None     # migrated stocks leave the SME list at once
    sym_now = {chart_id("nse", r): r["sym"] for r in latest.values()}
    msh = {k: sym_now[k] for k, d0 in main_since.items() if k in main_now_k and d0 > cutoff}
    def open_on(k, day):
        for rec in charts.get(k) or []:
            if rec[0] >= day:
                return rec[1]
        return None
    ipo, info = {}, {}
    for k, d0 in main_since.items():
        if k in msh:
            info[k] = [d0.isoformat(), None, r2(open_on(k, d0))]
    nse_ipos = load_nse_ipos()
    if nse_ipos is None:
        print("  NSE IPO list unavailable — IPO list kept from the previous run")
    else:
        for sym, (ld, price) in nse_ipos.items():
            k = nkey(sym)
            if cutoff < ld <= latest_day and k in recent:
                f1 = first.get(k)
                if k in msh or (f1 and f1[0] < ld - timedelta(days=5) and k in main_now_k):
                    # already traded before this listing date = SME stock that moved to mainboard, not an IPO
                    if k not in msh:
                        msh[k] = sym
                        info[k] = [ld.isoformat(), None, r2(open_on(k, ld))]
                    continue
                ipo[k] = sym
                f0, adj = first.get(k), split_factor_since_listing(k)
                info[k] = [ld.isoformat(), r2(price * adj) if price else None, r2(f0[1] * adj) if f0 else None]
    # BSE-only listings: first trading day inside our history. Only new scrip codes (54xxxx) — old codes
    # appearing late are old companies resuming trade after a suspension, not IPOs.
    for k, (d, o, sym) in first.items():
        if (k.startswith("B_54") and k in recent and d > cutoff and d > sessions[0] + timedelta(days=7)):
            ipo[k] = sym
            info[k] = [d.isoformat(), None, r2(o * split_factor_since_listing(k))]
    fixed["ipo"] = ipo if nse_ipos is not None else None
    fixed["msh"] = msh
    print("  " + ", ".join(f"{lid}: {'—' if v is None else len(v)}" for lid, v in fixed.items()))
    return fixed, info


def track_changes(fixed, ipo_info, latest_day, nse_days, bse_days):
    """Compares today's fixed lists with the previous trading day, appends additions / removals to
    data/list_changes.csv and keeps today's lists in data/fixed_lists.json (both public market facts).
    Returns (lists for the page, changes for the page)."""
    state_path, log_path = os.path.join(DATA_DIR, LIST_STATE), os.path.join(DATA_DIR, CHANGE_LOG)
    iso = latest_day.isoformat()
    state = None
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    # re-running on the same trading day compares with the same earlier base again
    if state and state.get("date") == iso:
        base, base_date, base_miss, base_ipo = (state.get("base"), state.get("base_date"),
                                                state.get("base_miss") or {}, state.get("base_ipo") or {})
    elif state:
        base, base_date, base_miss, base_ipo = (state.get("lists"), state.get("date"),
                                                state.get("miss") or {}, state.get("ipo_dates") or {})
    else:
        base, base_date, base_miss, base_ipo = None, None, {}, {}
    main_now = {chart_id("nse", r) for r in nse_days[-1][1].values() if r["series"] in NSE_MAIN}
    cutoff = (latest_day - timedelta(days=365)).isoformat()
    ipo_dates = {**base_ipo, **{k: v[0] for k, v in ipo_info.items()}}
    today, miss = {}, {}
    for lid, label in FIXED:
        new, old = fixed.get(lid), (base or {}).get(lid)
        if new is None:
            if old is not None:
                print(f"  {label}: source unavailable today, keeping the previous list")
            new = old or {}
        elif old and len(new) < 0.8 * len([k for k in old if k not in (fixed.get("msh") or {}) or lid != "ipo"]):
            print(f"  {label}: only {len(new)} vs {len(old)} yesterday — looks like a data problem, keeping the previous list")
            new = old
        new = dict(new)
        if lid in ("ipo", "nsme"):                 # a stock moved to Mainboard Shift never stays in these lists (even from a kept old list)
            new = {k: v for k, v in new.items() if k not in (fixed.get("msh") or {})}
        if lid in ("nsme", "bsme", "ipo", "msh") and old:
            # illiquid stocks skip days: they leave only after RECENT sessions without a trade,
            # unless the reason is clear (moved to mainboard, IPO older than a year)
            for k in set(old) - set(new):
                if (lid == "nsme" and k in main_now) or (lid in ("ipo", "msh") and ipo_dates.get(k, "9") <= cutoff):
                    continue
                if lid == "ipo" and k in (fixed.get("msh") or {}):       # moved to the Mainboard Shift list
                    continue
                c = (base_miss.get(lid) or {}).get(k, 0) + 1
                if c < RECENT:
                    new[k] = old[k]
                    miss.setdefault(lid, {})[k] = c
        today[lid] = new

    traded_recent = set()
    for _, rows in nse_days[-RECENT:]:
        traded_recent.update(chart_id("nse", r) for r in rows.values())
    for _, rows in bse_days[-RECENT:]:
        traded_recent.update(chart_id("bse", r) for r in rows.values())

    rows = []
    if os.path.exists(log_path):
        with open(log_path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["Date"] != iso]
    if base is not None:
        added = removed = 0
        for lid, label in FIXED:
            old, new = base.get(lid) or {}, today[lid]
            for k in sorted(set(new) - set(old)):
                rows.append({"Date": iso, "List": label, "Change": "added", "Symbol": new[k],
                             "Exchange": "BSE" if k.startswith("B_") else "NSE", "Key": k, "Reason": ""})
                added += 1
            for k in sorted(set(old) - set(new)):
                if lid in ("ipo", "msh") and ipo_dates.get(k, "9") <= cutoff:
                    why = "1 saal pura"
                elif lid == "ipo" and k in today["msh"]:
                    why = "SME se aaya tha - Mainboard Shift list mein"
                elif lid == "nsme" and k in main_now:
                    why = "mainboard pe migrate"
                elif k not in traded_recent or (base_miss.get(lid) or {}).get(k):
                    why = "trading band / delist"
                else:
                    why = ""
                rows.append({"Date": iso, "List": label, "Change": "removed", "Symbol": old[k],
                             "Exchange": "BSE" if k.startswith("B_") else "NSE", "Key": k, "Reason": why})
                removed += 1
        print(f"  changes vs {base_date}: {added} added, {removed} removed")
    else:
        print("  first run: lists saved, changes start from the next trading day")
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Date", "List", "Change", "Symbol", "Exchange", "Key", "Reason"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["Date"], r["List"], r["Change"], r["Symbol"])))
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"date": iso, "lists": today, "miss": miss,
                   "ipo_dates": {k: v for k, v in ipo_dates.items() if k in today["ipo"] or k in today["msh"]},
                   "base_date": base_date, "base": base, "base_miss": base_miss, "base_ipo": base_ipo},
                  f, separators=(",", ":"))

    # for the page: members, today's changes, NEW (added in the last 30 days), every removal (Old list)
    since = (latest_day - timedelta(days=30)).isoformat()
    lab2id = {label: lid for lid, label in FIXED}
    chg = {"today": [], "new": {}, "exits": []}
    for r in rows:
        lid = lab2id.get(r["List"])
        if not lid:
            continue
        if r["Change"] == "removed":
            chg["exits"].append([r["Date"], lid, r["Key"], r["Symbol"], r["Reason"]])
        elif r["Date"] > since:
            chg["new"].setdefault(r["Key"], []).append([lid, r["Date"]])
        if r["Date"] == iso:
            chg["today"].append([lid, "+" if r["Change"] == "added" else "-", r["Key"], r["Symbol"], r["Reason"]])
    for k in list(today["ipo"]) + list(today["msh"]):                    # kept for a few quiet days: still show the listing date
        ipo_info.setdefault(k, [ipo_dates.get(k), None, None])
    lists = {lid: sorted(today[lid]) for lid, _ in FIXED}
    return lists, chg


CAP_INDICES = [("L", "Nifty 100"), ("M", "Nifty Midcap 150"), ("S", "Nifty Smallcap 250"), ("Mi", "Nifty Microcap 250")]
MCAP_ANCHORS = {"RELIANCE": (6e5, 5e6), "TCS": (3e5, 2.5e6)}      # sanity range, Rs Cr


def load_nse_mcap(latest_day):
    """{symbol: market cap in Rs Cr} from NSE's own daily press file (PR<ddmmyy>.zip -> mcap<ddmmyyyy>.csv).
    Tries the latest session and a few days back. Empty dict if missing or if Reliance / TCS look wrong."""
    for back in range(0, 6):
        d = latest_day - timedelta(days=back)
        if d.weekday() > 4:
            continue
        raw, st = fetch(f"https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip", tries=2)
        if not raw:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            name = next(n for n in z.namelist() if n.lower().startswith("mcap"))
            rows = list(csv.reader(io.StringIO(z.read(name).decode("latin-1"))))
        except Exception:
            continue
        head = [h.strip().lower() for h in rows[0]]
        try:
            i_sym, i_ser = head.index("symbol"), head.index("series")
            i_mc = next(i for i, h in enumerate(head) if h.startswith("market cap"))
        except (ValueError, StopIteration):
            continue
        out = {}
        for r in rows[1:]:
            if len(r) <= i_mc:
                continue
            try:
                v = float(r[i_mc].strip()) / 1e7
            except ValueError:
                continue
            if v > 0:
                out[r[i_sym].strip()] = (v, r[i_ser].strip())
        bad = [k for k, (lo, hi) in MCAP_ANCHORS.items() if k in out and not lo <= out[k][0] <= hi]
        if bad or not any(k in out for k in MCAP_ANCHORS):
            print(f"  market cap file rejected ({d}): {'sanity check failed' if bad else 'anchors missing'}")
            return {}
        print(f"  market cap: {len(out)} NSE securities (file of {d:%d-%b-%Y})")
        return out
    print("  market cap file not available")
    return {}


def load_bse_shares():
    """data/bse_shares.csv (refreshed weekly from BSE's site through the owner's browser) -> {BSE code: (shares, close used, as-of)}."""
    out = {}
    try:
        with open(os.path.join(HERE, "data", "bse_shares.csv"), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["BSE_Code"]] = (float(r["Shares"]), float(r["Close_Used"]), r["AsOf"])
    except (OSError, ValueError, KeyError):
        pass
    return out


def add_mcap(stocks, index_members, latest_day):
    """s['mc'] = market cap Rs Cr (NSE's own file). s['mcl'] = L / M / S / Mi by rank among NSE mainboard stocks
    (top 100 / 101-250 / 251-500 / rest). If the file is missing, s['mcl'] falls back to NSE index lists
    (Nifty 100 / Midcap 150 / Smallcap 250 / Microcap 250)."""
    mc = load_nse_mcap(latest_day)
    for s in stocks:
        if s["x"] == "N" and s["s"] in mc:
            v = mc[s["s"]][0]
            s["mc"] = round(v) if v >= 100 else round(v, 1)
    sh, hid = load_bse_shares(), 0
    for s in stocks:      # BSE-only stocks: weekly share count x today's close
        if s["x"] == "B" and s.get("code") in sh and s["c"] > 0:
            n_sh, c0, _ = sh[s["code"]]
            if not 0.55 <= s["c"] / c0 <= 1.8:      # price jumped / halved since the share count was taken: split / bonus? skip until refreshed
                hid += 1
                continue
            v = n_sh * s["c"] / 1e7
            s["mc"] = round(v) if v >= 100 else round(v, 1)
    if sh:
        print(f"  BSE-only market cap: {sum(1 for s in stocks if s['x'] == 'B' and s.get('mc'))} stocks (hidden for price jump: {hid})")
    main = sorted((s for s in stocks if s.get("mc") and s["seg"] == "NSE"), key=lambda s: -s["mc"])
    if main:
        for i, s in enumerate(main):
            s["mcl"] = "L" if i < 100 else "M" if i < 250 else "S" if i < 500 else "Mi"
        print(f"  cap class by market cap rank: {len(main)} stocks")
        return
    sets = [(code, set(index_members.get(label) or [])) for code, label in CAP_INDICES]
    n = 0
    for s in stocks:
        if s["x"] != "N":
            continue
        for code, members in sets:
            if s["s"] in members:
                s["mcl"] = code
                n += 1
                break
    print(f"  cap class by NSE index lists (fallback): {n} stocks")


def chart_id(ex, r):
    """File name (without .json) of a stock's chart data; the page builds the same id."""
    if ex == "bse":
        return "B_" + re.sub(r"[^A-Za-z0-9_-]", "_", r["code"] or r["sym"])
    return "N_" + re.sub(r"[^A-Za-z0-9_-]", "_", r["sym"])


def write_charts(folder, charts):
    """One small JSON file per stock with its adjusted daily candles: c/<id>.json."""
    cdir = os.path.join(folder, "c")
    os.makedirs(cdir, exist_ok=True)
    for name in os.listdir(cdir):
        if name.endswith(".json"):
            os.remove(os.path.join(cdir, name))
    epoch = date(1970, 1, 1)
    for cid, recs in charts.items():
        out = {"d": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        for d, o, h, l, c, _prev, v in recs:
            out["d"].append((d - epoch).days)
            out["o"].append(round(o, 2)); out["h"].append(round(h, 2))
            out["l"].append(round(l, 2)); out["c"].append(round(c, 2))
            out["v"].append(int(v))
        with open(os.path.join(cdir, cid + ".json"), "w") as f:
            json.dump(out, f, separators=(",", ":"))


def write_csv(stocks, index_members, meta):
    """Full data file that Claude reads (data/scan_full.csv in the repository)."""
    idx = {}
    for name, syms in index_members.items():
        for s in syms:
            idx.setdefault(s, []).append(name)
    periods = [p[0] for p in meta["periods"]]
    path = os.path.join(DATA_DIR, "scan_full.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Symbol", "Exchange", "BSE_Code", "Name", "Segment", "Series", "Sector", "Close"]
                   + ["Chg_" + p for p in periods]
                   + ["High_52W", "From_52WH_pct", "New_52W_High", "Vol_x_50d",
                      "Darvas_Status", "Box_Top", "Box_Bottom", "Box_Sessions",
                      "IPO_Sessions", "IPO_Breakout", "Volume", "Value_Cr", "Indices", "ISIN",
                      "Momentum_Score", "Market_Cap_Cr", "Cap_Class"])
        for s in stocks:
            dv = s.get("dv") or {}
            w.writerow([s["s"], "BSE" if s["x"] == "B" else "NSE", s["code"], s["n"], s["seg"],
                        s["sr"], s["sec"], s["c"]]
                       + [s["ch"].get(p, "") for p in periods]
                       + [s["h52"], s["f52"], s["nh"], s["vx"],
                          dv.get("st", ""), dv.get("t", ""), dv.get("b", ""), dv.get("d", ""),
                          s["ipo"] or "", s["ib"], s["v"], s["tv"],
                          "; ".join(idx.get(s["s"], [])) if s["x"] == "N" else "", s["i"], s["ms"],
                          s.get("mc", ""), {"L": "Large", "M": "Mid", "S": "Small", "Mi": "Micro"}.get(s.get("mcl"), "")])
    with open(os.path.join(DATA_DIR, "meta_full.json"), "w") as f:
        json.dump(meta, f, indent=1)


def write_email_summary(stocks, lists, changes, mom_prev, meta):
    """data/email.json: the nightly Watchlist email (apps_script/Code.gs reads it). Public market facts only:
    top 10 of every fixed list, new in top 20, Darvas breakouts, volume spikes and today's list changes."""
    by = {s["k"]: s for s in stocks}

    def row(s, rank=None, prev=None):
        return {"k": s["k"], "s": s["s"], "n": s["n"], "ms": s["ms"], "c": s["c"], "d1": s["ch"].get("1D"),
                "vx": (s.get("mi") or [None])[0], "r": rank, "pr": prev}

    out = {"date": meta["asOfISO"], "asOf": meta["asOf"], "generated": meta["generated"],
           "lists": [], "new20": [], "breakout": [], "volume": [], "changes": []}
    union = set()
    for lid, label in FIXED:
        keys = [k for k in lists.get(lid, []) if k in by]
        union.update(keys)
        today = sorted(keys, key=lambda k: (-(by[k]["ms"] or 0), by[k]["s"]))
        prev = sorted((k for k in keys if mom_prev.get(k) is not None), key=lambda k: (-mom_prev[k], by[k]["s"]))
        pr = {k: i + 1 for i, k in enumerate(prev)}
        out["lists"].append({"id": lid, "name": label, "count": len(keys),
                             "top": [row(by[k], i + 1, pr.get(k)) for i, k in enumerate(today[:10])]})
        if pr:
            for i, k in enumerate(today[:20]):
                if not pr.get(k) or pr[k] > 20:
                    out["new20"].append(dict(row(by[k], i + 1, pr.get(k)), list=label))
    pool = sorted((by[k] for k in union), key=lambda s: (-(s["ms"] or 0), s["s"]))
    bo = [s for s in pool if s.get("dv") and s["dv"].get("st") == "bo"]
    vol = [s for s in pool if ((s.get("mi") or [None])[0] or 0) >= 2]
    out["breakoutCount"], out["volumeCount"] = len(bo), len(vol)
    out["breakout"] = [row(s) for s in bo[:25]]
    out["volume"] = [row(s) for s in vol[:25]]
    names = dict(FIXED)
    out["changes"] = [{"list": names.get(c[0], c[0]), "change": "added" if c[1] == "+" else "removed",
                       "s": c[3], "why": c[4]} for c in changes.get("today", [])]
    with open(os.path.join(DATA_DIR, "email.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- app files

ICON_192 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAENklEQVR4nO3dS27bSABF0XIj+1BmXo57mt14N5l6Oe6Zs5MeCBCESLJpPrJ+PGeUODJFoC6qyNInT6eX9wJr/dP6BBibgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAhoYKfX361PQUDDOtfTvCEBDem6m7YNCWg8t8U0bEhAg7nbyp/XX/XP5ExAw2tYTxHQWG6nn7b1FAENpMN6ioBG0fx2/REBDaC3C+drAupdz/UUAXWu83qKgHrWfz1FQGPprZ4ioG71edN+S0A9GqWeIqAOdbvlc9fT6eW99TmwMpoepiUBtRdOOV5MPbR8wfKGMgYmICICIiIgIgJqaawtn7sE1MwE9RQBtTJHPUVATUxTTxFQfTPVUwRU2WT1FAERElA9A73LZzkBVTJlPUVAdcx36XMhoN0N8eGK1QS0r7nrKQLa1fT1lFJ+tD6BjV2PWduhOkI9ZbKA/hqzy1/rD9tB6inTBPT5bc75X9uO35T1lDkCWniTXG1CmnXL567hL6JXbLGcXn/vtzFzqHrK0DNQGMEeE9LEG4aPjBrQJ0N1CaLy0nacC+drQy5hS+o5//lb45csbcesp4w4Ay2s5/aH+01Ih62njBXQd9O5+5jlc8zCko5cTxkooLCe2wd/t6Tlz3KcesooAW1Vz91fTJa2o9203+o9oM9Hd5PRWre0/Xn9pZ7SeUB7TDxfHvC7E9LB9RvQl/V8vD2vOOzPf//7/AErLpL++sVD6TGgJcvWunouv/hlRuWbS9sx6ykdBrRk2Vpdz8XH2/OShsqyCemw9ZTedqJ7HqRH+9rNT6ytXmagCndbm7iekPo5q4a6CKjnieeRbk+ssvZL2Ij1cNE4IPWMrtkSJp05tJmB1DONBgGpZyZVl7BR7tVZrl5AJp4p1VvCHlWinqFVXcJu30PTfz07veY/jWa38f2nU6q85j+62ndh527mrmfDI/SvwW38EPWwUPvXwhiagIgIiIiAiAiIiICICIiIgIjs8lLG6h3YI+z9T2b7GSjZv/94ez7C9v9MNg5ok+HX0EBcAxEREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEdk4oE0+2PXlQfJnWXKEOs+Sf91W2y/sMgMReTq9vG9+0DqfTK3z/al1nmXdf+Hbw5cF7hIQx2EJIyIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAi/wMGelvdl+PwXwAAAABJRU5ErkJggg=="
ICON_512 = "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAM9UlEQVR4nO3dQVIcSRZF0VRb7QPNmNZOpCm7YTeasoXehTSDndQgy1CWgCQyMzzif3/nzNqqGrnA9G54CLq/3H37eQAgz//2PgAA+xAAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBQ0d3jj72PMD8BAMo5rr8GjCYAQC2nu68BQ325+/Zz7zMA/OujxX95fNj4JAncAIAqzjzvuwqMIABADxqwOgEASliy7xqwLgEA9rd82TVgRQIA7OzSTdeAtfguIGBPt6y5bw26kRsAsJsbn+VdBW4kAMA+VplvDbiFAAA7WGu4vQW6hQAAW7P+RQgA0JL1v50AAJta5fHf+q9CAIDt+DvbUgQA2IhX/9UIALAF61+QAADDWf+aBAAYy/qXJQDAQNa/MgEARrH+xQkAUJr1H0cAgCH8wFd9AgCszw98tSAAwMq8+u9CAIA1Wf9GBABYjfXvRQCAdVj/dgQAWIH170gAgFtZ/6YEACjB+m9PAICb+IGvvgQAuJ4f+GpNAIArefXfnQAA17D+ExAA4GLWfw4CAFzG+k9DAIALWP+ZCACwlPWfjAAAm7L+dQgAsIgf+JqPAACf8wNfUxIA4BNe/c/qy923n3ufAaii1JO+YIwmAMDhUGz6T8nAOF4BAXXX/1D7bN0JAKSrv7D1T9iUAEC0Ltva5Zy9CADk6rWqvU7bggAAhBIACNXxgbrjmSsTAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIASOQnqjgIAASy/hwJAGSx/rwSAAhi/TklAJDC+vMHAYAI1p+3BADmZ/15lwAAhBIAmJzHfz4iADAz688ZAgDTsv6cJwAwJ+vPpwQAJmT9WUIAYDbWn4UEAKZi/VlOAGAe1p+LCABAKAGASXj851ICADOw/lxBAKA96891BAB6s/5cTQCgMevPLQQAurL+3EgAoCXrz+0EAPqx/qxCAABCCQA0s8rj/8vjw+0fhO4EADrx8ocVCQC0sdb6e/znSACgB+vP6gQAGrD+jCAAUJ31ZxABgNKsP+MIANRl/RlKAGBy1p+PCAAU5Qe+GO2vvQ9AP38Mk4kZwQ98sQE3AC5w9/jj7TCZqtV59c823ABY5Pwkvf5Ti3M7689m3AD43PJJeveKwHLWny25AXDOdXvkQnAd68/G3AD40O175EKwnPVne24AvG/F4XYh+JT1ZxduALxj0GO7C8FQ1p9LuQHwHxsMtAvBH/zAF3sRAH7b+PFcCQ5+ioJdeQXEv3ZcothXQ179sy83AKo8hKZdCKw/u3MDSFdk/U8lXAisPxW4AUSrvLMTXwisP0W4AeSqvP6nJrsQWH/qcANI1HFP57gQWH9KEYA4Hdf/1BwluEXsb5zVeQWUpfv6n2r3asgPfFGNG0CKXlu5XJcLwayff1oTgAiD1ud0c3cfuOMBambAq39qEoD5bbD+r/+xSAYOlbbS+lOWAExuxCKfWaLXf1SkBLuPpvWnMgGY1jYP/uf/tSIZOOw0oNaf4gRgTvuu/9t/P7AE1p/6BGBCG7/2uei/XqQEo1fV+tOCAEylyIP/px+qSAYOtRe28tmYgwDMo/76v/2YRUqw7u/RD3zRhQBMotH6v/34RTJwWOP3u/vvBZYTgBkUfOl/3a+1+3reeCHw6p9eBKC3pg/+53/dIhk4XPh5sP60IwCNTbb+bw/QqATWn44EoKvur32WqFaCjz4/1p+mBKCfWR/8z6j8asj605cANBO4/q8KXgisP635P4TpJHn9T708PlQ4s/WnOzeANhJe+l+kzoXgFq2/BHQnAA148D+vyN8QQDsCUJ31X6jjhWC+rwK9CEBpg177PD/dH55W/8BX+vr917ofsEsJrD+7E4CiBo3X//9+rDP9R89P94cBGTjUfjVk/alAACoauP5VPT/dj2jAoeSFwPpThG8DLSdw/Y+OV4FxinzzaIUzwJEbQC0DX/p3MO4e8GrfC4H1pxQBqGLcd/t0Wf+jDRpwtP3fEFh/qhGAEnyv5142uxD4WlCQAOxv6I/49nr8P9rsEnCq4N8Vw2gCsCcP/gWNeDXkK0JNArAb61/ZihcCXxHK8m2g+7D+Xdz4zaO+IlTmBrAD/7ue7Vx3IfBFoTgB2JQH/+4W/g2BrwgtCMB2rP80zlwIfDloRAA24rXPlHwJaE0AhvPgD9Tku4DGsv5AWQIwkPUHKvMKaBQv/YHiBGB9HvyBFrwCWpn1B7pwA1iT1z5AIwKwDg/+QDteAa3A+gMdCcCtrD/QlFdAN/HSH+hLAK7kwR/oziuga1h/YAJuABfz2geYgwBcwIM/MBOvgJay/sBkBGAR6w/Mxyugz3npD0xJAM7x4A9MzCugD1l/YG4C8KHVl/rl8cH6A3UIwDkr7rXpB6oRgE+sMtzWHyhIAIaz/kBNAvC5qxfcS3+gMt8GusjL48Ol3xRk+rt4frrf+wi/ff3+a+8jEMQNYKmLBt36t/D8dF9q/Q8lj8TEBOACS2bda58uKu9s5bMxEwG4zPlxN/1d1F/Y+idkAgKwGuvfRZdt7XJO+hKAi7079Na/i16r2uu0tCMA1zidey/9gaYE4ErH0Tf9vXR8oO54ZroQgOtZf6A1AQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUH/tfYCNPD/d732E375+/7X3EQACAlBq+o+OR5IBYF+TvwIquP6vKp8NSDBzAOovbP0TAhObNgBdtrXLOYH5zBmAXqva67TANOYMAACfmjAAHR+oO54Z6G7CAACwhAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEmDMDX77/2PsLFxp3ZZ2ObjzzOuDO/PD4M+sjjdDxzZRMGAIAl5gxArwe90af12djy469r9Gl7PVD3Om0Lcwbg0OfP+Tbn9NnY/le53Tbn7LKqXc7Zy7QBOHT4c77lCX029vq1rrPlCetva/0TNjVzAA61/5xvfzafjX1/xeW2P1vlha18tu6+3H37ufcZtvD8dL/3EX7bfXp8Nk75bJy6e/yx7wFOmf7RUgIAwB8mfwUEwEcEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQv0D0MeIsRtgwooAAAAASUVORK5CYII="
MANIFEST = '{\n  "name": "NSE Stock Scanner",\n  "short_name": "NSE Scanner",\n  "start_url": "./",\n  "scope": "./",\n  "display": "standalone",\n  "background_color": "#f6f7f9",\n  "theme_color": "#1f4fd8",\n  "icons": [\n    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},\n    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},\n    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}\n  ]\n}\n'
SERVICE_WORKER = '// Network-first: always try to get today\'s data, fall back to the last copy when offline.\nconst CACHE = "nse-scanner-v4";\nself.addEventListener("install", e => self.skipWaiting());\nself.addEventListener("activate", e => e.waitUntil(self.clients.claim()));\nself.addEventListener("fetch", e => {\n  if (e.request.method !== "GET") return;\n  e.respondWith(\n    fetch(e.request, {cache: "no-store"})\n      .then(res => { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return res; })\n      .catch(() => caches.match(e.request))\n  );\n});\n'


def write_app_files(folder):
    """Write the files that make the page installable as a phone app."""
    import base64 as _b64
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(MANIFEST)
    with open(os.path.join(folder, "sw.js"), "w", encoding="utf-8") as f:
        f.write(SERVICE_WORKER)
    for name, data in (("icon-192.png", ICON_192), ("icon-512.png", ICON_512)):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(_b64.b64decode(data))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock Scanner</title>
<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#1f4fd8">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NSE Scanner">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-192.png">
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#6b7280;--line:#e5e7eb;
--up:#0a8a4a;--upbg:#e7f6ee;--dn:#c62828;--dnbg:#fdecec;--acc:#1f4fd8;--accbg:#e8eefc;--amb:#a15c00;--ambbg:#fff4e0}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaed;--mute:#9aa0a6;
--line:#2a2f3a;--up:#3ecf7f;--upbg:#12301f;--dn:#ff6b6b;--dnbg:#3a1717;--acc:#7aa2ff;--accbg:#1c2640;--amb:#ffb84d;--ambbg:#3a2a10}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:16px;max-width:1200px;margin:auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mute);font-size:12px}
.wrap{max-width:1200px;margin:auto;padding:0 16px 40px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end}
.row+.row,details .row{margin-top:10px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--ink);padding:7px 12px;font:inherit;cursor:pointer;border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button.on{background:var(--acc);color:#fff}
select,input{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 10px;max-width:100%}
input[type=search]{flex:1;min-width:140px}
input[type=number]{width:110px}
label.l{font-size:12px;color:var(--mute);display:flex;flex-direction:column;gap:3px;max-width:100%}
label.grow{flex:1;min-width:200px}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit}
.btn:disabled{opacity:.4}
summary{cursor:pointer;color:var(--acc);font-size:13px;margin-top:10px}
.hint{font-size:12px;color:var(--mute);margin-top:8px}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.stat{flex:1;min-width:100px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
.stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--mute)}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:var(--card);font-size:12px;color:var(--mute);cursor:pointer;user-select:none}
th:hover{color:var(--ink)}
th.sorted{color:var(--acc)}
td.l,th.l{text-align:left}
tbody tr.r{cursor:pointer}
tbody tr.r:hover td{background:var(--accbg)}
td.sym b{font-weight:600}
td.sym small{display:block;color:var(--mute);max-width:220px;overflow:hidden;text-overflow:ellipsis}
.tag{font-size:10px;padding:1px 5px;border-radius:4px;background:var(--accbg);color:var(--acc);margin-left:4px;vertical-align:middle}
.tag.b{background:var(--ambbg);color:var(--amb)}
.pill{display:inline-block;min-width:68px;padding:2px 6px;border-radius:6px;font-weight:600}
.up{color:var(--up);background:var(--upbg)} .dn{color:var(--dn);background:var(--dnbg)} .na{color:var(--mute)}
.chip{display:inline-block;font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600}
.chip.bo{background:var(--upbg);color:var(--up)} .chip.box{background:var(--accbg);color:var(--acc)} .chip.form{color:var(--mute)}
.chip.nh{background:var(--ambbg);color:var(--amb)}
tr.det td{background:var(--bg);text-align:left;white-space:normal;padding:12px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px 18px}
.dgrid div span{display:block;font-size:11px;color:var(--mute)}
.dgrid div b{font-weight:600;overflow-wrap:anywhere}
.pager{display:flex;justify-content:space-between;align-items:center;padding:10px;color:var(--mute);font-size:12px;gap:8px}
.foot{color:var(--mute);font-size:12px;margin-top:10px}
.tabs{display:flex;gap:6px;margin-top:12px}
.tabs button{flex:0 0 auto;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:20px;padding:7px 16px;font:inherit;font-weight:600;cursor:pointer}
.tabs button.on{background:var(--acc);border-color:var(--acc);color:#fff}
[hidden]{display:none!important}
.chartbox{margin-top:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.chart{height:320px;width:100%;display:flex;flex-direction:column}
.chart .pmain{flex:1;min-height:0}.chart .prsi{height:90px;border-top:1px solid var(--line)}.chart.withrsi{height:410px}
.indbar{display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line)}
button.ind{border:1px solid var(--line);background:var(--card);color:var(--mute);border-radius:14px;padding:3px 9px;font:inherit;font-size:11px;cursor:pointer}
button.ind i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;opacity:.35}
button.ind.on{color:var(--ink);border-color:var(--ink)} button.ind.on i{opacity:1}
.cbtns{display:flex;gap:10px;align-items:center}.btn.sm{padding:4px 10px;font-size:12px}
#fs{position:fixed;inset:0;z-index:50;background:var(--bg);display:flex;flex-direction:column}
#fs .fshead{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;padding:10px 12px;background:var(--card);border-bottom:1px solid var(--line)}
#fs .fshead a{color:var(--acc);font-weight:600;text-decoration:none;font-size:13px}
#fschart{flex:1;min-height:0;background:var(--card)}#fschart .chart.withrsi{height:100%}
#fs .chartbar{background:var(--card)}
.chartbar{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;justify-content:space-between;padding:8px 10px;font-size:11px;color:var(--mute);border-top:1px solid var(--line)}
.chartbar i{display:inline-block;width:14px;height:0;border-top:2px solid;vertical-align:middle;margin-right:4px}
.chartbar i.dash{border-top-style:dashed}
.chartbar a{color:var(--acc);font-weight:600;text-decoration:none;font-size:12px}
.chip.hold{background:var(--accbg);color:var(--acc)} .chip.part{background:var(--ambbg);color:var(--amb)} .chip.exit{background:var(--dnbg);color:var(--dn)}
.chip.warn{background:var(--dnbg);color:var(--dn)} .chip.tgt{background:var(--upbg);color:var(--up)}
td small.sub2{display:block;color:var(--mute);font-size:11px}
h3.sec{font-size:15px;margin:18px 0 8px}
.steps{margin:8px 0 0;padding-left:20px;font-size:13px;line-height:1.6}
.err{color:var(--dn);font-size:13px;margin-top:8px}
input.url{flex:1;min-width:200px}
.tabs{overflow-x:auto}
#ticker{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);overflow:hidden;white-space:nowrap;font-size:12px;font-variant-numeric:tabular-nums}
#ticker .tk{display:inline-flex;gap:20px;padding:7px 10px;animation:tk var(--tkdur,60s) linear infinite;will-change:transform}
#ticker:hover .tk,#ticker:active .tk{animation-play-state:paused}
#ticker a{color:var(--ink);text-decoration:none;cursor:pointer}
#ticker a b{margin-right:4px} #ticker .r1{color:var(--mute);margin-right:3px}
@keyframes tk{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){#ticker{overflow-x:auto} #ticker .tk{animation:none}}
button.wlb{border:1px solid var(--line);background:var(--card);color:var(--acc);border-radius:6px;padding:1px 6px;font:inherit;font-size:11px;font-weight:600;cursor:pointer;margin-left:6px;vertical-align:middle}
button.wlb.on{background:var(--upbg);color:var(--up);border-color:transparent}
.score{display:inline-block;min-width:36px;padding:2px 6px;border-radius:6px;font-weight:700;text-align:center}
.score.hi{background:var(--upbg);color:var(--up)} .score.mid{background:var(--ambbg);color:var(--amb)} .score.lo{background:var(--bg);color:var(--mute)}
td.rk b{display:block;font-size:15px} .rkc{font-size:11px;color:var(--mute)} .rkc.u{color:var(--up)} .rkc.d{color:var(--dn)}
.panel.new20{border-color:var(--amb);background:var(--ambbg)} .panel.new20 b{color:var(--amb)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
button.tg{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:14px;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer}
button.tg.on{background:var(--acc);border-color:var(--acc);color:#fff}
button.tg small{opacity:.7}
textarea{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;width:100%;margin-top:8px}
.mbk{width:100%;margin-top:6px} .mbk td{padding:5px 4px;border-bottom:1px solid var(--line);white-space:normal;text-align:left;vertical-align:top}
.mbk td.p{text-align:right;white-space:nowrap;font-weight:600}
.mbk small{display:block;color:var(--mute);font-size:11px}
.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:3px;max-width:180px} .bar i{display:block;height:100%;background:var(--acc)}
.wled{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-bottom:12px}
.wled h4{margin:0 0 4px;font-size:13px}
tr.n20 td:first-child{box-shadow:inset 3px 0 var(--amb)}
.ok{color:var(--up)}
.tag.g{background:var(--bg);color:var(--mute);border:1px solid var(--line)}
button.tg.gone{border-style:dashed;color:var(--mute)}
.selbar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;font-size:13px}
.selbar label{display:flex;align-items:center;gap:6px;cursor:pointer}
input.selcb{width:18px;height:18px;margin:0 0 4px;cursor:pointer;display:block;margin-left:auto;margin-right:auto}
tr.selr td{background:var(--accbg)}
#undo{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:60;display:flex;gap:12px;align-items:center;background:var(--ink);color:var(--bg);padding:10px 14px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.25);max-width:calc(100% - 32px);font-size:13px}
#undo .btn{background:transparent;color:var(--bg);border-color:var(--bg);font-weight:700}
.mlab{font-size:12px;color:var(--mute);margin:12px 0 6px;font-weight:600} .mlab small{font-weight:400}
.tag.new{background:var(--ambbg);color:var(--amb);font-weight:700}
.tag.w{background:var(--accbg);color:var(--acc)}
.tag.old{background:var(--dnbg);color:var(--dn)}
.tag.a{background:var(--upbg);color:var(--up);font-size:11px}
small.ipol{display:block;color:var(--mute);font-size:11px;margin-top:2px}
.sortrow{display:flex;gap:6px}
.chgrow{margin-top:10px} .chgrow b{display:block;font-size:12px;margin-bottom:4px}
button.tg.addc{color:var(--up)} button.tg.remc{color:var(--dn)}
.livedot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--up);vertical-align:middle}
#pick{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.35);display:flex;align-items:flex-end;justify-content:center}
#pick .pickbox{background:var(--card);border-radius:14px 14px 0 0;padding:16px;width:100%;max-width:520px;box-shadow:0 -4px 20px rgba(0,0,0,.2);max-height:88vh;overflow-y:auto}
.sugs{border:1px solid var(--line);border-radius:8px;margin-top:6px;max-height:340px;overflow-y:auto;background:var(--card)}
.sugs .hint{padding:8px 10px;margin:0}
button.sug{display:flex;align-items:center;gap:8px;width:100%;text-align:left;border:0;border-bottom:1px solid var(--line);background:none;color:var(--ink);font:inherit;font-size:13px;padding:8px 10px;cursor:pointer}
button.sug:last-child{border-bottom:0} button.sug:hover,button.sug:focus{background:var(--accbg);outline:0}
button.sug .sm1{flex:1;min-width:0} button.sug .sm1 small{display:block;color:var(--mute);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button.sug .sx{font-size:10.5px;border:1px solid var(--line);border-radius:4px;padding:0 4px;color:var(--mute);margin-left:4px;vertical-align:1px}
button.sug .sp{font-variant-numeric:tabular-nums;white-space:nowrap} button.sug .ok2{color:var(--up);font-weight:600;white-space:nowrap;min-width:52px;text-align:right}
button.sug .add2{color:var(--acc);font-weight:600;white-space:nowrap;min-width:52px;text-align:right}
.choice{margin-top:8px} .choice>b{font-size:12.5px}
select.persel{font:inherit;font-size:11.5px;padding:1px 2px;margin-top:2px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
#pick button.tg{padding:9px 14px;font-size:14px}
@media (max-width:700px){td.sym{white-space:normal;min-width:0;width:100%} td.sym small{max-width:none;white-space:normal} .hide-m{display:none} th,td{padding:8px 6px} .pill{min-width:0} .dgrid{grid-template-columns:1fr 1fr} .tag,.chip{display:inline-block;margin-top:2px} .chart{height:260px} .chart.withrsi{height:350px} .stat b{font-size:16px} .stat{min-width:96px}}
.verdict{font-size:21px;font-weight:800;line-height:1.3;margin:0 0 6px;padding:10px 12px;border-radius:10px;overflow-wrap:anywhere}
.verdict.up{color:var(--up);background:var(--upbg)} .verdict.dn{color:var(--dn);background:var(--dnbg)} .verdict.mid{color:var(--amb);background:var(--ambbg)}
.md{font-size:15px;line-height:1.55;overflow-wrap:anywhere;margin-top:8px}
.md h1,.md h2,.md h3,.md h4{margin:16px 0 6px;line-height:1.3} .md h1{font-size:19px} .md h2{font-size:17px} .md h3{font-size:15.5px} .md h4{font-size:14.5px}
.md>:first-child{margin-top:4px} .md p{margin:8px 0} .md ul,.md ol{margin:6px 0;padding-left:22px} .md li{margin:3px 0} .md li.sub{margin-left:18px}
.md blockquote{margin:8px 0;padding:4px 12px;border-left:3px solid var(--acc);background:var(--accbg);border-radius:0 8px 8px 0}
.md code{background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:0 4px;font-size:13px}
.md pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto} .md pre code{border:0;padding:0;background:none}
.md hr{border:0;border-top:1px solid var(--line);margin:14px 0} .md a{color:var(--acc)}
.mdt{overflow-x:auto;margin:10px 0;border:1px solid var(--line);border-radius:8px}
.mdt th,.mdt td{padding:6px 8px;text-align:left;font-size:13px;line-height:1.4;cursor:auto;position:static}
.mdt td{white-space:normal;min-width:64px} .mdt th{background:var(--bg);color:var(--ink);font-weight:600}
.mdt tr:last-child td{border-bottom:0}
.tgi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px;cursor:pointer}
.tgi.hl{border-color:var(--amb);box-shadow:inset 4px 0 var(--amb)}
.tgi .th{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;font-size:12px;color:var(--mute)}
.tgi .th b{color:var(--ink)} .tgi .tt{margin-left:auto;white-space:nowrap}
.tgi .hd{display:block;font-weight:600;font-size:15px;color:var(--ink);text-decoration:none;margin:7px 0 4px;line-height:1.35;overflow-wrap:anywhere}
.tgi .tx{font-size:13px;color:var(--mute);line-height:1.45;white-space:pre-line;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.tgi .stk{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px} .tgi .stk .tg{display:inline-flex;gap:5px;align-items:center}
.tgi .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
a.btn{text-decoration:none;display:inline-block;color:var(--acc);font-weight:600}
.tgty{font-size:11px;padding:1px 7px;border-radius:4px;font-weight:600;white-space:nowrap}
.tgty.b{background:var(--accbg);color:var(--acc)} .tgty.s{background:var(--ambbg);color:var(--amb)} .tgty.r{background:var(--upbg);color:var(--up)} .tgty.o{background:var(--bg);color:var(--mute);border:1px solid var(--line)}
.rate{font-weight:700} .rate.up{color:var(--up);background:none} .rate.dn{color:var(--dn);background:none}
details.more>summary{font-weight:600;font-size:14px;margin:6px 0 12px;padding:10px 12px;background:var(--card);border:1px solid var(--line);border-radius:10px}
.connect{font-size:14px;line-height:1.5} .connect ol{margin:8px 0 0;padding-left:20px}
@media (max-width:480px){.tabs button{padding:7px 10px;font-size:13px}}
</style></head><body>
<div id="fs" hidden><div class="fshead"><span id="fsname"></span><span class="cbtns"><a id="fsnew" target="_blank" rel="noopener">Naye tab mein ↗</a><a id="fstv" target="_blank" rel="noopener">TradingView ↗</a><button class="btn sm" id="fsclose">✕ Band karo</button></span></div><div class="indbar" id="fsind"></div><div id="fschart"></div><div class="chartbar"><span id="fsleg"></span></div></div>
<div id="ticker" hidden><div class="tk" id="tk"></div></div>
<div id="undo" hidden><span id="undomsg"></span><button class="btn sm" id="undobtn">Undo</button></div>
<header><h1>Stock Scanner</h1>
<div class="sub" id="sub"></div>
<nav class="tabs"><button data-v="aaj" class="on">Aaj</button><button data-v="tg">Telegram</button><button data-v="scan">Scanner</button><button data-v="wl">Watchlist</button><button data-v="pf">Portfolio</button></nav></header>
<div class="wrap" id="v-wl" hidden>
  <div class="panel">
    <div class="row" style="justify-content:space-between;align-items:center"><b>Watchlist</b><span class="sub" id="wlsync"></span></div>
    <div class="mlab">Fixed lists <small>· roz apne aap update · jo dekhna hai on karo</small></div>
    <div class="chips" id="wlmodeF"></div>
    <div class="mlab">Meri lists</div>
    <div class="chips" id="wlmodeM"></div>
    <details id="wladd"><summary>+ Stocks add karo</summary>
      <div class="row"><label class="l">Kis list mein?<select id="wltarget"></select></label></div>
      <div class="row"><input type="search" id="wlfind" placeholder="Naam ya symbol likho… jaise sterlite, tata, hdfc bank, 500325" autocomplete="off" spellcheck="false"></div>
      <div class="sugs" id="wlsug" hidden></div>
      <div class="hint">Ya bahut saare ek saath: symbols ya company ke naam paste karo, kisi bhi format mein: <b>RELIANCE, NSE:TCS, BSE:500325</b>. Comma, space ya nayi line se alag karo (naam ho to comma ya nayi line se, jaise <b>tata steel, hdfc bank</b>). Scanner tab mein har stock pe <b>+ WL</b> button bhi hai. Har list mein 1000 tak stocks.</div>
      <textarea id="wlpaste" rows="3" placeholder="RELIANCE, NSE:TCS&#10;BSE:500325"></textarea>
      <div class="row"><button class="btn" id="wlpastebtn">Add karo</button>
        <label class="btn">CSV import (Kite etc.)<input type="file" id="wlcsv" accept=".csv,.txt,text/csv" hidden></label></div>
      <div class="hint" id="wlmsg"></div>
      <div id="wlchoice"></div>
    </details>
    <details id="wlrm"><summary>Hatao / clear</summary>
      <div class="hint">Watchlist 1–5 aur "Hataye gaye" list se stocks hata sakte ho: list on karo, rows pe checkbox lagao, phir <b>Selected hatao</b>. Fixed lists se kuch hataya nahi ja sakta. Har baar 10 second tak <b>Undo</b> milta hai. Notes aur tags stock ke saath rehte hain, kabhi delete nahi hote.</div>
      <div class="row"><label class="l grow">Poori list khali karo<select id="wlclrsel"></select></label><button class="btn" id="wlclear">Sab clear karo</button></div>
    </details>
    <details id="wlsetup"><summary>Google Sheet connection</summary>
      <div class="hint">Watchlist 1–5, hataye gaye list, notes, tags aur rank history sirf aapki Google Sheet mein save hote hain (public GitHub repo mein nahi). Sheet connect hone tak sirf is browser mein. Link aur key sirf is browser mein save hote hain.</div>
      <div class="row"><input class="url" id="wlurl" placeholder="Web app URL (https://script.google.com/macros/s/…/exec)"></div>
      <div class="row"><input class="url" id="wlkey" placeholder="Secret key (setup se mili)"><button class="btn" id="wlconnect">Connect karo</button><button class="btn" id="wldisc">Disconnect</button></div>
      <div class="row"><button class="btn" id="wlexport">Dikh rahe stocks ka CSV</button></div>
    </details>
  </div>
  <details class="panel chgbox" id="wlchg" hidden><summary><b>Aaj ke badlav</b> <span class="na" id="wlchgn"></span></summary><div id="wlchglist"></div></details>
  <div class="panel new20" id="wlnew" hidden><b>New in Top 20 today</b><div class="chips" id="wlnewlist" style="margin-top:8px"></div></div>
  <div class="panel">
    <div class="row"><input type="search" id="wlq" placeholder="Search symbol, naam ya note…">
      <label class="l">Sort<span class="sortrow"><select id="wlsort"></select><button class="btn" id="wlasc" title="Ulta order"></button></span></label></div>
    <div class="mlab">Auto tags <small>· roz ke data se</small></div><div class="chips" id="wltagsA"></div>
    <div class="mlab">Mere tags</div><div class="chips" id="wltagsM"></div>
  </div>
  <div class="stats" id="wlstats"></div>
  <div class="selbar" id="wlselbar"><label><input type="checkbox" id="wlselall"> <span id="wlsellab">Sab select karo</span></label><button class="btn sm" id="wlseldel" disabled>Selected hatao</button></div>
  <div class="tbl"><table><thead><tr id="wlhead"></tr></thead><tbody id="wlbody"></tbody></table>
  <div class="pager"><span id="wlinfo"></span><span><button class="btn" id="wlmore">Aur dikhao</button></span></div></div>
  <div class="foot" id="wlfoot"></div>
</div>
<div id="pick" hidden><div class="pickbox"><div id="pickt"></div><div class="chips" id="pickl" style="margin:10px 0"></div>
<div class="row" style="margin-top:4px"><label class="l">Naam se dhundo, is list mein add karo<select id="pickn"></select></label></div>
<div class="row"><input type="search" id="pickq" placeholder="Naam ya symbol… jaise sterlite, tata" autocomplete="off" spellcheck="false"></div>
<div class="sugs" id="picksug" hidden></div>
<div class="row"><button class="btn" id="pickx">Ho gaya</button></div></div></div>
<div class="wrap" id="v-aaj">
  <div class="panel">
    <div class="row" style="justify-content:space-between;align-items:center"><b>Aaj ka Trading Brief</b>
      <span class="row" style="gap:6px;align-items:center"><select id="aajsel" aria-label="Kaunsa brief" hidden></select><button class="btn sm" id="aajref" title="Sheet se dobara lao">↻</button></span></div>
    <div class="sub" id="aajstat" style="margin-top:8px"></div>
  </div>
  <div id="aajbox"></div>
</div>
<div class="wrap" id="v-tg" hidden>
  <div class="panel">
    <div class="row" style="justify-content:space-between;align-items:center"><b>Telegram</b>
      <span class="row" style="gap:6px;align-items:center"><span class="sub" id="tgsync"></span><button class="btn sm" id="tgref" title="Sheet se dobara lao">↻</button></span></div>
    <div class="chips" id="tgchips" style="margin-top:10px"></div>
    <div class="row"><input type="search" id="tgq" placeholder="Search stock, broker ya headline…"></div>
  </div>
  <div id="tglist"></div>
  <div class="foot" id="tgfoot"></div>
</div>
<div class="wrap" id="v-pf" hidden>
  <div class="panel" id="pfsetup">
    <b>Portfolio sheet ka link</b>
    <div class="hint" style="margin-top:4px">Link sirf is phone/browser mein save hota hai. Website kholne wale doosre logon ko aapka portfolio nahi dikhta.</div>
    <div class="row" style="margin-top:8px"><input class="url" id="pfurl" placeholder="Google Sheet ka link yahan paste karo"><button class="btn" id="pfsave">Save</button></div>
    <ol class="steps" id="pfhelp">
      <li>Google Sheet <b>My Holdings (auto-updated)</b> kholo.</li>
      <li><b>File → Share → Publish to web</b> dabao, "Holdings" tab aur <b>Comma-separated values (.csv)</b> chuno, phir <b>Publish</b>.</li>
      <li>Jo link mile use copy karke upar paste karo aur <b>Save</b> dabao.</li>
    </ol>
    <div class="err" id="pferr"></div>
  </div>
  <div class="stats" id="pfstats"></div>
  <div class="tbl" id="pfholdbox" hidden><table><thead><tr id="pfhead"></tr></thead><tbody id="pfbody"></tbody></table></div>
  <h3 class="sec" id="pfxtitle" hidden>Beche hue (Exited)</h3>
  <div class="tbl" id="pfexitbox" hidden><table><thead><tr id="pfxhead"></tr></thead><tbody id="pfxbody"></tbody></table></div>
  <div class="foot" id="pffoot"></div>
</div>
<div class="wrap" id="v-scan" hidden>
<div class="panel">
  <div class="row"><div class="seg" id="periods"></div></div>
  <div class="row">
    <label class="l grow">Setup<select id="setup"></select></label>
    <label class="l grow">Universe<select id="universe"></select></label>
  </div>
  <div class="row">
    <label class="l">Show<select id="dir">
      <option value="all">All stocks</option><option value="up">Gainers only</option><option value="dn">Losers only</option></select></label>
    <label class="l">Sort by<select id="sortsel"></select></label>
    <label class="l grow">Sector<select id="sector"></select></label>
    <label class="l" id="mcapl">Cap size<select id="mcap"><option value="">All sizes</option><option value="L">Large (top 100)</option><option value="M">Mid (101–250)</option><option value="S">Small (251–500)</option><option value="Mi">Micro (501+)</option></select></label>
  </div>
  <details>
    <summary>More filters</summary>
    <div class="row">
      <label class="l">Min price ₹<input type="number" id="minp" min="0" placeholder="0"></label>
      <label class="l">Max price ₹<input type="number" id="maxp" min="0" placeholder="any"></label>
      <label class="l">Min traded value ₹Cr<input type="number" id="minv" min="0" step="0.1" placeholder="0"></label>
      <label class="l">Min % change<input type="number" id="minc" step="0.5" placeholder="any"></label>
      <label class="l">Min volume × avg<input type="number" id="minx" min="0" step="0.5" placeholder="any"></label>
      <label class="l">Max % below 52W high<input type="number" id="max52" min="0" step="1" placeholder="any"></label>
    </div>
  </details>
  <div class="row">
    <input type="search" id="q" placeholder="Search symbol or name…">
    <button class="btn" id="scanwl" title="Naam se stock dhundo aur Watchlist 1-5 mein daalo">+ WL</button>
    <button class="btn" id="csv">Export CSV</button>
    <button class="btn" id="reset">Reset</button>
  </div>
  <div class="hint" id="hint"></div>
</div>
<div class="stats" id="stats"></div>
<div class="tbl"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
<div class="pager"><span id="pinfo"></span><span><button class="btn" id="prev">‹ Prev</button> <button class="btn" id="next">Next ›</button></span></div></div>
<div class="foot" id="foot"></div>
</div>
<script>
const D = /*__DATA__*/;
const M = D.meta, S = D.stocks, IDX = D.indices;
const idxSets = {}, memberOf = {};
for (const k in IDX) { idxSets[k] = new Set(IDX[k]); IDX[k].forEach(s => (memberOf[s] = memberOf[s] || []).push(k)); }
const PAGE = 100;
const DEF = {period:"1D", setup:"all", uni:"ALL", sec:"", mcl:"", dir:"all", sort:"chg", asc:false, q:"", minp:null, maxp:null, minv:null, minc:null, minx:null, max52:null, page:0};
const st = {...DEF};
const open = new Set();

const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (n, d=2) => n==null ? "—" : Number(n).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtInt = n => n==null ? "—" : Number(n).toLocaleString("en-IN");
const sgn = n => n==null ? "—" : (n>0?"+":"")+fmt(n)+"%";
const cnt = k => M.counts[k] || 0;

const SETUPS = [
  ["all","All stocks", s=>true, "Every stock, sorted by the column you choose."],
  ["dbo","Darvas breakout today", s=>s.dv&&s.dv.st==="bo", "Closed above the top of its Darvas box today. Classic Darvas: under More filters set “Min volume × avg” to 1.5 and “Max % below 52W high” to 10."],
  ["dnear","In Darvas box — near top (≤3%)", s=>s.dv&&s.dv.st==="box"&&s.c>=s.dv.t*0.97, "Box is confirmed and price is within 3% of the box top — a watchlist for tomorrow's breakouts."],
  ["dbox","In Darvas box (any)", s=>s.dv&&s.dv.st==="box", "Box is confirmed (top held 3 days, then bottom held 3 days) and price is still inside it."],
  ["nh","New 52-week high today", s=>s.nh, "Today's high is above every high of the past year."],
  ["n52","Within 5% of 52-week high", s=>s.f52!=null&&s.f52>=-5, "Closing price is no more than 5% below the 52-week high."],
  ["vol","Volume spike (≥2× average)", s=>s.vx!=null&&s.vx>=2, "Today's volume is at least twice the 50-day average."],
  ["ipo","IPO base breakout (listed < 1 yr)", s=>s.ib, "Listed within the last year and closed at its highest level since listing."],
  ["ipoall","Recent listings (< 1 yr)", s=>s.ipo!=null, "Stocks that started trading within the last year."],
];
const SETUP = Object.fromEntries(SETUPS.map(x=>[x[0],x]));
const UNIS = [
  ["ALL", `All stocks — NSE + BSE (${fmtInt(S.length)})`, null],
  ["NM", `NSE Mainboard (${fmtInt(cnt("NSE"))})`, ["NSE"]],
  ["NS", `NSE SME (${fmtInt(cnt("NSE SME"))})`, ["NSE SME"]],
  ["BS", `BSE SME (${fmtInt(cnt("BSE SME"))})`, ["BSE SME"]],
  ["AS", `All SME — NSE + BSE (${fmtInt(cnt("NSE SME")+cnt("BSE SME"))})`, ["NSE SME","BSE SME"]],
  ["BM", `BSE-only Mainboard (${fmtInt(cnt("BSE"))})`, ["BSE"]],
];
const UNI = Object.fromEntries(UNIS.map(x=>[x[0],x[2]]));

$("sub").textContent = `Prices as of ${M.asOf} · ${fmtInt(S.length)} stocks · updated ${M.generated}`;

M.periods.forEach(([k,l]) => {
  const b = document.createElement("button"); b.textContent = k; b.title = l; b.dataset.k = k;
  b.onclick = () => { st.period = k; st.page = 0; render(); };
  $("periods").appendChild(b);
});
SETUPS.forEach(([v,t]) => $("setup").add(new Option(t,v)));
UNIS.forEach(([v,t]) => $("universe").add(new Option(t,v)));
const og = document.createElement("optgroup"); og.label = "NSE indices";
Object.keys(IDX).forEach(k => og.appendChild(new Option(`${k} (${IDX[k].length})`, "I:"+k)));
$("universe").appendChild(og);
$("sector").add(new Option("All sectors",""));
[...new Set(S.map(s=>s.sec).filter(Boolean))].sort().forEach(s => $("sector").add(new Option(s,s)));
[["chg","% change"],["ms","Momentum score"],["f52","Closest to 52W high"],["vx","Volume × average"],["c","Price"],["tv","Traded value"],["sym","Symbol A–Z"]]
  .forEach(([v,t])=>$("sortsel").add(new Option(t,v)));
const HAS_MC = S.some(s => s.mc);
if (HAS_MC) $("sortsel").add(new Option("Market cap","mc"));
if (!S.some(s => s.mcl)) $("mcapl").hidden = true;

const COLS = [
  {k:"sym", t:"Stock", cls:"l"},
  {k:"c", t:"Close ₹"},
  {k:"chg", t:"% Chg"},
  {k:"ms", t:"Score", cls:"hide-m"},
  {k:"f52", t:"From 52W high", cls:"hide-m"},
  {k:"vx", t:"Vol × avg", cls:"hide-m"},
  {k:"dv", t:"Darvas box", cls:"l hide-m"},
  {k:"tv", t:"Value ₹Cr", cls:"hide-m"},
  {k:"sec", t:"Sector", cls:"l hide-m"},
];
const DVORD = {bo:3, box:2, form:1};
function val(s, k){
  switch(k){
    case "sym": return s.s; case "sec": return s.sec||"";
    case "chg": return s.ch[st.period];
    case "dv": return s.dv ? DVORD[s.dv.st] : null;
    default: return s[k];
  }
}

function filtered(){
  const q = st.q.trim().toUpperCase();
  const segs = st.uni.startsWith("I:") ? null : UNI[st.uni];
  const set = st.uni.startsWith("I:") ? idxSets[st.uni.slice(2)] : null;
  const fn = SETUP[st.setup][2];
  return S.filter(s => {
    if (segs && !segs.includes(s.seg)) return false;
    if (set && !(s.x==="N" && set.has(s.s))) return false;
    if (st.sec && s.sec!==st.sec) return false;
    if (st.mcl && s.mcl!==st.mcl) return false;
    if (!fn(s)) return false;
    const c = s.ch[st.period];
    if (st.dir==="up" && !(c>0)) return false;
    if (st.dir==="dn" && !(c<0)) return false;
    if (st.minc!=null && (c==null || c < st.minc)) return false;
    if (st.minp!=null && s.c < st.minp) return false;
    if (st.maxp!=null && s.c > st.maxp) return false;
    if (st.minv!=null && s.tv < st.minv) return false;
    if (st.minx!=null && (s.vx==null || s.vx < st.minx)) return false;
    if (st.max52!=null && (s.f52==null || s.f52 < -st.max52)) return false;
    if (q && !s.s.toUpperCase().includes(q) && !s.n.toUpperCase().includes(q) && !(s.code||"").includes(q)) return false;
    return true;
  });
}

function sorted(arr){
  const k = st.sort, dir = st.asc ? 1 : -1;
  return arr.sort((a,b) => {
    let x = val(a,k), y = val(b,k);
    if (x==null && y==null) return 0; if (x==null) return 1; if (y==null) return -1;
    if (typeof x === "string") return x.localeCompare(y) * dir;
    return (x - y) * dir;
  });
}

function dvCell(s){
  const d = s.dv;
  if (!d) return `<span class="na">—</span>`;
  if (d.st==="form") return `<span class="chip form">Forming</span>`;
  const lab = d.st==="bo" ? "Breakout" : "In box";
  return `<span class="chip ${d.st}">${lab}</span> <small class="na">${fmt(d.b)}–${fmt(d.t)}</small>`;
}

// ---------- charts
const LWC_URL = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";
let lwcPromise = null, pending = [], live = {scan:[], pf:[], fs:[], wl:[]}, chartSeq = 0;
const chartData = new Map();
const IND_DEF = [["ema20","20 EMA","#7c4dff"],["dma50","50 DMA","#00897b"],["dma200","200 DMA","#ef6c00"],["vol50","Vol avg","#5c6bc0"],["rsi","RSI 14","#8e24aa"],["boxes","Darvas boxes","#2962ff"]];
let IND = {ema20:true, dma50:true, dma200:false, vol50:false, rsi:false, boxes:true};
try { const saved = JSON.parse(localStorage.getItem("ind") || "null"); if (saved) IND = Object.assign(IND, saved); } catch(e){}
function loadLib(){
  if (window.LightweightCharts) return Promise.resolve();
  if (!lwcPromise) lwcPromise = new Promise((res, rej) => {
    const sc = document.createElement("script"); sc.src = LWC_URL; sc.onload = res;
    sc.onerror = () => { lwcPromise = null; rej(new Error("chart library load nahi hui")); };
    document.head.appendChild(sc);
  });
  return lwcPromise;
}
function getCandles(k){
  if (!chartData.has(k)) chartData.set(k, fetch(`c/${k}.json`).then(r => { if (!r.ok) throw new Error("chart data nahi mila"); return r.json(); })
    .catch(e => { chartData.delete(k); throw e; }));
  return chartData.get(k);
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const dayStr = n => new Date(n*864e5).toISOString().slice(0,10);
function tvUrl(s){ return `https://www.tradingview.com/chart/?symbol=${s.x==="B"?"BSE":"NSE"}%3A${encodeURIComponent(s.s)}`; }
function indChips(){
  return IND_DEF.map(([k,t,c]) => `<button class="ind${IND[k]?" on":""}" data-ind="${k}"><i style="background:${c}"></i>${t}</button>`).join("");
}
function chartHtml(s, ctx, view){
  const id = "ch" + (++chartSeq);
  pending.push({id, s, ctx, view: view || "scan"});
  return `<div class="chartbox"><div class="indbar">${indChips()}</div>
    <div class="chart" id="${id}"><div class="hint" style="padding:12px">Chart load ho raha hai…</div></div>
    <div class="chartbar"><span>${legend(ctx)}</span><span class="cbtns"><button class="btn sm" data-fs="${esc(s.k)}">⛶ Bada chart</button><a href="${tvUrl(s)}" target="_blank" rel="noopener">TradingView ↗</a></span></div></div>`;
}
function legend(ctx){
  const l = [`<span><i class="dash" style="border-color:#e08a00"></i>52W high</span>`];
  if (IND.boxes) l.push(`<span><i style="border-color:#2e9e5b"></i>box upar tuta</span>`, `<span><i style="border-color:#e53935"></i>neeche tuta</span>`, `<span><i style="border-color:#2962ff"></i>abhi ka box</span>`);
  if (ctx && ctx.avg) l.push(`<span><i class="dash" style="border-color:#888"></i>Avg</span>`, `<span><i class="dash" style="border-color:#e53935"></i>Stop</span>`, `<span><i class="dash" style="border-color:#2e9e5b"></i>Target</span>`);
  return l.join(" ");
}
function mountCharts(){
  const jobs = pending; pending = [];
  jobs.forEach(job => {
    Promise.all([loadLib(), getCandles(job.s.k)]).then(([, data]) => {
      const el = document.getElementById(job.id);
      if (!el) return;
      const entry = {el, s:job.s, data, ctx:job.ctx};
      drawInto(entry);
      live[job.view].push(entry);
    }).catch(e => { const el = document.getElementById(job.id); if (el) el.innerHTML = `<div class="err" style="padding:12px">${esc(e.message)}</div>`; });
  });
}
function destroy(entry){ (entry.charts||[]).forEach(c => { try { c.remove(); } catch(e){} }); entry.charts = []; }
function clearCharts(view){ live[view].forEach(destroy); live[view] = []; }
function drawInto(entry){
  destroy(entry);
  entry.el.innerHTML = "";
  const main = document.createElement("div"); main.className = "pmain"; entry.el.appendChild(main);
  let rsiEl = null;
  if (IND.rsi) { rsiEl = document.createElement("div"); rsiEl.className = "prsi"; entry.el.appendChild(rsiEl); }
  entry.el.classList.toggle("withrsi", !!IND.rsi);
  entry.charts = drawChart(main, rsiEl, entry.s, entry.data, entry.ctx);
}
function redrawAll(){
  Object.values(live).forEach(list => list.forEach(drawInto));
  document.querySelectorAll(".indbar").forEach(b => b.innerHTML = indChips());
  document.querySelectorAll(".chartbar > span:first-child").forEach((sp) => {
    const box = sp.closest(".chartbox, #fs"); const entry = Object.values(live).flat().find(e => box && box.contains(e.el));
    sp.innerHTML = legend(entry && entry.ctx);
  });
}
document.addEventListener("click", e => {
  const b = e.target.closest("button.ind");
  if (b) { e.stopPropagation(); IND[b.dataset.ind] = !IND[b.dataset.ind]; try { localStorage.setItem("ind", JSON.stringify(IND)); } catch(_){} redrawAll(); return; }
  const f = e.target.closest("button[data-fs]");
  if (f) { e.stopPropagation(); openFull(f.dataset.fs); }
}, true);

// indicator maths
function sma(a, n){ const o = new Array(a.length).fill(null); let s = 0; for (let i = 0; i < a.length; i++){ s += a[i]; if (i >= n) s -= a[i-n]; if (i >= n-1) o[i] = s/n; } return o; }
function ema(a, n){ const o = new Array(a.length).fill(null); const k = 2/(n+1); let e = null;
  for (let i = 0; i < a.length; i++){ if (i === n-1){ let s = 0; for (let j = 0; j < n; j++) s += a[j]; e = s/n; o[i] = e; } else if (i >= n){ e = a[i]*k + e*(1-k); o[i] = e; } } return o; }
function rsi(c, n=14){ const o = new Array(c.length).fill(null); let g = 0, l = 0;
  for (let i = 1; i < c.length; i++){ const d = c[i]-c[i-1], up = Math.max(d,0), dn = Math.max(-d,0);
    if (i <= n){ g += up; l += dn; if (i === n){ g /= n; l /= n; o[i] = l === 0 ? 100 : 100 - 100/(1+g/l); } }
    else { g = (g*(n-1)+up)/n; l = (l*(n-1)+dn)/n; o[i] = l === 0 ? 100 : 100 - 100/(1+g/l); } }
  return o; }
// every Darvas box in the history: top holds 3 sessions, then bottom holds 3 sessions
function darvasBoxes(H, L, C){
  const out = []; const n = H.length; if (n < 8) return out;
  let state = 0, top = H[0], ti = 0, cnt = 0, bot = 0, bcnt = 0, box = null;
  for (let i = 1; i < n; i++){
    const h = H[i], l = L[i], c = C[i];
    if (state === 2){
      if (c > box.t){ box.e = i; box.r = "up"; out.push(box); box = null; state = 0; top = h; ti = i; cnt = 0; }
      else if (c < box.b){ box.e = i; box.r = "down"; out.push(box); box = null; state = 0; top = h; ti = i; cnt = 0; }
      continue;
    }
    if (state === 0){
      if (h > top){ top = h; ti = i; cnt = 0; }
      else { cnt++; if (cnt >= 3){ let m = Infinity, mi = ti+1; for (let j = ti+1; j <= i; j++) if (L[j] < m){ m = L[j]; mi = j; }
        bot = m; bcnt = i - mi; state = 1; if (bcnt >= 3){ state = 2; box = {s:ti, t:top, b:bot}; } } }
      continue;
    }
    if (h > top){ state = 0; top = h; ti = i; cnt = 0; continue; }
    if (l < bot){ bot = l; bcnt = 0; } else bcnt++;
    if (bcnt >= 3){ state = 2; box = {s:ti, t:top, b:bot}; }
  }
  if (box){ box.e = n-1; box.r = "live"; out.push(box); }
  return out;
}
function drawChart(el, rsiEl, s, D2, ctx){
  const L = window.LightweightCharts, line = css("--line"), mute = css("--mute"), up = css("--up"), dn = css("--dn");
  const base = {autoSize:true, layout:{background:{type:"solid", color:css("--card")}, textColor:mute, fontSize:11},
    grid:{vertLines:{color:line}, horzLines:{color:line}}, rightPriceScale:{borderColor:line, minimumWidth:64}, timeScale:{borderColor:line},
    crosshair:{mode:0}, localization:{locale:"en-IN", dateFormat:"dd MMM 'yy", priceFormatter:p => Number(p).toFixed(2)}};
  const chart = L.createChart(el, base);
  const n = D2.d.length, T = D2.d.map(dayStr);
  const cs = chart.addCandlestickSeries({upColor:up, downColor:dn, wickUpColor:up, wickDownColor:dn, borderVisible:false, priceLineVisible:false});
  cs.setData(T.map((t,i) => ({time:t, open:D2.o[i], high:D2.h[i], low:D2.l[i], close:D2.c[i]})));
  cs.priceScale().applyOptions({scaleMargins:{top:0.1, bottom:0.24}});
  const vs = chart.addHistogramSeries({priceFormat:{type:"volume"}, priceScaleId:"vol", lastValueVisible:false, priceLineVisible:false});
  chart.priceScale("vol").applyOptions({scaleMargins:{top:0.8, bottom:0}});
  vs.setData(T.map((t,i) => ({time:t, value:D2.v[i], color:(D2.c[i]>=D2.o[i]?up:dn)+"66"})));
  const lineOf = (vals, color, opts={}) => { const ls = chart.addLineSeries(Object.assign({color, lineWidth:1.5, lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false}, opts));
    ls.setData(T.map((t,i) => vals[i]==null ? {time:t} : {time:t, value:vals[i]})); return ls; };
  if (IND.ema20) lineOf(ema(D2.c, 20), "#7c4dff");
  if (IND.dma50) lineOf(sma(D2.c, 50), "#00897b");
  if (IND.dma200) lineOf(sma(D2.c, 200), "#ef6c00");
  if (IND.vol50) lineOf(sma(D2.v, 50), "#5c6bc0", {priceScaleId:"vol", lineWidth:1});
  const pl = (price, color, title, style=2) => price && cs.createPriceLine({price, color, lineWidth:1, lineStyle:style, axisLabelVisible:true, title});
  pl(s.h52, "#e08a00", "52W high");
  if (IND.boxes) {
    const bx = darvasBoxes(D2.h, D2.l, D2.c).filter(b => b.e - b.s >= 3 && b.e >= n - 260);
    const col = {up:"#2e9e5b", down:"#e53935", live:"#2962ff"};
    // one short line per box edge (the chart library joins points across gaps, so each box gets its own series)
    bx.slice(-25).forEach(b => {
      const end = b.r === "live" ? n-1 : b.e;
      ["t","b"].forEach(edge => {
        const ls = chart.addLineSeries({color:col[b.r], lineWidth:2, lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false, autoscaleInfoProvider:() => null});
        ls.setData([{time:T[b.s], value:b[edge]}, {time:T[end], value:b[edge]}]);
      });
    });
  }
  if (ctx && ctx.avg) {
    pl(ctx.avg, "#888888", "Avg"); pl(ctx.avg*0.9, "#e53935", "Stop"); pl(ctx.avg*1.2, "#2e9e5b", "Target");
    const lo = ctx.avg*0.9, hi = ctx.avg*1.2;
    cs.applyOptions({autoscaleInfoProvider: orig => { const r = orig(); if (r && r.priceRange) {
      r.priceRange.minValue = Math.min(r.priceRange.minValue, lo); r.priceRange.maxValue = Math.max(r.priceRange.maxValue, hi); } return r; }});
  }
  const charts = [chart];
  if (rsiEl) {
    const rc = L.createChart(rsiEl, Object.assign({}, base, {timeScale:{borderColor:line, visible:false}}));
    const rs = rc.addLineSeries({color:"#8e24aa", lineWidth:1.5, priceLineVisible:false, lastValueVisible:true});
    const rv = rsi(D2.c, 14);
    rs.setData(T.map((t,i) => rv[i]==null ? {time:t} : {time:t, value:rv[i]}));
    [70,30].forEach(p => rs.createPriceLine({price:p, color:mute, lineWidth:1, lineStyle:2, axisLabelVisible:false}));
    rs.applyOptions({autoscaleInfoProvider:() => ({priceRange:{minValue:0, maxValue:100}})});
    charts.push(rc);
  }
  const start = {from:Math.max(0, n-130), to:n+3};
  charts.forEach(c => c.timeScale().setVisibleLogicalRange(start));
  if (charts.length > 1) requestAnimationFrame(() => {
    charts.forEach(c => c.timeScale().setVisibleLogicalRange(start));
    let busy = false;
    const sync = (a, b) => a.timeScale().subscribeVisibleLogicalRangeChange(r => { if (busy || !r) return; busy = true; b.timeScale().setVisibleLogicalRange(r); busy = false; });
    sync(charts[0], charts[1]); sync(charts[1], charts[0]);
  });
  return charts;
}

// full-screen chart (also opens from index.html#c=<id> in a new tab)
function openFull(k){
  const s = BYK.get(k); if (!s) return;
  clearCharts("fs");
  const held = HELD.get(k);
  $("fsname").innerHTML = `<b>${esc(s.s)}</b> <span class="na">${esc(s.n)}</span> · ₹${fmt(s.c)} <span style="color:${s.ch["1D"]>=0?"var(--up)":"var(--dn)"}">${sgn(s.ch["1D"])}</span>`;
  $("fsind").innerHTML = indChips();
  $("fstv").href = tvUrl(s);
  $("fsnew").href = location.pathname + "#c=" + encodeURIComponent(k);
  $("fsleg").innerHTML = legend(held);
  $("fs").hidden = false; document.body.style.overflow = "hidden";
  const id = "ch" + (++chartSeq);
  $("fschart").innerHTML = `<div class="chart" id="${id}" style="height:100%"></div>`;
  pending.push({id, s, ctx:held, view:"fs"}); mountCharts();
}
function closeFull(){ clearCharts("fs"); $("fs").hidden = true; document.body.style.overflow = ""; if (location.hash.startsWith("#c=")) history.replaceState(null, "", location.pathname); }

function detail(s, ctx, cols, view, pre){
  const d = s.dv || {};
  const items = [];
  const add = (k,v) => items.push(`<div><span>${k}</span><b>${v}</b></div>`);
  add("Exchange", `${esc(s.seg)} · ${esc(s.sr)}${s.code?` · BSE ${esc(s.code)}`:""}`);
  add("52-week high", `₹${fmt(s.h52)} (${sgn(s.f52)})${s.nh?" · new high today":""}`);
  add("Volume today", `${fmtInt(s.v)} · ${s.vx==null?"—":fmt(s.vx,1)+"× 50-day avg"}`);
  if (d.st==="bo" || d.st==="box") {
    add(d.st==="bo" ? "Broke out of box" : "Darvas box", `₹${fmt(d.b)} – ₹${fmt(d.t)} (height ${fmt((d.t-d.b)/d.t*100,1)}%)`);
    add("Box age", `${d.d} sessions`);
    add("Risk to box bottom", `${fmt((s.c-d.b)/s.c*100,1)}%`);
  } else add("Darvas box", d.st==="form" ? "Box still forming" : "Not enough history");
  if (ctx && ctx.avg) add("Aapka stop · target", `₹${fmt(ctx.avg*0.9)} · ₹${fmt(ctx.avg*1.2)} (avg ₹${fmt(ctx.avg)} se)`);
  else add("10% stop · 20% target", `₹${fmt(s.c*0.9)} · ₹${fmt(s.c*1.2)}`);
  if (s.mc || s.mcl) add("Market cap", `${s.mc?"₹"+fmtInt(s.mc)+" Cr":""}${s.mc&&s.mcl?" · ":""}${({L:"Large",M:"Mid",S:"Small",Mi:"Micro"})[s.mcl]||""}`);
  if (s.ipo!=null) add("Listed", `${s.ipo} sessions ago${s.ib?" · at high since listing":""}`);
  const ch = M.periods.map(([k])=>`${k} ${sgn(s.ch[k])}`).join(" · ");
  add("Returns", ch);
  if (memberOf[s.s] && s.x==="N") add("Indices", esc(memberOf[s.s].join(", ")));
  return `<tr class="det"><td colspan="${cols||COLS.length}">${pre||""}<div class="dgrid">${items.join("")}</div>${chartHtml(s, ctx, view)}</td></tr>`;
}

let current = [];
function render(){
  clearCharts("scan");
  document.querySelectorAll("#periods button").forEach(b => b.classList.toggle("on", b.dataset.k===st.period));
  $("sortsel").value = st.sort; $("setup").value = st.setup;
  $("hint").textContent = SETUP[st.setup][3];
  current = sorted(filtered());

  const ch = current.map(s=>s.ch[st.period]).filter(v=>v!=null);
  const up = ch.filter(v=>v>0).length, dn = ch.filter(v=>v<0).length;
  const med = ch.length ? [...ch].sort((a,b)=>a-b)[Math.floor(ch.length/2)] : null;
  const bo = current.filter(s=>s.dv&&s.dv.st==="bo").length;
  const pd = st.period==="1D" ? "vs previous close" : `since ${M.periodDates[st.period]||"—"}`;
  $("stats").innerHTML = `
    <div class="stat"><b>${fmtInt(current.length)}</b><span>stocks shown</span></div>
    <div class="stat"><b style="color:var(--up)">${fmtInt(up)}</b><span>advancing</span></div>
    <div class="stat"><b style="color:var(--dn)">${fmtInt(dn)}</b><span>declining</span></div>
    <div class="stat"><b>${med==null?"—":sgn(med)}</b><span>median ${pd}</span></div>
    <div class="stat"><b style="color:var(--up)">${fmtInt(bo)}</b><span>Darvas breakouts today</span></div>`;

  $("head").innerHTML = COLS.map(c => {
    const on = st.sort===c.k; const arrow = on ? (st.asc?" ▲":" ▼") : "";
    const t = c.k==="chg" ? `% Chg (${st.period})` : c.t;
    return `<th class="${c.cls||""} ${on?"sorted":""}" data-k="${c.k}">${t}${arrow}</th>`;
  }).join("");
  document.querySelectorAll("#head th").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if (st.sort===k) st.asc = !st.asc; else { st.sort = k; st.asc = (k==="sym"||k==="sec"); }
    st.page = 0; render();
  });

  const pages = Math.max(1, Math.ceil(current.length/PAGE));
  st.page = Math.min(st.page, pages-1);
  const rows = current.slice(st.page*PAGE, st.page*PAGE+PAGE);
  $("body").innerHTML = rows.map(s => {
    const c = s.ch[st.period], id = s.x+":"+(s.code||s.s);
    const cls = c==null ? "na" : c>0 ? "up" : c<0 ? "dn" : "na";
    let tags = "";
    if (s.seg==="NSE SME") tags += `<span class="tag">SME</span>`;
    if (s.seg==="BSE SME") tags += `<span class="tag b">BSE SME</span>`;
    if (s.seg==="BSE") tags += `<span class="tag b">BSE</span>`;
    if (s.seg==="NSE" && s.sr!=="EQ") tags += `<span class="tag">${esc(s.sr)}</span>`;
    if (s.nh) tags += ` <span class="chip nh">52W high</span>`;
    if (s.dv && s.dv.st==="bo") tags += ` <span class="chip bo">Breakout</span>`;
    const held = HELD.get(s.k);
    if (held) tags += ` <span class="chip hold">Holding</span>`;
    tags += wlBtn(s.k);
    const main = `<tr class="r" data-id="${esc(id)}"><td class="l sym"><b>${esc(s.s)}</b>${tags}<small>${esc(s.n)}</small></td>
      <td>${fmt(s.c)}</td>
      <td><span class="pill ${cls}">${sgn(c)}</span></td>
      <td class="hide-m">${scoreChip(s.ms,s)}</td>
      <td class="hide-m">${sgn(s.f52)}</td>
      <td class="hide-m">${s.vx==null?"—":fmt(s.vx,1)+"×"}</td>
      <td class="l hide-m">${dvCell(s)}</td>
      <td class="hide-m">${fmt(s.tv)}</td>
      <td class="l hide-m">${esc(s.sec)||"—"}</td></tr>`;
    return main + (open.has(id) ? detail(s, held, COLS.length, "scan") : "");
  }).join("") || `<tr><td colspan="${COLS.length}" class="l na" style="padding:24px">No stocks match these filters.</td></tr>`;
  document.querySelectorAll("#body tr.r").forEach(tr => tr.onclick = () => {
    const id = tr.dataset.id; open.has(id) ? open.delete(id) : open.add(id); render();
  });
  $("pinfo").textContent = current.length ? `Showing ${st.page*PAGE+1}–${Math.min((st.page+1)*PAGE,current.length)} of ${fmtInt(current.length)} · tap a stock for details` : "";
  $("prev").disabled = st.page===0; $("next").disabled = st.page>=pages-1;
  mountCharts();
}

const bind = (id, key, conv) => $(id).addEventListener("input", e => { st[key] = conv(e.target); st.page = 0; render(); });
const numOrNull = e => e.value==="" ? null : parseFloat(e.value);
bind("setup","setup",e=>e.value); bind("universe","uni",e=>e.value); bind("sector","sec",e=>e.value); bind("mcap","mcl",e=>e.value);
bind("dir","dir",e=>e.value); bind("q","q",e=>e.value);
["minp","maxp","minv","minc","minx","max52"].forEach(k => bind(k,k,numOrNull));
$("sortsel").addEventListener("input", e => { st.sort = e.target.value; st.asc = st.sort==="sym"; st.page=0; render(); });
$("prev").onclick = () => { st.page--; render(); window.scrollTo(0,0); };
$("next").onclick = () => { st.page++; render(); window.scrollTo(0,0); };
$("reset").onclick = () => location.reload();
$("csv").onclick = () => {
  const hdr = ["Symbol","Name","Segment","Series","BSE_Code","Sector","Close", ...M.periods.map(p=>"%Chg "+p[0]),
               "52W_High","From_52WH_%","Vol_x","Darvas","Box_Top","Box_Bottom","Volume","Value_Cr"];
  const q = v => `"${String(v??"").replace(/"/g,'""')}"`;
  const lines = [hdr.join(",")].concat(current.map(s => [s.s,s.n,s.seg,s.sr,s.code,s.sec,s.c, ...M.periods.map(p=>s.ch[p[0]]??""),
     s.h52,s.f52,s.vx,s.dv?s.dv.st:"",s.dv&&s.dv.t,s.dv&&s.dv.b,s.v,s.tv].map(q).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")],{type:"text/csv"}));
  a.download = `scan_${st.setup}_${st.period}_${M.asOf.replace(/ /g,"")}.csv`; a.click();
};

// ---------- portfolio (data lives only in this browser)
const store = {
  get(k){ try { return localStorage.getItem(k); } catch(e){ return null; } },
  set(k,v){ try { localStorage.setItem(k,v); } catch(e){} }
};
const BYK = new Map(S.map(s => [s.k, s])), BYISIN = new Map(), BYSYM = new Map();
S.forEach(s => { if (s.i) BYISIN.set(s.i, s); BYSYM.set(s.x+":"+s.s.toUpperCase(), s); if (!BYSYM.has("*:"+s.s.toUpperCase())) BYSYM.set("*:"+s.s.toUpperCase(), s); });
let HELD = new Map(), PF = null, pfOpen = new Set();

function parseCSV(t){
  const rows = []; let row = [], f = "", q = false;
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (q) { if (ch === '"') { if (t[i+1] === '"') { f += '"'; i++; } else q = false; } else f += ch; }
    else if (ch === '"') q = true;
    else if (ch === ",") { row.push(f); f = ""; }
    else if (ch === "\n" || ch === "\r") { if (ch === "\r" && t[i+1] === "\n") i++; row.push(f); rows.push(row); row = []; f = ""; }
    else f += ch;
  }
  if (f !== "" || row.length) { row.push(f); rows.push(row); }
  return rows;
}
const pnum = v => { if (v == null) return null; const x = parseFloat(String(v).replace(/[₹,\s%]/g, "")); return isNaN(x) ? null : x; };
const MON = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
function pdate(v){
  v = String(v||"").trim(); let m;
  if ((m = v.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/))) return Date.UTC(+m[1], +m[2]-1, +m[3]);
  if ((m = v.match(/^(\d{1,2})[ -]([A-Za-z]{3})[a-z]*[ -,]*(\d{4})/)) && MON[m[2].toLowerCase()]!=null) return Date.UTC(+m[3], MON[m[2].toLowerCase()], +m[1]);
  if ((m = v.match(/^(\d{1,2})[\/.-](\d{1,2})[\/.-](\d{4})/))) return Date.UTC(+m[3], +m[2]-1, +m[1]);
  return null;
}
const fdate = ms => ms==null ? "—" : new Date(ms).toLocaleDateString("en-IN", {day:"numeric", month:"short", year:"numeric", timeZone:"UTC"});
function sheetCsvUrl(u){
  u = (u||"").trim();
  if (!u) return "";
  if (/\/spreadsheets\/d\/e\//.test(u)) {               // "Publish to web" link
    u = u.replace(/\/pubhtml.*$/, "/pub?output=csv");
    if (!/output=csv/.test(u)) u += (u.includes("?") ? "&" : "?") + "output=csv";
    return u;
  }
  const m = u.match(/\/spreadsheets\/d\/([A-Za-z0-9_-]+)/);  // normal link (needs "anyone with the link")
  if (m) return `https://docs.google.com/spreadsheets/d/${m[1]}/gviz/tq?tqx=out:csv&headers=0&sheet=Holdings`;
  return u;
}
function matchStock(r){
  if (r.isin && BYISIN.has(r.isin)) return BYISIN.get(r.isin);
  const sym = (r.sym||"").toUpperCase().replace(/-(BE|ST|SM|BZ)$/, "");
  const x = (r.exch||"").toUpperCase().startsWith("B") ? "B" : "N";
  return BYSYM.get(x+":"+sym) || BYSYM.get((x==="B"?"N":"B")+":"+sym) || BYSYM.get("*:"+sym) || null;
}
function readSheet(rows){
  let synced = "", h = -1;
  rows.forEach((r, i) => {
    const a = (r[0]||"").trim().toLowerCase();
    if (a.startsWith("last synced")) synced = (r[1]||"").trim();
    if (h < 0 && a === "symbol") h = i;
  });
  if (h < 0) throw new Error("Sheet mein 'Symbol' wali heading nahi mili. Kya sahi tab (Holdings) publish kiya hai?");
  const H = rows[h].map(x => x.trim().toLowerCase());
  const col = (...names) => H.findIndex(x => names.some(n => x.startsWith(n)));
  const C = {sym:col("symbol"), exch:col("exchange"), seg:col("segment"), qty:col("quantity"), avg:col("avg price"),
             notes:col("notes"), isin:col("isin"), max:col("max qty"), status:col("status"), first:col("first"),
             xdate:col("exit date"), xpx:col("exit price"), sold:col("sold qty")};
  const g = (r, k) => C[k] >= 0 ? (r[C[k]]||"").trim() : "";
  const out = [];
  rows.slice(h+1).forEach(r => {
    const sym = g(r, "sym"); if (!sym) return;
    const qty = pnum(g(r, "qty")) || 0, avg = pnum(g(r, "avg")), maxq = Math.max(pnum(g(r, "max")) || 0, qty);
    let sold = pnum(g(r, "sold")); if (sold == null) sold = Math.max(0, maxq - qty);
    let status = g(r, "status");
    if (!/hold|partial|exit/i.test(status)) status = qty <= 0 ? "Exited" : (sold > 0 ? "Partial exit" : "Holding");
    const row = {sym, exch:g(r,"exch"), seg:g(r,"seg"), qty, avg, maxq, sold, status, notes:g(r,"notes"), isin:g(r,"isin").toUpperCase(),
                 xdate:pdate(g(r,"xdate")), xpx:pnum(g(r,"xpx")), xpxSrc:pnum(g(r,"xpx"))!=null ? "sheet" : null};
    row.s = matchStock(row);
    out.push(row);
  });
  return {rows: out, synced};
}
async function fillExitPrices(){
  const need = PF.rows.filter(r => r.sold > 0 && r.xpx == null && r.s);
  await Promise.all(need.map(async r => {
    if (r.xdate != null) {
      try {
        const d = await getCandles(r.s.k), day = Math.floor(r.xdate/864e5);
        let i = -1; for (let j = 0; j < d.d.length; j++) if (d.d[j] <= day) i = j;
        if (i >= 0) { r.xpx = d.c[i]; r.xpxSrc = "close"; return; }
      } catch(e){}
    }
    r.xpx = r.s.c; r.xpxSrc = "today";
  }));
}
async function loadPortfolio(){
  const url = store.get("pf_url");
  $("pfurl").value = url || "";
  $("pfhelp").hidden = !!url;
  if (!url) { PF = null; HELD = new Map(); renderPf(); return; }
  $("pferr").textContent = "Portfolio load ho raha hai…";
  try {
    const r = await fetch(sheetCsvUrl(url), {cache:"no-store"});
    if (!r.ok) throw new Error("Sheet nahi khul rahi (" + r.status + "). Link check karo.");
    const text = await r.text();
    if (/^\s*</.test(text)) throw new Error("Sheet CSV ki jagah web page de rahi hai. 'Publish to web' mein CSV chuno.");
    PF = readSheet(parseCSV(text));
    await fillExitPrices();
    HELD = new Map(PF.rows.filter(r => r.qty > 0 && r.s).map(r => [r.s.k, {avg:r.avg, qty:r.qty}]));
    $("pferr").textContent = "";
  } catch(e) {
    PF = null; HELD = new Map();
    $("pferr").textContent = (e && e.message && !/fetch/i.test(e.message)) ? e.message : "Sheet load nahi hui. Internet aur link check karo.";
  }
  renderPf(); render();
}
function flag(r){
  if (!r.s || !r.avg || r.qty <= 0) return "";
  const c = r.s.c, stop = r.avg*0.9, tgt = r.avg*1.2;
  if (c < stop) return ` <span class="chip warn">Stop toota</span>`;
  if (c < stop*1.03) return ` <span class="chip warn">Stop ke paas</span>`;
  if (c >= tgt) return ` <span class="chip tgt">Target</span>`;
  return "";
}
const stChip = st => /exit/i.test(st) && !/partial/i.test(st) ? `<span class="chip exit">Exited</span>` : /partial/i.test(st) ? `<span class="chip part">Partial exit</span>` : `<span class="chip hold">Holding</span>`;
const rupee = (n, d=0) => n==null ? "—" : (n<0?"-":"") + "₹" + fmt(Math.abs(n), d);
function renderPf(){
  clearCharts("pf");
  const has = !!(PF && PF.rows.length);
  ["pfholdbox"].forEach(id => $(id).hidden = !has);
  if (!PF) { $("pfstats").innerHTML = ""; $("pfxtitle").hidden = $("pfexitbox").hidden = true; $("pffoot").textContent = ""; return; }
  const hold = PF.rows.filter(r => r.qty > 0), exits = PF.rows.filter(r => r.sold > 0);
  let inv = 0, val = 0, day = 0, realised = 0, missing = 0;
  hold.forEach(r => {
    if (!r.s) { missing++; return; }
    const c = r.s.c, d1 = r.s.ch["1D"];
    val += r.qty*c; if (r.avg) inv += r.qty*r.avg;
    if (d1 != null) day += r.qty * (c - c/(1+d1/100));
  });
  exits.forEach(r => { if (r.avg && r.xpx != null) realised += r.sold*(r.xpx - r.avg); });
  const invMatched = hold.filter(r => r.s && r.avg).reduce((a,r)=>a+r.qty*r.avg, 0);
  const valMatched = hold.filter(r => r.s && r.avg).reduce((a,r)=>a+r.qty*r.s.c, 0);
  const pnl = valMatched - invMatched, pc = invMatched ? pnl/invMatched*100 : null;
  const cl = n => n>0 ? "var(--up)" : n<0 ? "var(--dn)" : "inherit";
  $("pfstats").innerHTML = `
    <div class="stat"><b>${rupee(val)}</b><span>current value (approx)</span></div>
    <div class="stat"><b>${rupee(inv)}</b><span>invested</span></div>
    <div class="stat"><b style="color:${cl(pnl)}">${rupee(pnl)}</b><span>P&amp;L ${pc==null?"":"("+sgn(pc)+")"}</span></div>
    <div class="stat"><b style="color:${cl(day)}">${rupee(day)}</b><span>aaj ka change</span></div>
    <div class="stat"><b style="color:${cl(realised)}">${rupee(realised)}</b><span>beche hue ka P&amp;L (approx)</span></div>
    <div class="stat"><b>${hold.length}</b><span>stocks held${exits.length?` · ${exits.filter(r=>r.qty<=0).length} exited`:""}</span></div>`;

  $("pfhead").innerHTML = `<th class="l">Stock</th><th>Close ₹</th><th>Value ₹</th><th>P&amp;L</th><th class="hide-m">Stop · Target ₹</th><th class="l hide-m">Darvas</th>`;
  const NC = 6;
  hold.sort((a,b) => (b.s ? b.qty*b.s.c : 0) - (a.s ? a.qty*a.s.c : 0));
  $("pfbody").innerHTML = hold.map((r, i) => {
    const s = r.s, id = "h:" + r.sym + ":" + r.exch;
    const name = `<b>${esc(r.sym)}</b> ${stChip(r.status)}${flag(r)}<small class="sub2">${fmtInt(r.qty)} @ ${r.avg?"₹"+fmt(r.avg):"avg nahi"}${s?" · "+esc(s.n):""}</small>`;
    if (!s) return `<tr><td class="l sym">${name}</td><td colspan="${NC-1}" class="l na">Scanner data mein nahi mila</td></tr>`;
    const d1 = s.ch["1D"], v = r.qty*s.c, pl = r.avg ? v - r.qty*r.avg : null, pp = r.avg ? (s.c/r.avg-1)*100 : null;
    const pcls = pp==null ? "na" : pp>0 ? "up" : pp<0 ? "dn" : "na";
    const main = `<tr class="r" data-id="${esc(id)}"><td class="l sym">${name}</td>
      <td>${fmt(s.c)}<small class="sub2" style="color:${cl(d1)}">${sgn(d1)}</small></td>
      <td>${fmt(v,0)}</td>
      <td><span class="pill ${pcls}">${sgn(pp)}</span><small class="sub2">${rupee(pl)}</small></td>
      <td class="hide-m">${r.avg ? fmt(r.avg*0.9)+" · "+fmt(r.avg*1.2) : "—"}</td>
      <td class="l hide-m">${dvCell(s)}</td></tr>`;
    return main + (pfOpen.has(id) ? detail(s, {avg:r.avg, qty:r.qty}, NC, "pf") : "");
  }).join("") || `<tr><td colspan="${NC}" class="l na" style="padding:24px">Abhi koi holding nahi.</td></tr>`;

  $("pfxtitle").hidden = $("pfexitbox").hidden = !exits.length;
  $("pfxhead").innerHTML = `<th class="l">Stock</th><th>Becha qty</th><th>Exit ₹ (approx)</th><th class="hide-m">Exit value ₹</th><th>P&amp;L</th>`;
  exits.sort((a,b) => (b.xdate||0) - (a.xdate||0));
  $("pfxbody").innerHTML = exits.map(r => {
    const s = r.s, id = "x:" + r.sym + ":" + r.exch;
    const pl = (r.avg && r.xpx!=null) ? r.sold*(r.xpx - r.avg) : null, pp = (r.avg && r.xpx!=null) ? (r.xpx/r.avg-1)*100 : null;
    const pcls = pp==null ? "na" : pp>0 ? "up" : pp<0 ? "dn" : "na";
    const src = r.xpxSrc==="close" ? "us din ka close" : r.xpxSrc==="today" ? "aaj ka price" : "sheet se";
    const main = `<tr class="r" data-id="${esc(id)}"><td class="l sym"><b>${esc(r.sym)}</b> ${stChip(r.qty<=0?"Exited":"Partial exit")}<small class="sub2">${r.xdate!=null?fdate(r.xdate):"date nahi"} · avg ${r.avg?"₹"+fmt(r.avg):"—"}</small></td>
      <td>${fmtInt(r.sold)}<small class="sub2">${r.xpx==null?"":"₹"+fmt(r.sold*r.xpx,0)}</small></td>
      <td>${r.xpx==null?"—":fmt(r.xpx)}<small class="sub2">${src}</small></td>
      <td class="hide-m">${r.xpx==null?"—":fmt(r.sold*r.xpx,0)}</td>
      <td><span class="pill ${pcls}">${sgn(pp)}</span><small class="sub2">${rupee(pl)}</small></td></tr>`;
    return main + (s && pfOpen.has(id) ? detail(s, {avg:r.avg}, 5, "pf") : "");
  }).join("");

  document.querySelectorAll("#pfbody tr.r, #pfxbody tr.r").forEach(tr => tr.onclick = () => {
    const id = tr.dataset.id; pfOpen.has(id) ? pfOpen.delete(id) : pfOpen.add(id); renderPf();
  });
  $("pffoot").innerHTML = `Holdings sheet: last synced ${esc(PF.synced||"—")}. Prices: ${esc(M.asOf)} ka close (live nahi). ${missing?missing+" holding(s) scanner data mein nahi mile. ":""}Exit price approx hai jab tak sheet mein asli price na ho. Not investment advice.`;
  mountCharts();
}
$("pfsave").onclick = () => { store.set("pf_url", $("pfurl").value.trim()); loadPortfolio(); };
// ---------- watchlist (Google Sheet via Apps Script, with a local copy for offline use)
const MP = D.mprev || {};
const BYCODE = new Map(S.filter(s => s.code).map(s => [String(s.code), s]));
S.forEach(s => { if (s.bc && !BYCODE.has(String(s.bc))) BYCODE.set(String(s.bc), s); });   // NSE stock by its BSE scrip code
const cmpStr = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const scoreChip = (v, s) => v==null ? `<span class="na">—</span>` : `<span class="score ${v>=70?"hi":v>=40?"mid":"lo"}"${s && s.ipo!=null && s.ipo<20 ? ' title="Bahut nayi listing (20 session se kam): score approx hai"' : ""}>${s && s.ipo!=null && s.ipo<20 ? "~" : ""}${v}</span>`;
// same parts, order and maths as momentum() in nse_scanner.py
const lin = (x, lo, hi, pts) => x==null ? 0 : Math.max(0, Math.min(1, (x-lo)/(hi-lo))) * pts;
function momParts(s){
  const ch = s.ch, d = s.dv || {}, m = s.mi || [];
  const gap = d.st==="box" && d.t ? (d.t - s.c)/d.t*100 : null;
  const pdv = d.st==="bo" ? 10 : gap==null ? 0 : gap <= 3 ? 7 : gap <= 7 ? 4 : 2;
  const rs = ch["3M"]==null || M.bench3M==null ? null : ch["3M"] - M.bench3M;
  const mx = s.ipo==null ? null : [20, ch["1W"]==null?0:10, ch["1M"]==null?0:10, ch["3M"]==null?0:15, m[0]==null?0:10, 10, m[1]?7.5:0, m[2]?7.5:0, rs==null?0:10];
  return {rs, v20:m[0], d50:m[1], d200:m[2], mx, p:[
    lin(s.f52, -25, 0, 20), lin(ch["1W"], -5, 10, 10), lin(ch["1M"], -10, 20, 10), lin(ch["3M"], -15, 40, 15),
    (ch["1D"]||0) >= 0 ? lin(m[0], 0.5, 2.5, 10) : 0, pdv,
    m[1] && s.c > m[1] ? 7.5 : 0, m[2] && s.c > m[2] ? 7.5 : 0, lin(rs, -10, 25, 10)]};
}
const MOM_PARTS = [
  ["52W high ke kitna paas", 20, s => `${sgn(s.f52)} 52W high (₹${fmt(s.h52)}) se · 0% par poore 20, −25% ya neeche 0`],
  ["1 week change", 10, s => `${sgn(s.ch["1W"])} · −5% par 0, +10% par poore 10`],
  ["1 month change", 10, s => `${sgn(s.ch["1M"])} · −10% par 0, +20% par poore 10`],
  ["3 month change", 15, s => `${sgn(s.ch["3M"])} · −15% par 0, +40% par poore 15`],
  ["Volume vs 20-din avg", 10, (s,m) => `${m.v20==null?"—":fmt(m.v20,1)+"×"} · 0.5× par 0, 2.5× par poore 10 · sirf green din (aaj ${sgn(s.ch["1D"])}) pe milte hain`],
  ["Darvas box", 10, s => { const d = s.dv||{}; const now = d.st==="bo" ? "aaj breakout" : d.st==="box" ? `box mein, top ₹${fmt(d.t)} se ${fmt((d.t-s.c)/d.t*100,1)}% neeche` : d.st==="form" ? "box ban raha hai" : "data kam hai";
    return `${now} · breakout 10, top se ≤3% 7, ≤7% 4, box mein 2`; }],
  ["50 DMA ke upar", 7.5, (s,m) => `Close ₹${fmt(s.c)} vs 50 DMA ${m.d50==null?"— (50 din ka data nahi)":"₹"+fmt(m.d50)}`],
  ["200 DMA ke upar", 7.5, (s,m) => `Close ₹${fmt(s.c)} vs 200 DMA ${m.d200==null?"— (200 din ka data nahi)":"₹"+fmt(m.d200)}`],
  ["Nifty se strength (3M)", 10, (s,m) => `Stock 3M ${sgn(s.ch["3M"])} − Nifty 50 ${sgn(M.bench3M)} = ${m.rs==null?"—":(m.rs>0?"+":"")+fmt(m.rs)+" pts"} · −10 par 0, +25 par poore 10`],
];
const WLK = {cache:"wl_cache", queue:"wl_queue", api:"wl_api"};
const jget = (k, d) => { try { const v = JSON.parse(store.get(k) || "null"); return v == null ? d : v; } catch(e){ return d; } };
let WL = new Map(), wlQueue = [], wlOpen = new Set(), wlSel = new Set(), wlLimit = 100, wlBusy = false, wlAgain = false, wlErr = "", wlSynced = null, wlDirty = false;
let RANK = new Map(), PRANK = new Map(), wlVisible = [], wlTags = new Set(), wlForce = "", wlVer = 0;
const ISO = M.asOfISO || "", MAXW = 1000;
const dayNum = iso => Math.floor(Date.parse(iso + "T00:00:00Z") / 864e5);
const fdIso = iso => iso ? fdate(Date.parse(iso + "T00:00:00Z")) : "—";

// ---- prices. End-of-day by default. Phase 2 (live feed, e.g. Dhan via your own server) only has to call
//   LIVE_FEED.update({"N_RELIANCE": {ltp: 1501.2, prev: 1480}, ...})
// and LIVE_FEED.symbols() tells it which stocks are on screen. Score / rank stay end-of-day.
const LIVE = new Map();
function px(s){ const l = LIVE.get(s.k); return l && l.ltp ? {c:l.ltp, d1:l.prev ? (l.ltp/l.prev - 1)*100 : s.ch["1D"], live:true} : {c:s.c, d1:s.ch["1D"], live:false}; }
// Watchlist "% change" period (trading sessions back, from closing prices); s.tb = closes 2,3,4,5,10,21,63 sessions back
const TPER = [["1","1 din"],["2","2 din"],["3","3 din"],["4","4 din"],["5","1 hafta"],["10","2 hafte"],["21","1 mahina"],["63","3 mahine"]];
const TPI = {"2":0, "3":1, "4":2, "5":3, "10":4, "21":5, "63":6};
let WPER = store.get("wl_per"); if (!TPER.some(p => p[0] === WPER)) WPER = "1";
const perLabel = () => TPER.find(p => p[0] === WPER)[1];
const base1 = s => s.ch["1D"] != null ? s.c / (1 + s.ch["1D"] / 100) : null;
function chgP(s){
  const p = px(s);
  if (WPER === "1") return p.d1;
  const tb = s.tb || [];
  // live: today is one session after the last close, so N sessions back = the end-of-day close N-1 sessions back
  const b = !p.live ? tb[TPI[WPER]] : WPER === "2" ? base1(s) : ({"3":tb[0], "4":tb[1], "5":tb[2]})[WPER] ?? tb[TPI[WPER]];
  return b ? (p.c / b - 1) * 100 : null;
}
const chgCol = v => v>0?"var(--up)":v<0?"var(--dn)":"var(--mute)";
const pxCell = s => { const p = px(s), v = chgP(s); return `${fmt(p.c)}${p.live?' <span class="livedot" title="live"></span>':""}<small class="sub2" style="color:${chgCol(v)}" title="${esc(perLabel())} ka % change">${sgn(v)}</small>`; };
let liveTimer = null;
window.LIVE_FEED = {
  update(map){
    Object.keys(map).forEach(k => LIVE.set(k, map[k]));
    if (liveTimer) return;
    liveTimer = setTimeout(() => {                 // repaint prices only (open charts and notes stay as they are)
      liveTimer = null;
      document.querySelectorAll("[data-pk]").forEach(td => { const s = BYK.get(td.dataset.pk); if (s && LIVE.has(s.k)) td.innerHTML = pxCell(s); });
      renderTicker(wlRanks());
    }, 1000);
  },
  clear(){ LIVE.clear(); wlRefresh(); },
  symbols(){ return wlVisible.map(k => BYK.get(k)).filter(Boolean).map(s => ({k:s.k, sym:s.s, ex:s.x==="B"?"BSE":"NSE", isin:s.i, code:s.code})); },
};

// ---- lists
const FIXED = [["n50","Nifty 50","N50"],["n500","Nifty 500","N500"],["fo","F&O","F&O"],["nsme","NSE SME","SME"],["bsme","BSE SME","BSE SME"],["ipo","IPO","IPO"],["msh","Mainboard Shift","Shift"]];
const MYL = [1,2,3,4,5].map(n => ["w"+n, "Watchlist "+n, "W"+n]);
const LNAME = Object.fromEntries([...FIXED, ...MYL, ["old","Hataye gaye","Old"]].map(x => [x[0], x[1]]));
const LLAB = Object.fromEntries([...FIXED, ...MYL].map(x => [x[0], x[2]]));
const FSET = Object.fromEntries(FIXED.map(([id]) => [id, new Set((D.lists || {})[id] || [])]));
const IPO = D.ipo || {};
const CHG = Object.assign({today:[], new:{}, exits:[]}, D.chg || {});
const EXITS = new Map();
CHG.exits.forEach(([d, lid, k, sym, why]) => { if (!EXITS.has(k)) EXITS.set(k, []); EXITS.get(k).push({d, lid, sym, why}); });
const symOf = k => (BYK.get(k) || WL.get(k) || (EXITS.get(k) || [{}])[0]).s || (WL.get(k) || {}).sym || ((EXITS.get(k) || [{}])[0].sym) || k.slice(2);

// Item (one per stock, notes/tags belong to the stock): {k, sym, ex, name, added, notes, tags, lists:[1-5], oldDel}
function norm(it){
  if (!Array.isArray(it.lists)) it.lists = (it.mine !== false && !it.hidden) ? [1] : [];   // older versions: "Meri list" -> Watchlist 1
  delete it.mine; delete it.hidden;
  it.lists = [...new Set(it.lists.map(Number).filter(n => n >= 1 && n <= 5))].sort();
  it.notes = it.notes || ""; it.tags = it.tags || []; it.oldDel = it.oldDel || "";
  return it;
}
const blank = k => { const s = BYK.get(k); return {k, sym:s ? s.s : symOf(k), ex:k[0]==="B"?"BSE":"NSE", name:s ? s.n : "", notes:"", tags:[], lists:[], oldDel:""}; };
const getIt = k => WL.get(k) || blank(k);
const oldOf = k => (EXITS.get(k) || []).filter(e => e.d > getIt(k).oldDel);
function members(id){
  if (FSET[id]) return [...FSET[id]];
  if (id === "old") return [...EXITS.keys()].filter(k => oldOf(k).length);
  const n = +id.slice(1);
  return [...WL.values()].filter(i => i.lists.includes(n)).map(i => i.k);
}
let GS = {on:new Set()};
function loadMode(){
  let m = jget("wl_mode3", null);
  if (!m) { const v2 = jget("wl_mode", null);            // earlier version: groups + "mine"
    if (v2 && v2.on) m = {on: v2.on.filter(g => !(v2.gone || []).includes(g)).map(g => g === "mine" ? "w1" : g)}; }
  GS = {on:new Set((m && m.on) || (members("w1").length ? ["w1"] : ["n50"]))};
}
const saveMode = () => store.set("wl_mode3", JSON.stringify({on:[...GS.on]}));
function visibleKeys(){ const out = new Set(); GS.on.forEach(id => members(id).forEach(k => out.add(k))); return [...out]; }

// ---- auto tags (from today's end-of-day numbers) and my tags
const AUTO = [
  ["Darvas Breakout", s => !!(s.dv && s.dv.st === "bo")],
  ["Near Darvas Top", s => !!(s.dv && s.dv.st === "box" && s.dv.t && s.c >= s.dv.t * 0.97)],
  ["Volume Spike", s => (s.mi || [])[0] >= 2],
  ["Near 52W High", s => s.f52 != null && s.f52 >= -5],
  ["IPO Base", s => !!s.ib],
];
const autoOf = s => s ? AUTO.filter(([, f]) => f(s)).map(([t]) => t) : [];

function wlSaveCache(){ store.set(WLK.cache, JSON.stringify({items:[...WL.values()], at:Date.now(), synced:wlSynced})); }
function wlSaveQueue(){ store.set(WLK.queue, JSON.stringify(wlQueue)); }
const wlApi = () => jget(WLK.api, null);
// same rules as applyOp_ in apps_script/Code.gs
function applyOp(op){
  if (op.op === "patch") op.items.forEach(p => {
    let e = WL.get(p.k);
    if (!e) { e = norm(Object.assign(blank(p.k), {sym:p.sym || blank(p.k).sym, ex:p.ex || blank(p.k).ex, name:p.name || blank(p.k).name, added:new Date().toISOString().slice(0,10)})); WL.set(p.k, e); }
    if (p.notes != null) e.notes = p.notes;
    if (p.tags) e.tags = p.tags;
    if (p.lists) e.lists = [...new Set(p.lists.map(Number))].sort();
    if (p.addList) e.lists = [...new Set([...e.lists, +p.addList])].sort();
    if (p.delList) e.lists = e.lists.filter(n => n !== +p.delList);
    if (p.mine === true) e.lists = [...new Set([...e.lists, 1])].sort();        // version 2 queue entries
    if (p.mine === false) e.lists = e.lists.filter(n => n !== 1);
    if (p.oldDel != null) e.oldDel = p.oldDel;
    if (!e.lists.length && !e.notes && !e.tags.length && !e.oldDel) WL.delete(p.k);
  });
  else if (op.op === "add") op.items.forEach(it => { const e = WL.get(it.k) || norm(Object.assign(blank(it.k), it, {lists:[]})); WL.set(it.k, e);
    if (!e.lists.includes(1)) e.lists = [...e.lists, 1].sort(); if (!e.notes && it.notes) e.notes = it.notes; });
  else if (op.op === "remove") op.keys.forEach(k => WL.delete(k));
  else if (op.op === "update") { const e = WL.get(op.k); if (e) { if (op.notes != null) e.notes = op.notes; if (op.tags) e.tags = op.tags; } }
}
function wlDo(op){ applyOp(op); wlQueue.push(op); wlSaveQueue(); wlSaveCache(); wlRefresh(); wlSync(); }
const patchOne = (k, fields) => { const b = getIt(k); return Object.assign({k, sym:b.sym, ex:b.ex, name:b.name}, fields); };
const wlPatch = list => { if (list.length) wlDo({op:"patch", items:list}); };

async function wlSync(){
  const a = wlApi();
  if (!a || !a.url) { wlRefresh(); return; }
  if (wlBusy) { wlAgain = true; return; }
  wlBusy = true; wlStatus("Sheet se sync ho raha hai…");
  try {
    const sent = wlQueue.slice();
    const res = sent.length
      ? await fetch(a.url, {method:"POST", body:JSON.stringify({key:a.key, ops:sent})})  // text/plain: no CORS pre-check
      : await fetch(a.url + (a.url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(a.key || ""));
    let j; try { j = await res.json(); } catch(e) { throw new Error("Web app ka jawab samajh nahi aaya. Deploy mein 'Who has access: Anyone' chuna hai?"); }
    if (!j.ok) throw new Error(j.error || "Sheet ne error diya");
    wlVer = j.version || 1;
    const old = wlVer < 3;          // older script would drop the new kind of changes: keep them queued
    if (!old) { wlQueue = wlQueue.slice(sent.length); wlSaveQueue(); }
    WL = new Map((j.items||[]).map(i => [i.k, norm(i)]));
    wlQueue.forEach(applyOp);
    wlSynced = Date.now(); wlErr = old ? "Sheet ka Apps Script purana hai: naya Code.gs paste karke New version deploy karo (badlav tab tak is phone mein rukenge)" : "";
    wlSaveCache();
    if (!old) saveRankHistory();
  } catch(e) {
    wlErr = (e && e.message && !/fetch|network|load failed/i.test(e.message)) ? e.message : "offline";
  }
  wlBusy = false; wlRefresh();
  if (wlAgain) { wlAgain = false; wlSync(); }
}
function wlStatus(msg){
  const a = wlApi(), n = wlQueue.length;
  let t = msg;
  if (!t) {
    if (!a || !a.url) t = "Sirf is browser mein save · Sheet connect nahi";
    else if (wlErr === "offline") t = `Offline · ${n ? n + " badlav baad mein sync honge" : "purani copy dikh rahi hai"}`;
    else if (wlErr) t = "⚠ " + esc(wlErr);
    else t = `<span class="ok">✓ Sheet se synced</span> ${wlSynced ? new Date(wlSynced).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit"}) : ""}${n ? " · " + n + " pending" : ""}`;
  }
  $("wlsync").innerHTML = t;
}

// ---- rank history of Watchlist 1-5: in the Google Sheet (and the last 10 days in this browser), never in the repo
const rankWithin = keys => keys.filter(k => BYK.has(k)).sort((a,b) => (BYK.get(b).ms - BYK.get(a).ms) || cmpStr(BYK.get(a).s, BYK.get(b).s));
function saveRankHistory(){
  if (!ISO) return;
  const loc = jget("wl_rankhist", {}), sent = jget("wl_histsent", {});
  const ops = [];
  for (let n = 1; n <= 5; n++) {
    const ranked = rankWithin(members("w" + n));
    if (!ranked.length) continue;
    (loc[ISO] = loc[ISO] || {})[n] = Object.fromEntries(ranked.map((k,i) => [k, i+1]));
    if (wlApi() && wlVer >= 3 && sent[ISO + ":" + n] !== ranked.length) {
      ops.push({op:"hist", date:ISO, list:n, rows:ranked.map((k,i) => [k, BYK.get(k).s, i+1, BYK.get(k).ms])});
      sent[ISO + ":" + n] = ranked.length;
    }
  }
  Object.keys(loc).sort().slice(0, -10).forEach(d => delete loc[d]);
  store.set("wl_rankhist", JSON.stringify(loc));
  store.set("wl_histsent", JSON.stringify(Object.fromEntries(Object.entries(sent).filter(([k]) => k.slice(0,10) >= Object.keys(loc).sort()[0]))));
  if (ops.length) { wlQueue.push(...ops); wlSaveQueue(); wlSync(); }
}
const histCache = new Map();
function loadHist(k){
  if (histCache.has(k)) return;
  const loc = jget("wl_rankhist", {});
  histCache.set(k, Object.keys(loc).sort().flatMap(d => Object.entries(loc[d]).filter(([, m]) => m[k]).map(([n, m]) => [d, +n, m[k]])));
  const a = wlApi();
  if (!a || !a.url || wlVer < 3) return;
  fetch(a.url + (a.url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(a.key || "") + "&hist=" + encodeURIComponent(k))
    .then(r => r.json()).then(j => { if (j.ok && j.hist) { histCache.set(k, j.hist.map(h => [h[0], +h[1], +h[2]])); if (wlOpen.has(k)) renderWl(); } }).catch(() => {});
}

// ---- ranks: worked out only among the stocks on screen
function wlRanks(){
  const keys = visibleKeys(), sym = k => (BYK.get(k) || {}).s || k;
  const today = rankWithin(keys);
  const prev = keys.filter(k => MP[k] != null).sort((a,b) => (MP[b] - MP[a]) || cmpStr(sym(a), sym(b)));
  RANK = new Map(today.map((k,i) => [k, i+1])); PRANK = new Map(prev.map((k,i) => [k, i+1]));
  wlVisible = keys;
  return today;
}
const rankChg = k => { const r = RANK.get(k), p = PRANK.get(k); return r && p ? p - r : null; };
function rankCell(k){
  const r = RANK.get(k); if (!r) return `<b class="na">—</b>`;
  const c = rankChg(k);
  const ch = !PRANK.size ? "" : c == null ? `<span class="rkc u">new</span>` : c > 0 ? `<span class="rkc u">▲ ${c}</span>` : c < 0 ? `<span class="rkc d">▼ ${-c}</span>` : `<span class="rkc">=</span>`;
  return `<b>${r}</b>${ch}`;
}
const isNew20 = k => RANK.get(k) <= 20 && PRANK.size > 0 && !(PRANK.get(k) <= 20);

function myTags(){
  const used = new Map(["Watch Closely","PEAD"].map(t => [t, 0]));
  WL.forEach(it => it.tags.forEach(t => { if (!used.has(t)) used.set(t, 0); }));
  wlVisible.forEach(k => { const it = WL.get(k); if (it) it.tags.forEach(t => used.set(t, used.get(t) + 1)); });
  return [...used.entries()];
}
const wlLabel = k => { const it = WL.get(k); return it && it.lists.length ? "✓ W" + it.lists.join(",") : "+ WL"; };
function wlBtn(k){ const it = WL.get(k), on = !!(it && it.lists.length); return `<button class="wlb${on?" on":""}" data-wl="${esc(k)}" title="Watchlist 1-5 mein daalo / hatao">${wlLabel(k)}</button>`; }
function wlButtons(){ document.querySelectorAll("button.wlb").forEach(b => { const it = WL.get(b.dataset.wl); b.classList.toggle("on", !!(it && it.lists.length)); b.textContent = wlLabel(b.dataset.wl); }); }

function renderTicker(order){
  const top = order.slice(0, 20).map(k => BYK.get(k));
  $("ticker").hidden = !top.length;
  if (!top.length) return;
  const one = top.map((s,i) => { const p = px(s), v = chgP(s);
    return `<a data-tk="${esc(s.k)}"><span class="r1">${i+1}.</span><b>${esc(s.s)}</b>₹${fmt(p.c)} <span style="color:${chgCol(v)}">${sgn(v)}${WPER !== "1" ? ` <small>(${esc(perLabel())})</small>` : ""}</span></a>`; }).join("");
  $("tk").innerHTML = one + one;          // twice, so the loop is seamless
  $("tk").style.setProperty("--tkdur", Math.max(20, top.length * 4) + "s");
}

function wlRefresh(){
  const order = wlRanks();
  renderTicker(order); wlButtons(); wlStatus();
  if (!$("pick").hidden) renderPick();
  if ($("v-wl").hidden) return;
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#wlbody textarea, #wlbody input[type=text], #wlbody input:not([type])")) { wlDirty = true; return; }
  renderWl(order);
}

// ---- sorting
const ipoRef = k => { const i = IPO[k]; return i ? (i[1] || i[2]) : null; };
const ipoPct = k => { const s = BYK.get(k), r = ipoRef(k); return s && r ? (px(s).c / r - 1) * 100 : null; };
const SORTS = [
  ["score", "Momentum Score", (k,s) => s && s.ms],
  ["mcap", "Market cap ₹ Cr", (k,s) => s && s.mc],
  ["chg", "% change", (k,s) => s && chgP(s)],          // follows the period dropdown above the price column
  ["vol", "Volume (aaj)", (k,s) => s && s.v],
  ["vx", "Volume spike (× 20-din avg)", (k,s) => s && (s.mi || [])[0]],
  ["tv", "Turnover ₹", (k,s) => s && s.tv],
  ["f52", "52W high se doori", (k,s) => s && s.f52],
  ["rk", "Rank gain (kal se)", k => rankChg(k)],
  ["price", "Price", (k,s) => s && px(s).c],
  ["name", "Naam A–Z", (k,s) => (s ? s.n : getIt(k).name || symOf(k)).toUpperCase()],
  ["ipod", "IPO / Shift: date", k => IPO[k] ? IPO[k][0] : null],
  ["ipor", "IPO / Shift: % vs issue / pehla din", k => ipoPct(k)],
];
const SORTF = Object.fromEntries(SORTS.map(x => [x[0], x[2]]));
let SO = Object.assign({by:"score", asc:false}, jget("wl_sort", {}));
if (["d1","w1","m1","m3"].includes(SO.by)) SO.by = "chg";      // older saved choices
if (!SORTF[SO.by]) SO = {by:"score", asc:false};
SORTS.forEach(([v, t]) => $("wlsort").add(new Option(t, v)));
const sortBtn = () => { $("wlsort").querySelector('option[value="chg"]').textContent = `% change (${perLabel()})`; $("wlsort").value = SO.by; $("wlasc").textContent = SO.asc ? "↑ Chhota pehle" : "↓ Bada pehle"; if (SO.by === "name") $("wlasc").textContent = SO.asc ? "↑ A–Z" : "↓ Z–A"; };
$("wlsort").addEventListener("input", e => { SO = {by:e.target.value, asc:e.target.value === "name"}; store.set("wl_sort", JSON.stringify(SO)); sortBtn(); wlLimit = 100; renderWl(); });
$("wlasc").onclick = () => { SO.asc = !SO.asc; store.set("wl_sort", JSON.stringify(SO)); sortBtn(); renderWl(); };

function wlFiltered(order){
  const q = $("wlq").value.trim().toUpperCase();
  const set = new Set(order), missing = wlVisible.filter(k => !set.has(k));
  let list = order.concat(missing).filter(k => {
    const it = getIt(k), s = BYK.get(k);
    if (wlTags.size && ![...autoOf(s), ...it.tags].some(t => wlTags.has(t))) return false;
    if (q && !symOf(k).toUpperCase().includes(q) && !((s && s.n) || it.name || "").toUpperCase().includes(q) && !it.notes.toUpperCase().includes(q)) return false;
    return true;
  });
  const f = SORTF[SO.by], dir = SO.asc ? 1 : -1;
  if (SO.by !== "score" || SO.asc) list = list.map(k => [k, f(k, BYK.get(k))]).sort((a, b) => {
    const x = a[1], y = b[1];
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
  }).map(x => x[0]);
  if (wlForce && !list.includes(wlForce)) list.unshift(wlForce);
  return list;
}

// ---- row labels: fixed lists, NEW (30 days), Watchlist 1-5, Old ("… se hata - date")
function labels(k){
  const out = [];
  FIXED.forEach(([id,, lab]) => { if (FSET[id].has(k) && !(id === "n500" && FSET.n50.has(k))) out.push(`<span class="tag g">${esc(lab)}</span>`); });
  (CHG.new[k] || []).forEach(([lid, d]) => out.push(`<span class="tag new" title="${esc(LNAME[lid])} mein ${fdIso(d)} ko aaya">NEW ${esc(LLAB[lid])}</span>`));
  getIt(k).lists.forEach(n => out.push(`<span class="tag w">W${n}</span>`));
  oldOf(k).forEach(e => out.push(`<span class="tag old">${esc(LNAME[e.lid])} se hata - ${fdIso(e.d)}${e.why ? " · " + esc(e.why) : ""}</span>`));
  return out.join("");
}
function ipoLine(k, s){
  const i = IPO[k], shift = FSET.msh.has(k); if (!i || !(FSET.ipo.has(k) || shift)) return "";
  const pct = ipoPct(k), days = ISO && i[0] ? dayNum(ISO) - dayNum(i[0]) : null;
  const ref = shift ? (i[2] ? `Mainboard pehle din open ₹${fmt(i[2])}` : "pehle din ka price nahi mila") : i[1] ? `Issue ₹${fmt(i[1])}` : i[2] ? `Listing din open ₹${fmt(i[2])}` : "issue price nahi mila";   // split/bonus adjusted
  return `<small class="ipol">${shift ? "Mainboard Shift" : "IPO"}: ${fdIso(i[0])} · ${ref}${s ? ` · abhi ₹${fmt(px(s).c)}` : ""}${pct == null ? "" : ` · <b style="color:${pct>=0?"var(--up)":"var(--dn)"}">${sgn(pct)}</b>`}${days == null ? "" : ` · ${days} din`}</small>`;
}

function wlEditor(k, s){
  const it = getIt(k), mine = new Set(it.tags), m = momParts(s);
  const parts = MOM_PARTS.map(([lab, max, why], i) => { const p = m.p[i] || 0;
    return `<tr><td>${lab}<small>${why(s, m)}</small><div class="bar"><i style="width:${Math.round(p/max*100)}%"></i></div></td><td class="p">${m.mx && m.mx[i]===0 ? "— (data nahi)" : fmt(p,1).replace(/\.0$/,"") + " / " + max}</td></tr>`; }).join("");
  const h = histCache.get(k) || [];
  const hist = [1,2,3,4,5].map(n => { const r = h.filter(x => x[1] === n).slice(-10); return r.length > 1 ? `W${n}: ${r.map(x => `<span title="${esc(x[0])}">${x[2]}</span>`).join(" → ")}` : ""; }).filter(Boolean);
  const auto = autoOf(s);
  return `<div class="wled">
    <div><h4>Momentum Score: ${scoreChip(s.ms,s)} <span class="na" style="font-weight:400">${MP[k] != null ? ` · kal ${MP[k]}` : ""} · rank ${RANK.get(k)||"—"}${PRANK.get(k)?` (kal ${PRANK.get(k)})`:""}</span></h4>
      <table class="mbk">${parts}<tr><td><b>Total</b></td><td class="p">${s.ms} / 100</td></tr>${m.mx && m.mx.reduce((a,b)=>a+b,0) < 100 ? `<tr><td colspan="2"><small>Nayi listing: jin parts ka data nahi hai unhe hata kar, mile hue parts ko 100 mein scale kiya (${fmt(m.p.reduce((a,b)=>a+b,0),1)} ÷ ${fmt(m.mx.reduce((a,b)=>a+b,0),1)} × 100).</small></td></tr>` : ""}</table>
      ${hist.length ? `<div class="hint">Rank history (list ke andar): ${hist.join(" · ")}</div>` : ""}</div>
    <div><h4>Watchlist mein</h4><div class="chips">${[1,2,3,4,5].map(n => `<button class="tg${it.lists.includes(n)?" on":""}" data-wlist="${n}" data-k="${esc(k)}">W${n}</button>`).join("")}</div>
      ${auto.length ? `<h4 style="margin-top:12px">Auto tags</h4><div class="chips">${auto.map(t => `<span class="tag a">${esc(t)}</span>`).join("")}</div>` : ""}
      <h4 style="margin-top:12px">Mere tags</h4><div class="chips">${myTags().map(([t]) => `<button class="tg${mine.has(t)?" on":""}" data-wtag="${esc(t)}" data-k="${esc(k)}">${esc(t)}</button>`).join("")}</div>
      <div class="row"><input id="wlnt-${esc(k)}" placeholder="Naya tag likho" maxlength="30" style="flex:1"><button class="btn sm" data-wnewtag="${esc(k)}">+ Tag</button></div>
      <h4 style="margin-top:12px">Notes</h4><textarea id="wln-${esc(k)}" rows="3" data-k="${esc(k)}" placeholder="Is stock ke baare mein note…">${esc(it.notes)}</textarea>
      <div class="row"><button class="btn sm" data-wsave="${esc(k)}">Note save karo</button></div></div></div>`;
}

function chip(id, name, cnt){ return `<button class="tg${GS.on.has(id)?" on":""}" data-wmode="${id}">${esc(name)} <small>${fmtInt(cnt)}</small></button>`; }
function renderMode(){
  $("wlmodeF").innerHTML = FIXED.map(([id, name]) => chip(id, name, FSET[id].size)).join("");
  $("wlmodeM").innerHTML = MYL.map(([id, name]) => chip(id, name, members(id).length)).join("") + chip("old", "Hataye gaye (Old)", members("old").length);
}
const editOn = () => [...GS.on].filter(id => id[0] === "w" || id === "old");
const canSel = k => editOn().some(id => id === "old" ? oldOf(k).length > 0 : getIt(k).lists.includes(+id.slice(1)));

function renderChanges(){
  const t = CHG.today || [];
  $("wlchg").hidden = !t.length;
  if (!t.length) return;
  $("wlchgn").textContent = `(${t.length})`;
  if (t.length <= 12 && !$("wlchg").dataset.init) { $("wlchg").open = true; $("wlchg").dataset.init = 1; }
  $("wlchglist").innerHTML = FIXED.map(([id, name]) => {
    const add = t.filter(x => x[0] === id && x[1] === "+"), rem = t.filter(x => x[0] === id && x[1] === "-");
    if (!add.length && !rem.length) return "";
    const b = (x, cls) => `<button class="tg ${cls}" data-tk="${esc(x[2])}">${x[1] === "+" ? "+" : "−"} ${esc(x[3])}${x[4] ? ` <small>${esc(x[4])}</small>` : ""}</button>`;
    return `<div class="chgrow"><b>${esc(name)}</b><div class="chips">${add.map(x => b(x, "addc")).join("")}${rem.map(x => b(x, "remc")).join("")}</div></div>`;
  }).join("");
}

function renderWl(order){
  clearCharts("wl");
  order = order || wlRanks();
  renderMode(); renderChanges(); sortBtn();
  const list = wlFiltered(order);
  const listSet = new Set(list);
  [...wlSel].forEach(k => { if (!listSet.has(k) || !canSel(k)) wlSel.delete(k); });
  const tagChip = ([t, n]) => `<button class="tg${wlTags.has(t)?" on":""}" data-wtf="${esc(t)}">${esc(t)} <small>${n}</small></button>`;
  const vis = wlVisible.map(k => BYK.get(k));
  $("wltagsA").innerHTML = AUTO.map(([t, f]) => tagChip([t, vis.filter(s => s && f(s)).length])).join("");
  $("wltagsM").innerHTML = myTags().map(tagChip).join("");
  const new20 = order.filter(isNew20);
  $("wlnew").hidden = !new20.length;
  $("wlnewlist").innerHTML = new20.map(k => { const s = BYK.get(k), p = PRANK.get(k);
    return `<button class="tg" data-tk="${esc(k)}"><b>${esc(s.s)}</b> #${RANK.get(k)} <small>${p?"kal #"+p:"naya"}</small></button>`; }).join("");
  const sc = order.map(k => BYK.get(k).ms);
  $("wlstats").innerHTML = `
    <div class="stat"><b>${fmtInt(wlVisible.length)}</b><span>stocks dikh rahe</span></div>
    <div class="stat"><b>${sc.length ? Math.round(sc.reduce((a,b)=>a+b,0)/sc.length) : "—"}</b><span>average score</span></div>
    <div class="stat"><b style="color:var(--up)">${sc.filter(v=>v>=70).length}</b><span>score 70+</span></div>
    <div class="stat"><b style="color:var(--amb)">${new20.length}</b><span>new in top 20</span></div>`;
  const selectable = list.filter(canSel);
  $("wlselbar").hidden = !editOn().length;
  $("wlselall").checked = selectable.length > 0 && wlSel.size === selectable.length;
  $("wlselall").indeterminate = wlSel.size > 0 && wlSel.size < selectable.length;
  $("wlsellab").textContent = `Sab select karo (${fmtInt(selectable.length)})`;
  $("wlseldel").disabled = !wlSel.size;
  $("wlseldel").textContent = wlSel.size ? `Selected hatao (${fmtInt(wlSel.size)})` : "Selected hatao";
  const NC = 5;
  $("wlhead").innerHTML = `<th>#</th><th class="l">Stock</th><th>Score</th><th>Close ₹<br><select id="wlper" class="persel" title="Har row pe kitne din ka % change dikhe (trading din, closing price se)">${TPER.map(([v, l]) => `<option value="${v}"${v === WPER ? " selected" : ""}>${l}</option>`).join("")}</select></th><th class="l hide-m">Darvas</th>`;
  const cb = k => canSel(k) ? `<input type="checkbox" class="selcb" data-sel="${esc(k)}"${wlSel.has(k)?" checked":""} aria-label="Select">` : "";
  $("wlbody").innerHTML = list.slice(0, wlLimit).map(k => {
    const it = getIt(k), s = BYK.get(k);
    const tags = labels(k) + it.tags.map(t => `<span class="tag">${esc(t)}</span>`).join("");
    const forced = k === wlForce && !wlVisible.includes(k) ? " · is mode mein nahi" : "";
    if (!s) return `<tr class="r" data-wk="${esc(k)}"><td class="rk">${cb(k)}<b class="na">—</b></td><td class="l sym" colspan="4"><b>${esc(symOf(k))}</b>${tags}<small>${esc(it.name||"")} · aaj ka data nahi (trade nahi hua ya delist)${forced}</small>${ipoLine(k, null)}</td></tr>`;
    const held = HELD.get(k);
    const main = `<tr class="r${isNew20(k)?" n20":""}${wlSel.has(k)?" selr":""}" data-wk="${esc(k)}"><td class="rk">${cb(k)}${rankCell(k)}</td>
      <td class="l sym"><b>${esc(s.s)}</b>${tags}${held?` <span class="chip hold">Holding</span>`:""}${s.dv&&s.dv.st==="bo"?` <span class="chip bo">Breakout</span>`:""}<small>${esc(s.n)}${s.mc?` · ₹${fmtInt(s.mc)} Cr`:(s.mcl?` · ${({L:"Large",M:"Mid",S:"Small",Mi:"Micro"})[s.mcl]}`:"")}${it.notes?" · 📝 "+esc(it.notes.slice(0,60)):""}${forced}</small>${ipoLine(k, s)}</td>
      <td>${scoreChip(s.ms,s)}</td>
      <td data-pk="${esc(k)}">${pxCell(s)}</td>
      <td class="l hide-m">${dvCell(s)}</td></tr>`;
    return main + (wlOpen.has(k) ? detail(s, held, NC, "wl", wlEditor(k, s)) : "");
  }).join("") || `<tr><td colspan="${NC}" class="l na" style="padding:24px">${!GS.on.size ? "Upar koi list on karo." : wlVisible.length ? "Is filter mein koi stock nahi." : "Is list mein abhi koi stock nahi. “+ Stocks add karo” kholo, ya Scanner mein kisi stock pe “+ WL” dabao."}</td></tr>`;
  $("wlinfo").textContent = list.length ? `${fmtInt(Math.min(wlLimit, list.length))} / ${fmtInt(list.length)} dikh rahe · tap karo score ka breakdown, notes aur chart ke liye` : "";
  $("wlmore").hidden = list.length <= wlLimit;
  $("wlfoot").innerHTML = `Rank aur score order sirf on ki hui lists ke stocks mein. Momentum Score roz ${esc(M.asOf)} ke end-of-day data se (live nahi). Rank change ${esc(M.prevAsOf||"pichhle din")} ke score se. Fixed lists: NSE index files, NSE F&amp;O list, NSE/BSE bhavcopy (SME: pichhle 10 sessions mein trade hue stocks), Market cap (₹ Cr): NSE stocks ka NSE ki apni daily file se; BSE-only stocks ka BSE ke shares (hafte mein ek baar) × aaj ka close se (split/bonus ke baad refresh tak khali ho sakta hai; BSE-only ko cap size tag nahi). Cap size = NSE mainboard mein market cap rank: Large top 100, Mid 101–250, Small 251–500, Micro baaki (SME mein tag nahi). File na mile to NSE index lists se. Mainboard Shift: pichhle 1 saal mein NSE SME se mainboard pe aaye stocks (ye IPO list mein nahi ginte). Nayi listing ka score: jis part ka data nahi (3M, 50/200 DMA, Nifty) use hata kar baaki ko 100 mein scale karte hain; 20 session se kam wale pe ~ lagta hai. IPO: NSE past issues list + BSE-only nayi listings (BSE wale ka issue price nahi milta, listing din ka open dikhta hai; dono split/bonus ke hisaab se adjust). NEW = pichhle 30 din mein list mein aaya. Nifty strength = stock ka 3M return minus Nifty 50 stocks ka equal-weight average 3M return (${sgn(M.bench3M)}). Not investment advice.`;
  wlDirty = false;
  mountCharts();
}

// ---- undo (10 seconds after any removal)
let undoSnap = null, undoTimer = null;
function snapshot(keys){ return {items: keys.map(k => [k, WL.has(k) ? JSON.parse(JSON.stringify(WL.get(k))) : null])}; }
function offerUndo(msg, snap){
  undoSnap = snap; $("undomsg").textContent = msg; $("undo").hidden = false;
  clearTimeout(undoTimer); undoTimer = setTimeout(() => { $("undo").hidden = true; undoSnap = null; }, 10000);
}
$("undobtn").onclick = () => {
  const sn = undoSnap; if (!sn) return;
  undoSnap = null; $("undo").hidden = true; clearTimeout(undoTimer);
  wlPatch(sn.items.map(([k, it]) => patchOne(k, it ? {notes:it.notes, tags:it.tags, lists:it.lists, oldDel:it.oldDel}
                                                   : {notes:"", tags:[], lists:[], oldDel:""})));
};
// remove from the editable lists that are on (Watchlist 1-5, Hataye gaye); fixed lists are read-only
function removeKeys(keys){
  const on = editOn();
  keys = [...new Set(keys)].filter(canSel); if (!keys.length) return;
  const snap = snapshot(keys);
  wlPatch(keys.map(k => { const it = getIt(k), ex = oldOf(k);
    const f = {lists: it.lists.filter(n => !on.includes("w" + n))};
    if (on.includes("old") && ex.length) f.oldDel = ex.map(e => e.d).sort().pop();
    return patchOne(k, f); }));
  keys.forEach(k => wlSel.delete(k));
  offerUndo(`${fmtInt(keys.length)} stock hataye (${on.map(id => LNAME[id]).join(", ")})`, snap);
}
$("wlseldel").onclick = () => removeKeys([...wlSel]);
$("wlselall").onchange = e => { if (e.target.checked) wlFiltered(wlRanks()).filter(canSel).forEach(k => wlSel.add(k)); else wlSel.clear(); renderWl(); };
$("wlclrsel").innerHTML = `<option value="">List chuno…</option>` + [...MYL, ["old", "Hataye gaye (Old)"]].map(([id, name]) => `<option value="${id}">${name}</option>`).join("");
$("wlclear").onclick = () => {
  const id = $("wlclrsel").value; if (!id) return;
  const keys = members(id);
  if (!keys.length) { $("wlmsg").textContent = ""; return; }
  if (!confirm(`${LNAME[id]} ke saare ${keys.length} stocks hat jayenge (notes aur tags rahenge). Pakka?`)) return;
  const snap = snapshot(keys);
  wlPatch(keys.map(k => id === "old" ? patchOne(k, {oldDel: oldOf(k).map(e => e.d).sort().pop()}) : patchOne(k, {delList: +id.slice(1)})));
  offerUndo(`${LNAME[id]} khali ki (${keys.length})`, snap);
  $("wlclrsel").value = "";
};

// ---- adding (Watchlist 1-5 only)
function resolveToken(t){
  t = String(t||"").trim().replace(/^["']+|["']+$/g, "").toUpperCase();
  if (!t) return null;
  let x = null, m = t.match(/^(NSE|BSE|NS|BO)\s*:\s*(.+)$/);
  if (m) { x = m[1].startsWith("B") ? "B" : "N"; t = m[2].trim(); }
  if ((m = t.match(/^(.+)\.(NS|BO)$/))) { x = m[2]==="BO" ? "B" : "N"; t = m[1]; }
  t = t.replace(/-(EQ|BE|BZ|SM|ST)$/, "");
  if (/^IN[A-Z0-9]{10}$/.test(t)) return BYISIN.get(t) || null;
  if (/^\d{6}$/.test(t)) return BYCODE.get(t) || null;
  return (x ? BYSYM.get(x+":"+t) : BYSYM.get("N:"+t)) || BYSYM.get("*:"+t) || null;
}
function tokensFromCSV(text){
  const rows = parseCSV(text).filter(r => r.some(c => c.trim()));
  const PRI = [/^trading ?symbol$/, /^symbol$/, /^instrument/, /^ticker/, /^scrip ?code/, /^security ?id/, /^isin/, /^scrip/, /^stock/];
  for (let i = 0; i < Math.min(rows.length, 10); i++) {
    const H = rows[i].map(c => c.trim().toLowerCase());
    for (const re of PRI) {
      const c = H.findIndex(h => re.test(h));
      if (c >= 0) {
        const ex = H.findIndex(h => /^exch/.test(h));
        return rows.slice(i+1).map(r => { const v = (r[c]||"").trim(), e = ex >= 0 ? (r[ex]||"").trim().toUpperCase() : "";
          return v && e && !v.includes(":") && /^(NSE|BSE)/.test(e) ? e.slice(0,3)+":"+v : v; }).filter(Boolean);
      }
    }
  }
  return rows.map(r => r[0]).concat(...rows.map(r => r.slice(1))).map(c => c.trim()).filter(Boolean);  // no heading: every cell
}
function addTo(n, stocks){
  const fresh = stocks.filter(s => !getIt(s.k).lists.includes(n));
  const room = Math.max(0, MAXW - members("w" + n).length), put = fresh.slice(0, room);
  wlPatch(put.map(s => patchOne(s.k, {addList:n})));
  if (put.length && !GS.on.has("w" + n)) { GS.on.add("w" + n); saveMode(); wlRefresh(); }
  return {added: put.length, have: stocks.length - fresh.length, full: fresh.length - put.length};
}
for (let n = 1; n <= 5; n++) $("wltarget").add(new Option("Watchlist " + n, n));
$("wltarget").value = store.get("wl_target") || "1";
$("wltarget").addEventListener("input", e => store.set("wl_target", e.target.value));
// ---- name search: company name, NSE/BSE symbol, BSE scrip code (mainboard + SME); ignores case, spaces,
// dots and Ltd/Limited, and allows small spelling mistakes
const NOISE = new Set(["LTD","LIMITED","LTDS","THE","PVT","PRIVATE","INC","CO"]);
const nWords = x => String(x||"").toUpperCase().replace(/['\u2019.`]/g, "").split(/[^A-Z0-9]+/).filter(w => w && !NOISE.has(w));
const SIDX = S.map(s => { const w = nWords(s.n); return {s, w, j:w.join(""), sym:s.s.toUpperCase().replace(/[^A-Z0-9]/g, ""), code:String(s.code||s.bc||"")}; });
const SEGR = {"NSE":0, "BSE":1, "NSE SME":2, "BSE SME":3};
function osa(a, b, max){                     // edit distance (a swap of two letters = 1); stops early above max
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let p2 = null, p1 = Array.from({length:b.length+1}, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const c = [i]; let lo = i;
    for (let j = 1; j <= b.length; j++) {
      let v = Math.min(p1[j] + 1, c[j-1] + 1, p1[j-1] + (a[i-1] === b[j-1] ? 0 : 1));
      if (p2 && i > 1 && j > 1 && a[i-1] === b[j-2] && a[i-2] === b[j-1]) v = Math.min(v, p2[j-2] + 1);
      c.push(v); if (v < lo) lo = v;
    }
    if (lo > max) return max + 1;
    p2 = p1; p1 = c;
  }
  return p1[b.length];
}
const typoMax = q => q.length >= 7 ? 2 : 1;
function nearWord(q, words){                 // smallest typo distance between q and the start of any word
  const max = typoMax(q);
  let best = max + 1;
  for (const w of words) {
    if (w[0] !== q[0] && w[1] !== q[1]) continue;
    for (let L = q.length - 1; L <= q.length + 1; L++) {
      if (L < 1 || L > w.length) continue;
      best = Math.min(best, osa(q, w.slice(0, L), max));
      if (!best) return 0;
    }
    if (w.length < q.length - 1) best = Math.min(best, osa(q, w, max));
  }
  return best;
}
function searchStocks(query, limit){
  const qw = nWords(query), qj = qw.join("");
  if (!qj) return [];
  const out = [];
  for (const x of SIDX) {
    let sc = 0;
    if (/^\d+$/.test(qj)) sc = x.code === qj ? 100 : qj.length >= 3 && x.code.startsWith(qj) ? 85 : x.sym === qj ? 100 : x.sym.startsWith(qj) ? 85 : 0;
    else if (x.sym === qj) sc = 100;
    else if (x.j === qj) sc = 95;
    else if (x.sym.startsWith(qj)) sc = 85;
    else if (x.j.startsWith(qj)) sc = 80;
    else if (qw.every(w => x.w.some(n => n.startsWith(w)))) sc = 72;
    else if (qj.length >= 3 && (x.j.includes(qj) || x.sym.includes(qj))) sc = 60;
    else if (qj.length >= 4) {
      let tot = 0;
      for (const w of qw) {
        const d = w.length < 3 ? (x.w.some(n => n.startsWith(w)) ? 0 : 9) : nearWord(w, x.w);
        if (d > typoMax(w)) { tot = -1; break; }
        tot += d;
      }
      if (tot >= 0) sc = 50 - tot * 5;
      else if (osa(qj, x.sym, typoMax(qj)) <= typoMax(qj)) sc = 48;
      else if (qj.length >= 5 && osa(qj, x.j.slice(0, qj.length), typoMax(qj)) <= typoMax(qj)) sc = 45;
    }
    if (sc) out.push([sc, x.s]);
  }
  out.sort((a, b) => b[0] - a[0] || SEGR[a[1].seg] - SEGR[b[1].seg] || (b[1].tv || 0) - (a[1].tv || 0));
  return out.slice(0, limit || 12).map(([score, s]) => ({score, s}));
}
const justAdded = new Set();
function sugHtml(s, n){
  const inL = getIt(s.k).lists.includes(n);
  const right = justAdded.has(n + "|" + s.k) ? `<span class="ok2">✓ added</span>` : inL ? `<span class="ok2">✓ W${n}</span>` : `<span class="add2">+ W${n}</span>`;
  return `<button class="sug" data-sug="${esc(s.k)}"><span class="sm1"><b>${esc(s.s)}</b><span class="sx">${esc(s.seg)}</span>${s.code || s.bc ? ` <span class="na" style="font-size:11px">${esc(s.code || s.bc)}</span>` : ""}<small>${esc(s.n)}</small></span><span class="sp">₹${fmt(s.c)}</span>${right}</button>`;
}
function sugAdd(k, n){                       // tap on a suggestion: add to Watchlist n (already there = nothing to do)
  const s = BYK.get(k); if (!s) return;
  if (getIt(k).lists.includes(n)) { justAdded.add(n + "|" + k); return; }
  const r = addTo(n, [s]);
  if (r.full) { alert(`Watchlist ${n} mein 1000 stocks ho chuke hain.`); return; }
  justAdded.add(n + "|" + k);
}
function nameSearch(inp, box, target){       // live suggestions under a search box; returns a redraw function
  let tm = null;
  const draw = () => {
    const q = inp.value.trim();
    box.hidden = !q;
    if (!q) { box.innerHTML = ""; return; }
    const r = searchStocks(q, 12), n = target();
    box.innerHTML = r.length ? r.map(x => sugHtml(x.s, n)).join("") : `<div class="hint">“${esc(q)}” se koi stock nahi mila. Naam ka thoda hissa likh ke dekho.</div>`;
  };
  inp.addEventListener("input", () => { clearTimeout(tm); tm = setTimeout(draw, 90); });
  inp.addEventListener("keydown", e => { if (e.key !== "Enter") return; e.preventDefault(); clearTimeout(tm); draw();
    const b = box.querySelector("[data-sug]"); if (b) { sugAdd(b.dataset.sug, target()); draw(); } });
  box.addEventListener("click", e => { const b = e.target.closest("[data-sug]"); if (!b) return; sugAdd(b.dataset.sug, target()); draw(); });
  return draw;
}
const wlFindDraw = nameSearch($("wlfind"), $("wlsug"), () => +$("wltarget").value);

// paste box / CSV: exact symbols first; anything else is tried as a company name.
// A clear match is added, several matches are shown as choices, nothing = "nahi mile".
const SKIPW = /^(symbol|exchange|nse|bse|name|company)$/i;
const confident = r => r.length > 0 && (r[0].score >= 95 || r.length === 1 || (r[0].score >= 80 && r[1].score < 70));
function entriesFromText(text){
  return text.replace(/\b(NSE|BSE)\s*:\s*/gi, "$1:").split(/[\n\r,;|\t]+/).map(x => x.trim()).filter(Boolean);
}
let choiceGroups = [];
function addStocks(entries, label){
  const found = new Map(), miss = [], groups = [], n = +$("wltarget").value;
  const phrase = q => {
    if (SKIPW.test(q)) return;
    const r = searchStocks(q, 8);
    if (confident(r)) found.set(r[0].s.k, r[0].s);
    else if (r.length) groups.push([q, r.map(x => x.s)]);
    else miss.push(q);
  };
  entries.forEach(e => {
    const toks = e.split(/\s+/).filter(Boolean), res = toks.map(resolveToken);
    if (res.every(Boolean)) { res.forEach(s => found.set(s.k, s)); return; }
    const whole = toks.length > 1 ? searchStocks(e, 8) : [];
    if (confident(whole)) { found.set(whole[0].s.k, whole[0].s); return; }
    let run = [];                               // exact symbols are added; runs of other words are searched as one name
    const flush = () => { if (run.length) phrase(run.join(" ")); run = []; };
    toks.forEach((tk, i) => { if (res[i]) { flush(); found.set(res[i].k, res[i]); } else run.push(tk); });
    flush();
  });
  const r = addTo(n, [...found.values()]);
  $("wlmsg").innerHTML = `${label ? esc(label)+": " : ""}<b class="ok">${r.added} Watchlist ${n} mein add hue</b>${r.have?` · ${r.have} pehle se the`:""}${r.full?` · ⚠ ${r.full} nahi aaye (list mein 1000 ki limit)`:""}${groups.length?` · ${groups.length} ke liye neeche se chuno`:""}${miss.length?` · ${miss.length} nahi mile: ${esc([...new Set(miss)].slice(0,15).join(", "))}${miss.length>15?"…":""}`:""}`;
  choiceGroups = groups.slice(0, 30); drawChoices();
}
function drawChoices(){
  const n = +$("wltarget").value;
  $("wlchoice").innerHTML = choiceGroups.map(([q, ss]) => `<div class="choice"><b>“${esc(q)}” — kaunsa? <span class="na" style="font-weight:400">(tap = Watchlist ${n} mein add)</span></b><div class="sugs">${ss.map(s => sugHtml(s, n)).join("")}</div></div>`).join("");
}
$("wlchoice").addEventListener("click", e => { const b = e.target.closest("[data-sug]"); if (!b) return; sugAdd(b.dataset.sug, +$("wltarget").value); drawChoices(); });
$("wltarget").addEventListener("input", () => { wlFindDraw(); drawChoices(); });
$("wlpastebtn").onclick = () => { addStocks(entriesFromText($("wlpaste").value)); $("wlpaste").value = ""; };
$("wlcsv").onchange = e => { const f = e.target.files[0]; if (!f) return;
  f.text().then(t => addStocks(tokensFromCSV(t).map(x => x.trim()).filter(Boolean), f.name)); e.target.value = ""; };

// ---- "+ WL" picker (Scanner): which watchlist, 1-5
let pickK = null;
function renderPick(){
  $("pickl").hidden = !pickK;
  if (!pickK) $("pickt").innerHTML = `<b>Watchlist mein stock daalo</b> <span class="na">· naam ya symbol se dhundo</span>`;
  else {
    const s = BYK.get(pickK), it = getIt(pickK);
    $("pickt").innerHTML = `<b>${esc(s ? s.s : symOf(pickK))}</b> kis watchlist mein? <span class="na">(tap = daalo / hatao)</span>`;
    $("pickl").innerHTML = [1,2,3,4,5].map(n => `<button class="tg${it.lists.includes(n)?" on":""}" data-pick="${n}">${it.lists.includes(n)?"✓ ":""}Watchlist ${n} <small>${members("w"+n).length}</small></button>`).join("");
  }
  pickDraw();
}
for (let n = 1; n <= 5; n++) $("pickn").add(new Option("Watchlist " + n, n));
const pickDraw = nameSearch($("pickq"), $("picksug"), () => +$("pickn").value);
$("pickn").addEventListener("input", e => { $("wltarget").value = e.target.value; store.set("wl_target", e.target.value); pickDraw(); wlFindDraw(); drawChoices(); });
function openPick(k){
  pickK = k; $("pickn").value = $("wltarget").value; $("pickq").value = "";
  renderPick(); $("pick").hidden = false;
  if (!k) setTimeout(() => $("pickq").focus(), 50);
}
$("scanwl").onclick = () => openPick(null);
function togglePick(k, n){
  const it = getIt(k);
  if (it.lists.includes(n)) { const snap = snapshot([k]); wlPatch([patchOne(k, {delList:n})]); offerUndo(`${symOf(k)} Watchlist ${n} se hataya`, snap); }
  else { const r = addTo(n, [BYK.get(k)].filter(Boolean)); if (r.full) alert(`Watchlist ${n} mein 1000 stocks ho chuke hain.`); }
}
$("pickl").onclick = e => { const b = e.target.closest("[data-pick]"); if (b) togglePick(pickK, +b.dataset.pick); };
$("pickx").onclick = () => { $("pick").hidden = true; };
$("pick").onclick = e => { if (e.target.id === "pick") $("pick").hidden = true; };

// ---- clicks
function saveNote(k){ const ta = document.getElementById("wln-"+k), it = getIt(k); if (ta && ta.value !== it.notes) wlPatch([patchOne(k, {notes:ta.value})]); }
document.addEventListener("click", e => {
  const b = e.target.closest("button.wlb");
  if (b) { e.stopPropagation(); openPick(b.dataset.wl); return; }
  const tk = e.target.closest("[data-tk]");
  if (tk) { const k = tk.dataset.tk; wlOpen.add(k); wlForce = wlVisible.includes(k) ? "" : k; $("wlq").value = ""; wlTags.clear(); showView("wl");
    const row = document.querySelector(`#wlbody tr[data-wk="${CSS.escape(k)}"]`); if (row) row.scrollIntoView({block:"center"}); }
}, true);
const modeClick = e => { const b = e.target.closest("[data-wmode]"); if (!b) return; const g = b.dataset.wmode;
  GS.on.has(g) ? GS.on.delete(g) : GS.on.add(g); saveMode(); wlSel.clear(); wlForce = ""; wlLimit = 100; wlRefresh(); };
$("wlmodeF").onclick = modeClick; $("wlmodeM").onclick = modeClick;
const tagClick = e => { const b = e.target.closest("[data-wtf]"); if (!b) return; const t = b.dataset.wtf; wlTags.has(t) ? wlTags.delete(t) : wlTags.add(t); wlLimit = 100; renderWl(); };
$("wltagsA").onclick = tagClick; $("wltagsM").onclick = tagClick;
$("wlbody").addEventListener("click", e => {
  let b;
  if ((b = e.target.closest("input[data-sel]"))) { e.stopPropagation(); const k = b.dataset.sel; b.checked ? wlSel.add(k) : wlSel.delete(k); renderWl(); return; }
  if ((b = e.target.closest("[data-wlist]"))) { saveNote(b.dataset.k); togglePick(b.dataset.k, +b.dataset.wlist); return; }
  if ((b = e.target.closest("[data-wtag]"))) { const k = b.dataset.k, t = b.dataset.wtag, cur = getIt(k).tags;
    saveNote(k); wlPatch([patchOne(k, {tags: cur.includes(t) ? cur.filter(x => x !== t) : cur.concat(t)})]); return; }
  if ((b = e.target.closest("[data-wnewtag]"))) { const k = b.dataset.wnewtag, inp = document.getElementById("wlnt-"+k);
    const t = (inp.value || "").replace(/[,]/g, " ").trim().replace(/\s+/g, " "); if (!t) return;
    const cur = getIt(k).tags; saveNote(k); if (!cur.includes(t)) wlPatch([patchOne(k, {tags:cur.concat(t)})]); return; }
  if ((b = e.target.closest("[data-wsave]"))) { saveNote(b.dataset.wsave); b.textContent = "✓ Saved"; return; }
  const tr = e.target.closest("tr.r");
  if (tr) { const k = tr.dataset.wk; wlOpen.has(k) ? wlOpen.delete(k) : wlOpen.add(k); if (wlOpen.has(k)) loadHist(k); renderWl(); }
});
$("wlbody").addEventListener("focusout", e => {
  if (e.target.matches("textarea[data-k]")) saveNote(e.target.dataset.k);
  setTimeout(() => { if (wlDirty && !(document.activeElement && document.activeElement.closest && document.activeElement.closest("#wlbody textarea, #wlbody input[type=text], #wlbody input:not([type])"))) renderWl(); }, 0);
});
$("wlq").addEventListener("input", () => { wlLimit = 100; renderWl(); });
$("wlhead").addEventListener("change", e => { if (e.target.id !== "wlper") return;
  WPER = e.target.value; store.set("wl_per", WPER); wlRefresh(); });
$("wlmore").onclick = () => { wlLimit += 100; renderWl(); };

// ---- Google Sheet connection
$("wlconnect").onclick = () => {
  const url = $("wlurl").value.trim(), key = $("wlkey").value.trim();
  if (!/^https:\/\/script\.google(usercontent)?\.com\//.test(url)) { wlStatus("⚠ URL https://script.google.com/macros/s/…/exec jaisa hona chahiye"); return; }
  store.set(WLK.api, JSON.stringify({url, key}));
  if (WL.size) { wlQueue.unshift({op:"patch", items:[...WL.values()].map(i => patchOne(i.k, {notes:i.notes, tags:i.tags, lists:i.lists, oldDel:i.oldDel}))}); wlSaveQueue(); }  // copy this browser's lists into the Sheet
  wlErr = ""; wlSync();
  Object.values(SD).forEach(s => { s.at = 0; s.err = ""; });
};
$("wldisc").onclick = () => { if (confirm("Sheet se connection hatana hai? Watchlist ki copy is browser mein rahegi.")) { store.set(WLK.api, ""); wlQueue = []; wlSaveQueue(); wlErr = "";
  ["briefs", "tg", "hold"].forEach(n => { store.set("sd_" + n, ""); delete SD[n]; }); wlRefresh(); } };
$("wlexport").onclick = () => {
  const q = v => `"${String(v??"").replace(/"/g,'""')}"`;
  const lines = [["Rank","Symbol","Exchange","Name","Lists","Score","Close","Chg_1D",`Chg_${perLabel().replace(/ /g,"_")}`,"Auto_tags","My_tags","Notes"].join(",")].concat(wlFiltered(wlRanks()).map(k => {
    const it = getIt(k), s = BYK.get(k), lab = [...FIXED.filter(([id]) => FSET[id].has(k)).map(x => x[1]), ...it.lists.map(n => "Watchlist " + n), ...(oldOf(k).length ? ["Hataye gaye"] : [])];
    return [RANK.get(k)||"", symOf(k), it.ex, s ? s.n : it.name, lab.join("; "), s && s.ms, s && px(s).c, s && px(s).d1, s && chgP(s), autoOf(s).join("; "), it.tags.join("; "), it.notes].map(q).join(","); }));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], {type:"text/csv"}));
  a.download = `watchlist_${M.asOf.replace(/ /g,"")}.csv`; a.click();
};

function wlInit(){
  const c = jget(WLK.cache, null);
  if (c && c.items) { WL = new Map(c.items.map(i => [i.k, norm(i)])); wlSynced = c.synced || null; }
  wlQueue = jget(WLK.queue, []);
  loadMode();
  const a = wlApi();
  if (a) { $("wlurl").value = a.url || ""; $("wlkey").value = a.key || ""; }
  saveRankHistory();
  wlRefresh(); wlSync();
  window.addEventListener("online", () => wlSync());
}

// ---------- Aaj + Telegram tabs. Their data comes only from your Google Sheet through the Apps Script
// (actions "briefs", "telegram", "holdings"), never from this public repo. Same URL + key as the Watchlist connection.
const istToday = () => new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10);
const isoAdd = (iso, n) => new Date(Date.parse(iso + "T00:00:00Z") + n * 864e5).toISOString().slice(0, 10);
const safeUrl = u => /^https:\/\//i.test(u || "") ? u : "";
const SD = {};
const sdCache = n => jget("sd_" + n, null);
async function sheetGet(action){
  const a = wlApi();
  if (!a || !a.url) throw new Error("NOSHEET");
  const r = await fetch(a.url + (a.url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(a.key || "") + "&action=" + action);
  let j; try { j = await r.json(); } catch(e) { throw new Error("Web app ka jawab samajh nahi aaya. Deploy mein 'Who has access: Anyone' chuna hai?"); }
  if (!j.ok) throw new Error(j.error || "Sheet ne error diya");
  if ((j.version || 1) < 4) throw new Error("Sheet ka Apps Script purana hai: naya Code.gs paste karke New version deploy karo");
  return j;
}
function sdState(n){ return SD[n] || (SD[n] = {data: sdCache(n), err: "", busy: false, at: 0}); }
async function sdLoad(n, action, force){
  const s = sdState(n);
  if (s.busy || (!force && s.at && Date.now() - s.at < 120e3)) return s;
  s.busy = true;
  try { s.data = await sheetGet(action); store.set("sd_" + n, JSON.stringify(s.data)); s.err = ""; s.at = Date.now(); }
  catch(e) { const m = (e && e.message) || ""; s.err = m === "NOSHEET" ? "NOSHEET" : /fetch|network|load failed/i.test(m) ? "offline" : m; }
  s.busy = false;
  return s;
}
function sdMsg(s, empty){
  if (s.err === "NOSHEET") return `<div class="connect"><b>Google Sheet abhi connect nahi hai.</b> Ye data sirf aapki Google Sheet se aata hai.
    <ol><li><b>Watchlist</b> tab kholo → neeche <b>Google Sheet connection</b> dabao.</li><li>Web app URL aur secret key daalo → <b>Connect karo</b>. (Ek hi baar karna hai.)</li></ol>
    <div class="row"><button class="btn" data-goconn="1">Google Sheet connection kholo</button></div></div>`;
  if (!s.data && s.busy) return `<div class="na">Sheet se load ho raha hai…</div>`;
  if (!s.data && s.err) return `<div class="err">${s.err === "offline" ? "Internet nahi hai, aur is phone mein koi purani copy nahi." : "⚠ " + esc(s.err)}</div>`;
  return `<div class="na">${empty}</div>`;
}
const sdNote = s => s.busy ? "load ho raha hai…" : s.err === "offline" ? "offline · purani copy" : s.err && s.err !== "NOSHEET" ? "⚠ " + esc(s.err) : "";
document.addEventListener("click", e => {
  if (!e.target.closest("[data-goconn]")) return;
  showView("wl"); $("wlsetup").open = true; $("wlsetup").scrollIntoView({block:"center"}); $("wlurl").focus();
});
function fmtTime(t){
  t = String(t || "");
  const d = t.slice(0, 10), hm = t.slice(11, 16);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return esc(t);
  const today = istToday();
  return (d === today ? "Aaj" : d === isoAdd(today, -1) ? "Kal" : fdIso(d).replace(/ \d{4}$/, "")) + (hm ? " " + hm : "");
}

// ---- tiny Markdown renderer for the briefs: everything is escaped first, links must be http(s)
function mdEm(s){
  return s.replace(/\*\*(?=\S)(.+?)\*\*/g, "<b>$1</b>").replace(/__(?=\S)(.+?)__/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*(?=\S)([^*]+?)\*(?!\w)/g, "$1<i>$2</i>").replace(/~~(?=\S)(.+?)~~/g, "<s>$1</s>");
}
function mdInline(s){
  const keep = [], hold = h => "\u0001" + (keep.push(h) - 1) + "\u0002";
  const a = (u, x) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${x}</a>`;
  s = String(s).replace(/`([^`]+)`/g, (_, c) => hold(`<code>${esc(c)}</code>`))
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, x, u) => hold(a(u, mdEm(esc(x)))))
    .replace(/https?:\/\/[^\s<>"]*[^\s<>".,;:!?)\]]/g, u => hold(a(u, esc(u))));
  return mdEm(esc(s)).replace(/\u0001(\d+)\u0002/g, (_, i) => keep[+i]);
}
function md(src){
  const L = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  const LI = /^(\s*)([-*+•]|\d+[.)])\s+(.*)$/;
  let out = "", i = 0, para = [], m;
  const flush = () => { if (para.length) { out += `<p>${para.map(mdInline).join("<br>")}</p>`; para = []; } };
  const cells = l => l.trim().replace(/\\\|/g, "\u0003").replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim().replace(/\u0003/g, "|"));
  const isSep = l => l.includes("|") && l.includes("-") && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(l);
  while (i < L.length) {
    const l = L[i];
    if (/^\s*```/.test(l)) { flush(); const code = []; i++; while (i < L.length && !/^\s*```/.test(L[i])) code.push(L[i++]); i++;
      out += `<pre><code>${esc(code.join("\n"))}</code></pre>`; continue; }
    if (!l.trim()) { flush(); i++; continue; }
    if ((m = l.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) { flush(); const n = Math.min(m[1].length, 4); out += `<h${n}>${mdInline(m[2])}</h${n}>`; i++; continue; }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(l)) { flush(); out += "<hr>"; i++; continue; }
    if (l.includes("|") && i + 1 < L.length && isSep(L[i + 1])) {
      flush();
      const head = cells(l), al = cells(L[i + 1]).map(c => /^:-+:$/.test(c) ? "center" : /-:$/.test(c) ? "right" : "");
      const rows = []; i += 2;
      while (i < L.length && L[i].includes("|") && L[i].trim()) rows.push(cells(L[i++]));
      const td = (tag, c, j) => `<${tag}${al[j] ? ` style="text-align:${al[j]}"` : ""}>${mdInline(c || "")}</${tag}>`;
      out += `<div class="mdt"><table><thead><tr>${head.map((c, j) => td("th", c, j)).join("")}</tr></thead><tbody>`
        + rows.map(r => `<tr>${head.map((_, j) => td("td", r[j], j)).join("")}</tr>`).join("") + `</tbody></table></div>`;
      continue;
    }
    if (/^\s*>/.test(l)) { flush(); const q = []; while (i < L.length && /^\s*>/.test(L[i])) q.push(L[i++].replace(/^\s*>\s?/, ""));
      out += `<blockquote>${md(q.join("\n"))}</blockquote>`; continue; }
    if ((m = l.match(LI))) {
      flush();
      const ol = /\d/.test(m[2]), start = ol ? parseInt(m[2], 10) : 1, items = [];
      while (i < L.length && (m = L[i].match(LI)) && /\d/.test(m[2]) === ol) {
        items.push([m[1].length, m[3]]); i++;
        while (i < L.length && L[i].trim() && /^\s{2,}/.test(L[i]) && !LI.test(L[i])) items[items.length - 1][1] += " " + L[i++].trim();
      }
      const tag = ol ? "ol" : "ul";
      out += `<${tag}${ol && start !== 1 ? ` start="${start}"` : ""}>${items.map(([ind, x]) => `<li${ind >= 2 ? ' class="sub"' : ""}>${mdInline(x)}</li>`).join("")}</${tag}>`;
      continue;
    }
    para.push(l.trim()); i++;
  }
  flush();
  return out;
}

// ---- Aaj tab: latest Trading Brief from the "Briefs" tab of the Sheet
let aajIdx = 0;
const briefLabel = b => [b.date, b.type].filter(Boolean).join(" · ");
function vcls(v){
  v = String(v || "");
  if (/🟢|✅|📈/u.test(v)) return "up";
  if (/🔴|❌|📉|⛔/u.test(v)) return "dn";
  if (/🟡|🟠|⚠/u.test(v)) return "mid";
  if (/bull|buy|positive|risk.?on|strong|aggressive|tezi/i.test(v)) return "up";
  if (/bear|sell|avoid|negative|risk.?off|weak|defensive|mandi|no.?trade/i.test(v)) return "dn";
  return "mid";
}
async function aajShow(force){
  const p = sdLoad("briefs", "briefs", force);
  aajRender(); await p; aajRender();
}
function aajRender(){
  const s = sdState("briefs"), list = (s.data && s.data.briefs) || [], stt = (s.data && s.data.status) || {};
  if (aajIdx >= list.length) aajIdx = 0;
  $("aajsel").hidden = list.length < 2;
  $("aajsel").innerHTML = list.map((b, i) => `<option value="${i}">${i ? "" : "Latest · "}${esc(briefLabel(b) || "Brief " + (i + 1))}</option>`).join("");
  $("aajsel").value = String(aajIdx);
  const note = sdNote(s);
  $("aajstat").innerHTML = `<b>System status</b> · Prices: ${esc(M.generated)} · Brief: ${esc(stt.brief || "—")} · Telegram: ${stt.telegram ? fmtTime(stt.telegram) : "—"}${note ? " · " + note : ""}`;
  const b = list[aajIdx];
  $("aajbox").innerHTML = b
    ? `<div class="panel"><div class="verdict ${vcls(b.verdict)}">${mdInline(b.verdict || "—")}</div><div class="sub">${esc(briefLabel(b))}</div><div class="md">${md(b.md)}</div></div>`
    : `<div class="panel">${sdMsg(s, "Sheet ke <b>Briefs</b> tab mein abhi koi brief nahi hai.")}</div>`;
}
$("aajsel").addEventListener("input", e => { aajIdx = +e.target.value || 0; aajRender(); window.scrollTo(0, 0); });
$("aajref").onclick = () => aajShow(true);

// ---- Telegram tab: posts from the "Telegram" tab of the Sheet (filled by the Telegram bot), last 7 days
const TGCH = [["Broker report", "Broker report"], ["Sector report", "Sector report"], ["Result", "Result"], ["hold", "Meri holdings"]];
const TGTY = {"Broker report": "b", "Sector report": "s", "Result": "r"};
const tgF = new Set(jget("tg_f", []).filter(f => TGCH.some(c => c[0] === f)));
let tgQs = "", tgMore = false;
const tNum = v => { const n = parseFloat(String(v == null ? "" : v).replace(/[₹,\s]/g, "").replace(/^rs\.?/i, "")); return n > 0 ? n : null; };
const rcls = r => /buy|accumulate|\badd\b|outperform|overweight/i.test(r) ? "up" : /sell|reduce|underperform|underweight/i.test(r) ? "dn" : "";
function tgStocks(t){
  return String(t || "").split(/[,;|\n]+/).map(x => x.trim()).filter(Boolean).slice(0, 12).map(x => ({t: x, s: resolveToken(x)}));
}
async function tgShow(force){
  const p = Promise.all([sdLoad("tg", "telegram", force), sdLoad("hold", "holdings", force)]);
  tgRender(); await p; tgRender();
}
function tgItem(it){
  const tl = safeUrl(it.link), fl = safeUrl(it.file), tgt = tNum(it.target);
  const one = it.stk.length === 1;
  const stk = it.stk.map(x => {
    if (!x.s) return `<span class="tg">${esc(x.t)}</span>`;
    const c = px(x.s).c, up = tgt && one && c ? (tgt / c - 1) * 100 : null;
    return `<button class="tg" data-tk="${esc(x.s.k)}" title="Stock ki details"><b>${esc(x.s.s)}</b>${scoreChip(x.s.ms,x.s)}${up != null
      ? `<span style="color:${chgCol(up)};font-weight:600">${up > 0 ? "+" : ""}${fmt(up, 1)}% upside</span>` : ""}</button>`;
  }).join("");
  const a = (u, cls, txt) => `<a class="${cls}" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${txt}</a>`;
  return `<div class="tgi${it.highlight ? " hl" : ""}"${tl ? ` data-tgl="${esc(tl)}"` : ""}>`
    + `<div class="th"><span class="tgty ${TGTY[it.type] || "o"}">${esc(it.type || "Other")}</span>${it.house ? `<b>${esc(it.house)}</b>` : ""}`
    + `${it.rating ? `<span class="rate ${rcls(it.rating)}">${esc(it.rating)}</span>` : ""}${tgt ? `<span>Target ₹${fmt(tgt, tgt >= 100 ? 0 : 2)}</span>` : ""}`
    + `${it.mine ? `<span class="chip hold">Holding</span>` : ""}<span class="tt">${fmtTime(it.time)}${it.channel ? " · " + esc(it.channel) : ""}</span></div>`
    + (tl ? a(tl, "hd", esc(it.headline || "Telegram post")) : `<div class="hd">${esc(it.headline || "Telegram post")}</div>`)
    + (it.text ? `<div class="tx">${esc(it.text)}</div>` : "")
    + (stk ? `<div class="stk">${stk}</div>` : "")
    + `<div class="acts">${tl ? a(tl, "btn sm", "Telegram mein kholo ↗") : ""}${fl ? a(fl, "btn sm", "📄 File") : ""}</div></div>`;
}
function tgRender(){
  const s = sdState("tg"), h = sdState("hold");
  const mineSet = new Set((((h.data && h.data.holdings) || []).map(x => (resolveToken(x.symbol) || {}).k)).filter(Boolean));
  const from = isoAdd(istToday(), -6);
  const all = ((s.data && s.data.telegram) || []).filter(it => String(it.time || "").slice(0, 10) >= from).map(it => {
    const stk = tgStocks(it.stocks);
    return Object.assign({}, it, {stk, mine: !!it.holding || stk.some(x => x.s && (HELD.has(x.s.k) || mineSet.has(x.s.k)))});
  });
  const q = tgQs.trim().toUpperCase();
  const qOk = it => !q || [it.headline, it.text, it.stocks, it.house, it.channel, it.rating].some(v => String(v || "").toUpperCase().includes(q))
    || it.stk.some(x => x.s && (x.s.s.toUpperCase().includes(q) || x.s.n.toUpperCase().includes(q)));
  const base = all.filter(qOk);
  const types = [...tgF].filter(f => f !== "hold");
  const list = base.filter(it => (!types.length || types.includes(it.type)) && (!tgF.has("hold") || it.mine));
  const cnt = f => base.filter(it => f === "hold" ? it.mine : it.type === f).length;
  $("tgchips").innerHTML = TGCH.map(([f, t]) => `<button class="tg${tgF.has(f) ? " on" : ""}" data-tgf="${esc(f)}">${esc(t)} <small>${cnt(f)}</small></button>`).join("");
  const hl = list.filter(it => it.highlight), rest = list.filter(it => !it.highlight);
  let html = "";
  if (!all.length) html = `<div class="panel">${sdMsg(s, "Pichhle 7 din mein Telegram tab mein koi post nahi.")}</div>`;
  else if (!list.length) html = `<div class="panel na">Is filter mein koi post nahi. Chip hatao ya search khali karo.</div>`;
  else {
    html = hl.length ? hl.map(tgItem).join("") : `<div class="panel na">Koi highlighted post nahi. Baaki posts neeche hain.</div>`;
    if (rest.length) html += `<details class="more" id="tgmore"${tgMore ? " open" : ""}><summary>Baaki posts (${rest.length})</summary>${rest.map(tgItem).join("")}</details>`;
  }
  $("tglist").innerHTML = html;
  const tm = $("tgmore"); if (tm) tm.ontoggle = () => { tgMore = tm.open; };
  const note = sdNote(s);
  $("tgsync").innerHTML = (s.data && s.data.updated ? "Last post: " + fmtTime(s.data.updated) : "") + (note ? " · " + note : "");
  $("tgfoot").innerHTML = `Sirf pichhle 7 din ki posts (Sheet mein 30 din rakhi jaati hain). Data sirf aapki Google Sheet ke <b>Telegram</b> tab se aata hai, public repo mein nahi. Upside = target ÷ latest close (${esc(M.asOf)}), sirf jab post mein ek hi stock ho. Score = Momentum Score. Card pe tap = Telegram post. Not investment advice.`;
}
$("tgchips").onclick = e => { const b = e.target.closest("[data-tgf]"); if (!b) return; const f = b.dataset.tgf;
  tgF.has(f) ? tgF.delete(f) : tgF.add(f); store.set("tg_f", JSON.stringify([...tgF])); tgRender(); };
$("tgq").addEventListener("input", e => { tgQs = e.target.value; tgRender(); });
$("tgref").onclick = () => tgShow(true);
$("tglist").addEventListener("click", e => {
  if (e.target.closest("a,button,summary,input,select")) return;
  const c = e.target.closest(".tgi[data-tgl]"); if (c && !String(window.getSelection() || "")) window.open(c.dataset.tgl, "_blank", "noopener");
});

function showView(v){
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.v===v));
  ["aaj","tg","scan","wl","pf"].forEach(x => { $("v-" + x).hidden = v !== x; });
  if (v==="wl") renderWl();
  if (v==="aaj") aajShow();
  if (v==="tg") tgShow();
}
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => showView(b.dataset.v));
$("foot").innerHTML = `Source: NSE &amp; BSE end-of-day bhavcopy files (${M.sessions} sessions since ${esc(M.historyFrom)}) and NSE index lists. Prices adjusted for splits and bonuses. Stocks listed on both exchanges are shown once, with NSE data. Charts: TradingView Lightweight Charts. BSE: ${esc(M.bse||"—")}. Not investment advice.`;
wlInit();
render();
showView("aaj");
$("fsclose").onclick = closeFull;
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("fs").hidden) closeFull(); });
loadPortfolio().then(() => { if (!$("v-tg").hidden) tgRender(); if (location.hash.startsWith("#c=")) openFull(decodeURIComponent(location.hash.slice(3))); });
if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
  navigator.serviceWorker.register("sw.js").catch(()=>{});
}
</script></body></html>
"""

if __name__ == "__main__":
    main()
