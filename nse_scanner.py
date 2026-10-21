#!/usr/bin/env python3
"""
NSE Stock Scanner  —  all NSE-listed stocks (Mainboard + SME)
-------------------------------------------------------------
Downloads NSE's official end-of-day bhavcopy files and index constituent
lists, then builds an interactive scanner page (scanner.html) with:

  * Price change for 1 Day / 1 Week / 1 Month / 3 Months / 6 Months / 1 Year
  * Sort by any column (click the header), gainers / losers
  * Universe filter: All, Mainboard, SME, and Nifty indices
    (Nifty 50, Next 50, 100, 200, 500, Midcap 150, Smallcap 250,
     Microcap 250, Bank, IT, Pharma, Auto, FMCG, Metal, Realty, Energy …)
  * Sector filter, symbol search, min price / min traded value filters
  * Export the current view to CSV

Usage:
    pip install requests
    python nse_scanner.py            # uses the latest available trading day
    python nse_scanner.py 2026-10-21 # scan as of a specific date

Run after ~7 PM IST on a trading day (NSE publishes the bhavcopy after close).
Downloads are cached in ./nse_cache so re-runs are fast.

Note: returns are based on raw closing prices (not adjusted for splits/bonuses).
A stock that split or issued a bonus in the period will show a false big fall —
these are flagged with a ⚠ in the scanner when the move looks like a corporate action.
"""

import csv, io, json, os, sys, time, zipfile, webbrowser
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("Please install requests first:  pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("NSE_CACHE") or os.path.join(HERE, "nse_cache")
OUT_DIR = os.environ.get("OUT_DIR")          # set by the GitHub workflow
OUT = os.path.join(OUT_DIR, "index.html") if OUT_DIR else os.path.join(HERE, "scanner.html")
IN_CI = bool(os.environ.get("CI"))
os.makedirs(CACHE, exist_ok=True)
if OUT_DIR:
    os.makedirs(OUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

MAINBOARD_SERIES = {"EQ", "BE", "BZ"}
SME_SERIES = {"SM", "ST"}

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
session.headers.update(HEADERS)
_primed = False


def prime_cookies():
    global _primed
    if not _primed:
        try:
            session.get("https://www.nseindia.com/", timeout=15)
        except Exception:
            pass
        _primed = True


def fetch(url, tries=3):
    """GET with retries. Returns bytes, or None on 404/missing."""
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200 and r.content:
                return r.content
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                prime_cookies()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None


# ---------------------------------------------------------------- bhavcopy

def bhav_url(d):
    return ("https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")


def load_bhavcopy(d):
    """Return {symbol: row-dict} for date d, or None if no trading that day."""
    cache_file = os.path.join(CACHE, f"bhav_{d:%Y%m%d}.csv")
    missing_marker = cache_file + ".missing"
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            raw = f.read()
    elif os.path.exists(missing_marker) and d < date.today():
        return None
    else:
        if d.weekday() >= 5:
            return None
        blob = fetch(bhav_url(d))
        if not blob:
            if d < date.today():
                open(missing_marker, "w").close()
            return None
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            raw = z.read(z.namelist()[0])
        with open(cache_file, "wb") as f:
            f.write(raw)
    return parse_bhavcopy(raw)


def num(x):
    try:
        return float(str(x).strip().replace(",", ""))
    except ValueError:
        return None


def parse_bhavcopy(raw):
    text = raw.decode("utf-8", errors="ignore")
    rows = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        series = r.get("SctySrs", "")
        if series not in MAINBOARD_SERIES and series not in SME_SERIES:
            continue
        sym = r.get("TckrSymb")
        close = num(r.get("ClsPric"))
        if not sym or not close:
            continue
        rows[sym] = {
            "name": r.get("FinInstrmNm", ""),
            "series": series,
            "isin": r.get("ISIN", ""),
            "open": num(r.get("OpnPric")),
            "high": num(r.get("HghPric")),
            "low": num(r.get("LwPric")),
            "close": close,
            "prev": num(r.get("PrvsClsgPric")),
            "vol": num(r.get("TtlTradgVol")) or 0,
            "val": num(r.get("TtlTrfVal")) or 0,
        }
    return rows


def nearest_trading_day(target, max_back=10, label=""):
    """Walk back from target until a bhavcopy exists."""
    d = target
    for _ in range(max_back):
        data = load_bhavcopy(d)
        if data:
            print(f"  {label:<9} {d:%d %b %Y}  ({len(data):,} stocks)")
            return d, data
        d -= timedelta(days=1)
    print(f"  {label:<9} not found near {target:%d %b %Y}")
    return None, None


# ---------------------------------------------------------------- indices

def load_index(fname):
    cache_file = os.path.join(CACHE, fname)
    fresh = os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) < 7 * 86400
    if fresh:
        with open(cache_file, "rb") as f:
            raw = f.read()
    else:
        raw = (fetch(f"https://nsearchives.nseindia.com/content/indices/{fname}")
               or fetch(f"https://www.niftyindices.com/IndexConstituent/{fname}"))
        if not raw:
            if os.path.exists(cache_file):  # fall back to stale cache
                with open(cache_file, "rb") as f:
                    raw = f.read()
            else:
                return None
        else:
            with open(cache_file, "wb") as f:
                f.write(raw)
    out = {}
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8", errors="ignore"))):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("Symbol"):
            out[r["Symbol"]] = r.get("Industry", "")
    return out


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- app files

ICON_192 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAENklEQVR4nO3dS27bSABF0XIj+1BmXo57mt14N5l6Oe6Zs5MeCBCESLJpPrJ+PGeUODJFoC6qyNInT6eX9wJr/dP6BBibgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAhoYKfX361PQUDDOtfTvCEBDem6m7YNCWg8t8U0bEhAg7nbyp/XX/XP5ExAw2tYTxHQWG6nn7b1FAENpMN6ioBG0fx2/REBDaC3C+drAupdz/UUAXWu83qKgHrWfz1FQGPprZ4ioG71edN+S0A9GqWeIqAOdbvlc9fT6eW99TmwMpoepiUBtRdOOV5MPbR8wfKGMgYmICICIiIgIgJqaawtn7sE1MwE9RQBtTJHPUVATUxTTxFQfTPVUwRU2WT1FAERElA9A73LZzkBVTJlPUVAdcx36XMhoN0N8eGK1QS0r7nrKQLa1fT1lFJ+tD6BjV2PWduhOkI9ZbKA/hqzy1/rD9tB6inTBPT5bc75X9uO35T1lDkCWniTXG1CmnXL567hL6JXbLGcXn/vtzFzqHrK0DNQGMEeE9LEG4aPjBrQJ0N1CaLy0nacC+drQy5hS+o5//lb45csbcesp4w4Ay2s5/aH+01Ih62njBXQd9O5+5jlc8zCko5cTxkooLCe2wd/t6Tlz3KcesooAW1Vz91fTJa2o9203+o9oM9Hd5PRWre0/Xn9pZ7SeUB7TDxfHvC7E9LB9RvQl/V8vD2vOOzPf//7/AErLpL++sVD6TGgJcvWunouv/hlRuWbS9sx6ykdBrRk2Vpdz8XH2/OShsqyCemw9ZTedqJ7HqRH+9rNT6ytXmagCndbm7iekPo5q4a6CKjnieeRbk+ssvZL2Ij1cNE4IPWMrtkSJp05tJmB1DONBgGpZyZVl7BR7tVZrl5AJp4p1VvCHlWinqFVXcJu30PTfz07veY/jWa38f2nU6q85j+62ndh527mrmfDI/SvwW38EPWwUPvXwhiagIgIiIiAiAiIiICICIiIgIjs8lLG6h3YI+z9T2b7GSjZv/94ez7C9v9MNg5ok+HX0EBcAxEREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEdk4oE0+2PXlQfJnWXKEOs+Sf91W2y/sMgMReTq9vG9+0DqfTK3z/al1nmXdf+Hbw5cF7hIQx2EJIyIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAi/wMGelvdl+PwXwAAAABJRU5ErkJggg=="
ICON_512 = "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"
MANIFEST = '{\n  "name": "NSE Stock Scanner",\n  "short_name": "NSE Scanner",\n  "start_url": "./",\n  "scope": "./",\n  "display": "standalone",\n  "background_color": "#f6f7f9",\n  "theme_color": "#1f4fd8",\n  "icons": [\n    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},\n    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},\n    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}\n  ]\n}\n'
SERVICE_WORKER = '// Network-first: always try to get today\'s data, fall back to the last copy when offline.\nconst CACHE = "nse-scanner-v1";\nself.addEventListener("install", e => self.skipWaiting());\nself.addEventListener("activate", e => e.waitUntil(self.clients.claim()));\nself.addEventListener("fetch", e => {\n  if (e.request.method !== "GET") return;\n  e.respondWith(\n    fetch(e.request, {cache: "no-store"})\n      .then(res => { const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return res; })\n      .catch(() => caches.match(e.request))\n  );\n});\n'


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


def main():
    if len(sys.argv) > 1:
        as_of = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        as_of = date.today()

    print("Downloading NSE bhavcopies…")
    latest_day, latest = nearest_trading_day(as_of, label="Latest")
    if not latest:
        sys.exit("Could not download the latest bhavcopy. Check your internet, "
                 "or run after 7 PM IST on a trading day.")

    history = {}
    for key, _, days in PERIODS:
        if days:
            d, data = nearest_trading_day(latest_day - timedelta(days=days), label=key)
            history[key] = (d, data or {})

    print("Downloading index constituent lists…")
    index_members = {}
    sector = {}
    for label, fname in INDICES:
        m = load_index(fname)
        if m:
            index_members[label] = sorted(m.keys())
            for s, ind in m.items():
                if ind and s not in sector:
                    sector[s] = ind
            print(f"  {label:<26} {len(m):>4}")
        else:
            print(f"  {label:<26}  (unavailable — skipped)")

    def pct(a, b):
        return round((a / b - 1) * 100, 2) if a and b else None

    stocks = []
    for sym, r in latest.items():
        c = r["close"]
        rec = {
            "s": sym,
            "n": r["name"],
            "seg": "SME" if r["series"] in SME_SERIES else "Main",
            "sr": r["series"],
            "sec": sector.get(sym, ""),
            "c": c,
            "v": int(r["vol"]),
            "tv": round(r["val"] / 1e7, 2),  # ₹ crore
            "ch": {"1D": pct(c, r["prev"])},
        }
        for key, (_, data) in history.items():
            old = data.get(sym)
            rec["ch"][key] = pct(c, old["close"]) if old else None
        stocks.append(rec)

    meta = {
        "asOf": latest_day.strftime("%d %b %Y"),
        "generated": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "periodDates": {k: (d.strftime("%d %b %Y") if d else None) for k, (d, _) in history.items()},
        "periods": [[k, l] for k, l, _ in PERIODS],
        "counts": {
            "Main": sum(1 for s in stocks if s["seg"] == "Main"),
            "SME": sum(1 for s in stocks if s["seg"] == "SME"),
        },
    }

    html = TEMPLATE.replace("/*__DATA__*/", json.dumps(
        {"meta": meta, "stocks": stocks, "indices": index_members},
        separators=(",", ":")))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nDone: {len(stocks):,} stocks  ({meta['counts']['Main']:,} Mainboard, "
          f"{meta['counts']['SME']:,} SME) as of {meta['asOf']}")
    write_app_files(os.path.dirname(OUT))
    print(f"Scanner saved to {OUT}")
    if not IN_CI:
        try:
            webbrowser.open("file://" + OUT)
        except Exception:
            pass


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Stock Scanner</title>
<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#1f4fd8">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NSE Scanner">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-192.png">
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#6b7280;--line:#e5e7eb;
--up:#0a8a4a;--upbg:#e7f6ee;--dn:#c62828;--dnbg:#fdecec;--acc:#1f4fd8;--accbg:#e8eefc}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaed;--mute:#9aa0a6;
--line:#2a2f3a;--up:#3ecf7f;--upbg:#12301f;--dn:#ff6b6b;--dnbg:#3a1717;--acc:#7aa2ff;--accbg:#1c2640}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:16px;max-width:1200px;margin:auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mute);font-size:12px}
.wrap{max-width:1200px;margin:auto;padding:0 16px 40px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.row+.row{margin-top:10px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--ink);padding:7px 12px;font:inherit;cursor:pointer;border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button.on{background:var(--acc);color:#fff}
select,input{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
input[type=search]{flex:1;min-width:140px}
input[type=number]{width:110px}
label.l{font-size:12px;color:var(--mute);display:flex;flex-direction:column;gap:3px}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.stat{flex:1;min-width:110px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
.stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--mute)}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:var(--card);font-size:12px;color:var(--mute);cursor:pointer;user-select:none}
th:hover{color:var(--ink)}
th.sorted{color:var(--acc)}
td.l,th.l{text-align:left}
td.sym b{font-weight:600}
td.sym small{display:block;color:var(--mute);max-width:220px;overflow:hidden;text-overflow:ellipsis}
.tag{font-size:10px;padding:1px 5px;border-radius:4px;background:var(--accbg);color:var(--acc);margin-left:4px;vertical-align:middle}
.pill{display:inline-block;min-width:68px;padding:2px 6px;border-radius:6px;font-weight:600}
.up{color:var(--up);background:var(--upbg)} .dn{color:var(--dn);background:var(--dnbg)} .na{color:var(--mute)}
.warn{cursor:help}
tr.hi td{background:var(--accbg)}
.pager{display:flex;justify-content:space-between;align-items:center;padding:10px;color:var(--mute);font-size:12px}
.foot{color:var(--mute);font-size:12px;margin-top:10px}
@media (max-width:640px){td.sym small{max-width:120px} .hide-m{display:none}}
</style></head><body>
<header><h1>NSE Stock Scanner</h1>
<div class="sub" id="sub"></div></header>
<div class="wrap">
<div class="panel">
  <div class="row"><div class="seg" id="periods"></div></div>
  <div class="row">
    <label class="l">Universe<select id="universe"></select></label>
    <label class="l">Sector<select id="sector"></select></label>
    <label class="l">Show<select id="dir">
      <option value="all">All stocks</option><option value="up">Gainers only</option><option value="dn">Losers only</option></select></label>
    <label class="l">Sort by<select id="sortsel"></select></label>
  </div>
  <div class="row">
    <label class="l">Min price ₹<input type="number" id="minp" min="0" placeholder="0"></label>
    <label class="l">Max price ₹<input type="number" id="maxp" min="0" placeholder="any"></label>
    <label class="l">Min traded value (₹ Cr)<input type="number" id="minv" min="0" step="0.1" placeholder="0"></label>
    <label class="l">Min % change<input type="number" id="minc" step="0.5" placeholder="any"></label>
  </div>
  <div class="row">
    <input type="search" id="q" placeholder="Search symbol or name…">
    <label class="row" style="gap:4px;font-size:12px;color:var(--mute)"><input type="checkbox" id="hideca"> Hide ⚠ split/bonus suspects</label>
    <button class="btn" id="csv">Export CSV</button>
    <button class="btn" id="reset">Reset</button>
  </div>
</div>
<div class="stats" id="stats"></div>
<div class="tbl"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
<div class="pager"><span id="pinfo"></span><span><button class="btn" id="prev">‹ Prev</button> <button class="btn" id="next">Next ›</button></span></div></div>
<div class="foot" id="foot"></div>
</div>
<script>
const D = /*__DATA__*/;
const M = D.meta, S = D.stocks, IDX = D.indices;
const idxSets = {}; for (const k in IDX) idxSets[k] = new Set(IDX[k]);
const PAGE = 100;
const st = {period:"1D", uni:"ALL", sec:"", dir:"all", sort:"chg", asc:false, q:"", minp:null, maxp:null, minv:null, minc:null, hideca:false, page:0};

const $ = id => document.getElementById(id);
const fmt = (n, d=2) => n==null ? "—" : n.toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtInt = n => n==null ? "—" : n.toLocaleString("en-IN");

// Corporate-action suspicion: big one-shot drop in a longer period that looks like a split/bonus ratio
function caSuspect(s, p){
  const v = s.ch[p]; if (v==null || p==="1D") return false;
  return v <= -45;
}

$("sub").textContent = `Prices as of ${M.asOf} · ${fmtInt(M.counts.Main)} Mainboard + ${fmtInt(M.counts.SME)} SME stocks · built ${M.generated}`;

// period buttons
M.periods.forEach(([k,l]) => {
  const b = document.createElement("button"); b.textContent = k; b.title = l; b.dataset.k = k;
  b.onclick = () => { st.period = k; st.page = 0; render(); };
  $("periods").appendChild(b);
});

// universe
const uni = $("universe");
[["ALL","All NSE stocks"],["MAIN","Mainboard only"],["SME","SME only"]].forEach(([v,t]) => uni.add(new Option(t,v)));
const og = document.createElement("optgroup"); og.label = "Indices";
Object.keys(IDX).forEach(k => og.appendChild(new Option(`${k} (${IDX[k].length})`, "I:"+k)));
uni.appendChild(og);

// sector
const secs = [...new Set(S.map(s=>s.sec).filter(Boolean))].sort();
$("sector").add(new Option("All sectors",""));
secs.forEach(s => $("sector").add(new Option(s,s)));

const COLS = [
  {k:"sym", t:"Stock", cls:"l"},
  {k:"c", t:"Close ₹"},
  {k:"chg", t:"% Chg"},
  {k:"tv", t:"Value ₹Cr", cls:"hide-m"},
  {k:"v", t:"Volume", cls:"hide-m"},
  {k:"sec", t:"Sector", cls:"l hide-m"},
];
[["chg","% change"],["c","Price"],["tv","Traded value"],["v","Volume"],["sym","Symbol A–Z"]].forEach(([v,t])=>$("sortsel").add(new Option(t,v)));

function val(s, k){
  switch(k){
    case "sym": return s.s; case "sec": return s.sec||"";
    case "chg": return s.ch[st.period];
    default: return s[k];
  }
}

function filtered(){
  const q = st.q.trim().toUpperCase();
  const set = st.uni.startsWith("I:") ? idxSets[st.uni.slice(2)] : null;
  return S.filter(s => {
    if (st.uni==="MAIN" && s.seg!=="Main") return false;
    if (st.uni==="SME" && s.seg!=="SME") return false;
    if (set && !set.has(s.s)) return false;
    if (st.sec && s.sec!==st.sec) return false;
    const c = s.ch[st.period];
    if (st.dir==="up" && !(c>0)) return false;
    if (st.dir==="dn" && !(c<0)) return false;
    if (st.minc!=null && (c==null || c < st.minc)) return false;
    if (st.minp!=null && s.c < st.minp) return false;
    if (st.maxp!=null && s.c > st.maxp) return false;
    if (st.minv!=null && s.tv < st.minv) return false;
    if (st.hideca && caSuspect(s, st.period)) return false;
    if (q && !s.s.includes(q) && !s.n.toUpperCase().includes(q)) return false;
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

let current = [];
function render(){
  document.querySelectorAll("#periods button").forEach(b => b.classList.toggle("on", b.dataset.k===st.period));
  $("sortsel").value = st.sort;
  current = sorted(filtered());

  // stats
  const ch = current.map(s=>s.ch[st.period]).filter(v=>v!=null);
  const up = ch.filter(v=>v>0).length, dn = ch.filter(v=>v<0).length;
  const med = ch.length ? [...ch].sort((a,b)=>a-b)[Math.floor(ch.length/2)] : null;
  const pd = st.period==="1D" ? "vs previous close" : `since ${M.periodDates[st.period]||"—"}`;
  $("stats").innerHTML = `
    <div class="stat"><b>${fmtInt(current.length)}</b><span>stocks shown</span></div>
    <div class="stat"><b style="color:var(--up)">${fmtInt(up)}</b><span>advancing</span></div>
    <div class="stat"><b style="color:var(--dn)">${fmtInt(dn)}</b><span>declining</span></div>
    <div class="stat"><b>${med==null?"—":(med>0?"+":"")+fmt(med)+"%"}</b><span>median change ${pd}</span></div>`;

  // header
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
  $("body").innerHTML = rows.map((s,i) => {
    const c = s.ch[st.period];
    const cls = c==null ? "na" : c>0 ? "up" : c<0 ? "dn" : "na";
    const warn = caSuspect(s, st.period) ? ` <span class="warn" title="Possible split/bonus — raw prices not adjusted">⚠</span>` : "";
    const tag = s.seg==="SME" ? `<span class="tag">SME</span>` : (s.sr!=="EQ" ? `<span class="tag">${s.sr}</span>` : "");
    return `<tr><td class="l sym"><b>${s.s}</b>${tag}<small>${s.n}</small></td>
      <td>${fmt(s.c)}</td>
      <td><span class="pill ${cls}">${c==null?"—":(c>0?"+":"")+fmt(c)+"%"}</span>${warn}</td>
      <td class="hide-m">${fmt(s.tv)}</td><td class="hide-m">${fmtInt(s.v)}</td>
      <td class="l hide-m">${s.sec||"—"}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="l na" style="padding:24px">No stocks match these filters.</td></tr>`;
  $("pinfo").textContent = current.length ? `Showing ${st.page*PAGE+1}–${Math.min((st.page+1)*PAGE,current.length)} of ${fmtInt(current.length)}` : "";
  $("prev").disabled = st.page===0; $("next").disabled = st.page>=pages-1;
}

const bind = (id, key, conv) => $(id).addEventListener("input", e => { st[key] = conv(e.target); st.page = 0; render(); });
const numOrNull = e => e.value==="" ? null : parseFloat(e.value);
bind("universe","uni",e=>e.value); bind("sector","sec",e=>e.value); bind("dir","dir",e=>e.value);
bind("q","q",e=>e.value); bind("minp","minp",numOrNull); bind("maxp","maxp",numOrNull);
bind("minv","minv",numOrNull); bind("minc","minc",numOrNull); bind("hideca","hideca",e=>e.checked);
$("sortsel").addEventListener("input", e => { st.sort = e.target.value; st.asc = st.sort==="sym"; st.page=0; render(); });
$("prev").onclick = () => { st.page--; render(); window.scrollTo(0,0); };
$("next").onclick = () => { st.page++; render(); window.scrollTo(0,0); };
$("reset").onclick = () => location.reload();
$("csv").onclick = () => {
  const hdr = ["Symbol","Name","Segment","Series","Sector","Close", ...M.periods.map(p=>"%Chg "+p[0]),"Volume","Value_Cr"];
  const esc = v => `"${String(v??"").replace(/"/g,'""')}"`;
  const lines = [hdr.join(",")].concat(current.map(s => [s.s,s.n,s.seg,s.sr,s.sec,s.c, ...M.periods.map(p=>s.ch[p[0]]??""), s.v, s.tv].map(esc).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")],{type:"text/csv"}));
  a.download = `nse_scan_${st.period}_${M.asOf.replace(/ /g,"")}.csv`; a.click();
};
$("foot").innerHTML = "Source: NSE end-of-day bhavcopy & index constituent files. Returns use raw closing prices, not adjusted for splits, bonuses or dividends — ⚠ marks a ≥45% fall that may be a corporate action. Not investment advice.";
render();
if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
  navigator.serviceWorker.register("sw.js").catch(()=>{});
}
</script></body></html>"""

if __name__ == "__main__":
    main()
