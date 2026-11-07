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
**Name search** (in "+ Stocks add karo" and the scanner's **+ WL**): live suggestions by company name, NSE/BSE symbol or
BSE scrip code (NSE stocks too), mainboard + SME; ignores case, spaces, dots and Ltd/Limited, and allows small spelling
mistakes. In the paste box, words that are not exact symbols are matched as names; several matches are shown as choices.
Remove with checkboxes / Sab clear karo, 10-second Undo. Stocks removed from Watchlist 1–5 do not go to Old.

**Mode chips**: any mix of fixed lists, Watchlist 1–5 and Old; rank and rank change are worked out among the
stocks on screen. **Tags**: auto tags from daily data (Darvas Breakout, Near Darvas Top ≤3%, Volume Spike ≥2× 20-day
average, Near 52W High ≤5%, IPO Base) and my tags (Watch Closely, PEAD, custom); multi-select, any match.
**% change period** (dropdown above the price column, remembered): 1/2/3/4 din, 1/2 hafte, 1/3 mahine — trading
sessions back, from adjusted closing prices; the "% change" sort and the ticker follow it.
**Sort**: score, % change, volume, volume spike, turnover, distance from 52W high, rank gain, price, name,
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

## Aaj and Telegram tabs

Tabs, in order: **Aaj** (opens first), **Telegram**, **Scanner**, **Watchlist**, **Portfolio**.

Everything private on these two tabs comes **only from your Google Sheet** (through `apps_script/Code.gs`, with the
same Web app URL and secret key as the Watchlist connection). Nothing from them is stored in this public repo.

- **Aaj**: the newest Trading Brief from the Sheet's **Briefs** tab (A Date, B Type, C Verdict, D Brief markdown;
  newest in row 2): verdict as a big line, then the markdown (tables, emojis). Dropdown for the last 7 briefs.
  "System status" line: last update of prices, brief and Telegram.
- **Telegram**: the Sheet's **Telegram** tab (filled by the Telegram bot, see `docs/telegram-bot-api.md`), last 7 days.
  Highlighted posts first, the rest under "Baaki posts". Chips: Broker report, Sector report, Result, Meri holdings.
  Each stock shows its Momentum Score; upside % = target / latest close (only when the post has one stock).
  Tap a card for the Telegram post, "File" for the Drive file. Rows older than 30 days are deleted nightly.

## Nightly email

`nse_scanner.py` writes `data/email.json` (public market data only): top 10 of each fixed list, New in Top 20, Darvas
breakouts, volume spikes, list changes. The Apps Script mails it at about 9 PM IST on market days. No news, no Telegram.

## Google Sheet setup (one time)

1. Create a Google Sheet (any name) → **Extensions → Apps Script**.
2. Delete what is there, paste all of `apps_script/Code.gs`, click **Save**.
3. Choose `setup` in the function list → **Run** → allow the permissions. The secret key appears in the new **Setup** tab of the sheet.
4. **Deploy → New deployment** → type **Web app** → Execute as **Me**, Who has access **Anyone** → **Deploy** → copy the Web app URL.
5. Website → Watchlist → **Google Sheet connection** → paste the URL and key → **Connect karo**.
6. Choose `setupEmail` → **Run** → allow the permissions (send email, Drive, external service, triggers). A test email
   arrives at once. It also turns on the nightly Telegram cleanup. `stopEmail` turns the email off.

Updating an existing deployment: paste the new code → Save → **Deploy → Manage deployments → ✏️ → Version: New version → Deploy**
(the URL stays the same). Older sheets are upgraded automatically.
