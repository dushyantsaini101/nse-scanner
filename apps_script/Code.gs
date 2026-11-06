/**
 * Watchlist backend for the Stock Scanner page (GitHub Pages).
 * Stores the watchlist, tags and notes in the "Watchlist" tab of this Google Sheet.
 *
 * Setup (details in README.md):
 *   1. Google Sheet kholo → Extensions → Apps Script → is file ka poora code paste karo → Save.
 *   2. Upar function list mein "setup" chuno → Run → permission do. Secret key Sheet mein dikhegi.
 *   3. Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone → Deploy.
 *   4. Web app URL + secret key website ke Watchlist tab → "Google Sheet connection" mein daalo.
 */

const TAB = 'Watchlist';
// Version 2: "In Meri list" (added by hand) and "Hidden" (removed from the watchlist, notes kept).
// Rows from version 1 have these empty and count as Meri list.
const HEAD = ['Key', 'Symbol', 'Exchange', 'Name', 'Added', 'Tags', 'Notes', 'Updated', 'In Meri list', 'Hidden'];
const KEY_RE = /^[NB]_[A-Za-z0-9_-]{1,40}$/;

/** Run once from the editor: creates the tab and the secret key. */
function setup() {
  const props = PropertiesService.getScriptProperties();
  let key = props.getProperty('KEY');
  if (!key) {
    key = Utilities.getUuid().replace(/-/g, '').slice(0, 16);
    props.setProperty('KEY', key);
  }
  sheet_();
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
  return out_({ok: true, version: 2, items: read_(sheet_())});
}

/** Body (sent as text/plain): {"key": "...", "ops": [...]}
 *  ops: {op:"patch", items:[{k, sym, ex, name, notes?, tags?, mine?, hidden?}]}  (create or change)
 *       {op:"add", items:[...]}  {op:"remove", keys:[...]}  {op:"update", k, notes, tags}  (version 1) */
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
    (body.ops || []).forEach(op => applyOp_(map, op, now));
    const items = Array.from(map.values());
    write_(sh, items);
    return out_({ok: true, version: 2, items: items});
  } finally {
    lock.releaseLock();
  }
}

function applyOp_(map, op, now) {
  if (!op || typeof op !== 'object') return;
  if (op.op === 'patch') {
    (op.items || []).forEach(it => {
      if (!it || !KEY_RE.test(String(it.k))) return;
      let e = map.get(it.k);
      if (!e) {
        e = {k: it.k, sym: str_(it.sym, 40), ex: str_(it.ex, 5), name: str_(it.name, 120),
             added: str_(it.added, 10) || now.slice(0, 10), tags: [], notes: '', mine: false, hidden: false};
        map.set(it.k, e);
      }
      if (it.notes != null) e.notes = str_(it.notes, 5000);
      if (it.tags) e.tags = tags_(it.tags);
      if (it.mine != null) e.mine = !!it.mine;
      if (it.hidden != null) e.hidden = !!it.hidden;
      e.updated = now;
      if (!e.mine && !e.hidden && !e.notes && !e.tags.length) map.delete(it.k);   // nothing left to keep
    });
    return;
  }
  if (op.op === 'add') {
    (op.items || []).forEach(it => {
      if (!it || !KEY_RE.test(String(it.k))) return;
      const e = map.get(it.k);
      if (!e) {
        map.set(it.k, {
          k: it.k, sym: str_(it.sym, 40), ex: str_(it.ex, 5), name: str_(it.name, 120),
          added: str_(it.added, 10) || now.slice(0, 10),
          tags: tags_(it.tags), notes: str_(it.notes, 5000), updated: now,
          mine: it.mine == null ? true : !!it.mine, hidden: !!it.hidden
        });
      } else {
        if (!e.notes && it.notes) e.notes = str_(it.notes, 5000);
        const t = tags_(it.tags);
        if (t.length) e.tags = Array.from(new Set(e.tags.concat(t)));
        if (it.mine != null) e.mine = e.mine || !!it.mine;
        e.updated = now;
      }
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
  if (sh.getLastRow() === 0 || sh.getRange(1, HEAD.length).getValue() !== HEAD[HEAD.length - 1]) {
    sh.getRange(1, 1, 1, HEAD.length).setValues([HEAD]).setFontWeight('bold');   // new sheet, or upgrade from version 1
    sh.setFrozenRows(1);
  }
  return sh;
}

function read_(sh) {
  const n = sh.getLastRow() - 1;
  if (n < 1) return [];
  return sh.getRange(2, 1, n, HEAD.length).getDisplayValues()
    .filter(r => KEY_RE.test(r[0]))
    .map(r => ({
      k: r[0], sym: r[1], ex: r[2], name: r[3], added: r[4],
      tags: tags_(r[5].split(',')), notes: r[6], updated: r[7],
      mine: r[8] === '' ? true : r[8].toLowerCase() === 'yes', hidden: r[9].toLowerCase() === 'yes'
    }));
}

function write_(sh, items) {
  const last = sh.getLastRow();
  if (last > 1) sh.getRange(2, 1, last - 1, HEAD.length).clearContent();
  if (!items.length) return;
  const rng = sh.getRange(2, 1, items.length, HEAD.length);
  rng.setNumberFormat('@');                     // plain text: no auto dates / numbers
  rng.setValues(items.map(i => [i.k, i.sym, i.ex, i.name, i.added, i.tags.join(', '), i.notes, i.updated,
                                i.mine === false ? 'no' : 'yes', i.hidden ? 'yes' : 'no'].map(safe_)));
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
function tags_(list) {
  return Array.from(new Set((Array.isArray(list) ? list : [])
    .map(t => String(t).replace(/,/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 30)).filter(String))).slice(0, 20);
}
function safe_(v) { v = String(v == null ? '' : v); return /^[=+\-@]/.test(v) ? "'" + v : v; }
function out_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
