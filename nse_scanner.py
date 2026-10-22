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
    raw = get_raw("bse", latest_day)
    if not raw:
        bse_status = f"unavailable ({last_status.get('bse', 'not found')})"
        print(f"  BSE file for {latest_day:%d %b %Y} {bse_status} — continuing with NSE only")
    else:
        today_bse = parse(raw, "bse")
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

    # ---- analytics
    print("Calculating…")
    stocks, adj_log, charts = [], [], {}
    sess_idx = {d: i for i, d in enumerate(sessions)}
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
            if ex == "nse":
                seg = "NSE SME" if r["series"] in NSE_SME else "NSE"
            else:
                seg = "BSE SME" if r["series"] in BSE_SME else "BSE"
            stocks.append({
                "s": r["sym"], "n": r["name"], "seg": seg, "sr": r["series"], "k": cid, "i": r["isin"],
                "x": "B" if ex == "bse" else "N", "code": r["code"] if ex == "bse" else "",
                "sec": sector.get(r["sym"], "") if ex == "nse" else "",
                "c": r["c"], "v": int(r["vol"]), "tv": round(r["val"] / 1e7, 2),
                **a,
            })

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
    }

    html = TEMPLATE.replace("/*__DATA__*/", json.dumps(
        {"meta": meta, "stocks": stocks, "indices": index_members},
        separators=(",", ":")))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    write_app_files(os.path.dirname(OUT))
    write_charts(os.path.dirname(OUT), charts)
    write_csv(stocks, index_members, meta)
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
                      "IPO_Sessions", "IPO_Breakout", "Volume", "Value_Cr", "Indices", "ISIN"])
        for s in stocks:
            dv = s.get("dv") or {}
            w.writerow([s["s"], "BSE" if s["x"] == "B" else "NSE", s["code"], s["n"], s["seg"],
                        s["sr"], s["sec"], s["c"]]
                       + [s["ch"].get(p, "") for p in periods]
                       + [s["h52"], s["f52"], s["nh"], s["vx"],
                          dv.get("st", ""), dv.get("t", ""), dv.get("b", ""), dv.get("d", ""),
                          s["ipo"] or "", s["ib"], s["v"], s["tv"],
                          "; ".join(idx.get(s["s"], [])) if s["x"] == "N" else "", s["i"]])
    with open(os.path.join(DATA_DIR, "meta_full.json"), "w") as f:
        json.dump(meta, f, indent=1)


# ---------------------------------------------------------------- app files

ICON_192 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAENklEQVR4nO3dS27bSABF0XIj+1BmXo57mt14N5l6Oe6Zs5MeCBCESLJpPrJ+PGeUODJFoC6qyNInT6eX9wJr/dP6BBibgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAhoYKfX361PQUDDOtfTvCEBDem6m7YNCWg8t8U0bEhAg7nbyp/XX/XP5ExAw2tYTxHQWG6nn7b1FAENpMN6ioBG0fx2/REBDaC3C+drAupdz/UUAXWu83qKgHrWfz1FQGPprZ4ioG71edN+S0A9GqWeIqAOdbvlc9fT6eW99TmwMpoepiUBtRdOOV5MPbR8wfKGMgYmICICIiIgIgJqaawtn7sE1MwE9RQBtTJHPUVATUxTTxFQfTPVUwRU2WT1FAERElA9A73LZzkBVTJlPUVAdcx36XMhoN0N8eGK1QS0r7nrKQLa1fT1lFJ+tD6BjV2PWduhOkI9ZbKA/hqzy1/rD9tB6inTBPT5bc75X9uO35T1lDkCWniTXG1CmnXL567hL6JXbLGcXn/vtzFzqHrK0DNQGMEeE9LEG4aPjBrQJ0N1CaLy0nacC+drQy5hS+o5//lb45csbcesp4w4Ay2s5/aH+01Ih62njBXQd9O5+5jlc8zCko5cTxkooLCe2wd/t6Tlz3KcesooAW1Vz91fTJa2o9203+o9oM9Hd5PRWre0/Xn9pZ7SeUB7TDxfHvC7E9LB9RvQl/V8vD2vOOzPf//7/AErLpL++sVD6TGgJcvWunouv/hlRuWbS9sx6ykdBrRk2Vpdz8XH2/OShsqyCemw9ZTedqJ7HqRH+9rNT6ytXmagCndbm7iekPo5q4a6CKjnieeRbk+ssvZL2Ij1cNE4IPWMrtkSJp05tJmB1DONBgGpZyZVl7BR7tVZrl5AJp4p1VvCHlWinqFVXcJu30PTfz07veY/jWa38f2nU6q85j+62ndh527mrmfDI/SvwW38EPWwUPvXwhiagIgIiIiAiAiIiICICIiIgIjs8lLG6h3YI+z9T2b7GSjZv/94ez7C9v9MNg5ok+HX0EBcAxEREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEdk4oE0+2PXlQfJnWXKEOs+Sf91W2y/sMgMReTq9vG9+0DqfTK3z/al1nmXdf+Hbw5cF7hIQx2EJIyIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAi/wMGelvdl+PwXwAAAABJRU5ErkJggg=="
ICON_512 = "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"
MANIFEST = '{\n  "name": "NSE Stock Scanner",\n  "short_name": "NSE Scanner",\n  "start_url": "./",\n  "scope": "./",\n  "display": "standalone",\n  "background_color": "#f6f7f9",\n  "theme_color": "#1f4fd8",\n  "icons": [\n    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},\n    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},\n    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}\n  ]\n}\n'
SERVICE_WORKER = '// Network-first: always try to get today\'s data, fall back to the last copy when offline.\nconst CACHE = "nse-scanner-v2";\nself.addEventListener("install", e => self.skipWaiting());\nself.addEventListener("activate", e => e.waitUntil(self.clients.claim()));\nself.addEventListener("fetch", e => {\n  if (e.request.method !== "GET") return;\n  e.respondWith(\n    fetch(e.request, {cache: "no-store"})\n      .then(res => { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return res; })\n      .catch(() => caches.match(e.request))\n  );\n});\n'


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
.chart{height:320px;width:100%}
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
@media (max-width:700px){td.sym{white-space:normal;min-width:0;width:100%} td.sym small{max-width:none;white-space:normal} .hide-m{display:none} th,td{padding:8px 6px} .pill{min-width:0} .dgrid{grid-template-columns:1fr 1fr} .tag,.chip{display:inline-block;margin-top:2px} .chart{height:260px} .stat b{font-size:16px} .stat{min-width:96px}}
</style></head><body>
<header><h1>Stock Scanner</h1>
<div class="sub" id="sub"></div>
<nav class="tabs"><button data-v="scan" class="on">Scanner</button><button data-v="pf">Mera Portfolio</button></nav></header>
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
<div class="wrap" id="v-scan">
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
const DEF = {period:"1D", setup:"all", uni:"ALL", sec:"", dir:"all", sort:"chg", asc:false, q:"", minp:null, maxp:null, minv:null, minc:null, minx:null, max52:null, page:0};
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
[["chg","% change"],["f52","Closest to 52W high"],["vx","Volume × average"],["c","Price"],["tv","Traded value"],["sym","Symbol A–Z"]]
  .forEach(([v,t])=>$("sortsel").add(new Option(t,v)));

const COLS = [
  {k:"sym", t:"Stock", cls:"l"},
  {k:"c", t:"Close ₹"},
  {k:"chg", t:"% Chg"},
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
let lwcPromise = null, pending = [], live = {scan:[], pf:[]}, chartSeq = 0;
const chartData = new Map();
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
function chartHtml(s, ctx, view){
  const id = "ch" + (++chartSeq);
  pending.push({id, s, ctx, view: view || "scan"});
  const leg = [`<span><i style="border-color:${"#2962ff"}"></i>Darvas box</span>`, `<span><i class="dash" style="border-color:#e08a00"></i>52W high</span>`];
  if (ctx && ctx.avg) leg.push(`<span><i class="dash" style="border-color:#888"></i>Avg</span>`, `<span><i class="dash" style="border-color:#e53935"></i>Stop</span>`, `<span><i class="dash" style="border-color:#2e9e5b"></i>Target</span>`);
  return `<div class="chartbox"><div class="chart" id="${id}"><div class="hint" style="padding:12px">Chart load ho raha hai…</div></div>
    <div class="chartbar"><span>${leg.join(" ")}</span><a href="${tvUrl(s)}" target="_blank" rel="noopener">TradingView par kholo ↗</a></div></div>`;
}
function mountCharts(){
  const jobs = pending; pending = [];
  jobs.forEach(job => {
    Promise.all([loadLib(), getCandles(job.s.k)]).then(([, data]) => {
      const el = document.getElementById(job.id);
      if (!el) return;
      el.innerHTML = "";
      live[job.view].push(drawChart(el, job.s, data, job.ctx));
    }).catch(e => { const el = document.getElementById(job.id); if (el) el.innerHTML = `<div class="err" style="padding:12px">${esc(e.message)}</div>`; });
  });
}
function clearCharts(view){ live[view].forEach(c => { try { c.remove(); } catch(e){} }); live[view] = []; }
function drawChart(el, s, D2, ctx){
  const L = window.LightweightCharts, line = css("--line"), mute = css("--mute");
  const up = css("--up"), dn = css("--dn");
  const chart = L.createChart(el, {autoSize:true, layout:{background:{type:"solid", color:css("--card")}, textColor:mute, fontSize:11},
    grid:{vertLines:{color:line}, horzLines:{color:line}}, rightPriceScale:{borderColor:line}, timeScale:{borderColor:line},
    crosshair:{mode:0}, handleScroll:true, handleScale:true,
    localization:{locale:"en-IN", dateFormat:"dd MMM 'yy", priceFormatter:p => Number(p).toFixed(2)}});
  const n = D2.d.length;
  const cs = chart.addCandlestickSeries({upColor:up, downColor:dn, wickUpColor:up, wickDownColor:dn, borderVisible:false, priceLineVisible:false});
  cs.setData(D2.d.map((t,i) => ({time:dayStr(t), open:D2.o[i], high:D2.h[i], low:D2.l[i], close:D2.c[i]})));
  cs.priceScale().applyOptions({scaleMargins:{top:0.1, bottom:0.24}});
  const vs = chart.addHistogramSeries({priceFormat:{type:"volume"}, priceScaleId:"", lastValueVisible:false, priceLineVisible:false});
  vs.priceScale().applyOptions({scaleMargins:{top:0.8, bottom:0}});
  vs.setData(D2.d.map((t,i) => ({time:dayStr(t), value:D2.v[i], color:(D2.c[i]>=D2.o[i]?up:dn)+"66"})));
  const pl = (price, color, title, style=2) => price && cs.createPriceLine({price, color, lineWidth:1, lineStyle:style, axisLabelVisible:true, title});
  pl(s.h52, "#e08a00", "52W high");
  const dv = s.dv;
  if (dv && (dv.st==="box" || dv.st==="bo") && dv.s!=null) {
    const a = Math.max(0, n-1-dv.s), b = dv.st==="bo" ? n-2 : n-1;
    if (b > a) [dv.t, dv.b].forEach(p => {
      const ls = chart.addLineSeries({color:"#2962ff", lineWidth:2, lastValueVisible:false, priceLineVisible:false, crosshairMarkerVisible:false});
      ls.setData([{time:dayStr(D2.d[a]), value:p}, {time:dayStr(D2.d[b]), value:p}]);
    });
  }
  if (ctx && ctx.avg) {
    pl(ctx.avg, "#888888", "Avg"); pl(ctx.avg*0.9, "#e53935", "Stop"); pl(ctx.avg*1.2, "#2e9e5b", "Target");
    const lo = ctx.avg*0.9, hi = ctx.avg*1.2;
    cs.applyOptions({autoscaleInfoProvider: orig => { const r = orig(); if (r && r.priceRange) {
      r.priceRange.minValue = Math.min(r.priceRange.minValue, lo); r.priceRange.maxValue = Math.max(r.priceRange.maxValue, hi); } return r; }});
  }
  chart.timeScale().setVisibleLogicalRange({from:Math.max(0, n-130), to:n+3});
  return chart;
}

function detail(s, ctx, cols, view){
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
  if (s.ipo!=null) add("Listed", `${s.ipo} sessions ago${s.ib?" · at high since listing":""}`);
  const ch = M.periods.map(([k])=>`${k} ${sgn(s.ch[k])}`).join(" · ");
  add("Returns", ch);
  if (memberOf[s.s] && s.x==="N") add("Indices", esc(memberOf[s.s].join(", ")));
  return `<tr class="det"><td colspan="${cols||COLS.length}"><div class="dgrid">${items.join("")}</div>${chartHtml(s, ctx, view)}</td></tr>`;
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
    const main = `<tr class="r" data-id="${esc(id)}"><td class="l sym"><b>${esc(s.s)}</b>${tags}<small>${esc(s.n)}</small></td>
      <td>${fmt(s.c)}</td>
      <td><span class="pill ${cls}">${sgn(c)}</span></td>
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
bind("setup","setup",e=>e.value); bind("universe","uni",e=>e.value); bind("sector","sec",e=>e.value);
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
function showView(v){
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.v===v));
  $("v-scan").hidden = v!=="scan"; $("v-pf").hidden = v!=="pf";
  store.set("view", v);
}
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => showView(b.dataset.v));
$("foot").innerHTML = `Source: NSE &amp; BSE end-of-day bhavcopy files (${M.sessions} sessions since ${esc(M.historyFrom)}) and NSE index lists. Prices adjusted for splits and bonuses. Stocks listed on both exchanges are shown once, with NSE data. Charts: TradingView Lightweight Charts. BSE: ${esc(M.bse||"—")}. Not investment advice.`;
render();
showView(store.get("view")==="pf" ? "pf" : "scan");
loadPortfolio();
if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
  navigator.serviceWorker.register("sw.js").catch(()=>{});
}
</script></body></html>
"""

if __name__ == "__main__":
    main()
