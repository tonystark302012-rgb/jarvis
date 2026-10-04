// JARVIS · CALLS — live voice-call panel: start with any Dot, live timer,
// real-time captions (browser speech-to-text → caption POST → Dot reply),
// transcript receipt + download, background agent during the call.
(() => {
const root = document.getElementById('pane-calls');
if (!root) return;

root.innerHTML = `
  <div class="sec" id="cl-root">
    <div class="sec-head">
      <span class="sec-title">Calls</span>
      <span class="sec-sub">Talk to a Dot — captions stream live, transcript is a receipt</span>
      <div class="grow"></div>
      <button class="btn-sm primary" id="cl-new">＋ New call</button>
    </div>
    <div id="cl-live"></div>
    <div id="cl-history"></div>
  </div>`;

const $ = (id) => document.getElementById(id);
const S = { dots: [], calls: [], active: null, loaded: false,
            tick: null, poll: null, rec: null, recOn: false,
            lastSig: '', interim: '' };

const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition || null;

function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return (h ? String(h).padStart(2, '0') + ':' : '') +
    String(m).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
}

async function load() {
  try {
    S.dots = await JV.api('/api/dots');
    const all = await JV.api('/api/calls?status=any');
    S.calls = all;
    S.active = all.find(c => c.status === 'active') || null;
  } catch (e) {
    $('cl-history').innerHTML = `<div class="pane-empty">${JV.esc(e.message)}</div>`;
    return;
  }
  renderLive();
  renderHistory();
}

// ── start ───────────────────────────────────────────────────────────────────
$('cl-new').onclick = async () => {
  if (S.active) { JV.toast('A call is already live'); return; }
  try {
    if (!S.dots.length) S.dots = await JV.api('/api/dots');
  } catch (_) {}
  if (!S.dots.length) { JV.toast('Create a Dot first (Agents)'); return; }
  const pick = prompt('Call which Dot?\n' +
    S.dots.map(d => `${d.id}: ${d.name} — ${d.role || ''}`).join('\n') +
    '\n\nEnter Dot ID:', String(S.dots[0].id));
  if (!pick) return;
  try {
    const c = await JV.api('/api/calls', { method: 'POST',
      body: JSON.stringify({ dot_id: parseInt(pick, 10) }) });
    S.active = c;
    JV.toast('Call connected 📞');
    renderLive(); renderHistory(); startLive();
  } catch (e) { JV.toast(e.message); }
};

// ── live panel ──────────────────────────────────────────────────────────────
function renderLive() {
  const box = $('cl-live');
  if (!S.active) {
    box.innerHTML = '';
    stopLive();
    return;
  }
  const dot = S.dots.find(d => d.id === S.active.dot_id);
  box.innerHTML = `
    <div class="call-live" id="cl-card">
      <div class="cl-top">
        <span class="chip green">● LIVE</span>
        <span class="sec-title" style="text-transform:none;font-size:15px">${JV.esc(dot ? dot.name : 'Dot #' + S.active.dot_id)}</span>
        <span class="chip">${JV.esc(S.active.provider || 'none')} audio</span>
        ${S.active.page_id ? `<span class="chip">page #${S.active.page_id}</span>` : ''}
        <div class="grow"></div>
        <span class="call-timer" id="cl-timer">00:00</span>
        <button class="btn-sm ${S.recOn ? 'danger' : 'primary'}" id="cl-mic">${S.recOn ? '⏹ Mic off' : '🎤 Mic on'}</button>
        <button class="btn-sm danger" id="cl-end">End call</button>
      </div>
      ${!SpeechRec ? `<div class="sec-sub" style="margin-top:8px">⚠ This browser has no SpeechRecognition — type captions below (still live).</div>` : ''}
      <div class="call-caps" id="cl-caps"><div class="pane-empty">Captions appear here — speak or type…</div></div>
      <div class="row cap-composer" style="margin-top:10px">
        <input class="inp" id="cl-inp" placeholder="Say something (or type a caption)…">
        <button class="btn-sm primary" id="cl-send">Send caption</button>
        <button class="btn-sm" id="cl-bg">⚙ Background agent…</button>
        <a class="btn-sm" id="cl-dl" href="#" style="text-decoration:none">⬇ Transcript</a>
      </div>
      <div id="cl-extra"></div>
    </div>`;
  $('cl-end').onclick = endCall;
  $('cl-send').onclick = sendCaption;
  $('cl-inp').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); sendCaption(); }
  });
  $('cl-mic').onclick = toggleMic;
  $('cl-bg').onclick = showBgForm;
  $('cl-dl').onclick = (e) => { e.preventDefault(); downloadTranscript(S.active.id); };
  refreshCaps(true);
  startLive();
}

function startLive() {
  stopLive();
  const started = S.active ? S.active.started_at : 0;
  const upd = () => {
    const el = $('cl-timer');
    if (el && started) el.textContent = fmtDur(Date.now() / 1000 - started);
  };
  upd();
  S.tick = setInterval(upd, 500);
  S.poll = setInterval(() => refreshCaps(false), 2000);
}
function stopLive() {
  clearInterval(S.tick); S.tick = null;
  clearInterval(S.poll); S.poll = null;
  stopRec();
}

async function refreshCaps(force) {
  if (!S.active) return;
  try {
    const msgs = await JV.api(`/api/calls/${S.active.id}/transcript`);
    const sig = msgs.length + ':' + (msgs.at(-1) ? msgs.at(-1).content.slice(0, 24) : '');
    if (sig === S.lastSig && !force) return;
    S.lastSig = sig;
    const caps = $('cl-caps');
    if (!caps) return;
    if (!msgs.length) {
      caps.innerHTML = '<div class="pane-empty">Captions appear here — speak or type…</div>';
      return;
    }
    caps.innerHTML = msgs.slice(-40).map(m => `
      <div class="cap-line ${m.speaker === 'user' ? 'user' : 'dot'}">
        <div class="who">${m.speaker === 'user' ? 'You' : 'Dot'}</div>${JV.esc(m.content)}
      </div>`).join('') +
      (S.interim ? `<div class="cap-line user" style="opacity:.6"><div class="who">You (hearing…)</div>${JV.esc(S.interim)}</div>` : '');
    caps.scrollTop = caps.scrollHeight;
  } catch (_) {}
}

async function sendCaption() {
  const inp = $('cl-inp');
  const txt = (inp.value || '').trim();
  if (!txt || !S.active) return;
  inp.value = '';
  const caps = $('cl-caps');
  caps.insertAdjacentHTML('beforeend',
    `<div class="cap-line user"><div class="who">You</div>${JV.esc(txt)}</div>`);
  caps.insertAdjacentHTML('beforeend',
    `<div class="cap-line dot" id="cl-think"><div class="who">Dot</div>…</div>`);
  caps.scrollTop = caps.scrollHeight;
  try {
    const res = await JV.api(`/api/calls/${S.active.id}/captions`, {
      method: 'POST',
      body: JSON.stringify({ speaker: 'user', text: txt, generate_reply: true }) });
    const think = document.getElementById('cl-think');
    if (think) {
      if (res.dot_caption) {
        think.innerHTML = `<div class="who">Dot</div>${JV.esc(res.dot_caption)}`;
      } else think.remove();
    }
    S.lastSig = '';
    refreshCaps(false);
    JV.refreshPending();
  } catch (e) {
    const think = document.getElementById('cl-think');
    if (think) think.innerHTML = `<div class="who">Dot</div>${JV.esc('⚠ ' + e.message)}`;
  }
}

// ── mic (speech → captions) ─────────────────────────────────────────────────
function toggleMic() {
  if (S.recOn) { stopRec(); renderLive(); return; }
  if (!SpeechRec) { JV.toast('Speech recognition not supported in this browser'); return; }
  const rec = new SpeechRec();
  rec.lang = navigator.language || 'en-IN';
  rec.continuous = true;
  rec.interimResults = true;
  rec.onresult = (ev) => {
    let interim = '', final = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) final += r[0].transcript;
      else interim += r[0].transcript;
    }
    S.interim = interim;
    if (final.trim()) {
      const inp = $('cl-inp');
      if (inp) { inp.value = final.trim(); sendCaption(); }
    }
    refreshCaps(false);
  };
  rec.onerror = (e) => {
    JV.toast('Mic: ' + (e.error || 'error'));
    if (e.error === 'not-allowed' || e.error === 'service-not-allowed') stopRec();
  };
  rec.onend = () => { if (S.recOn) { try { rec.start(); } catch (_) {} } };
  try { rec.start(); S.recOn = true; S.rec = rec; }
  catch (_) { JV.toast('Mic could not start'); return; }
  renderLive();
}
function stopRec() {
  S.recOn = false;
  if (S.rec) { try { S.rec.stop(); } catch (_) {} S.rec = null; }
  S.interim = '';
}

// ── background agent during the call ────────────────────────────────────────
function showBgForm() {
  const box = $('cl-extra');
  if (!box) return;
  box.innerHTML = `
    <div class="card" style="margin-top:10px">
      <div class="sec-sub" style="margin-bottom:8px">Background compute agent — runs on this call's Dot while you talk
        (watch live via <b>dots action=task_runs which=&lt;id&gt;</b> / AGENDA).</div>
      <div class="row">
        <input class="inp" id="cl-bgi" placeholder="e.g. research the caller's company and summarize" style="flex:1;min-width:240px">
        <select class="inp" id="cl-bgt">
          <option value="60">every 60s</option>
          <option value="120" selected>every 2m</option>
          <option value="600">every 10m</option>
        </select>
        <button class="btn-sm primary" id="cl-bgs">Start</button>
      </div>
    </div>`;
  $('cl-bgs').onclick = async () => {
    const ins = $('cl-bgi').value.trim();
    if (!ins) { JV.toast('Instruction required'); return; }
    try {
      const r = await JV.api(`/api/calls/${S.active.id}/background`, {
        method: 'POST',
        body: JSON.stringify({ instruction: ins,
                               every_seconds: parseInt($('cl-bgt').value, 10) }) });
      JV.toast('Background agent live — task #' + r.task.id);
      box.innerHTML = `<div class="card" style="margin-top:10px">✓ Background agent <b>#${r.task.id}</b> running — see AGENDA → task runs.</div>`;
    } catch (e) { JV.toast(e.message); }
  };
}

// ── end / history ───────────────────────────────────────────────────────────
async function endCall() {
  if (!S.active) return;
  if (!confirm('End this call?')) return;
  try {
    const c = await JV.api(`/api/calls/${S.active.id}/end`, { method: 'POST' });
    JV.toast(`Call ended — ${fmtDur(c.elapsed_seconds || 0)}`);
  } catch (e) { JV.toast(e.message); }
  stopRec(); stopLive();
  S.active = null;
  S.lastSig = '';
  renderLive();
  load();
}

async function downloadTranscript(cid) {
  try {
    const msgs = await JV.api(`/api/calls/${cid}/transcript?format=json`);
    const lines = [`Call #${cid} transcript`, '-'.repeat(40)];
    msgs.forEach(m => lines.push(`[${m.speaker}] ${m.content}`));
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `call-${cid}-transcript.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { JV.toast(e.message); }
}

function renderHistory() {
  const past = S.calls.filter(c => c.status !== 'active');
  $('cl-history').innerHTML = past.length ? `
    <div class="sec-title" style="margin:18px 0 8px">Recent calls</div>
    <div class="card" style="padding:0">
      <table class="tbl">
        <thead><tr><th>#</th><th>dot</th><th>started</th><th>duration</th><th></th></tr></thead>
        <tbody>${past.slice(0, 30).map(c => `
          <tr>
            <td>${c.id}</td>
            <td>#${c.dot_id}${c.page_id ? ` · page ${c.page_id}` : ''}</td>
            <td class="mono muted">${new Date(c.started_at * 1000).toLocaleString()}</td>
            <td class="mono">${fmtDur((c.ended_at || c.started_at) - c.started_at)}</td>
            <td><button class="btn-sm" data-dl="${c.id}">⬇ Transcript</button></td>
          </tr>`).join('')}</tbody>
      </table>
    </div>` : '';
  $('cl-history').querySelectorAll('[data-dl]').forEach(b => {
    b.onclick = () => downloadTranscript(parseInt(b.dataset.dl, 10));
  });
}

JV.onShow('calls', () => { if (!S.loaded) { S.loaded = true; load(); }
  else { renderLive(); } });
})();
