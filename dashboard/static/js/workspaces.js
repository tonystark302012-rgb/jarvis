// JARVIS · SPACES — Notion-like workspace: tree, block editor (slash),
// markdown source mode, revisions, approval cards, per-page Dot chat.
// Block JSON is canonical (matches dots/blocks.py); the editor edits it
// directly and PATCHes with base_rev — a stale rev NEVER overwrites.
(() => {
const root = document.getElementById('pane-spaces');
if (!root) return;

root.innerHTML = `
  <div class="ws" id="ws-grid">
    <aside class="ws-tree">
      <div class="ws-tree-head">
        <span class="sec-title">Spaces</span>
        <button class="btn-sm" id="ws-add-space" title="New space">＋</button>
      </div>
      <input class="inp ws-search" id="ws-search" placeholder="Search pages…">
      <div id="ws-tree-body"><div class="pane-empty">No spaces yet — hit ＋ to create one.</div></div>
      <div class="ws-tree-foot">
        <button class="btn-sm" id="ws-new-page" style="width:100%">＋ New page</button>
      </div>
    </aside>

    <section class="ws-main">
      <div id="ws-gallery" class="ws-gallery" style="display:none"></div>
      <div id="ws-editor-wrap" style="display:none;flex:1;min-height:0;display:none;flex-direction:column">
        <div class="ws-toolbar">
          <input id="ws-title" placeholder="Untitled" autocomplete="off">
          <span class="chip accent" id="ws-rev">rev –</span>
          <span id="ws-save-state">saved</span>
          <div class="seg">
            <button id="ws-mode-edit" class="on">EDIT</button>
            <button id="ws-mode-src">SOURCE</button>
          </div>
          <button class="btn-sm" id="ws-plus-block">＋ Block</button>
        </div>
        <div class="ws-conflict" id="ws-conflict"></div>
        <div id="ws-editor"></div>
        <div id="ws-source-wrap">
          <textarea class="inp" id="ws-source" spellcheck="false"></textarea>
          <div class="ws-source-hint">Markdown source mode — canonical blocks are rebuilt from this on save.</div>
        </div>
      </div>
      <div id="ws-placeholder" class="pane-empty">
        Pick a page from the tree, or create a space with ＋ — pages support
        block editing with <b>/</b>, source mode, revisions and per-page Dot chat.
      </div>
    </section>

    <aside class="ws-side" id="ws-side" style="display:none">
      <div class="side-tabs">
        <button data-s="approvals" class="on">Approvals</button>
        <button data-s="history">History</button>
        <button data-s="chat">Chat</button>
      </div>
      <div class="side-body" id="ws-side-body"></div>
    </aside>
  </div>`;

const $ = (id) => document.getElementById(id);
const grid = $('ws-grid'), treeBody = $('ws-tree-body'),
      gallery = $('ws-gallery'), editorWrap = $('ws-editor-wrap'),
      placeholder = $('ws-placeholder'), editor = $('ws-editor'),
      conflictBox = $('ws-conflict'), side = $('ws-side'),
      sideBody = $('ws-side-body'), sourceWrap = $('ws-source-wrap'),
      sourceTa = $('ws-source'), titleInp = $('ws-title');

const S = {
  spaces: [], dots: [], expanded: new Set(), selSpace: null,
  page: null, blocks: [], rev: null, dirty: false, mode: 'edit',
  galMode: 'grid', sideTab: 'approvals', loaded: false,
  saveTimer: null, chatTimer: null, histSel: null, pending: [],
};

const SLASH = [
  ['text',      'Aa', 'Text',        'Plain paragraph'],
  ['bullet',    '•',  'Bullet',      'Bulleted list item'],
  ['numbered',  '1.', 'Numbered',    'Numbered list item'],
  ['checklist', '☑',  'Checklist',   'To-do with a checkbox'],
  ['quote',     '“',  'Quote',       'Highlighted quotation'],
  ['code',      '</>','Code',        'Monospace code block'],
  ['divider',   '—',  'Divider',     'Horizontal separation line'],
  ['table',     '▦',  'Table',       '3×3 table with header'],
];

// ── data ────────────────────────────────────────────────────────────────────
async function loadTree() {
  try {
    S.spaces = await JV.api('/api/spaces');
    for (const sp of S.spaces) sp.pages = await JV.api(`/api/spaces/${sp.id}/pages`);
  } catch (e) { S.spaces = []; }
  try { S.dots = await JV.api('/api/dots'); } catch (_) { S.dots = []; }
  renderTree();
}

function pageById(id) {
  for (const sp of S.spaces)
    for (const p of (sp.pages || [])) if (p.id === id) return { sp, p };
  return null;
}
function nestPages(pages) {
  const byParent = {};
  pages.forEach(p => {
    const k = p.parent_id == null ? 0 : p.parent_id;
    (byParent[k] = byParent[k] || []).push(p);
  });
  return byParent;
}

// ── tree ────────────────────────────────────────────────────────────────────
function renderTree() {
  const q = ($('ws-search').value || '').trim().toLowerCase();
  if (!S.spaces.length) {
    treeBody.innerHTML = '<div class="pane-empty">No spaces yet — hit ＋ to create one.</div>';
    return;
  }
  treeBody.innerHTML = '';
  for (const sp of S.spaces) {
    const spMatch = !q || sp.name.toLowerCase().includes(q);
    const kids = nestPages((sp.pages || []).filter(
      p => !q || p.title.toLowerCase().includes(q)));
    const hasAny = Object.values(kids).some(a => a.length) || spMatch;
    if (q && !hasAny) continue;
    const box = document.createElement('div');
    box.className = 'tree-space';
    const srow = document.createElement('div');
    srow.className = 'tree-row tree-space-row' + (S.selSpace === sp.id ? ' sel' : '');
    srow.innerHTML = `<span class="tw">${S.expanded.has(sp.id) || q ? '▾' : '▸'}</span>
      <span class="ti">📚</span><span class="tn"></span>
      <button class="tact" title="New page in this space">＋</button>`;
    srow.querySelector('.tn').textContent = sp.name;
    srow.onclick = (e) => {
      if (e.target.classList.contains('tact')) { newPage(sp.id); return; }
      S.selSpace = sp.id; S.page = null;
      if (S.expanded.has(sp.id)) S.expanded.delete(sp.id);
      else S.expanded.add(sp.id);
      renderTree(); showGallery(sp.id);
    };
    box.appendChild(srow);
    if (S.expanded.has(sp.id) || q) {
      const kidsEl = document.createElement('div');
      kidsEl.className = 'tree-kids';
      const walk = (list, depth) => {
        for (const pg of list) {
          const row = document.createElement('div');
          row.className = 'tree-row' + (S.page && S.page.id === pg.id ? ' sel' : '');
          row.style.paddingLeft = (8 + depth * 10) + 'px';
          row.innerHTML = `<span class="ti">📄</span><span class="tn"></span>
            <button class="tact" title="Sub-page">＋</button>`;
          row.querySelector('.tn').textContent = pg.title || 'Untitled';
          row.onclick = (e) => {
            if (e.target.classList.contains('tact')) {
              e.stopPropagation(); newPage(sp.id, pg.id); return;
            }
            openPage(pg.id);
          };
          kidsEl.appendChild(row);
          walk(kids[pg.id] || [], depth + 1);
        }
      };
      walk(kids[0] || [], 0);
      box.appendChild(kidsEl);
    }
    treeBody.appendChild(box);
  }
}

$('ws-search').addEventListener('input', renderTree);

$('ws-add-space').onclick = () => {
  const old = $('ws-add-space');
  const inp = document.createElement('input');
  inp.className = 'inp'; inp.placeholder = 'Space name — Enter';
  inp.style.margin = '0 10px 8px';
  old.parentNode.after(inp); inp.focus();
  const done = async () => {
    const name = inp.value.trim();
    inp.remove();
    if (!name) return;
    try {
      await JV.api('/api/spaces', { method: 'POST',
        body: JSON.stringify({ name }) });
      JV.toast('Space created');
      await loadTree();
    } catch (e) { JV.toast(e.message); }
  };
  inp.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') done();
    if (ev.key === 'Escape') inp.remove();
  });
  inp.addEventListener('blur', done);
};

async function newPage(spaceId, parentId) {
  if (!spaceId) {
    if (!S.spaces.length) { JV.toast('Create a space first'); return; }
    spaceId = S.selSpace || S.spaces[0].id;
  }
  try {
    const pg = await JV.api(`/api/spaces/${spaceId}/pages`, {
      method: 'POST',
      body: JSON.stringify({ title: 'Untitled',
        content_json: [{ type: 'text', text: '' }],
        parent_id: parentId || null }) });
    await loadTree();
    openPage(pg.id);
  } catch (e) { JV.toast(e.message); }
}
$('ws-new-page').onclick = () => newPage(null);

// ── gallery (grid/list of a space's pages) ──────────────────────────────────
function showGallery(spaceId) {
  S.selSpace = spaceId;
  placeholder.style.display = 'none';
  editorWrap.style.display = 'none';
  side.style.display = 'none';
  gallery.style.display = 'block';
  const sp = S.spaces.find(s => s.id === spaceId);
  if (!sp) return;
  const flat = [];
  const walk = (list) => list.forEach(p => { flat.push(p); walk((sp.pages || []).filter(c => c.parent_id === p.id)); });
  walk((sp.pages || []).filter(p => p.parent_id == null));
  const items = flat.map(p => `
    <div class="gal-item" data-id="${p.id}">
      <div class="gi-t">${JV.esc(p.title || 'Untitled')}</div>
      <div class="gi-m">rev ${p.rev} · #${p.id}${p.parent_id ? ' · sub-page' : ''}</div>
    </div>`).join('');
  gallery.innerHTML = `
    <div class="sec-head">
      <span class="sec-title">${JV.esc(sp.name)}</span>
      <span class="sec-sub">${flat.length} page(s)</span>
      <div class="grow"></div>
      <div class="seg">
        <button id="gal-grid" class="${S.galMode === 'grid' ? 'on' : ''}">GRID</button>
        <button id="gal-list" class="${S.galMode === 'list' ? 'on' : ''}">LIST</button>
      </div>
      <button class="btn-sm primary" id="gal-new">＋ New page</button>
    </div>
    <div class="${S.galMode === 'grid' ? 'gal-grid' : 'gal-list'}" id="gal-items">
      ${items || ''}
      <div class="gal-item gal-new" id="gal-empty-new">＋ Create the first page</div>
    </div>`;
  gallery.querySelectorAll('.gal-item[data-id]').forEach(el => {
    el.onclick = () => openPage(parseInt(el.dataset.id, 10));
  });
  $('gal-new').onclick = () => newPage(spaceId);
  $('gal-empty-new').onclick = () => newPage(spaceId);
  $('gal-grid').onclick = () => { S.galMode = 'grid'; showGallery(spaceId); };
  $('gal-list').onclick = () => { S.galMode = 'list'; showGallery(spaceId); };
}

// ── page + editor ───────────────────────────────────────────────────────────
async function openPage(id) {
  let pg;
  try { pg = await JV.api(`/api/pages/${id}`); }
  catch (e) { JV.toast(e.message); return; }
  S.page = pg;
  S.rev = pg.rev;
  S.blocks = (pg.blocks && pg.blocks.length) ? pg.blocks
           : [{ type: 'text', text: '' }];
  S.dirty = false; S.mode = 'edit'; S.histSel = null;
  hideConflict();
  placeholder.style.display = 'none';
  gallery.style.display = 'none';
  editorWrap.style.display = 'flex';
  side.style.display = 'flex';
  titleInp.value = pg.title || '';
  $('ws-rev').textContent = 'rev ' + pg.rev;
  setSaveState('saved', 'saved');
  $('ws-mode-edit').classList.add('on');
  $('ws-mode-src').classList.remove('on');
  sourceWrap.classList.remove('on');
  editor.style.display = 'block';
  renderBlocks();
  const hit = pageById(id);
  if (hit) S.selSpace = hit.sp.id;
  renderTree();
  showSide(S.sideTab);
}

function setSaveState(txt, cls) {
  const el = $('ws-save-state');
  el.textContent = txt; el.className = cls || '';
}

function markDirty() {
  S.dirty = true;
  setSaveState('saving…', 'dirty');
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(save, 1200);
}

async function save() {
  clearTimeout(S.saveTimer);
  if (!S.page) return;
  if (S.mode === 'source') return saveSource();
  const blocks = readBlocks();
  if (!blocks.length) blocks.push({ type: 'text', text: '' });
  try {
    const pg = await JV.api(`/api/pages/${S.page.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ base_rev: S.rev, title: titleInp.value,
                             content_json: blocks }) });
    S.rev = pg.rev; S.blocks = pg.blocks || blocks; S.dirty = false;
    $('ws-rev').textContent = 'rev ' + pg.rev;
    setSaveState('saved ✓', 'saved');
    loadTreeSilently();
  } catch (e) {
    if (e.status === 409 && e.data && e.data.conflict) showConflict(e.data);
    else { setSaveState('error', 'dirty'); JV.toast('Save failed: ' + e.message); }
  }
}

async function saveSource() {
  try {
    const pg = await JV.api(`/api/pages/${S.page.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ base_rev: S.rev, title: titleInp.value,
                             content_md: sourceTa.value }) });
    S.rev = pg.rev; S.dirty = false;
    $('ws-rev').textContent = 'rev ' + pg.rev;
    setSaveState('saved ✓', 'saved');
    S.blocks = (pg.blocks && pg.blocks.length) ? pg.blocks
             : [{ type: 'text', text: '' }];
  } catch (e) {
    if (e.status === 409 && e.data && e.data.conflict) showConflict(e.data);
    else { setSaveState('error', 'dirty'); JV.toast('Save failed: ' + e.message); }
  }
}

async function loadTreeSilently() { try { await loadTree(); } catch (_) {} }

function showConflict(data) {
  setSaveState('conflict', 'dirty');
  conflictBox.classList.add('show');
  conflictBox.innerHTML = `
    <b>⚠ Conflict — page moved to rev ${data.current_rev}.</b>
    <span>Your edit was NOT saved (stale base ${S.rev}); nothing was overwritten.</span>
    <div class="grow"></div>
    <button class="btn-sm danger" id="ws-load-latest">Load latest (drop mine)</button>
    <button class="btn-sm" id="ws-conf-dismiss">Keep editing</button>`;
  $('ws-load-latest').onclick = () => openPage(S.page.id);
  $('ws-conf-dismiss').onclick = hideConflict;
}
function hideConflict() { conflictBox.classList.remove('show'); conflictBox.innerHTML = ''; }

// mode toggle
$('ws-mode-edit').onclick = () => setMode('edit');
$('ws-mode-src').onclick  = () => setMode('source');
async function setMode(m) {
  if (S.mode === m || !S.page) return;
  if (S.dirty) await save();
  S.mode = m;
  $('ws-mode-edit').classList.toggle('on', m === 'edit');
  $('ws-mode-src').classList.toggle('on', m === 'source');
  if (m === 'source') {
    editor.style.display = 'none';
    sourceWrap.classList.add('on');
    try {
      const pg = await JV.api(`/api/pages/${S.page.id}`);
      sourceTa.value = pg.content_md || '';
      S.rev = pg.rev;
      $('ws-rev').textContent = 'rev ' + pg.rev;
    } catch (e) { JV.toast(e.message); }
  } else {
    sourceWrap.classList.remove('on');
    editor.style.display = 'block';
    try {
      const pg = await JV.api(`/api/pages/${S.page.id}`);
      S.rev = pg.rev; S.blocks = pg.blocks || [{ type: 'text', text: '' }];
      $('ws-rev').textContent = 'rev ' + pg.rev;
      renderBlocks();
    } catch (e) { JV.toast(e.message); }
  }
}
sourceTa.addEventListener('input', () => { S.dirty = true;
  setSaveState('saving…', 'dirty');
  clearTimeout(S.saveTimer); S.saveTimer = setTimeout(saveSource, 1400); });
titleInp.addEventListener('input', markDirty);

// ── block rendering ─────────────────────────────────────────────────────────
function renderBlocks() {
  editor.innerHTML = '';
  let n = 0;
  S.blocks.forEach((b, i) => {
    if (b.type === 'numbered') n++; else n = 0;
    editor.appendChild(blockNode(b, i, n));
  });
  if (!S.blocks.length) {
    S.blocks = [{ type: 'text', text: '' }];
    editor.appendChild(blockNode(S.blocks[0], 0, 0));
  }
}

function blockNode(b, i, num) {
  const el = document.createElement('div');
  el.className = 'blk';
  el.dataset.t = b.type;
  el.dataset.i = i;
  if (b.type === 'checklist') el.dataset.checked = b.checked ? '1' : '0';

  if (b.type === 'divider') {
    const hr = document.createElement('hr');
    hr.className = 'blk-hr';
    el.appendChild(hr);
  } else if (b.type === 'checklist') {
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.className = 'blk-check';
    cb.checked = !!b.checked;
    cb.onchange = () => { S.blocks[i].checked = cb.checked;
      el.dataset.checked = cb.checked ? '1' : '0'; markDirty(); };
    el.appendChild(cb);
    el.appendChild(textNode(b));
  } else if (b.type === 'table') {
    el.appendChild(tableNode(b, i));
  } else {
    const mark = document.createElement('span');
    mark.className = 'blk-mark';
    if (b.type === 'bullet')    mark.textContent = '•';
    else if (b.type === 'numbered') mark.textContent = num + '.';
    else if (b.type === 'quote')    mark.textContent = '';
    else if (b.type === 'code')     mark.textContent = '⌘';
    else mark.textContent = '';
    el.appendChild(mark);
    el.appendChild(textNode(b));
  }

  const del = document.createElement('button');
  del.className = 'blk-del'; del.textContent = '×';
  del.title = 'Delete block';
  del.onclick = (e) => {
    e.stopPropagation();
    S.blocks.splice(i, 1);
    if (!S.blocks.length) S.blocks.push({ type: 'text', text: '' });
    markDirty(); renderBlocks();
  };
  el.appendChild(del);
  return el;
}

function textNode(b) {
  const t = document.createElement('div');
  t.className = 'blk-text';
  t.contentEditable = 'true';
  t.spellcheck = false;
  t.textContent = b.text || '';
  if (!b.text && (b.type === 'text'))
    t.dataset.ph = 'Type, or press / for blocks';
  return t;
}

function tableNode(b, i) {
  const wrap = document.createElement('div');
  wrap.className = 'blk-table';
  const tbl = document.createElement('table');
  const rows = (b.rows && b.rows.length) ? b.rows : [['', '', ''], ['', '', ''], ['', '', '']];
  rows.forEach((row, ri) => {
    const tr = document.createElement('tr');
    row.forEach((cell, ci) => {
      const c = document.createElement(b.header && ri === 0 ? 'th' : 'td');
      c.contentEditable = 'true'; c.spellcheck = false;
      c.textContent = cell;
      c.oninput = () => { S.blocks[i].rows[ri][ci] = c.innerText; markDirty(); };
      c.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); c.blur(); } };
      tr.appendChild(c);
    });
    tbl.appendChild(tr);
  });
  wrap.appendChild(tbl);
  return wrap;
}

function readBlocks() {
  const nodes = Array.from(editor.querySelectorAll('.blk'));
  return nodes.map((el) => {
    const i = parseInt(el.dataset.i, 10);
    const base = S.blocks[i] || { type: 'text', text: '' };
    const t = el.dataset.t;
    if (t === 'divider') return { type: 'divider' };
    if (t === 'table')   return S.blocks[i];   // kept live via cell oninput
    const txt = el.querySelector('.blk-text');
    const out = { type: t, text: txt ? txt.innerText : '' };
    if (t === 'checklist') out.checked = !!(S.blocks[i] && S.blocks[i].checked);
    if (t === 'code') out.lang = (base && base.lang) || '';
    return out;
  });
}

// ── editor interactions ─────────────────────────────────────────────────────
editor.addEventListener('input', (e) => {
  const el = e.target.closest('.blk');
  if (!el) return;
  const i = parseInt(el.dataset.i, 10);
  const txt = el.querySelector('.blk-text');
  if (txt && S.blocks[i]) S.blocks[i].text = txt.innerText;
  if (S.blocks[i] && S.blocks[i].type === 'text' &&
      (S.blocks[i].text === '/' || S.blocks[i].text.startsWith('/')))
    openSlash(el, i);
  else if (slashOpen) closeSlash();
  markDirty();
});

editor.addEventListener('keydown', (e) => {
  const el = e.target.closest('.blk');
  if (!el) return;
  const i = parseInt(el.dataset.i, 10);

  if (slashOpen && slashIdx != null) {
    const items = slashMenu.querySelectorAll('.slash-item');
    if (e.key === 'ArrowDown') { e.preventDefault(); slashIdx = (slashIdx + 1) % items.length; paintSlash(); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); slashIdx = (slashIdx - 1 + items.length) % items.length; paintSlash(); return; }
    if (e.key === 'Enter')     { e.preventDefault(); pickSlash(slashList[slashIdx]); return; }
    if (e.key === 'Escape')    { e.preventDefault(); closeSlash(); return; }
  }

  if (e.key === 'Enter' && !e.shiftKey &&
      S.blocks[i] && S.blocks[i].type !== 'code' && S.blocks[i].type !== 'table') {
    e.preventDefault();
    S.blocks.splice(i + 1, 0, { type: 'text', text: '' });
    markDirty(); renderBlocks();
    focusBlock(i + 1, 0);
    return;
  }
  if (e.key === 'Backspace') {
    const txt = el.querySelector('.blk-text');
    const empty = txt && !(txt.innerText || '').trim();
    if (empty && S.blocks.length > 1) {
      e.preventDefault();
      S.blocks.splice(i, 1);
      markDirty(); renderBlocks();
      focusBlock(Math.max(0, i - 1), null);
    }
  }
});

function focusBlock(i, offset) {
  const el = editor.querySelector(`.blk[data-i="${i}"] .blk-text`);
  if (!el) return;
  el.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  const node = el.firstChild;
  if (node && node.nodeType === 3) {
    const pos = offset == null ? node.length
      : Math.min(offset, node.length);
    range.setStart(node, pos);
  } else range.selectNodeContents(el);
  range.collapse(true);
  sel.removeAllRanges(); sel.addRange(range);
}

// ── slash menu ──────────────────────────────────────────────────────────────
const slashMenu = document.getElementById('slash-menu');
let slashOpen = false, slashIdx = 0, slashList = SLASH, slashAt = 0;

function openSlash(el, i) {
  const q = (S.blocks[i].text || '').slice(1).toLowerCase();
  slashList = SLASH.filter(s => !q || s[2].toLowerCase().includes(q)
                              || s[0].includes(q));
  slashAt = i;
  if (!slashList.length) { closeSlash(); return; }
  slashOpen = true; slashIdx = 0;
  const r = el.getBoundingClientRect();
  slashMenu.style.left = Math.min(r.left, window.innerWidth - 250) + 'px';
  slashMenu.style.top  = Math.min(r.bottom + 4, window.innerHeight - 320) + 'px';
  paintSlash();
  slashMenu.classList.add('show');
}
function paintSlash() {
  slashMenu.innerHTML = slashList.map((s, k) => `
    <div class="slash-item ${k === slashIdx ? 'hot' : ''}" data-k="${k}">
      <div class="si-i">${s[1]}</div>
      <div><div class="si-n">${s[2]}</div><div class="si-d">${s[3]}</div></div>
    </div>`).join('');
  slashMenu.querySelectorAll('.slash-item').forEach(it => {
    it.onmousedown = (e) => { e.preventDefault(); pickSlash(slashList[parseInt(it.dataset.k, 10)]); };
  });
}
function closeSlash() {
  slashOpen = false;
  slashMenu.classList.remove('show');
}
function pickSlash(spec) {
  if (!spec) return;
  const [type] = spec;
  const i = slashAt;
  if (type === 'divider') {
    S.blocks[i] = { type: 'divider' };
    S.blocks.splice(i + 1, 0, { type: 'text', text: '' });
  } else if (type === 'table') {
    S.blocks[i] = { type: 'table', header: true,
      rows: [['', '', ''], ['', '', ''], ['', '', '']] };
    S.blocks.splice(i + 1, 0, { type: 'text', text: '' });
  } else if (type === 'code') {
    S.blocks[i] = { type: 'code', text: '', lang: '' };
  } else if (type === 'checklist') {
    S.blocks[i] = { type: 'checklist', text: '', checked: false };
  } else {
    S.blocks[i] = { type, text: '' };
  }
  closeSlash(); markDirty(); renderBlocks();
  focusBlock(i, 0);
}
$('ws-plus-block').onclick = () => {
  if (!S.page) return;
  S.blocks.push({ type: 'text', text: '/' });
  markDirty(); renderBlocks();
  focusBlock(S.blocks.length - 1, 1);
  const el = editor.querySelector(`.blk[data-i="${S.blocks.length - 1}"]`);
  if (el) openSlash(el, S.blocks.length - 1);
};

// ── side panel: approvals | history | chat ──────────────────────────────────
side.querySelectorAll('.side-tabs button').forEach(b => {
  b.onclick = () => showSide(b.dataset.s);
});
function showSide(tab) {
  S.sideTab = tab;
  side.querySelectorAll('.side-tabs button').forEach(
    b => b.classList.toggle('on', b.dataset.s === tab));
  if (tab === 'approvals') renderApprovals();
  else if (tab === 'history') renderHistory();
  else renderChat();
}

async function renderApprovals() {
  sideBody.innerHTML = '<div class="pane-empty">Loading…</div>';
  let rows = [], dots = {};
  try {
    rows = await JV.api('/api/pending?status=pending');
    try { (await JV.api('/api/dots')).forEach(d => dots[d.id] = d.name); } catch (_) {}
  } catch (e) { sideBody.innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return; }
  S.pending = rows;
  JV.refreshPending();
  if (!rows.length) {
    sideBody.innerHTML = '<div class="pane-empty">No proposals waiting — Dots’ page edits land here for your approval.</div>';
    return;
  }
  sideBody.innerHTML = rows.map(p => {
    const target = p.kind === 'edit'
      ? `edit of page #${p.page_id}` : `new page “${JV.esc(p.title)}”`;
    const preview = (p.content_md || '').slice(0, 400);
    return `<div class="approval-card" data-id="${p.id}">
      <div class="ac-h">
        <span class="ac-t">${JV.esc(p.title || '(untitled)')}</span>
        <span class="chip amber">${p.kind}</span>
      </div>
      <div class="ac-m">by <b>${JV.esc(dots[p.dot_id] || ('dot #' + p.dot_id))}</b>
        · ${target} · base rev ${p.base_rev}</div>
      <div class="ac-pre">${JV.esc(preview)}</div>
      <div class="row">
        <button class="btn-sm primary" data-act="approve">✓ Approve</button>
        <button class="btn-sm danger" data-act="decline">✗ Decline</button>
      </div>
      <div class="ac-err"></div>
    </div>`;
  }).join('');
  sideBody.querySelectorAll('.approval-card').forEach(card => {
    const pid = card.dataset.id;
    card.querySelector('[data-act="approve"]').onclick = async () => {
      try {
        await JV.api(`/api/pending/${pid}/approve`, { method: 'POST' });
        JV.toast('Approved — page saved');
        JV.refreshPending();
        if (S.page) openPage(S.page.id);
        renderApprovals();
      } catch (e) {
        const err = card.querySelector('.ac-err');
        err.classList.add('show');
        err.textContent = (e.status === 409 && e.data && e.data.reason)
          ? 'NOT saved — ' + e.data.reason + ' — re-propose after review.'
          : 'NOT saved — ' + e.message;
        JV.refreshPending();
      }
    };
    card.querySelector('[data-act="decline"]').onclick = async () => {
      try {
        await JV.api(`/api/pending/${pid}/decline`, { method: 'POST' });
        JV.toast('Proposal declined');
        JV.refreshPending();
        renderApprovals();
      } catch (e) { JV.toast(e.message); }
    };
  });
}

async function renderHistory() {
  if (!S.page) { sideBody.innerHTML = '<div class="pane-empty">Open a page first.</div>'; return; }
  sideBody.innerHTML = '<div class="pane-empty">Loading…</div>';
  let revs = [];
  try { revs = await JV.api(`/api/pages/${S.page.id}/revisions`); }
  catch (e) { sideBody.innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return; }
  if (!revs.length) { sideBody.innerHTML = '<div class="pane-empty">No revisions yet.</div>'; return; }
  sideBody.innerHTML = revs.map(r => `
    <div class="rev-row" data-rev="${r.rev}">
      <span class="rv">r${r.rev}</span>
      <span class="grow">${JV.esc(r.author)} — ${JV.esc(r.note || '')}</span>
      <span class="muted">${new Date(r.created_at * 1000).toLocaleTimeString()}</span>
    </div>
    <div class="runs-box" id="revbox-${r.rev}"></div>`).join('');
  sideBody.querySelectorAll('.rev-row').forEach(row => {
    row.onclick = async () => {
      const rev = row.dataset.rev;
      const box = document.getElementById('revbox-' + rev);
      if (box.classList.contains('show')) { box.classList.remove('show'); return; }
      try {
        const r = await JV.api(`/api/pages/${S.page.id}/revisions/${rev}`);
        box.innerHTML = `<div class="ac-pre" style="max-height:200px">${JV.esc((r.content_md || '').slice(0, 2000))}</div>
          <button class="btn-sm" data-restore="${rev}">Restore this version</button>
          <div class="ac-err"></div>`;
        box.classList.add('show');
        box.querySelector('[data-restore]').onclick = async () => {
          try {
            const pg = await JV.api(`/api/pages/${S.page.id}`, {
              method: 'PATCH',
              body: JSON.stringify({ base_rev: S.rev, content_md: r.content_md }) });
            JV.toast('Restored rev ' + rev);
            openPage(pg.id);
          } catch (e) {
            const err = box.querySelector('.ac-err');
            err.classList.add('show');
            err.textContent = 'Restore refused: ' + e.message +
              ' (page moved — load the latest and retry)';
          }
        };
      } catch (e) { JV.toast(e.message); }
    };
  });
}

async function renderChat() {
  if (!S.page) { sideBody.innerHTML = '<div class="pane-empty">Open a page first.</div>'; return; }
  let dotOpts = S.dots.map(d => `<option value="${d.id}">${JV.esc(d.name)}</option>`).join('');
  if (!S.dots.length) {
    try { S.dots = await JV.api('/api/dots'); } catch (_) {}
    dotOpts = S.dots.map(d => `<option value="${d.id}">${JV.esc(d.name)}</option>`).join('');
  }
  sideBody.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <select class="inp" id="pc-dot">${dotOpts || '<option>No dots yet</option>'}</select>
      <span class="chip">page #${S.page.id}</span>
    </div>
    <div class="chat-log" id="pc-log"><div class="pane-empty">Ask a Dot about THIS page — it reads the page, can propose edits (you approve in Approvals).</div></div>
    <div class="row">
      <input class="inp" id="pc-inp" placeholder="Ask about this page…">
      <button class="btn-sm primary" id="pc-send">SEND</button>
    </div>`;
  const log = $('pc-log');
  const send = async () => {
    const inp = $('pc-inp');
    const txt = inp.value.trim();
    const dotId = $('pc-dot').value;
    if (!txt || !dotId || dotId.startsWith('No ')) return;
    inp.value = '';
    log.insertAdjacentHTML('beforeend',
      `<div class="chat-line u"><div class="who">You</div>${JV.esc(txt)}</div>`);
    log.insertAdjacentHTML('beforeend',
      `<div class="chat-line d" id="pc-think"><div class="who d">Dot</div>thinking…</div>`);
    log.scrollTop = log.scrollHeight;
    try {
      const res = await JV.api(`/api/conversations/page:${S.page.id}/messages`, {
        method: 'POST',
        body: JSON.stringify({ content: txt, dot_id: parseInt(dotId, 10),
                               page_id: S.page.id }) });
      const think = document.getElementById('pc-think');
      if (think) think.remove();
      const reply = (res.reply && res.reply.content) || '(empty reply)';
      log.insertAdjacentHTML('beforeend',
        `<div class="chat-line d"><div class="who d">Dot</div>${JV.esc(reply)}</div>`);
      log.scrollTop = log.scrollHeight;
      JV.refreshPending();     // an edit proposal may have just landed
    } catch (e) {
      const think = document.getElementById('pc-think');
      if (think) think.innerHTML = `<div class="who d">Dot</div>${JV.esc('Error: ' + e.message)}`;
    }
  };
  $('pc-send').onclick = send;
  $('pc-inp').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); send(); }
  });
  // existing history
  try {
    const msgs = await JV.api(`/api/conversations/page:${S.page.id}/messages`);
    if (msgs.length) {
      log.innerHTML = '';
      msgs.slice(-30).forEach(m => {
        const who = m.role === 'user' ? 'You' : 'Dot';
        log.insertAdjacentHTML('beforeend',
          `<div class="chat-line ${m.role === 'user' ? 'u' : 'd'}">
             <div class="who ${m.role === 'user' ? '' : 'd'}">${who}</div>${JV.esc(m.content)}</div>`);
      });
      log.scrollTop = log.scrollHeight;
    }
  } catch (_) {}
}

// ── bootstrap ───────────────────────────────────────────────────────────────
JV.onShow('spaces', () => {
  if (!S.loaded) { S.loaded = true; loadTree(); }
  JV.refreshPending();
});
window.__spacesRefresh = loadTree;
})();
