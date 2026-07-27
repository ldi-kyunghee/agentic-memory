/* mem0×HaluMem 정성분석 SPA — 빌드 스텝 없는 vanilla JS */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opt) => {
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};

const S = {
  runs: [], run: null, judge: null, users: [], uuid: null,
  bundle: null, bundleB: null, runB: null,
  tab: "sessions", itab: "detail",
  session: null,            // 선택된 세션 id
  qaFilter: "all",
  anchor: "run",            // 코멘트/상세 앵커
  anchorObj: null,          // 상세 패널에 보여줄 객체
  comments: [], fielddict: {}, traceCache: new Map(),
  author: localStorage.getItem("analyst_name") || "",
};

/* ---------- 부트스트랩 ---------- */

async function boot() {
  S.fielddict = await api("/api/fielddict");
  S.runs = (await api("/api/runs")).filter((r) => r.available);
  const sel = $("#sel-run");
  sel.innerHTML = S.runs.map((r) => `<option value="${r.run}">${esc(r.label)}</option>`).join("");
  sel.onchange = () => setRun(sel.value);
  $("#sel-judge").onchange = () => { S.judge = $("#sel-judge").value; loadBundle(); };
  $("#sel-user").onchange = () => { S.uuid = $("#sel-user").value; loadBundle(); };
  $$("#tabs button").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
  $$("#insp-tabs button").forEach((b) => (b.onclick = () => setITab(b.dataset.itab)));
  $("#who").onclick = askName;
  $("#name-save").onclick = saveName;
  renderWho();
  if (!S.author) askName();
  await setRun(S.runs[0].run);
}

function askName() { $("#modal").classList.remove("hidden"); $("#name-input").value = S.author; $("#name-input").focus(); }
function saveName() {
  const v = $("#name-input").value.trim();
  if (!v) return;
  S.author = v; localStorage.setItem("analyst_name", v);
  $("#modal").classList.add("hidden"); renderWho();
}
function renderWho() { $("#who").textContent = S.author ? `👤 ${S.author}` : "👤 이름 설정"; }

async function setRun(run) {
  S.run = run;
  const meta = S.runs.find((r) => r.run === run);
  // 기본 judge: 20u 런은 전 유저를 덮는 qwen4b, 4u 런은 nano (nano는 첫 4유저만 라벨 보유)
  const defJudge = meta.users > 4 && meta.judges.includes("qwen4b") ? "qwen4b"
    : meta.judges.includes("nano") ? "nano" : meta.judges[0];
  $("#sel-judge").innerHTML = meta.judges.map((j) => `<option ${j === defJudge ? "selected" : ""}>${j}</option>`).join("");
  S.judge = $("#sel-judge").value;
  S.users = await api(`/api/runs/${run}/users`);
  const prev = S.uuid;
  $("#sel-user").innerHTML = S.users.map((u) => `<option value="${u.uuid}" ${u.uuid === prev ? "selected" : ""}>${esc(u.user_name)}</option>`).join("");
  S.uuid = $("#sel-user").value;
  await loadBundle();
}

async function loadBundle() {
  S.bundle = await api(`/api/bundle/${S.run}/${S.uuid}?judge=${S.judge}`);
  S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
  S.traceCache.clear();
  S.bundleB = null;
  const firstReal = S.bundle.sessions.find((s) => !s.generated_qa_session);
  S.session = firstReal ? firstReal.session_id : null;
  setAnchor("run", { run: S.run, uuid: S.uuid, user: S.bundle.user_name });
  render();
}

/* ---------- 탭/앵커 ---------- */

function setTab(t) { S.tab = t; $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t)); render(); }
function setITab(t) { S.itab = t; $$("#insp-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.itab === t)); renderInspector(); }
function setAnchor(anchor, obj) {
  S.anchor = anchor; S.anchorObj = obj;
  $$(".row.selected").forEach((el) => el.classList.remove("selected"));
  renderInspector();
}

/* ---------- 배지 헬퍼 ---------- */

const scoreBadge = (v) => v == null ? `<span class="badge bnull">–</span>` : `<span class="badge b${v}">${v}</span>`;
const verdictBadge = (v) => {
  if (!v) return `<span class="badge bnull">–</span>`;
  const c = v[0] === "C" ? "bC" : v[0] === "H" ? "bH" : "bO";
  return `<span class="badge ${c}">${esc(v[0])}</span>`;
};
const opChip = (op) => op ? `<span class="op ${esc(op)}">${esc(op)}</span>` : "";
const dictTip = (k) => esc(S.fielddict[k] || "");

/* ---------- 근사 턴 앵커링 (lexical, "추정") ---------- */

function tokens(t) {
  return new Set((t || "").toLowerCase().match(/[a-z가-힣0-9][a-z가-힣0-9.\-:/]{2,}/g) || []);
}
function anchorTurns(session) {
  const turnToks = session.dialogue.map((t) => tokens(t.content));
  const place = (text) => {
    const tk = tokens(text);
    if (!tk.size) return -1;
    let best = -1, bestScore = 0;
    turnToks.forEach((tt, i) => {
      let hit = 0; tk.forEach((w) => { if (tt.has(w)) hit++; });
      const score = hit / tk.size;
      if (score > bestScore) { bestScore = score; best = i; }
    });
    return bestScore >= 0.18 ? best : -1;
  };
  const map = session.dialogue.map(() => ({ g: [], e: [] }));
  session.golden.forEach((mp, i) => { const t = place(mp.memory_content); if (t >= 0) map[t].g.push(i); });
  session.extracted.forEach((m, i) => { const t = place(m.text); if (t >= 0) map[t].e.push(i); });
  return map;
}

/* ---------- 메인 렌더 ---------- */

function render() {
  if (!S.bundle) return;
  if (S.tab === "sessions") renderSessions();
  else if (S.tab === "qa") renderQA();
  else if (S.tab === "compare") renderCompare();
  else renderDigest();
  renderInspector();
}

/* ----- Sessions 탭 ----- */

function sessionSummary(s) {
  if (s.generated_qa_session) return null;
  const g2 = s.golden.filter((m) => m.judge?.score === 2 || m.judge?.label === "Correct").length;
  const g0 = s.golden.filter((m) => m.judge?.kind === "integrity" && m.judge?.score === 0).length;
  const a0 = s.extracted.filter((m) => m.judge?.score === 0).length;
  const qaBad = s.questions.filter((q) => q.judge && q.judge !== "Correct").length;
  return { g2, g0, a0, qaBad };
}

function renderSessions() {
  const sb = $("#sidebar");
  sb.innerHTML = S.bundle.sessions.map((s) => {
    if (s.generated_qa_session) return "";
    const m = sessionSummary(s);
    const flags = [
      m.g0 ? `<span class="badge b0" title="integrity 0점 골든">${m.g0}</span>` : "",
      m.qaBad ? `<span class="badge bH" title="오답 QA">${m.qaBad}</span>` : "",
    ].join("");
    return `<div class="side-item ${s.session_id === S.session ? "active" : ""}" data-sid="${s.session_id}">
      <b>S${s.session_id}</b>
      <span class="t">${esc((s.start_time || "").slice(0, 12))}</span>
      <span class="flags">${flags}</span></div>`;
  }).join("");
  $$(".side-item", sb).forEach((el) => (el.onclick = () => { S.session = +el.dataset.sid; renderSessions(); }));

  const s = S.bundle.sessions.find((x) => x.session_id === S.session);
  if (!s) { $("#content").innerHTML = "<p class='muted'>세션을 선택하세요</p>"; return; }
  const anchors = anchorTurns(s);

  const dlg = s.dialogue.map((t, ti) => {
    const a = anchors[ti];
    const chips = [
      ...a.g.map((gi) => `<span class="anchor-chip g" data-a="mp:${gi}" title="골든 (근사 앵커·추정): ${esc(s.golden[gi].memory_content)}">G ${esc(s.golden[gi].memory_content)}</span>`),
      ...a.e.map((ei) => `<span class="anchor-chip e" data-a="ext:${ei}" title="추출 (근사 앵커·추정): ${esc(s.extracted[ei].text)}">E ${esc(s.extracted[ei].text)}</span>`),
    ].join("");
    return `<div class="turn ${t.role}"><div class="role">${esc(t.role)}<br><span class="small muted">#${esc(t.dialogue_turn)}</span></div>
      <div class="bubble">${esc(t.content)}</div><div class="anchors">${chips}</div></div>`;
  }).join("");

  const golden = s.golden.map((mp, i) => {
    const j = mp.judge || {};
    const badge = j.kind === "update" ? verdictBadge(j.label) : scoreBadge(j.score);
    return `<div class="row" data-a="mp:${i}"><span>${badge}</span>
      ${mp.is_update === "True" ? '<span class="tagchip">upd</span>' : ""}
      ${mp.memory_source !== "system" ? `<span class="tagchip">${esc(mp.memory_source)}</span>` : ""}
      <span class="txt">${esc(mp.memory_content)}</span>
      <span class="small muted">${esc(mp.memory_type || "").split(" ")[0]}</span></div>`;
  }).join("");

  const extracted = s.extracted.map((m, i) => `
    <div class="row" data-a="ext:${i}"><span>${scoreBadge(m.judge?.score)}</span>${opChip(m.origin)}
      <span class="txt">${esc(m.text)}</span>
      ${m.judge?.is_included === "true" ? '<span class="tagchip" title="Target P 모수 포함">tgt</span>' : ""}</div>`).join("");

  const qas = s.questions.map((q, i) => `
    <div class="row" data-a="qa:${i}"><span>${verdictBadge(q.judge)}</span>
      <span class="txt">${esc(q.question)}</span>
      <span class="small muted">${esc(q.question_type || "")}</span></div>`).join("");

  $("#content").innerHTML = `
    <div class="hint">세션 S${s.session_id} — 행 클릭 시 우측에 상세/코멘트. 대화 오른쪽 칩은 <b>근사 턴 앵커(추정)</b>: G=골든, E=추출.</div>
    <div class="card"><h4 title="${dictTip("dialogue_turn")}">대화 (${s.dialogue.length}턴)</h4><div class="body">${dlg}</div></div>
    <div class="card"><h4 title="${dictTip("memory_points")}">골든 메모리 (${s.golden.length}) <span class="small muted">배지: integrity 0/1/2 또는 update C/H/O</span></h4><div class="body">${golden}</div></div>
    <div class="card"><h4 title="${dictTip("extracted_memories")}">추출 메모리 (${s.extracted.length}) <span class="small muted">배지: accuracy 0/1/2 · 유래 op</span></h4><div class="body">${extracted}</div></div>
    <div class="card"><h4 title="${dictTip("questions")}">QA (${s.questions.length})</h4><div class="body">${qas}</div></div>`;

  bindRowAnchors(s);
}

function bindRowAnchors(s) {
  $$("#content [data-a]").forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const [kind, idx] = el.dataset.a.split(":");
      const obj = kind === "mp" ? s.golden[+idx] : kind === "ext" ? s.extracted[+idx] : s.questions[+idx];
      setAnchor(`session:${s.session_id}/${kind}:${idx}`, obj);
      if (el.classList.contains("row")) el.classList.add("selected");
    };
  });
}

/* ----- QA 탭 ----- */

function allQAs() {
  const out = [];
  S.bundle.sessions.forEach((s) => {
    if (s.generated_qa_session) return;
    s.questions.forEach((q, i) => out.push({ s, q, i }));
  });
  return out;
}

function renderQA() {
  const filters = ["all", "Correct", "Hallucination", "Omission"];
  const items = allQAs().filter(({ q }) => S.qaFilter === "all" || (q.judge || "").startsWith(S.qaFilter[0]) && q.judge?.includes(S.qaFilter));
  $("#sidebar").innerHTML = `
    <div style="padding:8px"><div class="pill-filter">${filters.map((f) =>
      `<button class="${S.qaFilter === f ? "on" : ""}" data-f="${f}">${f === "all" ? "전체" : f[0]}</button>`).join("")}</div></div>` +
    items.map(({ s, q, i }) => `
      <div class="side-item" data-sid="${s.session_id}" data-qi="${i}">
        ${verdictBadge(q.judge)} <span class="txt small">${esc(q.question.slice(0, 60))}</span></div>`).join("");
  $$("#sidebar .pill-filter button").forEach((b) => (b.onclick = () => { S.qaFilter = b.dataset.f; renderQA(); }));
  $$("#sidebar .side-item").forEach((el) => (el.onclick = () => renderQADetail(+el.dataset.sid, +el.dataset.qi)));
  $("#content").innerHTML = `<p class="hint">좌측에서 QA를 선택하세요 (판정 필터: 전체/C/H/O). 총 ${allQAs().length}문항.</p>`;
}

function renderQADetail(sid, qi) {
  const s = S.bundle.sessions.find((x) => x.session_id === sid);
  const q = s.questions[qi];
  const ev = (q.evidence || []).map((e) => `<div class="row"><span class="tagchip">근거</span><span class="txt">${esc(e.memory_content)}</span></div>`).join("");
  // context는 런에 따라 배열(검색 hit 목록) 또는 문자열(포맷된 컨텍스트 원문)
  const ctxArr = Array.isArray(q.context) ? q.context : null;
  const ctx = ctxArr
    ? ctxArr.map((c, i) => {
        const text = typeof c === "string" ? c : c.memory || c.text || JSON.stringify(c);
        return `<div class="row"><span class="small muted">#${i + 1}</span><span class="txt">${esc(text)}</span></div>`;
      }).join("")
    : `<pre class="mono">${esc(q.context || "(없음)")}</pre>`;
  const ctxCount = ctxArr ? ctxArr.length : (q.context ? "원문" : 0);
  $("#content").innerHTML = `
    <div class="hint">S${sid} · ${esc(q.question_type || "")} — 4자 대조: 질문 → 정답 → 시스템 답변 → context</div>
    <div class="card"><h4>질문 ${verdictBadge(q.judge)}</h4><div class="body">${esc(q.question)}</div></div>
    <div class="card"><h4>골든 정답</h4><div class="body">${esc(q.answer)}</div></div>
    <div class="card"><h4 title="${dictTip("system_response")}">시스템 답변 (A′)</h4><div class="body">${esc(q.system_response || "(A′ 미실행)")}</div></div>
    <div class="card"><h4 title="${dictTip("evidence")}">Evidence (${(q.evidence || []).length})</h4><div class="body">${ev}</div></div>
    <div class="card"><h4 title="${dictTip("context")}">검색 Context (${ctxCount})</h4><div class="body">${ctx}</div></div>`;
  S.session = sid;
  setAnchor(`session:${sid}/qa:${qi}`, q);
}

/* ----- Compare 탭 ----- */

async function renderCompare() {
  const others = S.runs.filter((r) => r.run !== S.run);
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted">기준 런: <b>${esc(S.run)}</b></p>
    <p class="small">비교 런 선택:</p>
    <select id="sel-runb" style="width:100%">${others.map((r) => `<option value="${r.run}" ${r.run === S.runB ? "selected" : ""}>${esc(r.label)}</option>`).join("")}</select>
    <button id="btn-cmp" style="margin-top:8px;width:100%">비교 로드</button></div>`;
  $("#btn-cmp").onclick = async () => {
    S.runB = $("#sel-runb").value;
    try { S.bundleB = await api(`/api/bundle/${S.runB}/${S.uuid}?judge=nano`); }
    catch (e) { $("#content").innerHTML = `<p class="muted">비교 런에 이 유저가 없음: ${esc(e.message)}</p>`; return; }
    renderCompare();
  };
  if (!S.bundleB) { $("#content").innerHTML = "<p class='hint'>좌측에서 비교 런을 선택하고 로드하세요. 동일 유저를 두 런에서 나란히 봅니다.</p>"; return; }

  const rows = [];
  S.bundle.sessions.forEach((sa) => {
    if (sa.generated_qa_session) return;
    const sb = S.bundleB.sessions.find((x) => x.session_id === sa.session_id);
    const f = (s) => {
      if (!s) return "–";
      const m = sessionSummary(s);
      return `골든2점 ${m.g2}/${s.golden.length} · 추출 ${s.extracted.length} · acc0 ${m.a0} · QA오답 ${m.qaBad}`;
    };
    rows.push(`<tr><td>S${sa.session_id}</td><td>${f(sa)}</td><td>${f(sb)}</td></tr>`);
  });

  const qaRows = [];
  allQAs().forEach(({ s, q, i }) => {
    const sb = S.bundleB.sessions.find((x) => x.session_id === s.session_id);
    const qb = sb?.questions?.find((x) => x.question === q.question);
    const diff = qb && q.judge !== qb.judge;
    qaRows.push(`<tr${diff ? ' class="diffrow"' : ""}><td>S${s.session_id}</td>
      <td>${esc(q.question.slice(0, 90))}</td>
      <td ${diff ? 'class="diff"' : ""}>${verdictBadge(q.judge)}</td>
      <td ${diff ? 'class="diff"' : ""}>${verdictBadge(qb?.judge)}</td></tr>`);
  });

  $("#content").innerHTML = `
    <div class="hint">A = ${esc(S.run)} / B = ${esc(S.runB)} — 동일 유저 ${esc(S.bundle.user_name)}. 주황 = 판정 갈림.</div>
    <div class="card"><h4>세션 요약 대조</h4><div class="body">
      <table class="cmp"><tr><th></th><th>A: ${esc(S.run)}</th><th>B: ${esc(S.runB)}</th></tr>${rows.join("")}</table></div></div>
    <div class="card"><h4>QA 판정 대조</h4><div class="body">
      <table class="cmp"><tr><th>세션</th><th>질문</th><th>A</th><th>B</th></tr>${qaRows.join("")}</table></div></div>`;
}

/* ----- Digest 탭 ----- */

async function renderDigest() {
  const all = await api(`/api/digest/${S.uuid}`);
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted">유저 <b>${esc(S.bundle.user_name)}</b>의 전체 런·전체 분석가 코멘트</p>
    <a href="/api/export/${S.uuid}" target="_blank">📄 Markdown export</a></div>`;
  if (!all.length) { $("#content").innerHTML = "<p class='hint'>아직 코멘트가 없습니다.</p>"; return; }
  const byRun = {};
  all.forEach((c) => (byRun[c.run] = byRun[c.run] || []).push(c));
  $("#content").innerHTML = Object.entries(byRun).map(([run, list]) => `
    <div class="card"><h4>run: ${esc(run)} (${list.length})</h4><div class="body">
      ${list.map(cmtHTML).join("")}</div></div>`).join("");
  $$("#content .del").forEach((b) => (b.onclick = async () => {
    await api(`/api/comments/${b.dataset.id}?author=${encodeURIComponent(S.author)}`, { method: "DELETE" });
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
    renderDigest();
  }));
}

/* ---------- 인스펙터 ---------- */

function renderInspector() {
  $("#cmt-count").textContent = S.comments.filter((c) => c.anchor === S.anchor).length || "";
  const el = $("#insp-body");
  if (S.itab === "detail") {
    el.innerHTML = `<div class="anchor-label">앵커: ${esc(S.anchor)}</div><div class="jt" style="margin-top:8px">${jsonTree(S.anchorObj)}</div>`;
  } else if (S.itab === "trace") {
    renderTrace(el);
  } else {
    renderComments(el);
  }
}

function jsonTree(v, key = null) {
  const k = key !== null ? `<span class="k" title="${dictTip(key)}">${esc(key)}</span>: ` : "";
  if (v === null || v === undefined) return `<div class="leaf">${k}<span class="muted">null</span></div>`;
  if (typeof v === "string") return `<div class="leaf">${k}<span class="s">"${esc(v)}"</span></div>`;
  if (typeof v === "number" || typeof v === "boolean") return `<div class="leaf">${k}<span class="n">${v}</span></div>`;
  if (Array.isArray(v)) {
    if (!v.length) return `<div class="leaf">${k}[]</div>`;
    return `<details ${v.length <= 6 ? "open" : ""}><summary>${k}[${v.length}]</summary>${v.map((x, i) => jsonTree(x, String(i))).join("")}</details>`;
  }
  const keys = Object.keys(v);
  return `<details open><summary>${k}{${keys.length}}</summary>${keys.map((kk) => jsonTree(v[kk], kk)).join("")}</details>`;
}

async function renderTrace(el) {
  if (S.session == null) { el.innerHTML = "<p class='muted'>세션을 먼저 선택하세요</p>"; return; }
  el.innerHTML = "<p class='muted'>trace 로딩…</p>";
  let recs = S.traceCache.get(S.session);
  if (!recs) {
    try { recs = await api(`/api/trace/${S.run}/${S.uuid}?session=${S.session}`); }
    catch { el.innerHTML = "<p class='muted'>이 런에는 trace가 없습니다</p>"; return; }
    S.traceCache.set(S.session, recs);
  }
  el.innerHTML = `<div class="anchor-label">S${S.session} trace — ${recs.length}건 (시간순)</div>` +
    recs.map((r, i) => `<div class="tr-rec" data-i="${i}"><div class="hdr">
      <span class="small muted">#${r.seq}</span>
      <b title="${dictTip(r.event)}">${esc(r.event)}</b>
      <span class="tagchip">${esc(r.purpose || r.stage || "")}</span>
      <span class="small muted" style="margin-left:auto">${Math.round(r.duration_ms || 0)}ms</span></div></div>`).join("") +
    `<div id="tr-detail"></div>`;
  $$(".tr-rec", el).forEach((div) => (div.onclick = () => {
    const r = recs[+div.dataset.i];
    let html = "";
    if (r.llm) {
      html = r.llm.messages.map((m) => `<p class="small"><b>${esc(m.role)}</b></p><pre class="mono">${esc(m.content)}</pre>`).join("") +
        `<p class="small"><b>response</b></p><pre class="mono">${esc(r.llm.response)}</pre>`;
    } else {
      html = `<pre class="mono">${esc(JSON.stringify(r, null, 2))}</pre>`;
    }
    $("#tr-detail").innerHTML = `<div class="anchor-label" style="margin:8px 0">#${r.seq} ${esc(r.event)} ${esc(r.purpose || "")}</div>` + html;
  }));
}

function cmtHTML(c) {
  const mine = c.author === S.author;
  return `<div class="cmt"><div class="meta"><span class="author">${esc(c.author)}</span>
    ${c.tag ? `<span class="tagchip">${esc(c.tag)}</span>` : ""}
    <span>${esc(c.anchor)}</span><span>${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</span>
    ${mine ? `<button class="del" data-id="${c.id}">삭제</button>` : ""}</div>
    <div class="body-text">${esc(c.body)}</div></div>`;
}

function renderComments(el) {
  const here = S.comments.filter((c) => c.anchor === S.anchor);
  const others = S.comments.filter((c) => c.anchor !== S.anchor);
  el.innerHTML = `
    <div class="anchor-label">앵커: ${esc(S.anchor)}</div>
    <div id="cmt-form" style="margin:8px 0">
      <textarea id="cmt-body" placeholder="관찰/해석을 남겨주세요 (분담표 양식: 관찰 → 유형 태그 → 시사점)"></textarea>
      <div style="display:flex;gap:6px;margin-top:4px">
        <select id="cmt-tag"><option value="">태그 없음</option><option>강점</option><option>약점</option><option>병목</option><option>judge오판</option><option>추출누락</option><option>재작성drift</option><option>기타</option></select>
        <button class="primary" id="cmt-add">등록</button></div></div>
    <h4 class="small muted">이 앵커 (${here.length})</h4>${here.map(cmtHTML).join("") || "<p class='small muted'>없음</p>"}
    <h4 class="small muted">이 유저의 다른 앵커 (${others.length})</h4>${others.map(cmtHTML).join("")}`;
  $("#cmt-add").onclick = async () => {
    const body = $("#cmt-body").value.trim();
    if (!body) return;
    if (!S.author) { askName(); return; }
    await api("/api/comments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: S.run, uuid: S.uuid, anchor: S.anchor, author: S.author, tag: $("#cmt-tag").value, body }),
    });
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
    renderInspector();
  };
  $$(".del", el).forEach((b) => (b.onclick = async () => {
    await api(`/api/comments/${b.dataset.id}?author=${encodeURIComponent(S.author)}`, { method: "DELETE" });
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
    renderInspector();
  }));
}

boot().catch((e) => { document.body.innerHTML = `<pre style="padding:20px;color:#c92a2a">${esc(e.stack || e.message)}</pre>`; });
