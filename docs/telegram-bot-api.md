# Telegram bot ↔ Google Sheet API

The Telegram bot talks to the Apps Script web app (`apps_script/Code.gs`, version 4+) that is attached to the
Watchlist Google Sheet. Two calls are needed: **holdings** (read) and **telegram_add** (write).

- **Base URL**: the Web app URL of the deployment, `https://script.google.com/macros/s/<DEPLOYMENT_ID>/exec`
- **Secret key**: the 16-character key in the Sheet's **Setup** tab (cell B1). Keep it in the bot's environment
  (for example `SHEET_KEY`), never in code or in this repository.
- Every response is JSON. `ok: true` means success; otherwise `{"ok": false, "error": "..."}`. A wrong or missing key
  returns `ok: false` too (HTTP status is still 200: Apps Script cannot return other codes).
- Apps Script answers **GET** and **POST** with a redirect (302) to `script.googleusercontent.com`. Follow redirects.
  (`requests` does by default; with `curl` use `-L`.)

## 1. holdings (GET)

Returns the symbols with quantity > 0 from the sheet "My Holdings (auto-updated)" (tab `Holdings`, symbol in
column A, quantity in column D, data from row 5). Read only: that sheet is never written to.

```
GET <BASE_URL>?key=<SECRET_KEY>&action=holdings
```

Response:

```json
{
  "ok": true,
  "version": 4,
  "count": 2,
  "holdings": [
    {"symbol": "HDFCBANK", "qty": 10},
    {"symbol": "TCS", "qty": 3}
  ],
  "at": "2026-11-07 19:30"
}
```

- `symbol` is upper-case, as written in column A (NSE symbol). Same symbol on several rows: quantities are added.
- Rows with empty symbol or quantity ≤ 0 are left out.
- Use it to set `holding` to `true` on posts that mention a held stock. Cache it for the day.

Errors: `"Holdings" tab nahi mila holdings sheet mein`, or a Google permission error if the script's owner cannot open that sheet.

## 2. telegram_add (POST)

Adds a batch of posts to the **Telegram** tab (created automatically). Posts whose `link` is already in the tab are
skipped (**duplicates are detected by the Telegram link**, ignoring `http(s)://`, `www.`, `?query`, `#fragment` and a
trailing `/`). Optionally a row carries a file; it is saved in the Drive folder **"Telegram Reports (bot)"**
(created if missing) and the file's link goes to column K.

```
POST <BASE_URL>
Content-Type: text/plain;charset=utf-8        (application/json also works from a server)

{
  "key": "<SECRET_KEY>",
  "action": "telegram_add",
  "rows": [ <row>, <row>, ... ]
}
```

Send the body as a JSON string. From a browser use `text/plain` to avoid a CORS pre-flight; a bot on a server may use
either content type.

### Row fields

| Field | Required | Type | Goes to column | Notes |
|---|---|---|---|---|
| `link` | **yes** | string | J Telegram link | Must start with `https://`. Used for duplicate check. |
| `time` | no | string or number | A Time (IST) | `"2026-11-07 14:05"` or `"2026-11-07T14:05:00"` is stored as is (treated as IST). An ISO time with a zone (`...Z`, `+05:30`), or a Unix timestamp (seconds or milliseconds), is converted to IST. Missing/invalid: now. |
| `channel` | no | string | B Channel | max 100 chars |
| `type` | no | string | C Type | `Broker report`, `Sector report`, `Result` or `Other` (case-insensitive; anything else becomes `Other`) |
| `stocks` | no | string or array | D Stocks | `"BEL"`, `"TCS, INFY"` or `["TCS","INFY"]`. NSE symbols (or `BSE:500325`). |
| `house` | no | string | E Research house | e.g. `Motilal Oswal` |
| `rating` | no | string | F Rating | e.g. `Buy`, `Sell`, `Accumulate` |
| `target` | no | number or string | G Target ₹ | `520`, `"₹1,250"`. Not a positive number: left empty. |
| `headline` | no | string | H Headline | max 300 chars |
| `text` | no | string | I Text | extracted text, short; max 1500 chars |
| `highlight` | no | boolean | L Highlight (Y/N) | `true`, `"Y"`, `"yes"`, `1` = Y |
| `holding` | no | boolean | M Holding (Y/N) | same values; the app also checks holdings itself |
| `file` | no | object | K Drive file link | see below |
| `file_link` | no | string | K Drive file link | an existing `https://` link; ignored when `file` is sent |

Text that starts with `=`, `+`, `-` or `@` is stored with a leading `'` so the Sheet never runs it as a formula.

### Row `file` (optional)

```json
"file": {"name": "BEL_initiation.pdf", "mime": "application/pdf", "data": "<base64 of the file>"}
```

- `data`: plain base64 (a `data:...;base64,` prefix is accepted). Max **15 MB** after decoding.
- `mime`: `application/pdf`, `image/jpeg`, `image/png`, `image/webp` or `image/gif`. If missing it is guessed from
  the file name's extension. Other types are rejected for that row.
- `name`: the file name in Drive (characters `\ / : * ? " < > |` are replaced by `_`).
- The file stays **private** to the Google account that owns the script. The link in column K opens for that account.
- A duplicate row (same `link`) is skipped **before** its file is saved, so no extra file is created.
- Keep each request small: one file per row, and ideally ≤ 5 rows with files per request (Apps Script has a
  request size and a 6-minute run limit). Rows without files: up to **50 per request**.

### Example request

```json
{
  "key": "<SECRET_KEY>",
  "action": "telegram_add",
  "rows": [
    {
      "time": "2026-11-07 14:05",
      "channel": "Broker Calls India",
      "type": "Broker report",
      "stocks": ["BEL"],
      "house": "Motilal Oswal",
      "rating": "Buy",
      "target": 520,
      "headline": "BEL: order book strong, initiating Buy with TP ₹520",
      "text": "Defence orders give 3-year visibility. EPS CAGR 18% FY26-28.",
      "link": "https://t.me/example_channel/101",
      "highlight": true,
      "holding": false,
      "file": {"name": "BEL_initiation.pdf", "mime": "application/pdf", "data": "JVBERi0xLjcK..."}
    },
    {
      "channel": "Results Live",
      "type": "Result",
      "stocks": "TCS, INFY",
      "headline": "IT results: TCS beat, INFY in line",
      "link": "https://t.me/example_channel/102"
    }
  ]
}
```

### Response

```json
{
  "ok": true,
  "version": 4,
  "added": 1,
  "duplicates": 1,
  "errors": 0,
  "results": [
    {"i": 0, "link": "https://t.me/example_channel/101", "status": "added", "file": "https://drive.google.com/file/d/…/view"},
    {"i": 1, "link": "https://t.me/example_channel/102", "status": "duplicate"}
  ]
}
```

- `results[i]` matches `rows[i]`. `status` is `added`, `duplicate` or `error` (with an `error` message, e.g.
  `"link" (https://t.me/…) zaroori hai`, `file sirf PDF ya image (jpg, png, webp, gif) ho sakti hai`,
  `file 15 MB se badi hai`).
- One bad row never stops the others. `ok: true` with `errors > 0` means a partial success: retry only the `error`
  rows after fixing them. Retrying the same batch is safe: added rows come back as `duplicate`.
- `ok: false` means the whole request was refused: wrong key, body is not JSON (`Request samajh nahi aayi`),
  `rows` missing or not a list, or more than 50 rows.

## 3. Housekeeping done by the script

- **Retention**: every night (~3 AM IST) Telegram rows older than **30 days** and Drive files in
  "Telegram Reports (bot)" older than 30 days are deleted (files go to the Drive trash). The app shows the last 7 days.
- Writes are serialised with a lock, so two bot calls at the same time are safe (the second waits up to 25 s).
- The script never touches the Watchlist, Rank history, Setup or Briefs tabs from these calls.

## 4. Quick test with curl

```bash
curl -L -H "Content-Type: text/plain" \
  -d '{"key":"'"$SHEET_KEY"'","action":"telegram_add","rows":[{"link":"https://t.me/test/1","headline":"test","type":"Other"}]}' \
  "$BASE_URL"

curl -L "$BASE_URL?key=$SHEET_KEY&action=holdings"
```

Delete the test row from the Telegram tab afterwards.
