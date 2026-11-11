/**
 * Backend for the Stock Scanner app (GitHub Pages)  — version 6
 * Everything private lives only in this Google Sheet / your Google Drive (never in the public GitHub repo):
 *   - "Watchlist" tab     : one row per stock — Watchlist 1-5 membership, notes, tags, Old-list removals
 *   - "Rank history" tab  : daily rank of each stock inside Watchlist 1-5
 *   - "Briefs" tab        : Trading Briefs (A Date, B Type, C Verdict, D Brief markdown; newest in row 2) — read only
 *   - "Telegram" tab      : posts saved by the Telegram bot (action "telegram_add"); files go to the Drive folder
 *                           "Telegram Reports (bot)". Rows and files older than 30 days are deleted every night.
 *   - "Research" tab      : one row per stock researched in Claude chats (Claude adds/updates rows) — read only here
 *   - "Laptop" tab        : laptop watchlist (Excel remarks + folder file list + lists); Claude rewrites it on
 *                           "laptop watchlist refresh karo" — read only here, don't edit by hand
 *   - holdings            : read only from the sheet "My Holdings (auto-updated)" (never written to)
 *
 * Setup (details in README.md):
 *   1. Google Sheet → Extensions → Apps Script → is file ka poora code paste karo → Save.
 *   2. Pehli baar: function list mein "setup" chuno → Run → permission do. Secret key "Setup" tab mein dikhegi.
 *   3. Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone → Deploy.
 *      (Pehle se deployed ho to: Deploy → Manage deployments → ✏️ → Version: New version → Deploy. URL wahi rehta hai.)
 *   4. Web app URL + secret key website ke Watchlist tab → "Google Sheet connection" mein daalo.
 *   5. "stopEmail" chalao, phir "setupEmail" chalao (raat ~9 baje ka email + Telegram ki roz safai).
 *
 * API (all calls need the secret key; details for the bot in docs/telegram-bot-api.md):
 *   GET  ?key=…                      Watchlist items          GET ?key=…&hist=N_TCS   rank history of one stock
 *   GET  ?key=…&action=briefs        last 7 Trading Briefs    GET ?key=…&action=telegram   last 7 days of posts
 *   GET  ?key=…&action=holdings      symbols with quantity > 0
 *   GET  ?key=…&action=research      all rows of the Research tab
 *   GET  ?key=…&action=laptop        all rows of the Laptop tab
 *   POST {"key":…, "ops":[…]}        Watchlist changes        POST {"key":…, "action":"telegram_add", "rows":[…]}
 */

const VERSION = 6;
const TAB = 'Watchlist';
const HIST = 'Rank history';
const BRIEFS = 'Briefs';
const TG = 'Telegram';
const RESEARCH = 'Research';
const LAPTOP = 'Laptop';
const HEAD = ['Key', 'Symbol', 'Exchange', 'Name', 'Added', 'Tags', 'Notes', 'Updated', 'Watchlists', 'Old list removed till'];
const HIST_HEAD = ['Date', 'List', 'Rank', 'Symbol', 'Key', 'Score'];
const TG_HEAD = ['Time (IST)', 'Channel', 'Type', 'Stocks', 'Research house', 'Rating', 'Target ₹', 'Headline',
                 'Text', 'Telegram link', 'Drive file link', 'Highlight (Y/N)', 'Holding (Y/N)'];
const TG_TYPES = ['Broker report', 'Sector report', 'Result', 'Other'];
const TG_FOLDER = 'Telegram Reports (bot)';
const TG_KEEP_DAYS = 30;
const TG_MAX_ROWS = 50;                       // per telegram_add call
const TG_MAX_FILE = 15 * 1024 * 1024;         // bytes, after base64 decoding
const TG_MIME = {pdf: 'application/pdf', jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp', gif: 'image/gif'};
const HOLD_ID = '1lY37n9lk3gMk62iy7pO1wzyvIDHVFekMXNr1L7H5m3U';   // "My Holdings (auto-updated)" — read only
const HOLD_TAB = 'Holdings';
const HOLD_FIRST_ROW = 5;
const HIST_DAYS = 180;
const KEY_RE = /^[NB]_[A-Za-z0-9_-]{1,40}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TZ = 'Asia/Kolkata';

/** Run once from the editor: creates the tabs and the secret key. */
function setup() {
  const props = PropertiesService.getScriptProperties();
  let key = props.getProperty('KEY');
  if (!key) {
    key = Utilities.getUuid().replace(/-/g, '').slice(0, 16);
    props.setProperty('KEY', key);
  }
  write_(sheet_(), read_(sheet_()));         // creates / upgrades the Watchlist tab
  histSheet_();
  tgSheet_();
  const ss = SpreadsheetApp.getActive();
  let info = ss.getSheetByName('Setup');
  if (!info) info = ss.insertSheet('Setup');
  info.getRange('A1:B2').setValues([['Secret key (website mein daalo)', key],
                                    ['Note', 'Ye key kisi ko mat do. Nayi key chahiye to Apps Script mein newKey() chalao.']]);
  info.autoResizeColumns(1, 2);
  tidyTabs_();
  Logger.log('Setup ho gaya. Aapki SECRET KEY: ' + key);
}

/** Optional: make a new secret key (old one stops working). */
function newKey() {
  PropertiesService.getScriptProperties().deleteProperty('KEY');
  setup();
}

function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!keyOk_(p.key)) return out_({ok: false, error: badKeyMsg_()});
  const act = String(p.action || '');
  try {
    if (act === 'briefs') {
      const briefs = readBriefs_(Math.min(Math.max(Number(p.n) || 7, 1), 30));
      return out_({ok: true, version: VERSION, briefs: briefs,
                   status: {brief: briefs.length ? briefs[0].date : '', telegram: tgLastTime_()}});
    }
    if (act === 'telegram') {
      const days = Math.min(Math.max(Number(p.days) || 7, 1), TG_KEEP_DAYS);
      return out_({ok: true, version: VERSION, days: days, telegram: readTelegram_(days), updated: tgLastTime_()});
    }
    if (act === 'research') {
      const rows = readResearch_();
      return out_({ok: true, version: VERSION, count: rows.length, research: rows});
    }
    if (act === 'laptop') {
      const lp = readLaptop_();
      return out_({ok: true, version: VERSION, count: lp.rows.length, refreshed: lp.refreshed, laptop: lp.rows});
    }
    if (act === 'holdings') {
      const h = readHoldings_();
      return out_({ok: true, version: VERSION, count: h.length, holdings: h,
                   at: Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd HH:mm')});
    }
  } catch (err) {
    return out_({ok: false, error: String(err && err.message || err)});
  }
  if (act) return out_({ok: false, error: 'Unknown action: ' + act});
  if (p.hist) return out_({ok: true, version: VERSION, hist: histFor_(String(p.hist))});
  return out_({ok: true, version: VERSION, items: read_(sheet_())});
}

/** Body (sent as text/plain or JSON):
 *  {"key": "...", "ops": [...]}                     Watchlist changes:
 *     {op:"patch", items:[{k, sym, ex, name, notes?, tags?, lists?, addList?, delList?, oldDel?}]}
 *     {op:"hist", date, list, rows:[[key, symbol, rank, score], ...]}
 *     version 1/2 ops: add, remove, update, and patch with mine/hidden
 *  {"key": "...", "action": "telegram_add", "rows": [...]}   Telegram bot (docs/telegram-bot-api.md) */
function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return out_({ok: false, error: 'Request samajh nahi aayi (JSON nahi hai)'});
  }
  if (!body || !keyOk_(body.key)) return out_({ok: false, error: badKeyMsg_()});
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    if (body.action === 'telegram_add') return out_(telegramAdd_(body.rows));
    if (body.action) return out_({ok: false, error: 'Unknown action: ' + body.action});
    const sh = sheet_();
    const map = new Map(read_(sh).map(i => [i.k, i]));
    const now = Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd HH:mm');
    const hist = [];
    (body.ops || []).forEach(op => {
      if (op && op.op === 'hist') hist.push(op);
      else applyOp_(map, op, now);
    });
    const items = Array.from(map.values());
    write_(sh, items);
    if (hist.length) writeHist_(hist);
    return out_({ok: true, version: VERSION, items: items});
  } catch (err) {
    return out_({ok: false, error: String(err && err.message || err)});
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------- Watchlist (unchanged rules)
function blank_(it, now) {
  return {k: it.k, sym: str_(it.sym, 40), ex: str_(it.ex, 5), name: str_(it.name, 120),
          added: now.slice(0, 10), tags: [], notes: '', lists: [], oldDel: '', updated: now};
}

function applyOp_(map, op, now) {
  if (!op || typeof op !== 'object') return;
  if (op.op === 'patch') {
    (op.items || []).forEach(p => {
      if (!p || !KEY_RE.test(String(p.k))) return;
      let e = map.get(p.k);
      if (!e) { e = blank_(p, now); map.set(p.k, e); }
      if (p.notes != null) e.notes = str_(p.notes, 5000);
      if (p.tags) e.tags = tags_(p.tags);
      if (Array.isArray(p.lists)) e.lists = lists_(p.lists);
      if (p.addList) e.lists = lists_(e.lists.concat([p.addList]));
      if (p.delList) e.lists = e.lists.filter(n => n !== Number(p.delList));
      if (p.mine === true) e.lists = lists_(e.lists.concat([1]));          // version 2 pages
      if (p.mine === false) e.lists = e.lists.filter(n => n !== 1);
      if (p.oldDel != null) e.oldDel = DATE_RE.test(String(p.oldDel)) ? String(p.oldDel) : '';
      e.updated = now;
      if (!e.lists.length && !e.notes && !e.tags.length && !e.oldDel) map.delete(p.k);   // nothing left to keep
    });
  } else if (op.op === 'add') {                                            // version 1 pages
    (op.items || []).forEach(it => {
      if (!it || !KEY_RE.test(String(it.k))) return;
      const e = map.get(it.k) || blank_(it, now);
      e.lists = lists_(e.lists.concat([1]));
      if (!e.notes && it.notes) e.notes = str_(it.notes, 5000);
      e.updated = now;
      map.set(it.k, e);
    });
  } else if (op.op === 'remove') {
    (op.keys || []).forEach(k => map.delete(String(k)));
  } else if (op.op === 'update') {
    const e = map.get(String(op.k));
    if (!e) return;
    if (op.notes != null) e.notes = str_(op.notes, 5000);
    if (op.tags) e.tags = tags_(op.tags);
    e.updated = now;
  }
}

function sheet_() {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(TAB);
  if (!sh) sh = ss.insertSheet(TAB, 0);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, HEAD.length).setValues([HEAD]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

/** Reads by column name, so sheets from version 1 / 2 work too. */
function read_(sh) {
  const n = sh.getLastRow() - 1;
  if (n < 1) return [];
  const cols = Math.max(sh.getLastColumn(), HEAD.length);
  const head = sh.getRange(1, 1, 1, cols).getDisplayValues()[0].map(h => String(h).trim());
  const c = name => head.indexOf(name);
  const iLists = c('Watchlists'), iOld = c('Old list removed till'), iMine = c('In Meri list'), iHidden = c('Hidden');
  return sh.getRange(2, 1, n, cols).getDisplayValues()
    .filter(r => KEY_RE.test(r[0]))
    .map(r => {
      let lists;
      if (iLists >= 0) lists = lists_(String(r[iLists]).split(','));
      else if (iMine >= 0) lists = (r[iMine] === '' || r[iMine].toLowerCase() === 'yes') && !(iHidden >= 0 && r[iHidden].toLowerCase() === 'yes') ? [1] : [];
      else lists = [1];                                                     // version 1: everything was "Meri list"
      return {k: r[0], sym: r[1], ex: r[2], name: r[3], added: r[4], tags: tags_(r[5].split(',')), notes: r[6],
              updated: r[7], lists: lists, oldDel: iOld >= 0 && DATE_RE.test(r[iOld]) ? r[iOld] : ''};
    });
}

function write_(sh, items) {
  const last = sh.getLastRow(), cols = Math.max(sh.getLastColumn(), HEAD.length);
  if (last > 1) sh.getRange(2, 1, last - 1, cols).clearContent();
  sh.getRange(1, 1, 1, cols).clearContent();
  sh.getRange(1, 1, 1, HEAD.length).setValues([HEAD]).setFontWeight('bold');
  sh.setFrozenRows(1);
  if (!items.length) return;
  const rng = sh.getRange(2, 1, items.length, HEAD.length);
  rng.setNumberFormat('@');                     // plain text: no auto dates / numbers
  rng.setValues(items.map(i => [i.k, i.sym, i.ex, i.name, i.added, i.tags.join(', '), i.notes, i.updated,
                                i.lists.join(','), i.oldDel || ''].map(safe_)));
}

function histSheet_() {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(HIST);
  if (!sh) {
    sh = ss.insertSheet(HIST);
    sh.getRange(1, 1, 1, HIST_HEAD.length).setValues([HIST_HEAD]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

/** Replaces the rows of the same date + list, keeps the last HIST_DAYS days. */
function writeHist_(ops) {
  const sh = histSheet_();
  const n = sh.getLastRow() - 1;
  let rows = n > 0 ? sh.getRange(2, 1, n, HIST_HEAD.length).getDisplayValues() : [];
  const cutoff = Utilities.formatDate(new Date(Date.now() - HIST_DAYS * 864e5), TZ, 'yyyy-MM-dd');
  const drop = new Set(ops.map(o => o.date + '|' + o.list));
  rows = rows.filter(r => r[0] >= cutoff && !drop.has(r[0] + '|' + r[1]));
  ops.forEach(o => {
    if (!DATE_RE.test(String(o.date))) return;
    const list = Number(o.list);
    if (!(list >= 1 && list <= 5)) return;
    (o.rows || []).slice(0, 1000).forEach(x => {
      if (KEY_RE.test(String(x[0]))) rows.push([o.date, String(list), String(Number(x[2])), str_(x[1], 40), String(x[0]), String(Number(x[3]))]);
    });
  });
  if (n > 0) sh.getRange(2, 1, n, HIST_HEAD.length).clearContent();
  if (rows.length) {
    const rng = sh.getRange(2, 1, rows.length, HIST_HEAD.length);
    rng.setNumberFormat('@');
    rng.setValues(rows);
  }
}

function histFor_(k) {
  if (!KEY_RE.test(k)) return [];
  const sh = histSheet_();
  if (sh.getLastRow() < 2) return [];
  return sh.getRange(2, 5, sh.getLastRow() - 1, 1).createTextFinder(k).matchEntireCell(true).findAll()
    .map(cell => sh.getRange(cell.getRow(), 1, 1, HIST_HEAD.length).getDisplayValues()[0])
    .map(r => [r[0], Number(r[1]), Number(r[2]), Number(r[5])])
    .sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
}

// ---------------------------------------------------------------- Briefs tab (read only)
function readBriefs_(n) {
  const sh = SpreadsheetApp.getActive().getSheetByName(BRIEFS);
  if (!sh || sh.getLastRow() < 2) return [];
  const rows = sh.getRange(2, 1, Math.min(sh.getLastRow() - 1, n + 20), 4).getDisplayValues();
  return rows.filter(r => String(r[2]).trim() || String(r[3]).trim()).slice(0, n)
    .map(r => ({date: String(r[0]).trim(), type: String(r[1]).trim(), verdict: String(r[2]).trim(), md: String(r[3])}));
}

// ---------------------------------------------------------------- Telegram tab
function tgSheet_() {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(TG);
  if (!sh) sh = ss.insertSheet(TG);
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, TG_HEAD.length).setValues([TG_HEAD]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function tgRows_() {
  const sh = tgSheet_();
  const n = sh.getLastRow() - 1;
  return n > 0 ? sh.getRange(2, 1, n, TG_HEAD.length).getDisplayValues() : [];
}

function readTelegram_(days) {
  const cutoff = Utilities.formatDate(new Date(Date.now() - (days - 1) * 864e5), TZ, 'yyyy-MM-dd');
  return tgRows_().filter(r => r[9] && r[0] >= cutoff)
    .map(r => ({time: r[0], channel: r[1], type: r[2], stocks: r[3], house: r[4], rating: r[5], target: r[6],
                headline: r[7], text: r[8], link: r[9], file: r[10], highlight: yes_(r[11]), holding: yes_(r[12])}))
    .sort((a, b) => a.time < b.time ? 1 : a.time > b.time ? -1 : 0);
}

function tgLastTime_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(TG);
  if (!sh || sh.getLastRow() < 2) return '';
  return sh.getRange(2, 1, sh.getLastRow() - 1, 1).getDisplayValues().map(r => r[0]).reduce((a, b) => b > a ? b : a, '');
}

function telegramAdd_(rows) {
  if (!Array.isArray(rows)) return {ok: false, error: '"rows" list chahiye'};
  if (rows.length > TG_MAX_ROWS) return {ok: false, error: 'Ek baar mein zyada se zyada ' + TG_MAX_ROWS + ' rows bhejo'};
  const sh = tgSheet_();
  const seen = new Set(tgRows_().map(r => linkKey_(r[9])).filter(String));
  const results = [], add = [];
  let folder = null;
  rows.forEach((r, i) => {
    const res = {i: i, link: '', status: 'error'};
    results.push(res);
    try {
      if (!r || typeof r !== 'object') throw new Error('row object nahi hai');
      const link = str_(r.link, 500);
      if (!/^https:\/\//i.test(link)) throw new Error('"link" (https://t.me/…) zaroori hai');
      res.link = link;
      const lk = linkKey_(link);
      if (seen.has(lk)) { res.status = 'duplicate'; return; }
      let fileLink = /^https:\/\//i.test(str_(r.file_link, 500)) ? str_(r.file_link, 500) : '';
      if (r.file && r.file.data) {
        folder = folder || tgFolder_();
        const f = saveFile_(folder, r.file);
        fileLink = f.getUrl();
        res.file = fileLink;
      }
      const target = num_(r.target);
      add.push([tgTime_(r.time), str_(r.channel, 100), tgType_(r.type),
                (Array.isArray(r.stocks) ? r.stocks.join(', ') : str_(r.stocks, 300)).slice(0, 300),
                str_(r.house, 100), str_(r.rating, 50), target == null ? '' : String(target),
                str_(r.headline, 300), str_(r.text, 1500), link, fileLink,
                yes_(r.highlight) ? 'Y' : 'N', yes_(r.holding) ? 'Y' : 'N'].map(safe_));
      seen.add(lk);
      res.status = 'added';
    } catch (err) {
      res.error = String(err && err.message || err);
    }
  });
  if (add.length) {
    const start = sh.getLastRow() + 1;
    const rng = sh.getRange(start, 1, add.length, TG_HEAD.length);
    rng.setNumberFormat('@');
    rng.setValues(add);
  }
  const count = s => results.filter(x => x.status === s).length;
  return {ok: true, version: VERSION, added: count('added'), duplicates: count('duplicate'), errors: count('error'), results: results};
}

function saveFile_(folder, f) {
  let name = str_(f.name, 150).replace(/[\\/:*?"<>|\r\n]+/g, '_') || 'file';
  const ext = (name.match(/\.([A-Za-z0-9]+)$/) || [, ''])[1].toLowerCase();
  let mime = str_(f.mime, 100).toLowerCase();
  if (!mime) mime = TG_MIME[ext] || '';
  const okMime = Object.keys(TG_MIME).map(k => TG_MIME[k]);
  if (okMime.indexOf(mime) < 0) throw new Error('file sirf PDF ya image (jpg, png, webp, gif) ho sakti hai');
  if (!ext) name += '.' + Object.keys(TG_MIME).filter(k => TG_MIME[k] === mime)[0];
  const data = String(f.data).replace(/^data:[^,]*,/, '').replace(/\s+/g, '');
  if (data.length > TG_MAX_FILE * 1.37 + 16) throw new Error('file 15 MB se badi hai');
  const bytes = Utilities.base64Decode(data);
  if (!bytes.length) throw new Error('file khali hai');
  if (bytes.length > TG_MAX_FILE) throw new Error('file 15 MB se badi hai');
  return folder.createFile(Utilities.newBlob(bytes, mime, name));   // private: only you can open it
}

function tgFolder_() {
  const it = DriveApp.getFoldersByName(TG_FOLDER);
  return it.hasNext() ? it.next() : DriveApp.createFolder(TG_FOLDER);
}

/** Runs every night (setupEmail turns it on): removes Telegram rows and Drive files older than 30 days. */
function cleanupTelegram() {
  const cutoff = Utilities.formatDate(new Date(Date.now() - TG_KEEP_DAYS * 864e5), TZ, 'yyyy-MM-dd');
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  let removed = 0;
  try {
    const sh = tgSheet_();
    const rows = tgRows_();
    const keep = rows.filter(r => !(r[0] && r[0].slice(0, 10) < cutoff));
    removed = rows.length - keep.length;
    if (removed) {
      sh.getRange(2, 1, rows.length, TG_HEAD.length).clearContent();
      if (keep.length) {
        const rng = sh.getRange(2, 1, keep.length, TG_HEAD.length);
        rng.setNumberFormat('@');
        rng.setValues(keep);
      }
    }
  } finally {
    lock.releaseLock();
  }
  let files = 0;
  const folders = DriveApp.getFoldersByName(TG_FOLDER);
  const old = new Date(Date.now() - TG_KEEP_DAYS * 864e5);
  while (folders.hasNext()) {
    const fs = folders.next().getFiles();
    while (fs.hasNext()) {
      const f = fs.next();
      if (f.getDateCreated() < old) { f.setTrashed(true); files++; }
    }
  }
  Logger.log('Telegram safai: ' + removed + ' rows aur ' + files + ' files hatayi (' + TG_KEEP_DAYS + ' din se purani).');
}

function tgTime_(v) {
  if (v == null || v === '') return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd HH:mm');
  const s = String(v).trim();
  let m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?$/);
  if (m) return m[1] + ' ' + m[2];                                         // already IST
  let d = /^\d{10,13}$/.test(s) ? new Date(Number(s) * (s.length === 10 ? 1000 : 1)) : new Date(s);
  if (isNaN(d.getTime())) d = new Date();
  return Utilities.formatDate(d, TZ, 'yyyy-MM-dd HH:mm');
}
function tgType_(v) {
  const s = String(v || '').trim().toLowerCase();
  return TG_TYPES.filter(t => t.toLowerCase() === s)[0] || (/broker/.test(s) ? 'Broker report' : /sector/.test(s) ? 'Sector report' : /result/.test(s) ? 'Result' : 'Other');
}
function linkKey_(u) {
  return String(u || '').trim().toLowerCase().replace(/[?#].*$/, '').replace(/\/+$/, '').replace(/^https?:\/\/(www\.)?/, '');
}
function yes_(v) { return v === true || /^(y|yes|true|1)$/i.test(String(v == null ? '' : v).trim()); }
function num_(v) {
  if (v == null || v === '') return null;
  const n = Number(String(v).replace(/[₹,\s]/g, '').replace(/^rs\.?/i, ''));
  return isFinite(n) && n > 0 ? Math.round(n * 100) / 100 : null;
}

// ---------------------------------------------------------------- Research tab (read only; Claude fills it)
// Columns by header name: Company, NSE symbol, BSE code, Sector, Listing, View, Thesis, Red flags, Watch next,
// Research date, Price on research date, Chat link, Updated on
function readResearch_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(RESEARCH);
  if (!sh || sh.getLastRow() < 2) return [];
  const v = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getDisplayValues();
  const H = v[0].map(h => String(h).trim().toLowerCase());
  const col = name => H.indexOf(name);
  const c = {co: col('company'), nse: col('nse symbol'), bse: col('bse code'), sec: col('sector'), lst: col('listing'),
             view: col('view'), th: col('thesis'), rf: col('red flags'), wn: col('watch next'), d: col('research date'),
             p: col('price on research date'), link: col('chat link'), up: col('updated on')};
  const g = (r, i) => i < 0 ? '' : String(r[i] == null ? '' : r[i]).trim();
  return v.slice(1).filter(r => g(r, c.co)).map(r => ({
    company: g(r, c.co), nse: g(r, c.nse).toUpperCase(), bse: g(r, c.bse), sector: g(r, c.sec), listing: g(r, c.lst),
    view: g(r, c.view), thesis: g(r, c.th), redFlags: g(r, c.rf), watchNext: g(r, c.wn), date: g(r, c.d),
    price: num_(g(r, c.p)), link: g(r, c.link), updated: g(r, c.up)}));
}

// ---------------------------------------------------------------- Laptop tab (read only; Claude rewrites it)
// Columns by header name: Company, NSE symbol, BSE code, Source, View, Thesis, Red flags, Watch next, My position,
// Notes, Sector, Lists, Docs, Doc types, Doc names, Last file date, Last researched, Updated on
function readLaptop_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(LAPTOP);
  if (!sh || sh.getLastRow() < 2) return {rows: [], refreshed: ''};
  const v = sh.getRange(1, 1, sh.getLastRow(), sh.getLastColumn()).getDisplayValues();
  const H = v[0].map(h => String(h).trim().toLowerCase());
  const col = name => H.indexOf(name);
  const c = {co: col('company'), nse: col('nse symbol'), bse: col('bse code'), src: col('source'), view: col('view'),
             th: col('thesis'), rf: col('red flags'), wn: col('watch next'), pos: col('my position'), nt: col('notes'),
             sec: col('sector'), ls: col('lists'), dc: col('docs'), dt: col('doc types'), dn: col('doc names'),
             last: col('last file date'), lr: col('last researched'), up: col('updated on')};
  const g = (r, i) => i < 0 ? '' : String(r[i] == null ? '' : r[i]).trim();
  const rows = v.slice(1).filter(r => g(r, c.co)).map(r => ({
    company: g(r, c.co), nse: g(r, c.nse).toUpperCase(), bse: g(r, c.bse), source: g(r, c.src), view: g(r, c.view),
    thesis: g(r, c.th), redFlags: g(r, c.rf), watchNext: g(r, c.wn), position: g(r, c.pos), notes: g(r, c.nt),
    sector: g(r, c.sec), lists: g(r, c.ls), docs: Number(g(r, c.dc)) || 0, docTypes: g(r, c.dt), docNames: g(r, c.dn),
    last: g(r, c.last), lastResearched: g(r, c.lr)}));
  return {rows: rows, refreshed: rows.length ? g(v[1], c.up) : ''};
}

// ---------------------------------------------------------------- holdings (read only)
function readHoldings_() {
  const sh = SpreadsheetApp.openById(HOLD_ID).getSheetByName(HOLD_TAB);
  if (!sh) throw new Error('"' + HOLD_TAB + '" tab nahi mila holdings sheet mein');
  const last = sh.getLastRow();
  if (last < HOLD_FIRST_ROW) return [];
  const out = new Map();
  sh.getRange(HOLD_FIRST_ROW, 1, last - HOLD_FIRST_ROW + 1, 4).getValues().forEach(r => {
    const sym = String(r[0] == null ? '' : r[0]).trim().toUpperCase();
    const qty = typeof r[3] === 'number' ? r[3] : Number(String(r[3]).replace(/[,\s]/g, ''));
    if (sym && isFinite(qty) && qty > 0) out.set(sym, (out.get(sym) || 0) + qty);
  });
  return Array.from(out, ([symbol, qty]) => ({symbol: symbol, qty: qty}));
}

// ---------------------------------------------------------------- tidy tabs
/** Deletes the empty default "Sheet1" tab. Never touches Watchlist, Rank history, Setup or Briefs. */
function tidyTabs_() {
  const ss = SpreadsheetApp.getActive();
  const s1 = ss.getSheetByName('Sheet1');
  if (s1 && ss.getSheets().length > 1 && s1.getLastRow() === 0 && s1.getLastColumn() === 0) ss.deleteSheet(s1);
}

// ---------------------------------------------------------------- nightly email: Watchlist summary
// The GitHub workflow writes data/email.json (public market data only: top 10 of each fixed list, new in top 20,
// Darvas breakouts, volume spikes, list changes). This script mails it at ~9 PM IST on market days.
const EMAIL_URL = 'https://raw.githubusercontent.com/dushyantsaini101/nse-scanner/main/data/email.json';
const SITE_URL = 'https://dushyantsaini101.github.io/nse-scanner/';
const EMAIL_HOUR = 21;

/** Run once from the editor: turns on the nightly email + the nightly Telegram cleanup, sends a test email. */
function setupEmail() {
  stopEmail();
  tgSheet_();
  tidyTabs_();
  tgFolder_();                                          // asks for the Drive permission now
  try { readHoldings_(); } catch (err) { Logger.log('Holdings sheet: ' + err.message); }
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'cleanupTelegram')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('dailyEmail').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('cleanupTelegram').timeBased().atHour(3).everyDays(1).inTimezone(TZ).create();
  Logger.log('Email ON: ' + Session.getEffectiveUser().getEmail() + ' pe har market wale din raat ~9 baje. ' +
             'Telegram ki safai roz raat 3 baje. Abhi ek test email bhej raha hoon.');
  dailyEmail(true);
}

/** Run to turn the nightly email off (also removes the old "Aaj ki khaas khabar" email). */
function stopEmail() {
  ScriptApp.getProjectTriggers().filter(t => ['dailyNewsEmail', 'dailyEmail'].indexOf(t.getHandlerFunction()) >= 0)
    .forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log('Email band.');
}

/** Old trigger name (version 3): now sends the new email. */
function dailyNewsEmail() { dailyEmail(); }

/** Runs every hour; sends once a day from 9 PM IST, when today's market data is there (no data = no market = no email). */
function dailyEmail(test) {
  test = test === true;
  const props = PropertiesService.getScriptProperties();
  const now = new Date();
  const today = Utilities.formatDate(now, TZ, 'yyyy-MM-dd');
  const hour = Number(Utilities.formatDate(now, TZ, 'H'));
  if (!test && (props.getProperty('MAIL_SENT') === today || hour < EMAIL_HOUR)) return;
  const d = fetchJson_(EMAIL_URL);
  const to = Session.getEffectiveUser().getEmail();
  if (!d || (!test && d.date !== today)) {
    if (test) MailApp.sendEmail({to: to, name: 'Stock Scanner', subject: 'Test: Stock Scanner email chal raha hai',
      htmlBody: '<p>Email setup ho gaya, par abhi data file nahi mili. Market wale din raat ~9 baje Watchlist summary aayegi.</p>' +
                '<p><a href="' + SITE_URL + '">App kholo</a></p>'});
    return;
  }
  const html = emailHtml_(d, test && d.date !== today);
  MailApp.sendEmail({to: to, name: 'Stock Scanner', htmlBody: html,
                     subject: (test ? 'Test: ' : '') + 'Watchlist summary · ' + d.asOf});
  if (!test) props.setProperty('MAIL_SENT', today);
}

function emailHtml_(d, oldData) {
  const td = 'padding:4px 6px;border-bottom:1px solid #eee;';
  const pct = v => v == null ? '' : '<span style="color:' + (v >= 0 ? '#0a8a4a' : '#c62828') + '">' + (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%</span>';
  const rs = v => '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits: 2});
  const chg = (r, p) => !p ? '<span style="color:#a15c00">naya</span>' : p > r ? '<span style="color:#0a8a4a">▲' + (p - r) + '</span>'
                       : p < r ? '<span style="color:#c62828">▼' + (r - p) + '</span>' : '=';
  const chip = x => '<span style="display:inline-block;margin:2px 6px 2px 0;padding:3px 8px;border:1px solid #ddd;border-radius:12px">' +
                    '<b>' + esc_(x.s) + '</b> <span style="color:#666">' + x.ms + '</span> ' + pct(x.d1) +
                    (x.vx != null && x.vx >= 2 ? ' <span style="color:#666">' + Number(x.vx).toFixed(1) + '×</span>' : '') +
                    (x.list ? ' <span style="color:#666">' + esc_(x.list) + ' #' + x.r + '</span>' : '') + '</span>';
  const h3 = t => '<h3 style="margin:18px 0 6px;font-size:15px">' + t + '</h3>';
  let html = '<div style="font-family:Arial,sans-serif;font-size:14px;max-width:680px">';
  html += '<h2 style="margin:0 0 4px">Watchlist summary · ' + esc_(d.asOf) + '</h2>' +
          '<div style="color:#666;font-size:12px">' + (oldData ? '<b>Test: aaj ka data abhi nahi bana, ye pichhla data hai.</b> ' : '') +
          'Momentum Score aur rank ' + esc_(d.asOf) + ' ke closing price se. Rank change = kal ke rank se.</div>';
  (d.lists || []).forEach(l => {
    html += h3(esc_(l.name) + ' · top 10 <span style="color:#666;font-weight:400;font-size:12px">(' + l.count + ' stocks)</span>');
    if (!l.top.length) { html += '<div style="color:#666">Koi stock nahi.</div>'; return; }
    html += '<table style="border-collapse:collapse;width:100%;font-size:13px"><tr style="color:#666;text-align:left">' +
            '<th style="' + td + '">#</th><th style="' + td + '">Stock</th><th style="' + td + '">Score</th><th style="' + td + '">Close</th>' +
            '<th style="' + td + '">1D</th><th style="' + td + '">Rank</th></tr>';
    l.top.forEach(x => {
      html += '<tr><td style="' + td + '">' + x.r + '</td><td style="' + td + '"><b>' + esc_(x.s) + '</b><br><span style="color:#888;font-size:11px">' +
              esc_(x.n) + '</span></td><td style="' + td + '"><b>' + x.ms + '</b></td><td style="' + td + '">' + rs(x.c) + '</td><td style="' + td + '">' +
              pct(x.d1) + '</td><td style="' + td + '">' + chg(x.r, x.pr) + '</td></tr>';
    });
    html += '</table>';
  });
  html += h3('New in Top 20 (' + (d.new20 || []).length + ')');
  html += (d.new20 || []).length ? d.new20.map(chip).join('') : '<div style="color:#666">Aaj koi naya stock top 20 mein nahi aaya.</div>';
  html += h3('Darvas breakout (' + (d.breakoutCount || 0) + ')');
  html += (d.breakout || []).length ? d.breakout.map(chip).join('') + (d.breakoutCount > d.breakout.length ? '<div style="color:#666;font-size:12px">Score ke hisaab se top ' + d.breakout.length + '.</div>' : '')
        : '<div style="color:#666">Aaj fixed lists mein koi breakout nahi.</div>';
  html += h3('Volume spike ≥2× (' + (d.volumeCount || 0) + ')');
  html += (d.volume || []).length ? d.volume.map(chip).join('') + (d.volumeCount > d.volume.length ? '<div style="color:#666;font-size:12px">Score ke hisaab se top ' + d.volume.length + '.</div>' : '')
        : '<div style="color:#666">Aaj fixed lists mein koi volume spike nahi.</div>';
  html += h3('List changes (' + (d.changes || []).length + ')');
  html += (d.changes || []).length ? '<div>' + d.changes.map(c => '<div style="padding:3px 0">' + (c.change === 'added' ? '<span style="color:#0a8a4a">+ </span>' : '<span style="color:#c62828">− </span>') +
          '<b>' + esc_(c.s) + '</b> ' + (c.change === 'added' ? 'judha' : 'hata') + ': ' + esc_(c.list) + (c.why ? ' <span style="color:#666">(' + esc_(c.why) + ')</span>' : '') + '</div>').join('') + '</div>'
        : '<div style="color:#666">Aaj kisi list mein badlav nahi.</div>';
  html += '<p style="margin-top:18px"><a href="' + SITE_URL + '">App kholo</a> · Not investment advice.</p>' +
          '<p style="color:#999;font-size:11px">Band karna ho to Apps Script mein stopEmail() chalao.</p></div>';
  return html;
}

// ---------------------------------------------------------------- helpers
function fetchJson_(url) {
  try {
    const r = UrlFetchApp.fetch(url + '?v=' + Date.now(), {muteHttpExceptions: true});
    return r.getResponseCode() === 200 ? JSON.parse(r.getContentText()) : null;
  } catch (err) {
    return null;
  }
}
function esc_(t) {
  return String(t == null ? '' : t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}
function keyOk_(k) {
  const key = PropertiesService.getScriptProperties().getProperty('KEY');
  return !!key && String(k || '') === key;
}
function badKeyMsg_() {
  return PropertiesService.getScriptProperties().getProperty('KEY')
    ? 'Galat secret key. Sheet ke "Setup" tab wali key daalo.'
    : 'Pehle Apps Script mein setup() chalao.';
}
function str_(v, max) { return String(v == null ? '' : v).trim().slice(0, max); }
function lists_(a) {
  return Array.from(new Set((a || []).map(Number).filter(n => n >= 1 && n <= 5))).sort();
}
function tags_(list) {
  return Array.from(new Set((Array.isArray(list) ? list : [])
    .map(t => String(t).replace(/,/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 30)).filter(String))).slice(0, 20);
}
function safe_(v) { v = String(v == null ? '' : v); return /^[=+\-@]/.test(v) ? "'" + v : v; }
function out_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
