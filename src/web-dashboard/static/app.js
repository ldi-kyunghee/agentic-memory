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
  backbone: null, prompt: null, backboneB: null, promptB: null, runB: null,
  bundle: null, bundleB: null, bundleCache: new Map(),
  tab: "sessions", itab: "detail",
  session: null, qaFilter: "all", digestScope: "user",
  anchor: "run", anchorObj: null, pendingQuote: "",
  comments: [], fielddict: {}, traceCache: new Map(), anchorCacheB: new Map(),
  author: localStorage.getItem("analyst_name") || "",
};

/* ---------- 로딩 표시 ---------- */

let busyCount = 0;
function busy(on, msg = "불러오는 중…") {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  $("#loading").classList.toggle("hidden", busyCount === 0);
  if (on) $("#loading-msg").textContent = msg;
  $$("#topbar select").forEach((s) => (s.disabled = busyCount > 0));
}

/* ---------- 부트스트랩 ---------- */

async function boot() {
  busy(true, "레지스트리 로딩…");
  try {
    S.fielddict = await api("/api/fielddict");
    S.runs = (await api("/api/runs")).filter((r) => r.available);
    S.first4 = await api("/api/first4");  // nano judge 라벨 보유 유저 (★ 표시용)
  } finally { busy(false); }

  const backbones = [...new Set(S.runs.map((r) => r.backbone))];
  $("#sel-backbone").innerHTML = backbones.map((b) => `<option>${esc(b)}</option>`).join("");
  $("#sel-backbone").onchange = () => { S.backbone = $("#sel-backbone").value; syncPrompts("A"); applyRun(); };
  $("#sel-prompt").onchange = () => { S.prompt = $("#sel-prompt").value; applyRun(); };
  $("#sel-judge").onchange = () => { S.judge = $("#sel-judge").value; loadBundle(); };
  $("#sel-user").onchange = () => { S.uuid = $("#sel-user").value; loadBundle(); };

  $("#btn-addb").onclick = () => { $("#grp-b").classList.remove("hidden"); $("#btn-addb").style.display = "none"; initB(); };
  $("#btn-delb").onclick = () => {
    $("#grp-b").classList.add("hidden"); $("#btn-addb").style.display = "";
    S.backboneB = S.promptB = S.runB = null; S.bundleB = null; S.anchorCacheB.clear(); render();
  };
  $("#sel-backbone-b").onchange = () => { S.backboneB = $("#sel-backbone-b").value; syncPrompts("B"); applyRunB(); };
  $("#sel-prompt-b").onchange = () => { S.promptB = $("#sel-prompt-b").value; applyRunB(); };

  $$("#tabs button").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
  $$("#insp-tabs button").forEach((b) => (b.onclick = () => setITab(b.dataset.itab)));
  $("#who").onclick = askName;
  $("#name-save").onclick = saveName;
  renderWho();
  if (!S.author) askName();

  initResizers();
  initDescBar();
  initSelectionComment();
  startPolling();

  // 강제 재로딩: 서버·클라이언트 캐시 전부 비우고 현재 화면 재구성
  $("#btn-reload").onclick = async () => {
    busy(true, "캐시 비우고 다시 로딩…");
    try {
      await api("/api/reload", { method: "POST" });
      S.bundleCache.clear(); S.traceCache.clear(); S.anchorCacheB.clear(); metricsCache.clear();
      await loadBundle();
    } finally { busy(false); }
  };

  S.backbone = backbones[0];
  syncPrompts("A");
  await applyRun();
}

function runsFor(backbone) { return S.runs.filter((r) => r.backbone === backbone); }
function resolveRun(backbone, prompt) { return S.runs.find((r) => r.backbone === backbone && r.prompt === prompt); }

function syncPrompts(which) {
  const bb = which === "A" ? S.backbone : S.backboneB;
  const sel = which === "A" ? $("#sel-prompt") : $("#sel-prompt-b");
  const prompts = [...new Set(runsFor(bb).map((r) => r.prompt))];
  sel.innerHTML = prompts.map((p) => `<option>${esc(p)}</option>`).join("");
  if (which === "A") S.prompt = sel.value; else S.promptB = sel.value;
}

async function applyRun() {
  const meta = resolveRun(S.backbone, S.prompt);
  if (!meta) return;
  S.run = meta.run;
  $("#emb-name").textContent = meta.embedder || "–";
  // judge 기본값은 nano(신뢰 기준). 이미 선택된 judge가 새 런에도 있으면 유지 (세팅 전환 시 리셋 방지)
  const defJudge = meta.judges.includes(S.judge) ? S.judge
    : meta.judges.includes("nano") ? "nano" : meta.judges[0];
  $("#sel-judge").innerHTML = meta.judges.map((j) => `<option ${j === defJudge ? "selected" : ""}>${j}</option>`).join("");
  S.judge = $("#sel-judge").value;
  busy(true, "유저 목록…");
  try { S.users = await api(`/api/runs/${S.run}/users`); } finally { busy(false); }
  const prev = S.uuid;
  // ★ = nano judge 라벨 보유 유저 (데이터셋 첫 4명) — 맨 위로 정렬해 기본 선택도 ★에서 시작
  const isF4 = (u) => (S.first4 || []).includes(u.uuid);
  const ordered = [...S.users].sort((a, b) => isF4(b) - isF4(a));
  $("#sel-user").innerHTML = ordered.map((u) =>
    `<option value="${u.uuid}" ${u.uuid === prev ? "selected" : ""}>${isF4(u) ? "★ " : ""}${esc(u.user_name)}</option>`).join("");
  S.uuid = $("#sel-user").value;
  await loadBundle();
}

function initB() {
  const backbones = [...new Set(S.runs.map((r) => r.backbone))];
  $("#sel-backbone-b").innerHTML = backbones.map((b) => `<option>${esc(b)}</option>`).join("");
  S.backboneB = S.backbone;
  $("#sel-backbone-b").value = S.backboneB;
  syncPrompts("B");
  const other = [...new Set(runsFor(S.backboneB).map((r) => r.prompt))].find((p) => p !== S.prompt);
  if (other) { $("#sel-prompt-b").value = other; S.promptB = other; }
  applyRunB();
}

async function fetchBundle(run, judge, uuid) {
  const key = `${run}|${judge}|${uuid}`;
  if (!S.bundleCache.has(key)) {
    S.bundleCache.set(key, await api(`/api/bundle/${run}/${uuid}?judge=${judge}`));
  }
  return S.bundleCache.get(key);
}

async function applyRunB() {
  const meta = resolveRun(S.backboneB, S.promptB);
  if (!meta) { S.runB = null; S.bundleB = null; render(); return; }
  S.runB = meta.run;
  const judgeB = meta.judges.includes(S.judge) ? S.judge : (meta.judges.includes("nano") ? "nano" : meta.judges[0]);
  busy(true, `B 세팅 로딩 (${S.runB})…`);
  try { S.bundleB = await fetchBundle(S.runB, judgeB, S.uuid); }
  catch { S.bundleB = null; }
  finally { busy(false); }
  S.anchorCacheB.clear();
  render();
}

function askName() { $("#modal").classList.remove("hidden"); $("#name-input").value = S.author; $("#name-input").focus(); }
function saveName() {
  const v = $("#name-input").value.trim();
  if (!v) return;
  S.author = v; localStorage.setItem("analyst_name", v);
  $("#modal").classList.add("hidden"); renderWho();
}
function renderWho() { $("#who").textContent = S.author ? `👤 ${S.author}` : "👤 이름 설정"; }

async function loadBundle() {
  busy(true, `A 세팅 로딩 (${S.run})…`);
  try {
    S.bundle = await fetchBundle(S.run, S.judge, S.uuid);
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
  } finally { busy(false); }
  S.traceCache.clear();
  const firstReal = S.bundle.sessions.find((s) => !s.generated_qa_session);
  S.session = firstReal ? firstReal.session_id : null;
  if (S.runB) { await applyRunB(); return; } // applyRunB가 render까지 수행
  setAnchor("run", { run: S.run, uuid: S.uuid, user: S.bundle.user_name }, false);
  render();
}

/* ---------- 실시간 동기화 (폴링) ---------- */

function startPolling() {
  setInterval(async () => {
    if (document.visibilityState !== "visible" || !S.run || !S.uuid) return;
    try {
      const fresh = await api(`/api/comments/${S.run}/${S.uuid}`);
      if (JSON.stringify(fresh) !== JSON.stringify(S.comments)) {
        S.comments = fresh;
        renderCmtMarks();
        if (S.itab === "comments") renderInspector();
        if (S.tab === "digest") renderDigest();
      }
    } catch {}
  }, 4000);
}

/* ---------- 레이아웃 리사이즈 ---------- */

function initResizers() {
  const saved = JSON.parse(localStorage.getItem("layout_widths") || "null");
  const w = { left: saved?.left || 240, right: saved?.right || 420 };
  const apply = () => { $("#layout").style.gridTemplateColumns = `${w.left}px 5px 1fr 5px ${w.right}px`; };
  apply();
  const drag = (el, side) => {
    el.onmousedown = (e) => {
      e.preventDefault(); el.classList.add("on");
      const startX = e.clientX, start = w[side];
      const move = (ev) => {
        const d = ev.clientX - startX;
        w[side] = Math.max(140, Math.min(window.innerWidth * 0.6, side === "left" ? start + d : start - d));
        apply();
      };
      const up = () => {
        el.classList.remove("on");
        document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
        localStorage.setItem("layout_widths", JSON.stringify(w));
      };
      document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
    };
  };
  drag($("#rz1"), "left"); drag($("#rz2"), "right");
}

/* ---------- 마우스 추적 툴팁 (필드 사전) ---------- */

function initDescBar() {
  const tip = $("#tip");
  const place = (e) => {
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 10);
    const y = e.clientY + 18 > window.innerHeight - tip.offsetHeight - 10
      ? e.clientY - tip.offsetHeight - 10 : e.clientY + 18;
    tip.style.left = `${Math.max(4, x)}px`;
    tip.style.top = `${Math.max(4, y)}px`;
  };
  let cur = null;
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-k], [data-desc]");
    if (!el) { if (cur) { cur = null; tip.classList.add("hidden"); } return; }
    if (el === cur) return;
    cur = el;
    if (el.dataset.desc) tip.innerHTML = el.dataset.desc;
    else {
      const k = el.dataset.k;
      const d = S.fielddict[k];
      tip.innerHTML = d ? `<b>${esc(k)}</b> — ${esc(d)}` : `<b>${esc(k)}</b>`;
    }
    tip.classList.remove("hidden");
    place(e);
  });
  document.addEventListener("mousemove", (e) => { if (cur && !tip.classList.contains("hidden")) place(e); });
  document.addEventListener("mouseout", (e) => {
    if (cur && !cur.contains(e.relatedTarget)) { cur = null; tip.classList.add("hidden"); }
  });
}

/* ---------- 드래그 하이라이트 코멘트 ---------- */

function initSelectionComment() {
  const btn = $("#sel-cmt");
  document.addEventListener("mouseup", (e) => {
    if (e.target === btn) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel?.toString().trim() || "";
      const inContent = sel?.anchorNode && $("#content").contains(sel.anchorNode);
      if (text.length < 3 || !inContent) { btn.classList.add("hidden"); return; }
      const el = (sel.anchorNode.nodeType === 3 ? sel.anchorNode.parentElement : sel.anchorNode)
        ?.closest("[data-a], [data-qa], [data-b-ext], .turn");
      if (!el) { btn.classList.add("hidden"); return; }
      btn.style.left = `${e.clientX + 8}px`;
      btn.style.top = `${e.clientY - 34}px`;
      btn.classList.remove("hidden");
      btn.onclick = () => {
        btn.classList.add("hidden");
        S.pendingQuote = text;
        const { anchor, obj } = anchorOfElement(el);
        setAnchor(anchor, obj, false);
        setITab("comments");
      };
    }, 10);
  });
}

function anchorOfElement(el) {
  const s = S.bundle.sessions.find((x) => x.session_id === S.session);
  if (el.dataset?.a) {
    const [kind, idx] = el.dataset.a.split(":");
    return { anchor: `session:${S.session}/${kind}:${idx}`, obj: kind === "mp" ? s.golden[+idx] : s.extracted[+idx] };
  }
  if (el.dataset?.qa != null) return { anchor: `session:${S.session}/qa:${el.dataset.qa}`, obj: s.questions[+el.dataset.qa] };
  if (el.dataset?.bExt != null) {
    const sb = S.bundleB?.sessions.find((x) => x.session_id === S.session);
    return { anchor: `session:${S.session}/extb:${el.dataset.bExt}`, obj: sb?.extracted[+el.dataset.bExt] };
  }
  if (el.classList?.contains("turn")) {
    const ti = +el.id.replace("turn-", "");
    return { anchor: `session:${S.session}/turn:${ti}`, obj: s.dialogue[ti] };
  }
  return { anchor: `session:${S.session}`, obj: s };
}

/* ---------- 탭/앵커 ---------- */

function setTab(t) { S.tab = t; $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t)); render(); }
function setITab(t) { S.itab = t; $$("#insp-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.itab === t)); renderInspector(); }
function setAnchor(anchor, obj, focusDetail = true) {
  S.anchor = anchor; S.anchorObj = obj;
  $$(".row.selected").forEach((el) => el.classList.remove("selected"));
  if (focusDetail && S.itab !== "comments") setITab("detail"); else renderInspector();
}

/* ---------- 배지 ---------- */

const scoreBadge = (v) => v == null ? `<span class="badge bnull" data-desc="judge 라벨 없음 — 이 judge 세트가 이 유저를 안 덮거나 무효 판정">–</span>` : `<span class="badge b${v}" data-desc="judge 점수 ${v} — 2=완전 포함(골든)/전부 근거(추출), 1=부분, 0=미포함/비지지·모순">${v}</span>`;
const verdictBadge = (v) => {
  if (!v) return `<span class="badge bnull">–</span>`;
  const c = v[0] === "C" ? "bC" : v[0] === "H" ? "bH" : "bO";
  return `<span class="badge ${c}" data-desc="판정: ${esc(v)}">${esc(v[0])}</span>`;
};
const opChip = (op) => op ? `<span class="op ${esc(op)}" data-desc="이 메모리를 만든 연산: ${esc(op)} (ADD=신규 추출 / UPDATE=기존 메모리 재작성본)">${esc(op)}</span>` : "";
// QA 상세 등 공간 여유 있는 곳은 풀네임 판정 배지
const verdictFull = (v) => v ? `<span class="badge ${v[0] === "C" ? "bC" : v[0] === "H" ? "bH" : "bO"}">${esc(v)}</span>` : `<span class="badge bnull">판정 없음</span>`;
// 미끼(interference) 골든은 포함 점수의 좋고 나쁨이 반전됨: 0=미포함=저항 성공(FMR 기여), 2=흡수=감점
const intfBadge = (v) => v == null ? `<span class="badge bnull" data-desc="judge 라벨 없음">–</span>`
  : v === 0 ? `<span class="badge b2" data-desc="미끼 차단 (포함 점수 0) — 시스템이 이 미끼를 흡수하지 않음 = 저항 성공, FMR에 긍정 기여">차단</span>`
  : v === 1 ? `<span class="badge b1" data-desc="미끼 일부 흡수 (포함 점수 1) — 미끼 내용 일부가 추출 메모리에 들어감">일부흡수</span>`
  : `<span class="badge b0" data-desc="미끼 흡수 (포함 점수 2) — 시스템이 미끼를 통째로 기억함 = FMR 감점">흡수</span>`;
const initials = (name) => esc((name || "?").trim().slice(0, 2));

/* ---------- 근사 턴 앵커링 ---------- */

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
  const gTurn = session.golden.map((mp) => place(mp.memory_content));
  const eTurn = session.extracted.map((m) => place(m.text));
  gTurn.forEach((t, i) => { if (t >= 0) map[t].g.push(i); });
  eTurn.forEach((t, i) => { if (t >= 0) map[t].e.push(i); });
  return { map, gTurn, eTurn };
}
function anchorsForB(sid) {
  if (!S.bundleB) return null;
  if (!S.anchorCacheB.has(sid)) {
    const sb = S.bundleB.sessions.find((x) => x.session_id === sid && !x.generated_qa_session);
    S.anchorCacheB.set(sid, sb ? { session: sb, ...anchorTurns(sb) } : null);
  }
  return S.anchorCacheB.get(sid);
}

/* ---------- 메인 렌더 ---------- */

function render() {
  if (!S.bundle) return;
  if (S.tab === "sessions") renderSessions();
  else if (S.tab === "qa") renderQA();
  else if (S.tab === "compare") renderCompare();
  else if (S.tab === "metrics") renderMetrics();
  else renderDigest();
  renderInspector();
}

function sessionSummary(s) {
  // 미끼(interference)는 점수 해석이 반전되므로 실패/포함 카운트에서 분리
  const reg = s.golden.filter((m) => m.memory_source !== "interference");
  const g2 = reg.filter((m) => m.judge?.score === 2 || m.judge?.label === "Correct").length;
  const g0 = reg.filter((m) => m.judge?.kind === "integrity" && m.judge?.score === 0).length;
  const gTot = reg.length;
  const a0 = s.extracted.filter((m) => m.judge?.score === 0).length;
  const qaBad = s.questions.filter((q) => q.judge && q.judge !== "Correct").length;
  return { g2, g0, gTot, a0, qaBad };
}

/* ----- Sessions ----- */

function renderSessions() {
  const sb = $("#sidebar");
  sb.innerHTML = S.bundle.sessions.map((s) => {
    if (s.generated_qa_session) return "";
    const m = sessionSummary(s);
    const flags = [
      m.g0 ? `<span class="badge b0" data-desc="빨간 원 — 이 세션에서 judge가 미포함(0점) 판정한 골든 수">${m.g0}</span>` : "",
      m.qaBad ? `<span class="badge bO" data-desc="보라 원 — 이 세션의 오답(H/O) QA 수">${m.qaBad}</span>` : "",
    ].join("");
    return `<div class="side-item ${s.session_id === S.session ? "active" : ""}" data-sid="${s.session_id}">
      <b>S${s.session_id}</b><span class="t">${esc((s.start_time || "").slice(0, 12))}</span>
      <span class="flags">${flags}</span></div>`;
  }).join("");
  $$(".side-item", sb).forEach((el) => (el.onclick = () => { S.session = +el.dataset.sid; renderSessions(); }));

  const s = S.bundle.sessions.find((x) => x.session_id === S.session);
  if (!s || s.generated_qa_session) { $("#content").innerHTML = "<p class='muted'>세션을 선택하세요</p>"; return; }
  const A = anchorTurns(s);
  const B = anchorsForB(s.session_id);

  // QA (워크플로상 최상단)
  const qas = s.questions.map((q, i) => {
    let aLabel = "", bBadge = "";
    if (B?.session) {
      aLabel = `<span class="small muted">A</span>`;
      const qb = B.session.questions?.find((x) => x.question === q.question);
      bBadge = `<span class="small" style="color:var(--bcol)">B</span>${verdictBadge(qb?.judge)}`;
    }
    return `<div class="row" data-qa="${i}" data-desc="클릭하면 이 QA의 4자 대조 화면으로 이동. 드래그로 텍스트 선택 후 코멘트도 가능">
      <span>${aLabel}${verdictBadge(q.judge)}</span>${bBadge}
      <span class="txt">${esc(q.question)}</span>
      <span class="small muted">${esc(q.question_type || "")}</span></div>`;
  }).join("");

  const dlg = s.dialogue.map((t, ti) => {
    const a = A.map[ti];
    const bMap = B?.map?.[ti];
    const chips = [
      ...a.g.map((gi) => {
        const mp = s.golden[gi];
        const upd = mp.is_update === "True", intf = mp.memory_source === "interference";
        const cls = upd ? " upd" : intf ? " intf" : "";
        const icon = upd ? "↻" : intf ? "⚠" : "";
        const desc = upd ? "갱신 골든 (근사 앵커·추정) — 과거 정보의 업데이트본. Update(C/H/O) 평가 대상"
          : intf ? "미끼(interference) 골든 (근사 앵커·추정) — AI 발화에만 있고 user가 확정 안 한 내용. 시스템이 흡수하면 감점(FMR)"
          : "골든 (근사 앵커·추정)";
        return `<span class="anchor-chip g${cls}" data-chip="mp:${gi}" data-desc="${desc}">G${icon} ${esc(mp.memory_content)}</span>`;
      }),
      ...a.e.map((ei) => `<span class="anchor-chip e" data-chip="ext:${ei}" data-desc="A 세팅 추출 (근사 앵커·추정)">A ${esc(s.extracted[ei].text)}</span>`),
      ...(bMap?.e || []).map((ei) => `<span class="anchor-chip eb" data-chip-b="${ei}" data-desc="B 세팅 추출 (근사 앵커·추정)">B ${esc(B.session.extracted[ei].text)}</span>`),
    ].join("");
    return `<div class="turn ${t.role}" id="turn-${ti}">
      <div class="main" data-turnx="${ti}" data-desc="클릭 = 이 턴 앵커 상세 펼침 + 코멘트 앵커 지정. 드래그 선택 후 코멘트도 가능">
        <div class="role">${esc(t.role)}<br><span class="small muted">#${esc(t.dialogue_turn)}</span></div>
        <div class="bubble">${esc(t.content)}</div>
        <div class="anchors">${chips}</div></div>
      <div class="turn-x hidden" id="turnx-${ti}"></div></div>`;
  }).join("");

  const goldenRows = s.golden.map((mp, i) => {
    const j = mp.judge || {};
    const upd = mp.is_update === "True", intf = mp.memory_source === "interference";
    const badge = j.kind === "update" ? verdictBadge(j.label) : intf ? intfBadge(j.score) : scoreBadge(j.score);
    return `<div class="row${upd ? " upd-row" : intf ? " intf-row" : ""}" data-a="mp:${i}" data-turn="${A.gTurn[i]}">
      <span>${badge}</span>
      ${upd ? '<span class="tagchip t-upd" data-k="is_update">↻ upd</span>' : ""}
      ${intf ? '<span class="tagchip t-intf" data-k="memory_source">⚠ 미끼</span>'
        : mp.memory_source !== "system" ? `<span class="tagchip" data-k="memory_source">${esc(mp.memory_source)}</span>` : ""}
      <span class="txt">${esc(mp.memory_content)}</span></div>`;
  }).join("");

  const extRows = s.extracted.map((m, i) => `
    <div class="row" data-a="ext:${i}" data-turn="${A.eTurn[i]}">
      <span>${scoreBadge(m.judge?.score)}</span>${opChip(m.origin)}
      <span class="txt">${esc(m.text)}</span></div>`).join("");

  let extBCard = "";
  if (B?.session) {
    const extBRows = B.session.extracted.map((m, i) => `
      <div class="row" data-b-ext="${i}">
        <span>${scoreBadge(m.judge?.score)}</span>${opChip(m.origin)}
        <span class="txt">${esc(m.text)}</span></div>`).join("");
    extBCard = `<div class="card b-card"><h4>추출 B: ${esc(S.runB)} (${B.session.extracted.length})</h4><div class="body">${extBRows}</div></div>`;
  } else if (S.runB) {
    extBCard = `<div class="card b-card"><h4>추출 B: ${esc(S.runB)}</h4><div class="body"><p class="small muted">이 유저/세션 데이터가 B 런에 없음</p></div></div>`;
  }

  $("#content").innerHTML = `
    <div class="hint">S${s.session_id} — QA부터 확인 → 대화 스크롤하며 골든/추출 대조. 행 클릭=우측 상세 · 드래그 선택=코멘트 · 턴 클릭=앵커 상세${S.bundleB ? ` · <span style="color:var(--bcol);font-weight:700">B=${esc(S.runB)}(핑크)</span>` : " · 상단 [+ 비교(B)]로 다른 세팅 비교"}</div>
    <div class="legend" data-desc="배지 범례 — 사이드바 원형 숫자: 빨강=미포함(0점) 골든 수, 보라=오답 QA 수">
      <b>범례</b>
      <span><span class="badge b2">2</span>완전</span><span><span class="badge b1">1</span>부분</span><span><span class="badge b0">0</span>실패</span>
      <span><span class="badge bC">C</span>/<span class="badge bH">H</span>/<span class="badge bO">O</span></span>
      <span class="anchor-chip g" style="cursor:default">G 골든(금)</span>
      <span class="anchor-chip g upd" style="cursor:default" data-desc="갱신 골든 — Update(C/H/O) 평가 대상">G↻ 갱신</span>
      <span class="anchor-chip g intf" style="cursor:default" data-desc="미끼 골든 — 흡수하면 감점(FMR)">G⚠ 미끼</span>
      <span class="anchor-chip e" style="cursor:default">A 추출(파랑)</span>
      ${S.bundleB ? '<span class="anchor-chip eb" style="cursor:default">B 추출(핑크)</span>' : ""}
      <span class="cmt-chip" style="cursor:default">가</span><span>= 코멘트 (호버/클릭)</span>
    </div>
    <div class="card"><h4 data-k="questions">QA (${s.questions.length})</h4><div class="body">${qas}</div></div>
    <div class="card"><h4 data-k="dialogue_turn">대화 (${s.dialogue.length}턴)
      <span class="small muted" style="margin-left:auto" data-desc="대화 오른쪽에 표시되는 골든/추출 메모리 칼럼의 너비 조절">메모리 표시 너비</span>
      <input type="range" id="anch-w" min="140" max="640" step="10" style="width:110px"></h4>
      <div class="body">${dlg}</div></div>
    <div class="${B?.session ? "three-col" : "two-col"}">
      <div class="card"><h4 data-k="memory_points" class="gold-h">골든 (${s.golden.length})</h4><div class="body">${goldenRows}</div></div>
      <div class="card"><h4 data-k="extracted_memories">추출 A: ${esc(S.run)} (${s.extracted.length})</h4><div class="body">${extRows}</div></div>
      ${extBCard}
    </div>`;

  // 칩 칼럼 너비 슬라이더 (localStorage 유지)
  const anchW = +localStorage.getItem("anchw") || 280;
  document.documentElement.style.setProperty("--anchw", `${anchW}px`);
  const slider = $("#anch-w");
  slider.value = anchW;
  slider.oninput = () => {
    document.documentElement.style.setProperty("--anchw", `${slider.value}px`);
    localStorage.setItem("anchw", slider.value);
  };
  slider.onclick = (ev) => ev.stopPropagation();

  $$("#content [data-a]").forEach((el) => {
    const [kind, idx] = el.dataset.a.split(":");
    el.onclick = () => {
      setAnchor(`session:${s.session_id}/${kind}:${idx}`, kind === "mp" ? s.golden[+idx] : s.extracted[+idx]);
      el.classList.add("selected");
    };
    const t = +el.dataset.turn;
    if (t >= 0) {
      el.onmouseenter = () => $(`#turn-${t}`)?.classList.add("hl");
      el.onmouseleave = () => $(`#turn-${t}`)?.classList.remove("hl");
    }
  });
  $$("#content [data-b-ext]").forEach((el) => {
    el.onclick = () => {
      const i = +el.dataset.bExt;
      setAnchor(`session:${s.session_id}/extb:${i}`, B.session.extracted[i]);
      el.classList.add("selected");
    };
  });
  $$("#content [data-qa]").forEach((el) => (el.onclick = () => renderQADetail(s.session_id, +el.dataset.qa, true)));
  // 칩 클릭: 우측 상세만 (자동 스크롤 없음 — 대화 읽던 위치 유지)
  $$("#content [data-chip]").forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const [kind, idx] = el.dataset.chip.split(":");
      const row = $(`#content [data-a="${kind}:${idx}"]`);
      row?.classList.add("selected");
      setAnchor(`session:${s.session_id}/${kind}:${idx}`, kind === "mp" ? s.golden[+idx] : s.extracted[+idx]);
    };
  });
  $$("#content [data-chip-b]").forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const i = +el.dataset.chipB;
      $(`#content [data-b-ext="${i}"]`)?.classList.add("selected");
      setAnchor(`session:${s.session_id}/extb:${i}`, B.session.extracted[i]);
    };
  });
  $$("#content [data-turnx]").forEach((el) => {
    el.onclick = () => {
      const ti = +el.dataset.turnx;
      setAnchor(`session:${s.session_id}/turn:${ti}`, s.dialogue[ti], false);
      toggleTurnPanel(s, A, B, ti);
    };
  });
  renderCmtMarks();
}

function toggleTurnPanel(s, A, B, ti) {
  const box = $(`#turnx-${ti}`);
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  const a = A.map[ti];
  const secA = `
    <h5>A: ${esc(S.run)} — 이 턴 앵커 (추정)</h5>
    ${a.g.map((gi) => { const mp = s.golden[gi]; const j = mp.judge || {};
      const badge = j.kind === "update" ? verdictBadge(j.label) : mp.memory_source === "interference" ? intfBadge(j.score) : scoreBadge(j.score);
      return `<div class="row"><span class="tagchip" style="color:var(--gold)">골든</span>${badge}<span class="txt">${esc(mp.memory_content)}</span></div>`; }).join("")}
    ${a.e.map((ei) => { const m = s.extracted[ei];
      return `<div class="row">${opChip(m.origin)}${scoreBadge(m.judge?.score)}<span class="txt">${esc(m.text)}</span></div>`; }).join("")}
    ${!a.g.length && !a.e.length ? "<p class='small muted'>앵커된 메모리 없음</p>" : ""}`;
  let secB = "";
  if (B?.session) {
    const bMap = B.map[ti] || { g: [], e: [] };
    secB = `<h5><span class="b-tag">B</span>: ${esc(S.runB)} — 이 턴 추출 (추정)</h5>
      ${bMap.e.map((ei) => { const m = B.session.extracted[ei];
        return `<div class="row">${opChip(m.origin)}${scoreBadge(m.judge?.score)}<span class="txt">${esc(m.text)}</span></div>`; }).join("") || "<p class='small muted'>앵커된 추출 없음</p>"}`;
  }
  box.innerHTML = secA + secB;
  box.classList.remove("hidden");
}

/* ----- 코멘트 마크 (노션풍 표시) ----- */

function anchorSelector(anchor) {
  const m = anchor.match(/^session:(\d+)(?:\/(\w+):(\d+))?$/);
  if (!m || +m[1] !== S.session) return null;
  if (!m[2]) return null;
  const kind = m[2], idx = m[3];
  if (kind === "mp" || kind === "ext") return `[data-a="${kind}:${idx}"]`;
  if (kind === "extb") return `[data-b-ext="${idx}"]`;
  if (kind === "qa") return `[data-qa="${idx}"]`;
  if (kind === "turn") return `#turn-${idx} .main`;
  return null;
}

function renderCmtMarks() {
  if (S.tab !== "sessions") return;
  $$("#content .cmt-chip.live").forEach((el) => el.remove());
  $$("#content .has-cmt").forEach((el) => el.classList.remove("has-cmt"));
  const byAnchor = {};
  S.comments.forEach((c) => (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c));
  Object.entries(byAnchor).forEach(([anchor, list]) => {
    const sel = anchorSelector(anchor);
    if (!sel) return;
    const el = $(`#content ${sel}`);
    if (!el) return;
    el.classList.add("has-cmt");
    const chip = document.createElement("span");
    chip.className = "cmt-chip live";
    chip.textContent = list.length > 1 ? `${initials(list[0].author)}+${list.length - 1}` : initials(list[0].author);
    chip.dataset.desc = `코멘트 ${list.length}개 — ${esc(list.map((c) => `${c.author}: ${c.body.slice(0, 40)}`).join(" | "))} (클릭하면 스레드)`;
    chip.onclick = (ev) => {
      ev.stopPropagation();
      const { obj } = resolveAnchorObj(anchor);
      setAnchor(anchor, obj, false);
      setITab("comments");
    };
    el.appendChild(chip);
  });
}

function resolveAnchorObj(anchor) {
  const m = anchor.match(/^session:(\d+)(?:\/(\w+):(\d+))?$/);
  const s = m ? S.bundle.sessions.find((x) => x.session_id === +m[1]) : null;
  if (!m || !s) return { obj: null };
  if (!m[2]) return { obj: s };
  const idx = +m[3];
  if (m[2] === "mp") return { obj: s.golden[idx] };
  if (m[2] === "ext") return { obj: s.extracted[idx] };
  if (m[2] === "qa") return { obj: s.questions[idx] };
  if (m[2] === "turn") return { obj: s.dialogue[idx] };
  if (m[2] === "extb") return { obj: S.bundleB?.sessions.find((x) => x.session_id === +m[1])?.extracted[idx] };
  return { obj: null };
}

function gotoAnchor(anchor) {
  const m = anchor.match(/^session:(\d+)/);
  if (!m) return;
  setTab("sessions");
  S.session = +m[1];
  renderSessions();
  const sel = anchorSelector(anchor);
  const el = sel ? $(`#content ${sel}`) : null;
  el?.scrollIntoView({ block: "center", behavior: "smooth" });
  el?.classList.add("flash");
  setTimeout(() => el?.classList.remove("flash"), 1500);
  const { obj } = resolveAnchorObj(anchor);
  setAnchor(anchor, obj, false);
  setITab("comments");
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
  const items = allQAs().filter(({ q }) => S.qaFilter === "all" || q.judge === S.qaFilter);
  $("#sidebar").innerHTML = `
    <div style="padding:8px"><div class="pill-filter">${filters.map((f) =>
      `<button class="${S.qaFilter === f ? "on" : ""}" data-f="${f}">${f === "all" ? "전체" : f[0]}</button>`).join("")}</div></div>` +
    items.map(({ s, q, i }) => `
      <div class="side-item" data-sid="${s.session_id}" data-qi="${i}">
        ${verdictBadge(q.judge)} <span class="txt small">${esc(q.question.slice(0, 60))}</span></div>`).join("");
  $$("#sidebar .pill-filter button").forEach((b) => (b.onclick = () => { S.qaFilter = b.dataset.f; renderQA(); }));
  $$("#sidebar .side-item").forEach((el) => (el.onclick = () => renderQADetail(+el.dataset.sid, +el.dataset.qi, false)));
  $("#content").innerHTML = `<p class="hint">좌측에서 QA를 선택하세요 (판정 필터: 전체/C/H/O). 총 ${allQAs().length}문항.</p>`;
}

function parseContext(raw) {
  if (Array.isArray(raw)) return raw.map((c) => (typeof c === "string" ? c : c.memory || c.text || JSON.stringify(c)));
  if (typeof raw !== "string") return null;
  const a = raw.indexOf("["), b = raw.lastIndexOf("]");
  if (a < 0 || b <= a) return null;
  try { const arr = JSON.parse(raw.slice(a, b + 1)); return Array.isArray(arr) ? arr : null; } catch { return null; }
}

function renderQADetail(sid, qi, fromSession) {
  const s = S.bundle.sessions.find((x) => x.session_id === sid);
  const q = s.questions[qi];
  const ev = (q.evidence || []).map((e) => `<div class="row"><span class="tagchip" data-k="evidence">근거</span><span class="txt">${esc(e.memory_content)}</span></div>`).join("");
  const items = parseContext(q.context);
  const listHTML = items
    ? items.map((c, i) => `<div class="row"><span class="small muted">#${i + 1}</span><span class="txt">${esc(c)}</span></div>`).join("")
    : null;
  const rawHTML = `<pre class="mono">${esc(typeof q.context === "string" ? q.context : JSON.stringify(q.context, null, 2))}</pre>`;

  let bCard = "";
  if (S.bundleB) {
    const sb = S.bundleB.sessions.find((x) => x.session_id === sid);
    const qb = sb?.questions?.find((x) => x.question === q.question);
    bCard = `<div class="card b-card"><h4>B: ${esc(S.runB)} 답변 ${verdictFull(qb?.judge)}</h4>
      <div class="body">${esc(qb?.system_response || "(B 런에 답변 없음)")}</div></div>`;
  }

  $("#content").innerHTML = `
    ${fromSession ? `<span class="backlink" id="btn-back">← 세션 S${sid}로 돌아가기</span>` : ""}
    <div class="hint">S${sid} · ${esc(q.question_type || "")} — 질문 → 정답 → ${S.bundleB ? "A/B 답변(판정)" : "답변(판정)"} → context</div>
    <div class="card"><h4 data-k="question">질문</h4><div class="body">${esc(q.question)}</div></div>
    <div class="card"><h4 data-k="answer">골든 정답</h4><div class="body">${esc(q.answer)}</div></div>
    <div class="card"><h4 data-k="system_response">${S.bundleB ? `A: ${esc(S.run)} 답변` : "시스템 답변 (A′)"} ${verdictFull(q.judge)}</h4><div class="body">${esc(q.system_response || "(A′ 미실행)")}</div></div>
    ${bCard}
    <div class="card"><h4 data-k="evidence">Evidence (${(q.evidence || []).length})</h4><div class="body">${ev}</div></div>
    <div class="card"><h4 data-k="context">검색 Context (${items ? items.length + "건" : "원문"})
      ${items ? '<button class="ctx-toggle" id="ctx-toggle">원문 보기</button>' : ""}</h4>
      <div class="body"><div id="ctx-list">${listHTML ?? rawHTML}</div><div id="ctx-raw" class="hidden">${rawHTML}</div></div></div>`;
  if (items) {
    $("#ctx-toggle").onclick = () => {
      const showRaw = $("#ctx-raw").classList.toggle("hidden");
      $("#ctx-list").classList.toggle("hidden", !showRaw);
      $("#ctx-toggle").textContent = showRaw ? "원문 보기" : "목록 보기";
    };
  }
  $("#btn-back") && ($("#btn-back").onclick = () => renderSessions());
  S.session = sid;
  setAnchor(`session:${sid}/qa:${qi}`, q);
}

/* ----- Compare ----- */

function renderCompare() {
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted">A: <b>${esc(S.run)}</b></p>
    <p class="small muted">B: <b>${esc(S.runB || "미선택")}</b></p>
    <p class="small">상단의 [+ 비교(B)]에서 B 세팅을 선택하면 여기서 전체 대조표가 나옵니다.</p></div>`;
  if (!S.bundleB) { $("#content").innerHTML = "<p class='hint'>상단 [+ 비교(B)]로 비교 세팅을 먼저 선택하세요.</p>"; return; }

  const agg = (bundle) => {
    const a = { g: 0, g2: 0, ext: 0, a0: 0, C: 0, H: 0, O: 0 };
    bundle.sessions.forEach((s) => {
      if (s.generated_qa_session) return;
      const m = sessionSummary(s);
      a.g += m.gTot; a.g2 += m.g2; a.ext += s.extracted.length; a.a0 += m.a0;
      s.questions.forEach((q) => { if (q.judge) a[q.judge[0]] = (a[q.judge[0]] || 0) + 1; });
    });
    return a;
  };
  const fmtAgg = (a) => `골든 포함 <b>${a.g2}/${a.g}</b> (${(a.g2 / Math.max(a.g, 1) * 100).toFixed(0)}%) ·
    추출 <b>${a.ext}</b>개 (acc0 ${a.a0}) · QA ${verdictBadge("Correct")}${a.C} ${verdictBadge("Hallucination")}${a.H} ${verdictBadge("Omission")}${a.O}`;
  const aA = agg(S.bundle), aB = agg(S.bundleB);

  const qas = allQAs().map(({ s, q, i }) => {
    const sb = S.bundleB.sessions.find((x) => x.session_id === s.session_id);
    const qb = sb?.questions?.find((x) => x.question === q.question);
    return { s, q, i, qb, diff: qb && q.judge !== qb.judge };
  });

  const qaRows = qas.map((r, ri) => `<tr class="qa-row" data-ri="${ri}" data-desc="클릭하면 A/B의 답변·정답·근거를 펼쳐 대조">
      <td>S${r.s.session_id}</td>
      <td>${esc(r.q.question.slice(0, 90))}</td>
      <td ${r.diff ? 'class="diff"' : ""}>${verdictBadge(r.q.judge)}</td>
      <td ${r.diff ? 'class="diff"' : ""}>${verdictBadge(r.qb?.judge)}</td></tr>`);

  $("#content").innerHTML = `
    <div class="hint">A = ${esc(S.run)} / B = ${esc(S.runB)} — 유저 ${esc(S.bundle.user_name)}. 주황 = 판정 갈림. QA 행 클릭 = 상세 대조.</div>
    <div class="card"><h4 data-desc="이 유저 전체에 대한 A/B 세팅 요약 — 골든 포함률(judge 2점 비율), 추출량, accuracy 0점 수, QA 판정 분포">요약 (유저 전체)</h4><div class="body">
      <div class="row" style="cursor:default"><span class="tagchip">A</span><span class="txt">${esc(S.run)}</span><span>${fmtAgg(aA)}</span></div>
      <div class="row" style="cursor:default"><span class="tagchip" style="color:var(--bcol)">B</span><span class="txt">${esc(S.runB)}</span><span>${fmtAgg(aB)}</span></div>
    </div></div>
    <div class="card"><h4 data-desc="같은 질문에 대한 A/B 판정 대조 — 주황 셀이 판정이 갈린 질문. 행 클릭 시 양쪽 답변·정답·근거 펼침">QA 판정 대조 (${qas.filter((r) => r.diff).length}건 갈림 / ${qas.length})</h4><div class="body">
      <table class="cmp" id="cmp-qa"><tr><th>세션</th><th>질문</th><th>A</th><th>B</th></tr>${qaRows.join("")}</table></div></div>`;

  $$("#cmp-qa .qa-row").forEach((tr) => {
    tr.onclick = () => {
      const open = tr.nextElementSibling?.classList.contains("qa-x");
      $$("#cmp-qa .qa-x").forEach((x) => x.remove());
      if (open) return;
      const r = qas[+tr.dataset.ri];
      const x = document.createElement("tr");
      x.className = "qa-x";
      x.innerHTML = `<td colspan="4">
        <p class="small"><b>골든 정답</b> — ${esc(r.q.answer)}</p>
        <p class="small"><b>A 답변</b> ${verdictFull(r.q.judge)}</p><pre class="mono">${esc(r.q.system_response || "(없음)")}</pre>
        <p class="small"><b>B 답변</b> ${verdictFull(r.qb?.judge)}</p><pre class="mono">${esc(r.qb?.system_response || "(B 런에 없음)")}</pre>
        <p class="small"><b>근거 골든</b> — ${(r.q.evidence || []).map((e) => esc(e.memory_content)).join(" · ") || "없음"}</p></td>`;
      tr.after(x);
    };
  });
}

/* ----- Metrics (전체 실험 지표 테이블) ----- */

const METRIC_DEFS = {
  r: "Recall↑ — 골든 메모리 포인트(interference 제외) 중 judge가 '완전 포함(2점)'으로 판정한 비율. 추출 커버리지의 핵심 지표",
  wr: "Weighted Recall↑ — 부분 포함(1점)에 0.5 가중치를 준 포함률. R과의 격차가 크면 '부분만 건진' 골든이 많다는 뜻",
  acc: "Accuracy↑ — 추출 메모리가 당해 세션 대화·골든에 근거하는 정도(2/1/0 가중평균). 괄호=채점된 추출 메모리 수. ⚠ 교차 세션 내용을 담은 UPDATE 재작성본은 구조적으로 불리",
  tp: "Target Precision↑ — 필드(주제) 단위로 골든과 대응한다고 판정된 추출 메모리만 모수(괄호)로 한 accuracy 가중평균",
  fmr: "FMR↑ — 미끼(interference: AI 발화에만 있고 user가 확정 안 한 내용) 골든 중 시스템이 흡수하지 않은 비율. 높을수록 distractor 저항이 강함",
  f1: "F1↑ — R과 Target P의 조화평균. R≪P 레짐에선 사실상 R이 지배 (F1 개선 ≡ R 개선)",
  upd_c: "Update Correct↑ — 갱신 요구 골든에 대해 갱신본의 모든 원자 정보·수치가 정확한 비율",
  upd_h: "Update Hallucination↓ — 갱신 중 틀린 값을 만들어낸 비율",
  upd_o: "Update Omission↓ — 갱신 중 디테일을 누락한 비율 (Qwen 계열의 주 실패 모드)",
  qa_c: "QA Correct↑ — 다요소 질문에 대한 답변이 완전 정답인 비율",
  qa_h: "QA Hallucination↓ — 답변이 날조인 비율",
  qa_o: "QA Omission↓ — 답변이 누락·회피인 비율",
};
const METRIC_COLS = [
  { k: "r", label: "R↑", dir: 1 }, { k: "wr", label: "Weighted R↑", dir: 1 },
  { k: "acc", label: "Acc.↑ (#mem)", dir: 1, n: "acc_n" }, { k: "tp", label: "Target P↑ (#mem)", dir: 1, n: "tp_n" },
  { k: "fmr", label: "FMR↑", dir: 1 }, { k: "f1", label: "F1↑", dir: 1 },
  { k: "upd_c", label: "Upd C↑", dir: 1 }, { k: "upd_h", label: "Upd H↓", dir: -1 }, { k: "upd_o", label: "Upd O↓", dir: -1 },
  { k: "qa_c", label: "QA C↑", dir: 1 }, { k: "qa_h", label: "QA H↓", dir: -1 }, { k: "qa_o", label: "QA O↓", dir: -1 },
];
const metricsCache = new Map();
S.metricsJudge = "nano"; S.metricsScope = "first4";

async function renderMetrics() {
  const key = `${S.metricsJudge}|${S.metricsScope}`;
  if (!metricsCache.has(key)) {
    busy(true, "지표 집계 중 (공식 집계 함수)…");
    try { metricsCache.set(key, await api(`/api/metrics?judge=${S.metricsJudge}&scope=${S.metricsScope}`)); }
    finally { busy(false); }
  }
  const data = metricsCache.get(key);

  // 스코프 옵션: 공통 4유저 + 개별 유저(이름은 현재 유저 목록에서 매핑)
  const nameOf = (uid) => S.users.find((u) => u.uuid === uid)?.user_name || uid.slice(0, 8);
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted"><b>Judge LLM</b></p>
    <div class="pill-filter" style="margin:0 0 10px">
      <button class="${S.metricsJudge === "nano" ? "on" : ""}" data-j="nano" data-desc="GPT-5-Nano judge — 신뢰 기준 라벨, 첫 4유저">nano</button>
      <button class="${S.metricsJudge === "qwen4b" ? "on" : ""}" data-j="qwen4b" data-desc="Qwen3-4B judge — 20유저 전체를 덮지만 판정 왜곡 있음 (일부 런만 보유)">qwen4b</button></div>
    <p class="small muted"><b>유저 범위</b></p>
    <select id="metrics-scope" style="width:100%">
      <option value="first4" ${S.metricsScope === "first4" ? "selected" : ""}>첫 4유저 (전 실험 공통)</option>
      <option value="all" ${S.metricsScope === "all" ? "selected" : ""}>런별 전체 유저</option>
      ${data.first4.map((u) => `<option value="${u}" ${S.metricsScope === u ? "selected" : ""}>${esc(nameOf(u))}</option>`).join("")}
    </select>
    <p class="small muted" style="margin-top:10px">굵은 값 = 열별 최고(방향 반영). 셀 호버 = 순위·해석, 행 첫 칸 호버 = 런 요약 노트.</p></div>`;
  $$("#sidebar .pill-filter button").forEach((b) => (b.onclick = () => { S.metricsJudge = b.dataset.j; renderMetrics(); }));
  $("#metrics-scope").onchange = () => { S.metricsScope = $("#metrics-scope").value; renderMetrics(); };

  const rows = data.rows.filter((r) => r.metrics);
  // 열별 최고/순위 (방향 반영)
  const rank = {};
  METRIC_COLS.forEach((c) => {
    const vals = rows.map((r) => r.metrics[c.k]);
    const sorted = [...vals].sort((a, b) => c.dir === 1 ? b - a : a - b);
    rank[c.k] = { sorted, best: sorted[0] };
  });
  const judgeName = data.judge === "nano" ? "GPT-5-Nano" : "Qwen3-4B";

  const body = rows.map((r) => {
    const m = r.metrics;
    const sysName = `Mem0-Classic-OSS${r.prompt === "custom" ? " +custom" : ""}`;
    const cells = METRIC_COLS.map((c) => {
      const v = m[c.k];
      const rk = rank[c.k].sorted.indexOf(v) + 1;
      const isBest = v === rank[c.k].best;
      const bestRow = rows.find((x) => x.metrics[c.k] === rank[c.k].best);
      const nTxt = c.n ? ` <span class="small muted">(${m[c.n].toLocaleString()})</span>` : "";
      const desc = `<b>${esc(c.label)}</b> = ${v} — ${rk}위/${rows.length} (${c.dir === 1 ? "높을수록" : "낮을수록"} 좋음 · 최고 ${rank[c.k].best}: ${esc(bestRow.label)})<br>${esc(METRIC_DEFS[c.k])}`;
      return `<td data-desc="${esc(desc)}" ${isBest ? 'style="font-weight:800"' : ""}>${v.toFixed(2)}${nTxt}</td>`;
    }).join("");
    return `<tr>
      <td data-desc="${esc(r.note || r.label)}"><b>${esc(sysName)}</b></td>
      <td>${m.n_users}</td>
      <td>${esc(r.backbone)}<br><span class="small muted">${esc(r.prompt)} prompt</span></td>
      <td>${esc(judgeName)}</td>${cells}</tr>`;
  }).join("");

  $("#content").innerHTML = `
    <div class="hint">HaluMem Table 3 지표 — judge 레코드에서 <b>공식 집계 함수로 실시간 산출</b> (문서 테이블과 동일 수치). 범위: ${S.metricsScope === "first4" ? "전 실험 공통 첫 4유저" : S.metricsScope === "all" ? "런별 전체 유저 (유저 수 다름 주의)" : "유저 " + esc(nameOf(S.metricsScope)) + " 1명"} · judge=${judgeName}</div>
    <div class="card"><div class="body" style="overflow-x:auto">
      <table class="cmp"><tr>
        <th data-desc="메모리 시스템 — 전부 mem0 OSS 0.1.118 classic. +custom = HaluMem 원본 추출 프롬프트 이식">Memory System</th>
        <th data-desc="이 행의 지표가 집계된 유저 수">#Users</th>
        <th data-desc="memory agent 백본 (fact 추출·update 결정 LLM) × 추출 프롬프트">Agent LLM</th>
        <th data-desc="채점 LLM — 행 간 비교는 동일 judge에서만 유효">Judge LLM</th>
        ${METRIC_COLS.map((c) => `<th data-desc="${esc(METRIC_DEFS[c.k])}">${esc(c.label)}</th>`).join("")}
      </tr>${body}</table></div></div>
    ${rows.length < data.rows.length ? `<p class="hint">⚠ ${data.rows.length - rows.length}개 런은 이 judge(${judgeName}) 라벨이 없어 표시 제외</p>` : ""}`;
}

/* ----- Digest ----- */

async function renderDigest() {
  const all = await api(`/api/digest/${S.uuid}`);
  const scoped = S.digestScope === "session"
    ? all.filter((c) => c.run === S.run && (c.anchor === `session:${S.session}` || c.anchor.startsWith(`session:${S.session}/`)))
    : all;
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted">유저 <b>${esc(S.bundle.user_name)}</b> 코멘트</p>
    <div class="pill-filter" style="margin:0 0 8px">
      <button class="${S.digestScope === "user" ? "on" : ""}" data-sc="user">유저 전체</button>
      <button class="${S.digestScope === "session" ? "on" : ""}" data-sc="session">현재 세션 S${S.session}</button></div>
    <a href="/api/export/${S.uuid}" target="_blank">📄 Markdown export</a></div>`;
  $$("#sidebar .pill-filter button").forEach((b) => (b.onclick = () => { S.digestScope = b.dataset.sc; renderDigest(); }));
  if (!scoped.length) {
    $("#content").innerHTML = `<div class="card"><h4>Digest — 코멘트 모아보기</h4><div class="body">
      <p>선택 범위(${S.digestScope === "user" ? "유저 전체" : `세션 S${S.session}`})에 코멘트가 없습니다.</p>
      <p class="small muted">코멘트 남기기: Sessions/QA 탭에서 항목 클릭 → 우측 코멘트 탭. 또는 텍스트 드래그 선택 → 💬 버튼.<br>
      세션/유저 단위 종합 코멘트는 항목 클릭 없이 우측 코멘트 탭에서 바로 작성하면 됩니다 (앵커 run/session).</p></div></div>`;
    return;
  }
  const byRun = {};
  scoped.forEach((c) => (byRun[c.run] = byRun[c.run] || []).push(c));
  $("#content").innerHTML = Object.entries(byRun).map(([run, list]) => `
    <div class="card"><h4>run: ${esc(run)} (${list.length})</h4><div class="body">
      ${list.map((c) => cmtHTML(c, run === S.run)).join("")}</div></div>`).join("");
  $$("#content .del").forEach((b) => (b.onclick = async () => {
    await api(`/api/comments/${b.dataset.id}?author=${encodeURIComponent(S.author)}`, { method: "DELETE" });
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
    renderDigest();
  }));
  $$("#content .goto").forEach((b) => (b.onclick = () => gotoAnchor(b.dataset.anchor)));
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
  const k = key !== null ? `<span class="k" data-k="${esc(key)}">${esc(key)}</span>: ` : "";
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

function traceSummary(r) {
  if (r.event === "llm_call") {
    let facts = "";
    try { const j = JSON.parse(r.llm.response); if (j.facts) facts = `fact ${j.facts.length}개`; else if (j.memory) facts = `결정 ${j.memory.length}건`; } catch {}
    return facts || (r.llm?.response || "").slice(0, 60);
  }
  if (r.event === "retrieval") return `"${(r.retrieval?.query || "").slice(0, 40)}" → hit ${r.retrieval?.hits?.length ?? 0}`;
  if (r.event === "memory_write") {
    const ops = {};
    (r.writes || []).forEach((w) => (ops[w.op] = (ops[w.op] || 0) + 1));
    return Object.entries(ops).map(([o, n]) => `${o} ${n}`).join(" · ") || "0건";
  }
  return "";
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
  el.innerHTML = `<div class="anchor-label">S${S.session} trace — ${recs.length}건 (시간순) · 클릭하면 펼침 (한 번에 하나)</div>` +
    recs.map((r, i) => `<div class="tr-rec" data-i="${i}">
      <div class="hdr" data-k="${esc(r.event)}">
        <span class="small muted">#${r.seq}</span>
        <b>${esc(r.event === "llm_call" ? r.purpose : r.event)}</b>
        <span class="tagchip">${esc(r.stage || "")}</span>
        <span class="summ">${esc(traceSummary(r))}</span>
        <span class="small muted">${Math.round(r.duration_ms || 0)}ms</span></div>
      <div class="x hidden"></div></div>`).join("");
  $$(".tr-rec", el).forEach((div) => {
    const hdr = $(".hdr", div), x = $(".x", div);
    hdr.onclick = () => {
      const wasOpen = !x.classList.contains("hidden");
      $$(".tr-rec .x", el).forEach((o) => { o.classList.add("hidden"); o.innerHTML = ""; });
      clearTraceHighlight();
      if (wasOpen) return;
      const r = recs[+div.dataset.i];
      x.innerHTML = traceDetail(r);
      x.classList.remove("hidden");
      highlightTraceTarget(r);
    };
  });
}

function clearTraceHighlight() { $$(".hl-tr").forEach((el) => el.classList.remove("hl-tr")); }

function highlightTraceTarget(r) {
  if (S.tab !== "sessions") return;
  const s = S.bundle.sessions.find((x) => x.session_id === S.session);
  if (!s) return;
  const targets = [];
  const findRow = (kind, pred, list) => {
    list.forEach((item, i) => { if (pred(item)) targets.push(`[data-a="${kind}:${i}"]`); });
  };
  if (r.event === "retrieval" && r.stage === "ingest" && r.retrieval?.query) {
    findRow("ext", (m) => m.text === r.retrieval.query, s.extracted);
  } else if (r.event === "retrieval" && r.stage === "qa_retrieval" && r.ref?.question) {
    s.questions.forEach((q, i) => { if (q.question === r.ref.question) targets.push(`[data-qa="${i}"]`); });
  } else if (r.event === "retrieval" && r.stage === "update_probe" && r.ref) {
    const refText = Object.values(r.ref)[0];
    findRow("mp", (mp) => mp.memory_content === refText, s.golden);
  } else if (r.event === "memory_write") {
    (r.writes || []).forEach((w) => findRow("ext", (m) => m.text === w.text, s.extracted));
  }
  let first = null;
  targets.forEach((sel) => {
    const el = $(`#content ${sel}`);
    if (!el) return;
    el.classList.add("hl-tr");
    first = first || el;
    const t = +el.dataset.turn;
    if (t >= 0) $(`#turn-${t}`)?.classList.add("hl-tr");
  });
  first?.scrollIntoView({ block: "center", behavior: "smooth" });
}

function traceDetail(r) {
  if (r.event === "llm_call" && r.llm) {
    return r.llm.messages.map((m) => `<p class="small"><b>${esc(m.role)}</b></p><pre class="mono">${esc(m.content)}</pre>`).join("") +
      `<p class="small"><b>response</b></p><pre class="mono">${esc(r.llm.response)}</pre>`;
  }
  if (r.event === "retrieval" && r.retrieval) {
    const hits = (r.retrieval.hits || []).map((h) => {
      const score = h.score != null ? h.score.toFixed(3) : "–";
      const text = h.text || h.memory || h.payload?.data || JSON.stringify(h).slice(0, 120);
      return `<div class="hit"><span class="sc">${score}</span><span>${esc(text)}</span></div>`;
    }).join("");
    return `<p class="small"><b>query</b> (${esc(r.retrieval.method)}, limit ${r.retrieval.limit})${r.ref ? ` · ref: ${esc(JSON.stringify(r.ref))}` : ""}</p>
      <pre class="mono">${esc(r.retrieval.query)}</pre>
      <p class="small"><b>hits ${(r.retrieval.hits || []).length}</b></p>${hits || "<p class='small muted'>0건</p>"}`;
  }
  if (r.event === "memory_write") {
    return (r.writes || []).map((w) => `<div class="hit"><span class="sc">${esc(w.op)}</span>
      <span>${esc(w.text)}${w.prev_text ? `<br><span class="small muted">← 이전: ${esc(w.prev_text)}</span>` : ""}</span></div>`).join("") || "0건";
  }
  return `<pre class="mono">${esc(JSON.stringify(r, null, 2))}</pre>`;
}

/* ---------- 코멘트 ---------- */

function cmtHTML(c, canGoto = false) {
  const mine = c.author === S.author;
  return `<div class="cmt"><div class="meta"><span class="author">${esc(c.author)}</span>
    ${c.tag ? `<span class="tagchip">${esc(c.tag)}</span>` : ""}
    <span>${esc(c.anchor)}</span><span>${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</span>
    ${canGoto && c.anchor.startsWith("session:") ? `<button class="del goto" style="color:var(--accent)" data-anchor="${esc(c.anchor)}">이동</button>` : ""}
    ${mine ? `<button class="del" data-id="${c.id}">삭제</button>` : ""}</div>
    ${c.quote ? `<div class="cmt-quote">“${esc(c.quote)}”</div>` : ""}
    <div class="body-text">${esc(c.body)}</div></div>`;
}

function renderComments(el) {
  const here = S.comments.filter((c) => c.anchor === S.anchor);
  const others = S.comments.filter((c) => c.anchor !== S.anchor);
  el.innerHTML = `
    <div class="anchor-label">앵커: ${esc(S.anchor)}</div>
    <div id="cmt-form" style="margin:8px 0">
      ${S.pendingQuote ? `<div class="cmt-quote">“${esc(S.pendingQuote)}” <button id="quote-clear" style="border:none;background:none;cursor:pointer;color:var(--bad)">×</button></div>` : ""}
      <textarea id="cmt-body" placeholder="관찰/해석을 남겨주세요 (관찰 → 유형 태그 → 시사점)"></textarea>
      <div style="display:flex;gap:6px;margin-top:4px">
        <select id="cmt-tag"><option value="">태그 없음</option><option>강점</option><option>약점</option><option>병목</option><option>judge오판</option><option>추출누락</option><option>재작성drift</option><option>기타</option></select>
        <button class="primary" id="cmt-add">등록</button></div></div>
    <h4 class="small muted">이 앵커 (${here.length})</h4>${here.map((c) => cmtHTML(c)).join("") || "<p class='small muted'>없음</p>"}
    <h4 class="small muted">이 유저의 다른 앵커 (${others.length})</h4>${others.map((c) => cmtHTML(c, true)).join("")}`;
  $("#quote-clear") && ($("#quote-clear").onclick = () => { S.pendingQuote = ""; renderComments(el); });
  $("#cmt-add").onclick = async () => {
    const body = $("#cmt-body").value.trim();
    if (!body) return;
    if (!S.author) { askName(); return; }
    await api("/api/comments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: S.run, uuid: S.uuid, anchor: S.anchor, author: S.author, tag: $("#cmt-tag").value, body, quote: S.pendingQuote }),
    });
    S.pendingQuote = "";
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
    renderInspector();
    renderCmtMarks();
  };
  $$(".del:not(.goto)", el).forEach((b) => (b.onclick = async () => {
    await api(`/api/comments/${b.dataset.id}?author=${encodeURIComponent(S.author)}`, { method: "DELETE" });
    S.comments = await api(`/api/comments/${S.run}/${S.uuid}`);
    renderInspector();
    renderCmtMarks();
  }));
  $$(".goto", el).forEach((b) => (b.onclick = () => gotoAnchor(b.dataset.anchor)));
}

boot().catch((e) => { document.body.innerHTML = `<pre style="padding:20px;color:#c92a2a">${esc(e.stack || e.message)}</pre>`; });
