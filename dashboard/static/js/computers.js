// JARVIS · COMPUTERS — safe agentic control of trusted PCs: jailed file
// browser, argv-only shell (no shell strings), agentic browser ops,
// full audit trail. Every op is gated by the PC's perms (T5/T6/T7).
(() => {
const root = document.getElementById('pane-computers');
if (!root) return;

root.innerHTML = `
  <div id="pc-list" class="sec">
    <div class="sec-head">
      <span class="sec-title">Computers</span>
      <span class="sec-sub">Trusted PC instances — jail-scoped files, allowlisted shell, browser control</span>
      <div class="grow"></div>
      <button class="btn-sm primary" id="pc-add">＋ Attach computer</button>
    </div>
    <div id="pc-cards"></div>
  </div>
  <div class="pc-panel" id="pc-panel">
    <div class="pc-head">
      <button class="btn-sm" id="pc-back">← All computers</button>
      <span class="sec-title" id="pc-name"></span>
      <span class="chip" id="pc-status"></span>
      <span class="muted mono" id="pc-root"></span>
      <span class="row" id="pc-perms"></span>
      <div class="grow"></div>
      <button class="btn-sm" id="pc-on">▶ Start</button>
      <button class="btn-sm danger" id="pc-off">⏹ Stop</button>
    </div>
    <div class="pc-tabs" id="pc-tabs">
      <button data-t="files" class="on">Files</button>
      <button data-t="shell">Shell</button>
      <button data-t="browser">Browser</button>
      <button data-t="audit">Audit</button>
    </div>
    <div class="pc-body on" id="pb-files"></div>
    <div class="pc-body" id="pb-shell"></div>
    <div class="pc-body" id="pb-browser"></div>
    <div class="pc-body" id="pb-audit"></div>
  </div>`;

const $ = (id) => document.getElementById(id);
const S = { comps: [], dots: [], sel: null, comp: null, tab: 'files',
            path: '.', loaded: false };

async function load() {
  try { S.comps = await JV.api('/api/computers'); }
  catch (e) { $('pc-cards').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return; }
  renderCards();
}

function permsStr(p) {
  if (!p) return '—';
  return Object.entries(p).map(([k, v]) =>
    `${k}:${v ? '✓' : '✗'}`).join('  ');
}

function renderCards() {
  if (!S.comps.length) {
    $('pc-cards').innerHTML = `<div class="pane-empty">
      No computers yet — <b>＋ Attach computer</b> binds a PC to one of your Dots
      (the Dot's executor). Its work dir + permission gates start here.</div>`;
    return;
  }
  $('pc-cards').innerHTML = `<div class="agents-grid">` + S.comps.map(c => `
    <div class="agent-card" data-id="${c.id}">
      <div class="a-n">🖥️ PC #${c.id} <span class="chip ${c.status === 'running' ? 'green' : ''}">${JV.esc(c.status)}</span></div>
      <div class="a-r">for Dot #${c.dot_id}<br><span class="mono muted">${JV.esc(c.root_path || '')}</span></div>
      <div class="a-p"><span class="chip accent" style="font-family:var(--mono);text-transform:none">${JV.esc(permsStr(c.perms))}</span></div>
    </div>`).join('') + `</div>`;
  $('pc-cards').querySelectorAll('.agent-card').forEach(el => {
    el.onclick = () => openComp(parseInt(el.dataset.id, 10));
  });
}

$('pc-add').onclick = async () => {
  try {
    if (!S.dots.length) S.dots = await JV.api('/api/dots');
  } catch (_) {}
  if (!S.dots.length) { JV.toast('Create a Dot first (Agents → ＋)'); return; }
  const opts = S.dots.map(d => `<option value="${d.id}">${JV.esc(d.name)}</option>`).join('');
  const win = window.open('', '_blank');
  const dotId = prompt(`Attach computer to which Dot?\n${S.dots.map(d => `${d.id}: ${d.name}`).join('\n')}\n\nEnter Dot ID:`,
    String(S.dots[0].id));
  if (!dotId) return;
  void opts; void win;
  try {
    const c = await JV.api('/api/computers', { method: 'POST',
      body: JSON.stringify({ dot_id: parseInt(dotId, 10) }) });
    JV.toast('Computer attached — work dir ' + (c.work || c.root_path || ''));
    load();
  } catch (e) { JV.toast(e.message); }
};

async function openComp(id) {
  S.sel = id; S.tab = 'files'; S.path = '.';
  let c;
  try { c = await JV.api(`/api/computers/${id}`); }
  catch (e) { JV.toast(e.message); return; }
  S.comp = c;
  $('pc-list').style.display = 'none';
  $('pc-panel').classList.add('on');
  $('pc-name').textContent = `PC #${c.id}`;
  $('pc-status').textContent = c.status;
  $('pc-status').className = 'chip ' + (c.status === 'running' ? 'green' : 'amber');
  $('pc-root').textContent = `${c.root_path || ''} · perms ${permsStr(c.perms)}`;
  renderPermToggles(c);
  $('pc-tabs').querySelectorAll('button').forEach(b =>
    b.classList.toggle('on', b.dataset.t === 'files'));
  showTab('files');
}

function renderPermToggles(c) {
  const box = $('pc-perms');
  const p = c.perms || {};
  box.innerHTML = ['files', 'shell', 'browser'].map(k => `
    <button class="btn-sm ${p[k] ? 'primary' : 'danger'}"
            data-perm="${k}" title="Toggle the ${k} permission (owner grant)"
      >${k}: ${p[k] ? '✓' : '✗'}</button>`).join('');
  box.querySelectorAll('[data-perm]').forEach(b => {
    b.onclick = async () => {
      const k = b.dataset.perm;
      const next = {};
      next[k] = !(c.perms && c.perms[k]);
      try {
        await JV.api(`/api/computers/${c.id}/perms`, { method: 'PATCH',
          body: JSON.stringify({ perms: next }) });
        JV.toast(`${k} ${next[k] ? 'granted' : 'revoked'}`);
        openComp(c.id);
      } catch (e) { JV.toast(e.message); }
    };
  });
}

function closeComp() {
  S.sel = null; S.comp = null;
  $('pc-panel').classList.remove('on');
  $('pc-list').style.display = 'block';
  load();
}
$('pc-back').onclick = closeComp;

$('pc-on').onclick = async () => {
  try { await JV.api(`/api/computers/${S.sel}/start`, { method: 'POST' });
    JV.toast('Computer started'); openComp(S.sel); }
  catch (e) { JV.toast(e.message); }
};
$('pc-off').onclick = async () => {
  try { await JV.api(`/api/computers/${S.sel}/stop`, { method: 'POST' });
    JV.toast('Computer stopped'); openComp(S.sel); }
  catch (e) { JV.toast(e.message); }
};

$('pc-tabs').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    S.tab = b.dataset.t;
    $('pc-tabs').querySelectorAll('button').forEach(
      x => x.classList.toggle('on', x.dataset.t === S.tab));
    showTab(S.tab);
  };
});

function showTab(t) {
  ['files', 'shell', 'browser', 'audit'].forEach(x =>
    $('pb-' + x).classList.toggle('on', x === t));
  if (t === 'files')   renderFiles();
  if (t === 'shell')   renderShell();
  if (t === 'browser') renderBrowser();
  if (t === 'audit')   renderAudit();
}

// ── files (jail-scoped; entries are strings, dirs end with '/') ─────────────
async function renderFiles() {
  const body = $('pb-files');
  body.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <button class="btn-sm" id="fl-up">⬆ Parent</button>
      <input class="inp mono" id="fl-path" value="${JV.esc(S.path)}" style="max-width:420px">
      <button class="btn-sm" id="fl-go">Go</button>
      <span class="muted" id="fl-count"></span>
    </div>
    <div class="card" style="padding:6px" id="fl-list"><div class="pane-empty">Loading…</div></div>
    <div id="fl-preview"></div>`;
  const go = () => { S.path = $('fl-path').value.trim() || '.'; renderFiles(); };
  $('fl-go').onclick = go;
  $('fl-path').addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  $('fl-up').onclick = () => {
    const parts = S.path.replace(/\/+$/, '').split('/').filter(Boolean);
    parts.pop();
    S.path = parts.length ? parts.join('/') : '.';
    renderFiles();
  };
  let data;
  try {
    data = await JV.api(`/api/computers/${S.sel}/files/list`, {
      method: 'POST', body: JSON.stringify({ path: S.path }) });
  } catch (e) {
    $('fl-list').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return;
  }
  S.path = data.path || S.path;
  $('fl-path').value = S.path;
  const entries = data.entries || [];
  $('fl-count').textContent = `${entries.length} entries`;
  const isImg = (p) => /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(p);
  $('fl-list').innerHTML = entries.length ? entries.map(raw => {
    const isDir = raw.endsWith('/');
    const name = isDir ? raw.slice(0, -1) : raw;
    return `<div class="file-row" data-p="${JV.esc(raw)}" data-dir="${isDir ? 1 : 0}">
      <span>${isDir ? '📁' : (isImg(raw) ? '🖼️' : '📄')}</span>
      <span class="grow" style="min-width:0;overflow:hidden;text-overflow:ellipsis">${JV.esc(name)}</span>
      ${!isDir && isImg(raw) ? '<button class="btn-sm" data-a="view">👁 View</button>' : ''}
      ${!isDir ? '<button class="btn-sm" data-a="read">Read</button>' : ''}
      ${!isDir ? '<button class="btn-sm" data-a="dl">⬇</button>' : ''}
    </div>`;
  }).join('') : '<div class="pane-empty">Empty directory.</div>';

  $('fl-list').querySelectorAll('.file-row').forEach(row => {
    row.onclick = async (e) => {
      const act = e.target.dataset && e.target.dataset.a;
      const p = row.dataset.p.replace(/\/$/, '');
      if (row.dataset.dir === '1' && !act) { S.path = p; renderFiles(); return; }
      if (act === 'view') {
        $('fl-preview').innerHTML = `
          <div class="sec-sub" style="margin-top:12px">${JV.esc(p)}</div>
          <img class="shot-img" id="fl-img" alt="loading…">
          <div class="row" style="margin-top:8px">
            <button class="btn-sm" id="fl-copy">⬇ Save into JARVIS uploads</button>
          </div>`;
        $('fl-img').src = `/api/computers/${S.sel}/image?path=${encodeURIComponent(p)}&token=${encodeURIComponent(JV.token)}`;
        $('fl-img').onerror = () => { $('fl-img').alt = 'failed to load (permissions or jail)'; };
        $('fl-copy').onclick = async () => {
          try {
            const r = await fetch(`/api/computers/${S.sel}/image?path=${encodeURIComponent(p)}&token=${encodeURIComponent(JV.token)}`);
            const blob = await r.blob();
            const fd = new FormData();
            fd.append('file', new File([blob], p.split('/').pop() || 'image.png'));
            const resp = await fetch('/api/upload', {
              method: 'POST', headers: { 'Authorization': `Bearer ${JV.token}` }, body: fd });
            JV.toast(resp.ok ? 'Saved into JARVIS uploads' : 'Upload failed');
          } catch (err) { JV.toast(err.message); }
        };
        return;
      }
      try {
        const r = await JV.api(`/api/computers/${S.sel}/files/read`, {
          method: 'POST', body: JSON.stringify({ path: p }) });
        if (act === 'dl') {
          const blob = new Blob([r.content || ''], { type: 'text/plain' });
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = p.split('/').pop() || 'file';
          a.click();
          URL.revokeObjectURL(a.href);
          return;
        }
        $('fl-preview').innerHTML = `
          <div class="sec-sub" style="margin-top:12px">${JV.esc(r.path)}
            ${r.truncated ? '· truncated' : ''}</div>
          <div class="term-out show" style="max-height:300px">${JV.esc(r.content || '')}</div>
          <div class="row" style="margin-top:8px">
            <button class="btn-sm" id="fl-edit-toggle">✏️ Edit</button>
          </div>
          <div id="fl-edit" style="display:none;margin-top:8px">
            <textarea class="inp mono" id="fl-text" rows="10">${JV.esc(r.content || '')}</textarea>
            <button class="btn-sm primary" id="fl-save" style="margin-top:8px">Save file</button>
          </div>`;
        $('fl-edit-toggle').onclick = () => {
          const ed = $('fl-edit');
          ed.style.display = ed.style.display === 'none' ? 'block' : 'none';
        };
        $('fl-save').onclick = async () => {
          try {
            await JV.api(`/api/computers/${S.sel}/files/write`, {
              method: 'POST',
              body: JSON.stringify({ path: p, content: $('fl-text').value }) });
            JV.toast('File saved (audited)');
            renderFiles();
          } catch (err) { JV.toast(err.message); }
        };
      } catch (err) { JV.toast(err.message); }
    };
  });
}

// ── shell (argv-only, quotes respected client-side; NEVER a shell string) ───
function splitArgv(line) {
  const out = []; let cur = ''; let q = null;
  for (const ch of line) {
    if (q) {
      if (ch === q) q = null; else cur += ch;
    } else if (ch === '"' || ch === "'") q = ch;
    else if (ch === ' ') { if (cur) { out.push(cur); cur = ''; } }
    else cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

function renderShell() {
  const body = $('pb-shell');
  const sh = S.comp && S.comp.perms && S.comp.perms.shell;
  body.innerHTML = `
    <div class="row" style="margin-bottom:8px">
      <span class="chip ${sh ? 'green' : 'red'}">shell ${sh ? 'enabled' : 'disabled in perms'}</span>
      <span class="chip">argv-only — no shell string ever</span>
      <span class="chip">audit: every exec logged</span>
    </div>
    <input class="inp mono" id="sh-inp" placeholder="ls -la  ·  git status  ·  df -h … (runs with a timeout + output cap)">
    <div class="term-out" id="sh-out"></div>
    <div id="sh-pend"></div>`;
  $('sh-inp').addEventListener('keydown', async e => {
    if (e.key !== 'Enter') return;
    const line = $('sh-inp').value.trim();
    if (!line) return;
    const argv = splitArgv(line);
    const out = $('sh-out');
    out.classList.add('show');
    out.textContent = `jarvis@pc${S.sel}$ ${line}\n…`;
    $('sh-inp').value = '';
    try {
      const res = await JV.api(`/api/computers/${S.sel}/exec`, {
        method: 'POST', body: JSON.stringify({ argv }) });
      out.textContent = `jarvis@pc${S.sel}$ ${line}\n${res.stdout || ''}` +
        (res.returncode ? `\n[exit ${res.returncode}]` : '') +
        (res.truncated ? '\n[truncated]' : '');
    } catch (err) {
      out.textContent = `jarvis@pc${S.sel}$ ${line}\n✗ ${err.message}`;
    }
    renderRecentExecs();
  });
  renderRecentExecs();
}

async function renderRecentExecs() {
  const box = $('sh-pend');
  if (!box) return;
  try {
    const rows = await JV.api(`/api/computers/${S.sel}/audit?limit=40`);
    const execs = rows.filter(r => r.action === 'exec').slice(0, 8);
    if (!execs.length) { box.innerHTML = ''; return; }
    box.innerHTML = `<div class="sec-sub" style="margin-top:14px">Recent execs</div>` +
      execs.map(r => `<div class="rev-row" style="cursor:default">
        <span class="chip ${r.ok ? 'green' : 'red'}">${r.ok ? 'rc=0' : 'fail'}</span>
        <span class="mono grow" style="min-width:0;overflow:hidden;text-overflow:ellipsis">${JV.esc(r.detail)}</span>
        <span class="muted" style="font-size:10px">${new Date(r.created_at * 1000).toLocaleTimeString()}</span>
      </div>`).join('');
  } catch (_) {}
}

// ── browser (agentic control: navigate/read/snapshot/screenshot/click/…) ────
const BROWSER_OPS = [
  ['navigate', 'URL', 'Open a URL in the PC browser'],
  ['read',     '',    'Visible text of the current page'],
  ['snapshot', '',    'Accessibility snapshot (elements + roles)'],
  ['screenshot', '',  'PNG of the current view'],
  ['click',    'selector', 'Click an element (a11y selector)'],
  ['type',     'selector+text', 'Type into a field'],
  ['key',      'key', 'Press a key (Enter, Tab…)'],
  ['scroll',   'dir', 'Scroll current view'],
];

function renderBrowser() {
  const body = $('pb-browser');
  const br = S.comp && S.comp.perms && S.comp.perms.browser;
  body.innerHTML = `
    <div class="row" style="margin-bottom:8px">
      <span class="chip ${br ? 'green' : 'red'}">browser ${br ? 'enabled' : 'disabled in perms'}</span>
      <span class="chip">Playwright on the PC (when installed)</span>
    </div>
    <div class="row" style="margin-bottom:8px">
      <input class="inp" id="bw-url" placeholder="https://…" style="max-width:420px">
      <button class="btn-sm primary" data-op="navigate">▶ Navigate</button>
      <button class="btn-sm" data-op="read">Read</button>
      <button class="btn-sm" data-op="snapshot">Snapshot</button>
      <button class="btn-sm" data-op="screenshot">📸 Screenshot</button>
    </div>
    <div class="row" style="margin-bottom:8px">
      <input class="inp" id="bw-sel" placeholder="selector / a11y ref" style="max-width:300px">
      <input class="inp" id="bw-text" placeholder="text (for type)" style="max-width:240px">
      <button class="btn-sm" data-op="click">Click</button>
      <button class="btn-sm" data-op="type">Type</button>
      <button class="btn-sm" data-op="key">Key</button>
      <button class="btn-sm" data-op="scroll">Scroll</button>
    </div>
    <div id="bw-out"><div class="pane-empty">Run an op — results land here.</div></div>`;
  body.querySelectorAll('[data-op]').forEach(b => {
    b.onclick = async () => {
      const op = b.dataset.op;
      const payload = { op };
      if (op === 'navigate') payload.url = $('bw-url').value.trim();
      if (op === 'click' || op === 'type') payload.selector = $('bw-sel').value.trim();
      if (op === 'type') payload.text = $('bw-text').value;
      if (op === 'key') payload.key = $('bw-text').value.trim() || 'Enter';
      if (op === 'scroll') payload.direction = $('bw-text').value.trim() || 'down';
      const out = $('bw-out');
      out.innerHTML = '<div class="pane-empty">Working…</div>';
      try {
        const res = await JV.api(`/api/computers/${S.sel}/browser`, {
          method: 'POST', body: JSON.stringify(payload) });
        renderBrowserResult(res, op);
      } catch (e) {
        out.innerHTML = `<div class="pane-empty" style="color:var(--red)">${JV.esc(e.message)}</div>`;
      }
    };
  });
}

function renderBrowserResult(res, op) {
  const out = $('bw-out');
  const img = res.image || res.data_url || res.screenshot;
  if (img) {
    out.innerHTML = `<img class="shot-img" src="${img}" alt="${JV.esc(op)}">
      <div class="sec-sub" style="margin-top:6px">${JV.esc(op)} @ ${new Date().toLocaleTimeString()}</div>`;
    return;
  }
  const text = res.text || res.content || res.snapshot ||
               res.markdown || JSON.stringify(res, null, 2);
  out.innerHTML = `<div class="term-out show" style="max-height:420px">${JV.esc(String(text))}</div>
    <div class="sec-sub" style="margin-top:6px">${JV.esc(op)} @ ${new Date().toLocaleTimeString()}</div>`;
}

// ── audit ───────────────────────────────────────────────────────────────────
async function renderAudit() {
  const body = $('pb-audit');
  body.innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <select class="inp" id="au-filter">
        <option value="">all actions</option>
        <option value="exec">shell exec</option>
        <option value="files_list">list dir</option>
        <option value="files_read">read file</option>
        <option value="files_write">write file</option>
        <option value="browser">browser ops</option>
      </select>
      <button class="btn-sm" id="au-refresh">↻ Refresh</button>
    </div>
    <div class="card" style="padding:0" id="au-table"><div class="pane-empty">Loading…</div></div>`;
  const load = async () => {
    const f = $('au-filter').value;
    let rows = [];
    try { rows = await JV.api(`/api/computers/${S.sel}/audit?limit=200`); }
    catch (e) {
      $('au-table').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return;
    }
    if (f) rows = rows.filter(r => r.action === f);
    $('au-table').innerHTML = rows.length ? `
      <table class="tbl">
        <thead><tr><th>when</th><th>actor</th><th>action</th><th>detail</th><th>ok</th></tr></thead>
        <tbody>${rows.map(r => `
          <tr>
            <td class="mono muted">${new Date(r.created_at * 1000).toLocaleString()}</td>
            <td><span class="chip ${r.actor === 'owner' ? 'accent' : 'amber'}">${JV.esc(r.actor)}</span></td>
            <td class="mono">${JV.esc(r.action)}</td>
            <td class="mono" style="max-width:420px;word-break:break-all">${JV.esc(r.detail)}</td>
            <td>${r.ok ? '<span class="chip green">✓</span>' : '<span class="chip red">✗</span>'}</td>
          </tr>`).join('')}</tbody>
      </table>` : '<div class="pane-empty">No audit entries match.</div>';
  };
  $('au-filter').onchange = load;
  $('au-refresh').onclick = load;
  load();
}

JV.onShow('computers', () => { if (!S.loaded) { S.loaded = true; load(); } });
})();
