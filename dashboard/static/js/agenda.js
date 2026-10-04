// JARVIS · AGENDA — recurring tasks (scheduler) + the skill registry
// (miner → draft → owner publishes / archives). 6d surfaces.
(() => {
const root = document.getElementById('pane-agenda');
if (!root) return;

root.innerHTML = `
  <div class="sec" id="ag-root">
    <div class="sec-head">
      <span class="sec-title">Agenda</span>
      <span class="sec-sub">Recurring instructions on a Dot + learned skills you approve</span>
      <div class="grow"></div>
      <button class="btn-sm primary" id="ag-add">＋ New task</button>
    </div>
    <div class="agenda-2">
      <div>
        <div class="sec-title" style="margin-bottom:8px">Tasks</div>
        <div class="card" style="padding:0" id="ag-tasks"><div class="pane-empty">Loading…</div></div>
      </div>
      <div>
        <div class="sec-head" style="margin-bottom:8px">
          <span class="sec-title">Skills</span>
          <div class="grow"></div>
          <button class="btn-sm" id="ag-mine">⛏ Mine from history</button>
        </div>
        <div id="ag-skills"><div class="pane-empty">Loading…</div></div>
      </div>
    </div>
  </div>`;

const $ = (id) => document.getElementById(id);
const S = { dots: [], loaded: false, openRuns: new Set() };

function everyLabel(s) {
  if (s % 3600 === 0 && s >= 3600) return `every ${s / 3600}h`;
  if (s % 60 === 0 && s >= 60)     return `every ${s / 60}m`;
  return `every ${s}s`;
}

// ── tasks ───────────────────────────────────────────────────────────────────
async function loadTasks() {
  let tasks = [];
  try {
    tasks = await JV.api('/api/tasks');
    if (!S.dots.length) S.dots = await JV.api('/api/dots');
  } catch (e) {
    $('ag-tasks').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return;
  }
  if (!tasks.length) {
    $('ag-tasks').innerHTML = '<div class="pane-empty">No recurring tasks — create one: a prompt, an interval, a Dot.</div>';
    return;
  }
  $('ag-tasks').innerHTML = `<table class="tbl">
    <thead><tr><th>task</th><th>every</th><th>status</th><th>runs</th><th></th></tr></thead>
    <tbody>${tasks.map(t => {
      const dot = S.dots.find(d => d.id === t.dot_id);
      return `<tr class="task-row" data-id="${t.id}">
        <td><b>${JV.esc(t.name)}</b><br>
          <span class="muted" style="font-size:11px">${JV.esc((t.instruction || '').slice(0, 90))}</span><br>
          <span class="chip accent">${JV.esc(dot ? dot.name : 'dot #' + t.dot_id)}</span></td>
        <td class="mono">${everyLabel(t.every_seconds)}<br>
          <span class="muted" style="font-size:10px">next ${t.next_run_at ? new Date(t.next_run_at * 1000).toLocaleString() : '—'}</span></td>
        <td><span class="chip ${t.status === 'active' ? 'green'
          : t.status === 'paused' ? 'amber' : 'red'}">${JV.esc(t.status)}</span></td>
        <td><button class="btn-sm" data-a="runs">Runs</button></td>
        <td><div class="row">
          ${t.status === 'active'
            ? '<button class="btn-sm" data-a="pause">Pause</button>'
            : t.status === 'paused'
              ? '<button class="btn-sm" data-a="resume">Resume</button>' : ''}
          ${t.status === 'failed' ? '<button class="btn-sm" data-a="retry">Retry</button>' : ''}
          ${t.status !== 'cancelled' && t.status !== 'done'
            ? '<button class="btn-sm danger" data-a="cancel">Cancel</button>' : ''}
        </div></td>
      </tr>
      <tr class="runs-box ${S.openRuns.has(t.id) ? '' : ''}" data-runs="${t.id}"><td colspan="5">
        <div class="pane-empty">Loading runs…</div></td></tr>`;
    }).join('')}</tbody></table>`;

  $('ag-tasks').querySelectorAll('tr.task-row').forEach(row => {
    const id = row.dataset.id;
    row.querySelectorAll('[data-a]').forEach(btn => {
      btn.onclick = async () => {
        const a = btn.dataset.a;
        if (a === 'runs') {
          const box = row.nextElementSibling;
          const isOpen = box.classList.contains('runs-box') &&
                         box.classList.contains('show');
          if (isOpen) { box.classList.remove('show'); return; }
          box.classList.add('show');
          try {
            const runs = await JV.api(`/api/tasks/${id}/runs`);
            box.firstElementChild.outerHTML = runs.length ? `
              <table class="tbl">
                <thead><tr><th>started</th><th>status</th><th>output</th></tr></thead>
                <tbody>${runs.map(r => `
                  <tr>
                    <td class="mono muted">${new Date(r.started_at * 1000).toLocaleString()}</td>
                    <td><span class="chip ${r.status === 'ok' ? 'green'
                      : r.status === 'running' ? 'accent' : 'red'}">${JV.esc(r.status)}</span></td>
                    <td class="mono" style="max-width:380px;word-break:break-word">${JV.esc((r.output || '').slice(0, 400))}</td>
                  </tr>`).join('')}</tbody>
              </table>` : '<div class="pane-empty">No runs yet.</div>';
          } catch (e) { JV.toast(e.message); }
          return;
        }
        try {
          await JV.api(`/api/tasks/${id}/${a}`, { method: 'POST' });
          JV.toast(a.charAt(0).toUpperCase() + a.slice(1) + ' ✓');
          loadTasks();
        } catch (e) { JV.toast(e.message); }
      };
    });
  });
}

$('ag-add').onclick = async () => {
  try { if (!S.dots.length) S.dots = await JV.api('/api/dots'); }
  catch (_) {}
  if (!S.dots.length) { JV.toast('Create a Dot first (Agents)'); return; }
  const name = prompt('Task name:', 'Morning brief');
  if (!name) return;
  const instruction = prompt('Instruction (what should the Dot do?):',
    'Summarize what happened since last run in 5 bullet points.');
  if (!instruction) return;
  const every = prompt('Repeat every (seconds — e.g. 3600 = hourly):', '3600');
  if (!every) return;
  const dotId = prompt('Dot ID:\n' +
    S.dots.map(d => `${d.id}: ${d.name}`).join('\n'), String(S.dots[0].id));
  if (!dotId) return;
  try {
    await JV.api('/api/tasks', { method: 'POST', body: JSON.stringify({
      name, instruction,
      every_seconds: parseInt(every, 10) || 3600,
      dot_id: parseInt(dotId, 10) }) });
    JV.toast('Task created');
    loadTasks();
  } catch (e) { JV.toast(e.message); }
};

// ── skills ──────────────────────────────────────────────────────────────────
async function loadSkills() {
  let rows = [];
  try { rows = await JV.api('/api/skills?status=any'); }
  catch (e) {
    $('ag-skills').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`; return;
  }
  if (!rows.length) {
    $('ag-skills').innerHTML = `<div class="pane-empty">No skills yet — hit <b>Mine from history</b> to let JARVIS
      propose skills from what it already learned, then publish the ones you like.</div>`;
    return;
  }
  $('ag-skills').innerHTML = rows.map(s => `
    <div class="skill-card ${s.status === 'draft' ? 'draft' : ''}" data-id="${s.id}">
      <div class="row">
        <b style="flex:1">${JV.esc(s.title)}</b>
        <span class="chip ${s.status === 'published' ? 'green'
          : s.status === 'archived' ? 'red' : 'amber'}">${JV.esc(s.status)}</span>
      </div>
      <div class="muted" style="font-size:11px;margin-top:4px">
        ${JV.esc(s.source_note || '')}
        ${s.published_at ? ' · published ' + new Date(s.published_at * 1000).toLocaleDateString() : ''}
      </div>
      <div class="row" style="margin-top:8px">
        <button class="btn-sm" data-a="body">View body</button>
        ${s.status === 'draft' ? '<button class="btn-sm primary" data-a="publish">Publish</button>' : ''}
        ${s.status === 'published' ? '<button class="btn-sm danger" data-a="archive">Archive</button>' : ''}
      </div>
      <div class="sk-b" id="skb-${s.id}">${JV.esc(s.body_md || '(empty)')}</div>
    </div>`).join('');
  $('ag-skills').querySelectorAll('.skill-card').forEach(card => {
    const id = card.dataset.id;
    card.querySelectorAll('[data-a]').forEach(btn => {
      btn.onclick = async () => {
        const a = btn.dataset.a;
        if (a === 'body') {
          document.getElementById('skb-' + id).classList.toggle('show');
          return;
        }
        try {
          await JV.api(`/api/skills/${id}/${a}`, { method: 'POST' });
          JV.toast(a === 'publish' ? 'Skill published ✓' : 'Skill archived');
          loadSkills();
        } catch (e) { JV.toast(e.message); }
      };
    });
  });
}

$('ag-mine').onclick = async () => {
  $('ag-mine').disabled = true;
  try {
    const r = await JV.api('/api/skills/mine', { method: 'POST' });
    JV.toast(r.created ? `${r.created} new draft skill(s) mined` : 'Nothing new to mine yet');
    loadSkills();
  } catch (e) { JV.toast(e.message); }
  finally { $('ag-mine').disabled = false; }
};

JV.onShow('agenda', () => {
  if (!S.loaded) { S.loaded = true; loadTasks(); loadSkills(); }
});
})();
