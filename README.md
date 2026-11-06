# NSE Stock Scanner

Scans every NSE-listed stock (Mainboard + SME) and shows % price change for
1D / 1W / 1M / 3M / 6M / 1Y, sortable, with index (Nifty 50, 500, Midcap,
Smallcap, sectoral…), SME, sector and price/value filters.

Rebuilt automatically every weekday evening by GitHub Actions from NSE's
official bhavcopy files, and published with GitHub Pages.

Manual refresh: Actions tab → "Update scanner" → Run workflow.
Run locally: `pip install requests` then `python nse_scanner.py`.

## Watchlist tab

**Fixed lists** (read-only, rebuilt by the daily workflow):

| List | Source |
|---|---|
| Nifty 50, Nifty 500 | NSE index constituent files |
| F&O | NSE `fo_mktlots.csv` |
| NSE SME, BSE SME | bhavcopy: SME-series stocks that traded in the last 10 sessions |
| IPO (listed in the last 365 days) | NSE past-issues list (listing date, issue price) + new BSE-only scrip codes (54xxxx; listing-day open instead of issue price). Prices are split/bonus adjusted |

- Changes are logged in `data/list_changes.csv` (date, list, added/removed, reason); today's lists are kept in
  `data/fixed_lists.json`. A stock that leaves a list goes to **Hataye gaye (Old)** with a tag such as
  "Nifty 50 se hata - 15 Dec 2026". SME/IPO stocks leave only after 10 sessions without a trade (they often skip
  days), unless the reason is clear (moved to mainboard, IPO older than a year). If a source looks broken
  (list shrinks by more than 20% or cannot be downloaded), yesterday's list is kept.
- **NEW** label for 30 days after a stock joins a fixed list; "Aaj ke badlav" box shows today's additions/removals.

**My lists**: Watchlist 1–5 (up to 1000 stocks each) — paste, CSV import (Kite), **+ WL** in the scanner (choose 1–5).
Remove with checkboxes / Sab clear karo, 10-second Undo. Stocks removed from Watchlist 1–5 do not go to Old.

**Mode chips**: any mix of fixed lists, Watchlist 1–5 and Old; rank and rank change are worked out among the
stocks on screen. **Tags**: auto tags from daily data (Darvas Breakout, Near Darvas Top ≤3%, Volume Spike ≥2× 20-day
average, Near 52W High ≤5%, IPO Base) and my tags (Watch Closely, PEAD, custom); multi-select, any match.
**Sort**: score, % changes, volume, volume spike, turnover, distance from 52W high, rank gain, price, name,
IPO listing date / % vs issue — with ascending/descending, remembered.

**Momentum Score 0–100**, recalculated daily from end-of-day data (also a column / sort in the scanner):

| Part | Points | Full points at → zero at |
|---|---|---|
| Closeness to 52-week high | 20 | at the high → 25% below |
| 1-week change | 10 | +10% → −5% |
| 1-month change | 10 | +20% → −10% |
| 3-month change | 15 | +40% → −15% |
| Volume vs 20-day average (green days only) | 10 | 2.5× → 0.5× |
| Darvas box | 10 | breakout today 10 · ≤3% below box top 7 · ≤7% 4 · inside box 2 |
| Close above 50 DMA | 7.5 | |
| Close above 200 DMA | 7.5 | |
| 3-month return vs Nifty 50 | 10 | +25 pts → −10 pts |

The bhavcopy has no index values, so "Nifty 50" is the equal-weight average 3-month return of the Nifty 50 stocks.

**Private data** — Watchlist 1–5, Old-list removals, notes, tags and rank history — lives only in your Google Sheet
(via `apps_script/Code.gs`), with a copy in the browser for offline use. Nothing personal is written to this repo.

**Live prices (Phase 2)**: the page reads prices through `px()`. A live feed only has to call
`LIVE_FEED.update({"N_RELIANCE": {ltp, prev}})` (keys = `N_<NSE symbol>` / `B_<BSE code>`);
`LIVE_FEED.symbols()` lists the stocks on screen to subscribe to. Score and rank stay end-of-day.

## News tab

Built by `news.py` (workflow **Update news**: market days every 30 minutes 9:00 AM–4:00 PM IST, plus 8:30 PM IST)
into `data/news/` — one JSON file per day, 30 days kept. The page reads it from raw.githubusercontent.com, so news
updates do not need a new Pages deploy. Only public exchange data and headlines are stored; filtering by your
Watchlist happens in the browser.

| Category | Source |
|---|---|
| Results, Order/Contract, Dividend/Bonus/Split, Fund Raise, Promoter/Insider | NSE corporate announcements (mainboard + SME, link = original PDF); BSE announcements RSS for BSE-only stocks |
| Board Meeting, Upcoming Results (next 7 days) | NSE board meetings + event calendar; BSE announcements for BSE-only stocks |
| Dividend/Bonus/Split ex-dates | NSE corporate actions |
| Promoter/Insider | NSE SAST Reg 29 and PIT disclosures |
| Bulk/Block Deal | NSE large-deals snapshot (falls back to `bulk.csv` / `block.csv`); BSE deals are not available (BSE's API blocks scripts) |
| Media News | Google News RSS: headline, source, time and link only. ~45 searches per run: stocks with filings today first, then a rotation over Nifty 500 + F&O + IPO + the 150 best-scored stocks |

Categories are matched from the filing's subject and text (`categorize()` in `news.py`). Every source is optional:
if one fails it is skipped (shown as ✗ under the News tab) and the rest still update. On NSE holidays the run does
nothing. In the Watchlist tab a 📰 icon marks stocks with news today; tap it to see that stock's news.

**Daily email "Aaj ki khaas khabar"** — `news.py` writes `data/news/khaas.json`: results, orders/contracts and
bulk/block buying for stocks with Momentum Score 60+, plus promoter buying and orders of ₹500 Cr+ for any stock.
The Apps Script (`setupEmail()` once) mails it every market evening, together with today's news for your Watchlist 1–5.

### Google Sheet setup (one time)

1. Create a Google Sheet (any name) → **Extensions → Apps Script**.
2. Delete what is there, paste all of `apps_script/Code.gs`, click **Save**.
3. Choose `setup` in the function list → **Run** → allow the permissions. The secret key appears in the new **Setup** tab of the sheet.
4. **Deploy → New deployment** → type **Web app** → Execute as **Me**, Who has access **Anyone** → **Deploy** → copy the Web app URL.
5. Website → Watchlist → **Google Sheet connection** → paste the URL and key → **Connect karo**.

Updating an existing deployment: paste the new code → Save → **Deploy → Manage deployments → ✏️ → Version: New version → Deploy**
(the URL stays the same). Older sheets are upgraded automatically.

Daily email: in Apps Script choose `setupEmail` → **Run** → allow the permissions (send email, connect to an external
service, triggers). A test email arrives at once. `stopEmail` turns it off.
