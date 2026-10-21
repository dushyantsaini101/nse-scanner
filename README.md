# NSE Stock Scanner

Scans every NSE-listed stock (Mainboard + SME) and shows % price change for
1D / 1W / 1M / 3M / 6M / 1Y, sortable, with index (Nifty 50, 500, Midcap,
Smallcap, sectoral…), SME, sector and price/value filters.

Rebuilt automatically every weekday evening by GitHub Actions from NSE's
official bhavcopy files, and published with GitHub Pages.

Manual refresh: Actions tab → "Update scanner" → Run workflow.
Run locally: `pip install requests` then `python nse_scanner.py`.
