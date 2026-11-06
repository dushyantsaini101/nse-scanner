# NSE Stock Scanner

Scans every NSE-listed stock (Mainboard + SME) and shows % price change for
1D / 1W / 1M / 3M / 6M / 1Y, sortable, with index (Nifty 50, 500, Midcap,
Smallcap, sectoral…), SME, sector and price/value filters.

Rebuilt automatically every weekday evening by GitHub Actions from NSE's
official bhavcopy files, and published with GitHub Pages.

Manual refresh: Actions tab → "Update scanner" → Run workflow.
Run locally: `pip install requests` then `python nse_scanner.py`.

## Watchlist tab

- Add stocks: paste symbols (`RELIANCE, NSE:TCS, BSE:500325`, comma / space / new line),
  **+ WL** button on every scanner row, CSV import (e.g. a Kite watchlist export), or a whole
  index (any Nifty index, all NSE SME, all BSE SME).
- **Momentum Score 0–100**, recalculated daily from end-of-day data (also a column / sort in the scanner):

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

  The bhavcopy has no index values, so "Nifty 50" is the equal-weight average 3-month
  return of the Nifty 50 stocks.
- Rank by score, rank change vs the previous session, "New in Top 20 today", ticker strip of the top 20.
- Notes and tags (Darvas, IPO Base, PEAD, SME, Watch Closely + your own), stored in a Google Sheet
  through `apps_script/Code.gs`. A copy is kept in the browser, so the page opens offline and
  changes made offline sync later.
- Rank history: `data/watchlist_ranks.csv`, written by the daily workflow when the repository secret
  `WATCHLIST_URL` is set to `<web app URL>?key=<secret key>`.

### Google Sheet setup (one time)

1. Create a Google Sheet (any name) → **Extensions → Apps Script**.
2. Delete what is there, paste all of `apps_script/Code.gs`, click **Save**.
3. Choose `setup` in the function list → **Run** → allow the permissions. The secret key appears in the new **Setup** tab of the sheet.
4. **Deploy → New deployment** → type **Web app** → Execute as **Me**, Who has access **Anyone** → **Deploy** → copy the Web app URL.
5. Website → Watchlist → **Google Sheet connection** → paste the URL and key → **Connect karo**.
6. GitHub repo → Settings → Secrets and variables → Actions → New repository secret
   `WATCHLIST_URL` = `https://script.google.com/macros/s/…/exec?key=<secret key>`.
