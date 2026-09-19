// Drive the config editor page in jsdom with a mocked server and report exactly
// which option every save posts, through every save path the page has.
//
//   npm install jsdom@22            # once, anywhere on the path (jsdom >= 23 needs Node 20)
//   node tests/ui/drive.js [index.html] [state.json]
//
// The state defaults to tests/ui/state.json, a fleet snapshot whose rms_data_quota and
// continuous_capture_quota rows differ between stations (the scenario behind the checks).
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || path.join(__dirname, '..', '..', 'config_editor', 'static', 'index.html');
const stateFile = process.argv[3] || path.join(__dirname, 'state.json');
const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
const html = fs.readFileSync(file, 'utf8').replace('/*__STATE__*/null', JSON.stringify(state));

const posts = [];
let pollState = state;
function makeFetch(win) {
  return async (url, opts = {}) => {
    if ((opts.method || 'GET') === 'POST') {
      const body = JSON.parse(opts.body);
      posts.push({ url, option: body.option, section: body.section, values: body.values });
      const payload = JSON.parse(JSON.stringify(state));
      payload.result = { written: Object.keys(body.values || {}), conflict: [], backups: {} };
      payload.warnings = {};
      return { ok: true, status: 200, json: async () => payload };
    }
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(pollState)) };
  };
}

const dom = new JSDOM(html, { url: 'http://localhost:8421/', runScripts: 'dangerously', pretendToBeVisual: true,
  beforeParse(win) { win.fetch = makeFetch(win); win.confirm = () => true; } });
const win = dom.window, doc = win.document;
const $ = (id) => doc.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function rowByName(name) {
  return [...doc.querySelectorAll('tbody tr.row')].find((tr) => tr.querySelector('td.opt').firstChild.textContent.trim() === name);
}
function inputs() { return [...$('rows').querySelectorAll('input')].map((i) => i.value); }
function keyEnter(el) { el.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); }
function keyEsc() { doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); }
let n = 0;
function check(label, cond, detail) { n++; console.log(`${cond ? 'PASS' : 'FAIL'} ${n}. ${label}${detail ? '  [' + detail + ']' : ''}`); if (!cond) process.exitCode = 1; }
const lastPost = () => posts[posts.length - 1];

(async () => {
  await sleep(50);
  check('page rendered rows', doc.querySelectorAll('tbody tr.row').length > 100);

  // A. open rms_data_quota, "Apply to all" (fill), then Save button
  rowByName('rms_data_quota').click();
  check('drawer shows the clicked option', $('dname').textContent === 'rms_data_quota', $('dname').textContent);
  $('setall').value = '965';
  $('setall-btn').click();
  check('fill puts 965 in every station box, posts nothing', inputs().every((v) => v === '965') && posts.length === 0);
  $('dsave').click(); await sleep(30);
  check('Save posts rms_data_quota', lastPost().option === 'rms_data_quota', JSON.stringify(lastPost()));
  check('Save posts only the stations that changed (E, F were already 965)',
        Object.keys(lastPost().values).sort().join() === 'US005A,US005B,US005C,US005D', Object.keys(lastPost().values).join());

  // B. open rms_data_quota, fill, Enter in the fill box (fill + save)
  rowByName('rms_data_quota').click();
  $('setall').value = '965'; keyEnter($('setall')); await sleep(30);
  check('Enter in the fill box posts the open option', lastPost().option === 'rms_data_quota', lastPost().option);

  // C. the suspected sequence: fill on one row, Esc without saving, open the row below, fill + Enter
  const before = posts.length;
  rowByName('rms_data_quota').click();
  $('setall').value = '965'; $('setall-btn').click();
  keyEsc();
  check('Esc discards staged values without posting', posts.length === before && !$('drawer').classList.contains('open'));
  rowByName('continuous_capture_quota').click();
  check('drawer now shows continuous_capture_quota', $('dname').textContent === 'continuous_capture_quota', $('dname').textContent);
  $('setall').value = '965'; keyEnter($('setall')); await sleep(30);
  check('...and Enter posts continuous_capture_quota for all six', lastPost().option === 'continuous_capture_quota'
        && Object.keys(lastPost().values).length === 6, lastPost().option);

  // D. drawer open on rms_data_quota while a poll sees files change on disk
  rowByName('rms_data_quota').click();
  pollState = JSON.parse(JSON.stringify(state)); pollState.files.forEach((f) => f.mtime += 1);
  await win.eval('poll()'); await sleep(20);
  check('poll during editing only raises the banner, keeps the drawer on the same option',
        $('banner').classList.contains('show') && $('dname').textContent === 'rms_data_quota');
  $('setall').value = '965'; keyEnter($('setall')); await sleep(30);
  check('save after that still posts rms_data_quota', lastPost().option === 'rms_data_quota', lastPost().option);
  pollState = state;

  // E. with "only varying" on (rows adjacent), clicking each of the two rows opens the right one
  $('only-vary').checked = true; $('only-vary').dispatchEvent(new win.Event('change'));
  const names = [...doc.querySelectorAll('tbody tr.row')].map((tr) => tr.querySelector('td.opt').firstChild.textContent.trim());
  const i = names.indexOf('rms_data_quota');
  check('in the varying view continuous_capture_quota is the very next row', names[i + 1] === 'continuous_capture_quota', names.slice(i, i + 2).join(' > '));
  rowByName('rms_data_quota').click();
  check('clicking rms_data_quota there opens rms_data_quota', $('dname').textContent === 'rms_data_quota');
  rowByName('continuous_capture_quota').click();
  check('clicking the next row switches the drawer to it', $('dname').textContent === 'continuous_capture_quota');
  keyEsc();

  // F. a re-render (filter typed) while the drawer is open keeps the row identity
  rowByName('rms_data_quota').click();
  $('q').value = 'quota'; $('q').dispatchEvent(new win.Event('input'));
  $('setall').value = '900'; keyEnter($('setall')); await sleep(30);
  check('re-render by filtering does not change which option is saved', lastPost().option === 'rms_data_quota' && lastPost().values.US005A === '900');

  // G. per-station input Enter
  $('q').value = ''; $('q').dispatchEvent(new win.Event('input'));
  rowByName('rms_data_quota').click();
  const inp = $('rows').querySelector('input'); inp.value = '901'; inp.dispatchEvent(new win.Event('input')); keyEnter(inp); await sleep(30);
  check('Enter in a station box posts that option for that station only', lastPost().option === 'rms_data_quota'
        && Object.keys(lastPost().values).join() === 'US005A', JSON.stringify(lastPost().values));

  console.log(`\n${file}: ${posts.length} posts, options posted: ${[...new Set(posts.map((p) => p.option))].join(', ')}`);
  win.close();
})();
