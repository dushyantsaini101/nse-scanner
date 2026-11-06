/**
 * Watchlist backend for the Stock Scanner page (GitHub Pages)  — version 3
 * Stores, in this Google Sheet only (never in the public GitHub repo):
 *   - "Watchlist" tab: one row per stock — Watchlist 1-5 membership, notes, tags, Old-list removals
 *   - "Rank history" tab: daily rank of each stock inside Watchlist 1-5
 *
 * Setup (details in README.md):
 *   1. Google Sheet → Extensions → Apps Script → is file ka poora code paste karo → Save.
 *   2. Function list mein "setup" chuno → Run → permission do. Secret key "Setup" tab mein dikhegi.
 *   3. Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone → Deploy.
 *      (Pehle se deployed ho to: Deploy → Manage deployments → ✏️ → Version: New version → Deploy.)
 *   4. Web app URL + secret key website ke Watchlist tab → "Google Sheet connection" mein daalo.
 * Sheets made by version 1 or 2 are upgraded automatically ("Meri list" becomes Watchlist 1).
 *
 * Daily email "Aaj ki khaas khabar" (optional): function list mein "setupEmail" chuno → Run → permission do.
 * Ek test email turant aayegi; phir har market wale din shaam ko. Band karna ho to "stopEmail" chalao.
 */

const VERSION = 3;
const TAB = 'Watchlist';
const HIST = 'Rank history';
const HEAD = ['Key', 'Symbol', 'Exchange', 'Name', 'Added', 'Tags', 'Notes', 'Updated', 'Watchlists', 'Old list removed till'];
const HIST_HEAD = ['Date', 'List', 'Rank', 'Symbol', 'Key', 'Score'];
const HIST_DAYS = 180;
const KEY_RE = /^[NB]_[A-Za-z0-9_-]{1,40}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

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
  const ss = SpreadsheetApp.getActive();
  let info = ss.getSheetByName('Setup');
  if (!info) info = ss.insertSheet('Setup');
  info.getRange('A1:B2').setValues([['Secret key (website mein daalo)', key],
                                    ['Note', 'Ye key kisi ko mat do. Nayi key chahiye to Apps Script mein newKey() chalao.']]);
  info.autoResizeColumns(1, 2);
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
  if (p.hist) return out_({ok: true, version: VERSION, hist: histFor_(String(p.hist))});
  return out_({ok: true, version: VERSION, items: read_(sheet_())});
}

/** Body (sent as text/plain): {"key": "...", "ops": [...]}
 *  {op:"patch", items:[{k, sym, ex, name, notes?, tags?, lists?, addList?, delList?, oldDel?}]}  create or change a stock
 *  {op:"hist", date, list, rows:[[key, symbol, rank, score], ...]}                              rank history of one list
 *  version 1/2 ops: add, remove, update, and patch with mine/hidden */
function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return out_({ok: false, error: 'Request samajh nahi aayi'});
  }
  if (!keyOk_(body.key)) return out_({ok: false, error: badKeyMsg_()});
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    const sh = sheet_();
    const map = new Map(read_(sh).map(i => [i.k, i]));
    const now = Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd HH:mm');
    const hist = [];
    (body.ops || []).forEach(op => {
      if (op && op.op === 'hist') hist.push(op);
      else applyOp_(map, op, now);
    });
    const items = Array.from(map.values());
    write_(sh, items);
    if (hist.length) writeHist_(hist);
    return out_({ok: true, version: VERSION, items: items});
  } finally {
    lock.releaseLock();
  }
}

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
  const cutoff = Utilities.formatDate(new Date(Date.now() - HIST_DAYS * 864e5), 'Asia/Kolkata', 'yyyy-MM-dd');
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

// ---------------------------------------------------------------- daily email: "Aaj ki khaas khabar"
// The GitHub workflow writes data/news/khaas.json (public filings / deals only). This script reads it, adds
// today's news of the stocks in your Watchlist 1-5 (from this Sheet), and mails it to you once every market evening.
const NEWS_URL = 'https://raw.githubusercontent.com/dushyantsaini101/nse-scanner/main/data/news/';
const SITE_URL = 'https://dushyantsaini101.github.io/nse-scanner/';
const CAT_NAME = {R: 'Results', O: 'Order/Contract', D: 'Dividend/Bonus/Split', B: 'Board Meeting', F: 'Fund Raise',
                  P: 'Promoter/Insider', K: 'Bulk/Block Deal', M: 'Media News'};

/** Run once from the editor: turns the daily email on and sends a test email right away. */
function setupEmail() {
  stopEmail();
  ScriptApp.newTrigger('dailyNewsEmail').timeBased().everyHours(1).create();
  Logger.log('Daily email ON: ' + Session.getEffectiveUser().getEmail() +
             ' pe har market wale din shaam ~9-11 baje aayega. Abhi ek test email bhej raha hoon.');
  dailyNewsEmail(true);
}

/** Run to turn the daily email off. */
function stopEmail() {
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'dailyNewsEmail')
    .forEach(t => ScriptApp.deleteTrigger(t));
}

/** Runs every hour; sends once a day, after the evening news update (or at 11 PM with what is there). */
function dailyNewsEmail(test) {
  test = test === true;
  const props = PropertiesService.getScriptProperties();
  const now = new Date();
  const today = Utilities.formatDate(now, 'Asia/Kolkata', 'yyyy-MM-dd');
  const hour = Number(Utilities.formatDate(now, 'Asia/Kolkata', 'H'));
  if (!test && (props.getProperty('MAIL_SENT') === today || hour < 19)) return;
  const kh = fetchJson_(NEWS_URL + 'khaas.json');
  const to = Session.getEffectiveUser().getEmail();
  if (!kh || kh.date !== today) {
    if (test) MailApp.sendEmail({to: to, name: 'Stock Scanner', subject: 'Test: Stock Scanner daily email chal raha hai',
      htmlBody: '<p>Email setup ho gaya. Aaj ki khabar abhi bani nahi (market band ya update baaki). ' +
                'Market wale din shaam ko "Aaj ki khaas khabar" aayegi.</p><p><a href="' + SITE_URL + '">Website kholo</a></p>'});
    return;
  }
  if (!test && !kh.final && hour < 23) return;          // wait for the evening update
  let mine = [];
  try {
    const keys = new Set(read_(sheet_()).filter(i => i.lists.length).map(i => i.k));
    if (keys.size) {
      const day = fetchJson_(NEWS_URL + today + '.json');
      mine = ((day && day.items) || []).filter(it => keys.has(it[2])).slice(0, 60);
    }
  } catch (err) { /* watchlist part is optional */ }
  const items = kh.items || [];
  const dt = Utilities.formatDate(now, 'Asia/Kolkata', 'd MMM yyyy');
  const pct = v => v == null ? '' : ' <span style="color:' + (v >= 0 ? '#0a8a4a' : '#c62828') + '">' + (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%</span>';
  const link = (url, text) => /^https?:\/\//.test(url || '') ? '<a href="' + esc_(url) + '">' + text + '</a>' : text;
  let html = '<div style="font-family:Arial,sans-serif;font-size:14px;max-width:640px">';
  html += '<h2 style="margin:0 0 4px">Aaj ki khaas khabar · ' + dt + '</h2>' +
          '<div style="color:#666;font-size:12px;margin-bottom:12px">Results, bade orders, promoter buying aur bulk/block buying — ' +
          'Momentum Score 60+ wale stocks (promoter buying aur ₹500 Cr+ orders sab stocks ke). Score kal ke close se.</div>';
  if (!items.length) html += '<p>Aaj koi khaas khabar nahi mili.</p>';
  let last = '';
  items.forEach(x => {
    if (x.why !== last) { html += '<h3 style="margin:14px 0 4px;font-size:15px">' + esc_(x.why) + '</h3>'; last = x.why; }
    html += '<div style="padding:6px 0;border-bottom:1px solid #eee"><b>' + esc_(x.sym) + '</b> <span style="color:#666">score ' + x.score + '</span>' +
            pct(x.chg) + '<br>' + link(x.url, esc_(x.title)) + ' <span style="color:#999;font-size:12px">' + esc_(x.src) + ' · ' + esc_(String(x.t).slice(11, 16)) + '</span></div>';
  });
  if (mine.length) {
    html += '<h3 style="margin:18px 0 4px;font-size:15px">Aapki Watchlist ki aaj ki khabar (' + mine.length + ')</h3>';
    mine.forEach(it => {
      html += '<div style="padding:6px 0;border-bottom:1px solid #eee"><b>' + esc_(it[3]) + '</b> <span style="color:#666">' +
              esc_(String(it[4]).split('').map(c => CAT_NAME[c] || c).join(', ')) + '</span><br>' +
              link(it[6], esc_(it[5])) + ' <span style="color:#999;font-size:12px">' + esc_(it[7]) + ' · ' + esc_(String(it[1]).slice(11, 16)) + '</span></div>';
    });
  }
  html += '<p style="margin-top:16px"><a href="' + SITE_URL + '">Website ka News tab kholo</a> · Not investment advice.</p>' +
          '<p style="color:#999;font-size:11px">Band karna ho to Apps Script mein stopEmail() chalao.</p></div>';
  MailApp.sendEmail({to: to, name: 'Stock Scanner', htmlBody: html,
                     subject: (test ? 'Test: ' : '') + 'Aaj ki khaas khabar · ' + dt + ' (' + items.length + ')'});
  if (!test) props.setProperty('MAIL_SENT', today);
}

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
