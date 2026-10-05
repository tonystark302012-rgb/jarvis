// JARVIS · AGENTS — Dots: create/delete, live chat (conversations/dot:<id>),
// permission grants (space/research/computer), owner memory (preferences).
// Real schema: {id, name, role_instructions, permissions{}} — proposals-only.
(() => {
const root = document.getElementById('pane-agents');
if (!root) return;

root.innerHTML = `
  <div class="sec" id="ag-root">
    <div class="sec-head">
      <span class="sec-title">Agents</span>
      <span class="sec-sub">Dots — specialist personalities: role prompt + permission grants + memory</span>
      <div class="grow"></div>
      <button class="btn-sm primary" id="ag-new">＋ New Dot</button>
    </div>
    <div class="agent-list" id="ag-list"></div>
    <div class="agent-detail" id="ag-detail"></div>
  </div>`;

const $ = (id) => document.getElementById(id);
const S = { dots: [], sel: null, tab: 'chat', loaded: false };
const PERMS = [
  ['space',     'Space tools (read pages, propose edits)'],
  ['research',  'Research tools (search, fetch, deep-dive)'],
  ['computer',  'Computer tools (files / shell / browser ops)'],
];

async function load() {
  try { S.dots = await JV.api('/api/dots'); }
  catch (e) { $('ag-list').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return; }
  renderList();
}

function granted(d) {
  const p = d.permissions || {};
  const on = PERMS.filter(([k]) => p[k]).map(([, l]) => l.split(' ')[0]);
  return on.length ? on : ['no tools'];
}

function renderList() {
  if (!S.dots.length) {
    $('ag-list').innerHTML = '<div class="pane-empty">No Dots yet — create one to give JARVIS a specialist persona.</div>';
    return;
  }
  $('ag-list').innerHTML = `<div class="agents-grid">` + S.dots.map(d => `
    <div class="agent-card ${S.sel === d.id ? 'sel' : ''}" data-id="${d.id}">
      <div class="a-n">${JV.esc(d.name)}</div>
      <div class="a-r">${JV.esc(d.role_instructions || d.role || 'No role yet')}</div>
      <div class="a-p">
        <span class="chip accent">${JV.esc(granted(d).join(', '))}</span>
        <span class="chip green">skills ✓ always</span>
      </div>
    </div>`).join('') + `</div>`;
  $('ag-list').querySelectorAll('.agent-card').forEach(c => {
    c.onclick = () => openDot(parseInt(c.dataset.id, 10));
  });
}

async function openDot(id) {
  let d;
  try { d = await JV.api(`/api/dots/${id}`); }
  catch (e) { JV.toast(e.message); return; }
  S.sel = id; S.tab = 'chat';
  renderList();
  $('ag-list').style.display = 'none';
  const det = $('ag-detail');
  det.classList.add('on');
  det.innerHTML = `
    <div class="sec-head">
      <button class="btn-sm" id="ag-back">← All Dots</button>
      <span class="sec-title">${JV.esc(d.name)}</span>
      <span class="chip accent">${JV.esc(granted(d).join(', '))}</span>
      <div class="grow"></div>
      <button class="btn-sm danger" id="ag-del">Delete</button>
    </div>
    <div class="seg" style="margin-bottom:12px">
      <button data-t="chat" class="on">CHAT</button>
      <button data-t="memory">MEMORY</button>
      <button data-t="config">CONFIG</button>
    </div>
    <div id="ag-body" style="flex:1;overflow-y:auto;min-height:0"></div>`;
  $('ag-back').onclick = closeDot;
  $('ag-del').onclick = async () => {
    if (!confirm(`Delete Dot "${d.name}"? Its conversation history goes too.`)) return;
    try { await JV.api(`/api/dots/${d.id}`, { method: 'DELETE' });
      JV.toast('Dot deleted'); S.sel = null; closeDot(); load(); }
    catch (e) { JV.toast(e.message); }
  };
  det.querySelectorAll('.seg button').forEach(b => {
    b.onclick = () => {
      S.tab = b.dataset.t;
      det.querySelectorAll('.seg button').forEach(
        x => x.classList.toggle('on', x.dataset.t === S.tab));
      renderBody(d);
    };
  });
  renderBody(d);
}

function closeDot() {
  S.sel = null;
  $('ag-detail').classList.remove('on');
  $('ag-detail').innerHTML = '';
  $('ag-list').style.display = 'block';
  renderList();
}

async function renderBody(d) {
  const body = $('ag-body');
  if (S.tab === 'chat')     return renderChat(d, body);
  if (S.tab === 'memory')   return renderMemory(d, body);
  return renderConfig(d, body);
}

// ── chat (real route: conversations/dot:<id>) ───────────────────────────────
async function renderChat(d, body) {
  body.innerHTML = `
    <div class="chat-log" id="dlog" style="max-height:46vh;overflow-y:auto"></div>
    <div class="row" style="position:sticky;bottom:0;background:var(--bg);padding-top:8px">
      <input class="inp" id="dinp" placeholder="Message ${JV.esc(d.name)}…">
      <button class="btn-sm primary" id="dsend">SEND</button>
    </div>`;
  const log = $('dlog');
  const add = (who, txt, isUser) => {
    log.insertAdjacentHTML('beforeend',
      `<div class="chat-line ${isUser ? 'u' : 'd'}">
        <div class="who ${isUser ? '' : 'd'}">${JV.esc(who)}</div>${JV.esc(txt)}</div>`);
    log.scrollTop = log.scrollHeight;
  };
  const convo = `dot:${d.id}`;
  try {
    const msgs = await JV.api(`/api/conversations/${convo}/messages`);
    if (msgs.length) {
      msgs.slice(-30).forEach(m => add(m.role === 'user' ? 'You' : d.name,
        m.content, m.role === 'user'));
    }
  } catch (_) {}
  const send = async () => {
    const inp = $('dinp');
    const txt = inp.value.trim();
    if (!txt) return;
    inp.value = '';
    add('You', txt, true);
    log.insertAdjacentHTML('beforeend',
      `<div class="chat-line d" id="dthink"><div class="who d">${JV.esc(d.name)}</div>thinking…</div>`);
    log.scrollTop = log.scrollHeight;
    try {
      const res = await JV.api(`/api/conversations/${convo}/messages`, {
        method: 'POST',
        body: JSON.stringify({ content: txt, dot_id: d.id }) });
      const think = document.getElementById('dthink');
      if (think) think.remove();
      if (res.reply && res.reply.content) add(d.name, res.reply.content, false);
      else add(d.name, '(no reply — brain offline?)', false);
      JV.refreshPending();
    } catch (e) {
      const think = document.getElementById('dthink');
      if (think) think.innerHTML = `<div class="who d">${JV.esc(d.name)}</div>${JV.esc('Error: ' + e.message)}`;
    }
  };
  $('dsend').onclick = send;
  $('dinp').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  $('dinp').focus();
}

// ── memory (owner preferences — injected into every Dot's system prompt) ────
async function renderMemory(d, body) {
  body.innerHTML = '<div class="pane-empty">Loading…</div>';
  let items = [];
  try { items = await JV.api('/api/memory'); }
  catch (e) { body.innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return; }
  body.innerHTML = `
    <div class="sec-sub" style="margin-bottom:10px">
      Owner-wide preferences — every Dot reads these in its system prompt
      (scope: <b>*</b> = all Dots, or a comma list of allowed Dot names).
    </div>
    <div class="row" style="margin-bottom:12px">
      <input class="inp" id="mm-key" placeholder="Key (e.g. preferred_stack)" style="max-width:240px">
      <input class="inp" id="mm-val" placeholder="Value" style="flex:1;min-width:200px">
      <input class="inp" id="mm-scope" placeholder="allowed: * or dot names" style="max-width:200px" value="*">
      <button class="btn-sm primary" id="mm-add">Add / Update</button>
    </div>
    <div class="card" style="padding:0">
      ${items.length ? items.map(it => `
        <div class="row" style="padding:10px 13px;border-bottom:1px solid var(--border)">
          <span class="mono" style="color:var(--accent-2);min-width:150px">${JV.esc(it.key)}</span>
          <span class="grow" style="font-size:13px">${JV.esc(String(it.value))}</span>
          <span class="chip">${JV.esc(JSON.stringify(it.allowed))}</span>
          <button class="btn-sm danger" data-id="${it.id}">✕</button>
        </div>`).join('')
        : '<div class="pane-empty">No memories yet — add one, or let a Dot remember through chat.</div>'}
    </div>`;
  $('mm-add').onclick = async () => {
    const key = $('mm-key').value.trim(), val = $('mm-val').value;
    const scope = $('mm-scope').value.trim() || '*';
    if (!key) { JV.toast('Key required'); return; }
    let allowed = scope;
    if (scope !== '*') allowed = scope.split(',').map(s => s.trim()).filter(Boolean);
    try {
      await JV.api('/api/memory', { method: 'PUT',
        body: JSON.stringify({ key, value: val, allowed }) });
      JV.toast('Memory saved'); renderMemory(d, body);
    } catch (e) { JV.toast(e.message); }
  };
  body.querySelectorAll('[data-id]').forEach(b => {
    b.onclick = async () => {
      try {
        await JV.api(`/api/memory/${b.dataset.id}`, { method: 'DELETE' });
        JV.toast('Removed'); renderMemory(d, body);
      } catch (e) { JV.toast(e.message); }
    };
  });
}

// ── config (name + role_instructions + permissions{}) ───────────────────────
async function renderConfig(d, body) {
  const p = d.permissions || {};
  body.innerHTML = `
    <div class="form-grid" style="max-width:640px">
      <label>Name</label>
      <input class="inp" id="cf-name" value="${JV.esc(d.name)}">
      <label>Role instructions (this Dot's personality + rules)</label>
      <textarea class="inp" id="cf-role" rows="7"
        placeholder="You are …">${JV.esc(d.role_instructions || '')}</textarea>
      <label>Permission grants (owner-side; changes take effect next turn)</label>
      <div class="perm-row">
        ${PERMS.map(([k, label]) => `
          <label class="perm-toggle">
            <input type="checkbox" data-perm="${k}" ${p[k] ? 'checked' : ''}> ${JV.esc(label)}
          </label>`).join('')}
      </div>
      <div class="sec-sub">Skills tools are always available by design (published skills are shared).
      Page writes by any Dot are proposals only — you approve them in Spaces → Approvals.</div>
      <div class="row" style="margin-top:6px">
        <button class="btn-sm primary" id="cf-save">Save config</button>
        <span class="muted" id="cf-state"></span>
      </div>
    </div>`;
  $('cf-save').onclick = async () => {
    const st = $('cf-state');
    st.textContent = 'saving…';
    const perms = {};
    body.querySelectorAll('[data-perm]').forEach(cb => {
      perms[cb.dataset.perm] = cb.checked;
    });
    try {
      const upd = await JV.api(`/api/dots/${d.id}`, { method: 'PATCH',
        body: JSON.stringify({
          name: $('cf-name').value.trim() || d.name,
          role: $('cf-role').value,
          permissions: perms,
        }) });
      S.dots = S.dots.map(x => x.id === d.id ? Object.assign({}, x, upd) : x);
      st.textContent = '✓ saved';
      JV.toast('Dot updated');
      renderList();
      openDot(d.id);
    } catch (e) { st.textContent = '✗ ' + e.message; }
  };
}

// ── new dot ─────────────────────────────────────────────────────────────────
$('ag-new').onclick = () => {
  const det = $('ag-detail');
  $('ag-list').style.display = 'none';
  det.classList.add('on');
  det.innerHTML = `
    <div class="sec-head">
      <button class="btn-sm" id="nd-back">← Cancel</button>
      <span class="sec-title">New Dot</span>
    </div>
    <div class="form-grid" style="max-width:640px">
      <label>Name (unique)</label>
      <input class="inp" id="nd-name" placeholder="e.g. Researcher">
      <label>Role instructions</label>
      <textarea class="inp" id="nd-role" rows="6"
        placeholder="You are …">You are a helpful JARVIS specialist.</textarea>
      <div class="row" style="margin-top:6px">
        <button class="btn-sm primary" id="nd-create">Create</button>
      </div>
    </div>`;
  $('nd-back').onclick = closeDot;
  $('nd-create').onclick = async () => {
    const name = $('nd-name').value.trim();
    if (!name) { JV.toast('Name required'); return; }
    try {
      const d = await JV.api('/api/dots', { method: 'POST',
        body: JSON.stringify({ name, role: $('nd-role').value }) });
      JV.toast('Dot created');
      await load();
      openDot(d.id);
    } catch (e) { JV.toast(e.message); }
  };
  $('nd-name').focus();
};

JV.onShow('agents', () => { if (!S.loaded) { S.loaded = true; load(); } });
})();
