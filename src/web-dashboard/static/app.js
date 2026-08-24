/* mem0×HaluMem 정성분석 SPA: 빌드 스텝 없는 vanilla JS */
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
  generator: "oss120",  // A' 레인 기본값 (없는 런은 mini -> qwen4b 폴백)
  backbone: null, prompt: null, backboneB: null, promptB: null, runB: null,
  bundle: null, bundleB: null, bundleCache: new Map(),
  tab: "sessions", itab: "detail",
  session: null, qaFilter: "all", digestScope: "user",
  anchor: "run", anchorObj: null, pendingQuote: "",
  comments: [], fielddict: {}, traceCache: new Map(), anchorCacheB: new Map(),
  author: localStorage.getItem("analyst_name") || "",
  showOtherCmts: localStorage.getItem("show_other_cmts") === "1",  // 다른 세팅 코멘트 표시 여부
  traceSide: "A",  // Trace 탭이 보는 세팅 (B 비교 중에만 의미)
};

/* ---------- 코멘트 세팅 스코프 ----------
   기본은 '지금 보고 있는 세팅에서 단 코멘트'만 노출: generator·judge가 같아야 하고,
   B측(extb) 앵커는 당시 B 런까지 현재 B 런과 같아야 함.
   세팅 미기록(구버전) 코멘트는 불일치로 분류돼 토글을 켜야 보임. */
function cmtMatches(c) {
  if ((c.generator || "") !== S.generator || (c.judge || "") !== S.judge) return false;
  if (c.anchor.includes("/extb:")) return (c.run_b || "") === (S.runB || "");
  return true;
}
function visibleComments() { return S.showOtherCmts ? S.comments : S.comments.filter(cmtMatches); }
function setShowOtherCmts(v) {
  S.showOtherCmts = v;
  localStorage.setItem("show_other_cmts", v ? "1" : "0");
  renderCmtMarks();
  renderInspector();
  if (S.tab === "digest") renderDigest();
}

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
    // runs.yaml의 사람용 judge 이름으로 기본 표를 보강한다. 새 레인을 추가할 때
    // app.js를 고치지 않아도 내부 키(ctxmatch-4u 등)가 화면에 노출되지 않는다
    try { Object.assign(JUDGE_NAMES, (await api("/api/labels")).judge_names || {}); } catch (e) {}
  } finally { busy(false); }

  const backbones = [...new Set(S.runs.map((r) => r.backbone))];
  // reasoning 백본은 effort 병기 (예: "gpt-5-nano · effort=default")
  const bbLabel = (b) => {
    const e = S.runs.find((r) => r.backbone === b)?.backbone_effort;
    return e ? `${b} · ${effortShort(e)}` : b;
  };
  S.bbLabel = bbLabel;
  $("#sel-backbone").innerHTML = backbones.map((b) => `<option value="${esc(b)}">${esc(bbLabel(b))}</option>`).join("");
  $("#sel-backbone").onchange = () => { S.backbone = $("#sel-backbone").value; syncPrompts("A"); applyRun(); };
  $("#sel-prompt").onchange = () => { S.prompt = $("#sel-prompt").value; applyRun(); };
  $("#sel-judge").onchange = () => { S.judge = $("#sel-judge").value; loadBundle(); };
  $("#sel-generator").onchange = () => { S.generator = $("#sel-generator").value; applyRun(); };
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

function laneOf(meta) { return meta.generators?.[S.generator]; }

async function applyRun() {
  const meta = resolveRun(S.backbone, S.prompt);
  if (!meta) return;
  S.run = meta.run;
  $("#emb-name").textContent = meta.embedder || "–";

  // Generator 레인: available + judge 보유 레인만 노출. 기본 oss120, 선택 유지, 없으면 mini -> qwen4b 폴백
  const lanes = Object.entries(meta.generators || {}).filter(([, g]) => g.available && g.judges.length);
  const defGen = lanes.some(([n]) => n === S.generator) ? S.generator
    : lanes.some(([n]) => n === "oss120") ? "oss120"
    : lanes.some(([n]) => n === "mini") ? "mini"
    : lanes.some(([n]) => n === "qwen4b") ? "qwen4b" : lanes[0]?.[0];
  $("#sel-generator").innerHTML = lanes.map(([n, g]) =>
    `<option value="${esc(n)}" ${n === defGen ? "selected" : ""}>${esc(g.label)}</option>`).join("");
  S.generator = $("#sel-generator").value;
  const lane = laneOf(meta);

  // judge: 선택한 레인의 judge만. 선택 유지, 없으면 oss120 -> nano 우선. 표시는 공식 모델명, 값은 내부 키
  const defJudge = lane.judges.includes(S.judge) ? S.judge
    : lane.judges.includes("oss120-genoss120") ? "oss120-genoss120"
    : lane.judges.includes("nano") ? "nano" : lane.judges[0];
  $("#sel-judge").innerHTML = lane.judges.map((j) =>
    `<option value="${esc(j)}" ${j === defJudge ? "selected" : ""}>${esc(judgeLabel(j))}</option>`).join("");
  S.judge = $("#sel-judge").value;

  busy(true, "유저 목록…");
  try { S.users = await api(`/api/runs/${S.run}/users?generator=${S.generator}`); } finally { busy(false); }
  const prev = S.uuid;
  // ★ = nano judge 라벨 보유 유저 (데이터셋 첫 4명): 맨 위로 정렬해 기본 선택도 ★에서 시작
  const isF4 = (u) => (S.first4 || []).includes(u.uuid);
  const ordered = [...S.users].sort((a, b) => isF4(b) - isF4(a));
  $("#sel-user").innerHTML = ordered.map((u) =>
    `<option value="${u.uuid}" ${u.uuid === prev ? "selected" : ""}>${isF4(u) ? "★ " : ""}${esc(u.user_name)}</option>`).join("");
  S.uuid = $("#sel-user").value;
  await loadBundle();
}

function initB() {
  const backbones = [...new Set(S.runs.map((r) => r.backbone))];
  $("#sel-backbone-b").innerHTML = backbones.map((b) => `<option value="${esc(b)}">${esc(S.bbLabel(b))}</option>`).join("");
  S.backboneB = S.backbone;
  $("#sel-backbone-b").value = S.backboneB;
  syncPrompts("B");
  const other = [...new Set(runsFor(S.backboneB).map((r) => r.prompt))].find((p) => p !== S.prompt);
  if (other) { $("#sel-prompt-b").value = other; S.promptB = other; }
  applyRunB();
}

async function fetchBundle(run, generator, judge, uuid) {
  const key = `${run}|${generator}|${judge}|${uuid}`;
  if (!S.bundleCache.has(key)) {
    S.bundleCache.set(key, await api(`/api/bundle/${run}/${uuid}?judge=${judge}&generator=${generator}`));
  }
  return S.bundleCache.get(key);
}

async function applyRunB() {
  const meta = resolveRun(S.backboneB, S.promptB);
  const laneB = meta ? laneOf(meta) : null;
  if (!meta || !laneB?.available || !laneB.judges.length) { S.runB = null; S.bundleB = null; render(); return; }
  S.runB = meta.run;
  const judgeB = laneB.judges.includes(S.judge) ? S.judge
    : laneB.judges.includes("oss120-genoss120") ? "oss120-genoss120"
    : laneB.judges.includes("nano") ? "nano" : laneB.judges[0];
  busy(true, `B 세팅 로딩 (${S.runB})…`);
  try { S.bundleB = await fetchBundle(S.runB, S.generator, judgeB, S.uuid); }
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
    S.bundle = await fetchBundle(S.run, S.generator, S.judge, S.uuid);
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
      tip.innerHTML = d ? `<b>${esc(k)}</b>: ${esc(d)}` : `<b>${esc(k)}</b>`;
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
  // B 요소를 짚으면 Trace 탭도 B 세팅으로 따라감 (pill로 수동 전환 가능)
  if (S.runB) S.traceSide = anchor.includes("/extb:") ? "B" : "A";
  $$(".row.selected").forEach((el) => el.classList.remove("selected"));
  if (focusDetail && S.itab !== "comments") setITab("detail"); else renderInspector();
}

/* ---------- 배지 ---------- */

const scoreBadge = (v) => v == null ? `<span class="badge bnull" data-desc="judge 라벨 없음. 이 judge 세트가 이 유저를 안 덮거나 무효 판정">–</span>` : `<span class="badge b${v}" data-desc="judge 점수 ${v}: 2=완전 포함(골든)/전부 근거(추출), 1=부분, 0=미포함/비지지·모순">${v}</span>`;
const verdictBadge = (v) => {
  if (!v) return `<span class="badge bnull">–</span>`;
  const c = v[0] === "C" ? "bC" : v[0] === "H" ? "bH" : "bO";
  return `<span class="badge ${c}" data-desc="판정: ${esc(v)}">${esc(v[0])}</span>`;
};
const opChip = (op) => op ? `<span class="op ${esc(op)}" data-desc="이 메모리를 만든 연산: ${esc(op)} (ADD=신규 추출 / UPDATE=기존 메모리 재작성본)">${esc(op)}</span>` : "";
// 무효 UPDATE(no-op): 갱신 지시를 받고 이전 메모리와 완전히 동일한 텍스트를 반환한 경우.
// non-reasoning Qwen 계열의 지배적 실패 모드 (4B 97% / 30B 97% vs Thinking 0.6% / Mini 0.1% / oss120 0%)
const isNoop = (m) => m?.origin === "UPDATE" && (m.previous_memory || "").trim() === (m.text || "").trim() && (m.text || "").trim() !== "";
const NOOP_DESC = "무효 UPDATE (no-op): 에이전트가 갱신을 지시받고 <b>이전 메모리와 글자까지 동일한 텍스트</b>를 반환했습니다. 내용이 바뀌지 않아 낡은 정보가 그대로 남고, 이 세션의 추출 목록에 재등록돼 accuracy 채점에서 불리해집니다. 백본별 no-op 비율: Qwen3-4B 97% · Qwen3-30B 97% · GPT-5-Nano 14% · 4B-Thinking 0.6% · GPT-5-Mini 0.1% · gpt-oss-120b 0%";
const memOps = (m) => opChip(m.origin) + (isNoop(m) ? `<span class="op noop" data-desc="${NOOP_DESC}">= 무변경</span>` : "");
// reasoning effort 축약 표시: "default(medium): …" -> "effort=default", "항상 사고 (…)" -> "항상 사고"
const effortShort = (e) => {
  const s = e.split("(")[0].split(":")[0].split(". ")[0].trim();
  return /^[a-z]/.test(s) ? `effort=${s}` : s;
};

// judge 내부 키 -> 공식 모델명 (effort 병기). 내부 키는 경로 해석용으로만 쓰고 UI엔 이 이름만 노출
const JUDGE_NAMES = {
  nano: "GPT-5-Nano (minimal)",
  qwen4b: "Qwen3-4B",
  "mini-genmini": "GPT-5-Mini (minimal)",
  // ⚠ 같은 모델이라도 채점 배치가 다르면 라벨이 다르다 (동일 입력 재채점 불일치 10~27% 실측).
  //    이름이 겹치면 "상세는 1인데 검토는 0" 같은 혼선이 생기므로 배치를 반드시 병기한다.
  "oss120-genoss120": "gpt-oss-120b (high · 1유저 배치)",
  "oss120-genoss120-4u": "gpt-oss-120b (high · 4유저 배치)",
  // 동일 입력 반복 채점: 항목별로 나란히 보면 judge가 어디서 흔들리는지 바로 드러난다
  "oss120-rep1": "gpt-oss-120b (high · 반복 1)",
  "oss120-rep2": "gpt-oss-120b (high · 반복 2)",
  "oss120-rep3": "gpt-oss-120b (high · 반복 3)",
  "oracle-oss120": "gpt-oss-120b (high · 오라클)",
  "oraclepad-oss120": "gpt-oss-120b (high · 오라클+잡음)",
};
// 같은 모델을 동일 입력으로 반복 채점한 세트: 이 사이의 라벨 차이가 곧 judge 자기 비일관성
const REPEAT_JUDGES = ["oss120-genoss120", "oss120-rep1", "oss120-rep2", "oss120-rep3"];
// 라벨 비교는 대소문자·공백·숫자/문자열 차이를 흡수해서 (거짓 불일치 방지)
const normLab = (v) => String(v ?? "").trim().toLowerCase();
const labMatch = (a, b) => normLab(a) !== "" && normLab(a) === normLab(b);
const judgeLabel = (j) => JUDGE_NAMES[j] || j;
// Metrics 표처럼 폭이 아쉬운 곳에서는 배치·반복 꼬리표를 떼고 모델명만 (판정 검토 모달은 전체 이름 유지)
const judgeShort = (j) => judgeLabel(j).replace(/\s*·\s*[^)]*(배치|반복\s*\d+|유저)/g, "");
// run 내부명(full-traced 등) -> 사람용 라벨 ("Qwen3-4B × default (20u)")
const runLabel = (name) => S.runs.find((r) => r.run === name)?.label || name;
// generator 내부 키 -> 라벨
const genLabel = (g) => S.runs[0]?.generators?.[g]?.label || g;
// QA 상세 등 공간 여유 있는 곳은 풀네임 판정 배지
const verdictFull = (v) => v ? `<span class="badge ${v[0] === "C" ? "bC" : v[0] === "H" ? "bH" : "bO"}">${esc(v)}</span>` : `<span class="badge bnull">판정 없음</span>`;
// 미끼(interference) 골든은 포함 점수의 좋고 나쁨이 반전됨: 0=미포함=저항 성공(FMR 기여), 2=흡수=감점
const intfBadge = (v) => v == null ? `<span class="badge bnull" data-desc="judge 라벨 없음">–</span>`
  : v === 0 ? `<span class="badge b2" data-desc="미끼 차단 (포함 점수 0): 시스템이 이 미끼를 흡수하지 않음 = 저항 성공, FMR에 긍정 기여">차단</span>`
  : v === 1 ? `<span class="badge b1" data-desc="미끼 일부 흡수 (포함 점수 1): 미끼 내용 일부가 추출 메모리에 들어감">일부흡수</span>`
  : `<span class="badge b0" data-desc="미끼 흡수 (포함 점수 2): 시스템이 미끼를 통째로 기억함 = FMR 감점">흡수</span>`;
const initials = (name) => esc((name || "?").trim().slice(0, 2));
// 골든 1건의 judge 배지 (update/미끼/일반 분기 공통화: 세션 카드·턴 패널·A/B 페어에서 재사용)
const goldenBadge = (mp) => {
  const j = mp?.judge || {};
  return j.kind === "update" ? verdictBadge(j.label)
    : mp?.memory_source === "interference" ? intfBadge(j.score)
    : scoreBadge(j.score);
};
// A/B 페어 배지: 앞에 소속 문자(A 파랑 / B 진회색)
const abPair = (badgeA, badgeB) => `<i class="ab a">A</i>${badgeA}<i class="ab b">B</i>${badgeB}`;
// B 번들에서 같은 세션의 같은 골든 찾기 (인덱스 우선, 텍스트 매칭 폴백)
function bGolden(sid, i, content) {
  const sb = S.bundleB?.sessions.find((x) => x.session_id === sid);
  if (!sb || sb.generated_qa_session) return null;
  const g = sb.golden?.[i];
  return g && g.memory_content === content ? g : (sb.golden || []).find((m) => m.memory_content === content) || null;
}
// 앵커 문자열 -> 사람용 라벨 ("session:3/extb:5" -> "S3 · B추출#5")
function anchorHuman(anchor) {
  if (anchor === "run") return "유저 전체";
  const m = anchor.match(/^session:(\d+)(?:\/(\w+):(\d+))?$/);
  if (!m) return anchor;
  if (!m[2]) return `S${m[1]} 세션 전체`;
  const names = { mp: "골든", ext: "A추출", extb: "B추출", qa: "QA", turn: "턴" };
  return `S${m[1]} · ${names[m[2]] || m[2]}#${m[3]}`;
}
// 앵커가 가리키는 세팅 칩: A/B 요소면 해당 런, 공통 요소(골든·턴·세션)면 없음
function anchorSideChip(anchor) {
  if (anchor.includes("/extb:")) return `<span class="tagchip" style="color:var(--bcol);font-weight:800">B: ${esc(runLabel(S.runB || "?"))}</span>`;
  if (anchor.includes("/ext:") || anchor.includes("/qa:")) return `<span class="tagchip" style="color:var(--accent);font-weight:800">A: ${esc(runLabel(S.run))}</span>`;
  return "";
}

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
  else if (S.tab === "beam") renderBeam();
  else if (S.tab === "memora") renderMemora();
  else renderDigest();
  renderInspector();
}

/* ---------- 구성 통계 (골든 = 데이터셋 속성 / 추출 = 모델 산출물) ---------- */

// 골든 구성: 세션 1개 또는 세션 배열
function goldComp(sessions) {
  const list = [].concat(sessions);
  let g = 0, upd = 0, updJ = 0, intf = 0;
  list.forEach((s) => (s.golden || []).forEach((m) => {
    g++;
    if (m.memory_source === "interference") intf++;
    if (m.is_update === "True") upd++;
    if (m.judge?.kind === "update") updJ++;
  }));
  return { g, upd, updJ, intf, plain: g - upd - intf };
}

// 추출 구성: mem0의 연산 기록(events)이 원본: extracted는 DELETE가 빠져 있어 연산 구성 계산 불가
function extComp(sessions) {
  const list = [].concat(sessions);
  let ops = 0, add = 0, upd = 0, noop = 0, del = 0, stored = 0;
  list.forEach((s) => {
    stored += (s.extracted || []).length;
    (s.events || []).forEach((e) => {
      ops++;
      if (e.event === "ADD") add++;
      else if (e.event === "UPDATE") {
        upd++;
        if ((e.previous_memory || "").trim() === (e.memory || "").trim()) noop++;
      } else if (e.event === "DELETE") del++;
    });
  });
  return { ops, add, upd, noop, updReal: upd - noop, del, stored };
}

const realSessions = (bundle) => (bundle?.sessions || []).filter((s) => !s.generated_qa_session);

// 누적 가로 막대 + 범례 칩 (구성 시각화 공통 문법)
function compBar(total, segs) {
  const use = segs.filter((s) => s.n > 0);
  if (!total || !use.length) return `<div class="cbar"><span class="seg s-none" style="width:100%"></span></div>`;
  const pct = (n) => (n / total * 100).toFixed(1);
  return `<div class="cbar">${use.map((s) =>
      `<span class="seg ${s.cls}" style="width:${pct(s.n)}%" data-desc="${esc(s.desc)}"></span>`).join("")}</div>
    <div class="cleg">${use.map((s) =>
      `<span class="ck ${s.cls}" data-desc="${esc(s.desc)}">${esc(s.label)} ${s.n.toLocaleString()}<i>${pct(s.n)}%</i></span>`).join("")}</div>`;
}

const goldSegs = (c) => [
  { n: c.plain, cls: "s-plain", label: "일반", desc: "일반 골든: 포함(2점)이 성공인 표준 평가 대상" },
  { n: c.upd, cls: "s-upd", label: "↻ 갱신", desc: `갱신 골든(is_update=True): 과거 정보의 갱신본. Update(C/H/O)로 채점 (이 범위에서 실제 채점 대상 ${c.updJ}건)` },
  { n: c.intf, cls: "s-intf", label: "⚠ 미끼", desc: "미끼 골든(interference): AI 발화에만 있고 user가 확정하지 않은 내용. 흡수하지 않아야 좋음(FMR)" },
];
const extSegs = (c) => [
  { n: c.add, cls: "s-add", label: "ADD", desc: "신규 추출: 기존에 없던 사실을 새로 저장" },
  { n: c.updReal, cls: "s-updr", label: "UPDATE", desc: "실질 UPDATE: 기존 메모리를 실제로 다른 내용으로 재작성" },
  { n: c.noop, cls: "s-noop", label: "= 무변경", desc: NOOP_DESC },
  { n: c.del, cls: "s-del", label: "DELETE", desc: "삭제: 기존 메모리를 제거 (저장 목록에서 빠짐)" },
];

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
  // B 비교 중이면 세션 문제 카운트를 양쪽 다 계산 (B는 점선 테두리 + 소속 문자)
  const sbMap = S.bundleB
    ? new Map(S.bundleB.sessions.filter((x) => !x.generated_qa_session).map((x) => [x.session_id, x]))
    : null;
  const flagsOf = (m, bSide) => [
    m.g0 ? `<span class="flag flag-g${bSide ? " bflag" : ""}" data-desc="G✗ (마젠타): ${bSide ? "B 세팅" : S.bundleB ? "A 세팅" : "이 세션"}에서 judge가 미포함(0점) 판정한 골든 수 (미끼 제외). 세션 문제 카운트이고, 판정 배지와는 다릅니다">G✗${m.g0}</span>` : "",
    m.qaBad ? `<span class="flag flag-q${bSide ? " bflag" : ""}" data-desc="Q✗ (브라운): ${bSide ? "B 세팅" : S.bundleB ? "A 세팅" : "이 세션"}의 오답(H/O) QA 수. 세션 문제 카운트이고, 판정 배지와는 다릅니다">Q✗${m.qaBad}</span>` : "",
  ].join("");
  // ── 유저 전체 구성 (사이드바 상단): 골든(데이터셋 공통) + 추출 A/B(모델 산출물)
  const rsA = realSessions(S.bundle);
  const gC = goldComp(rsA), eA = extComp(rsA);
  const nQA = rsA.reduce((n, s) => n + s.questions.length, 0);
  const extBlock = (label, cls, run, c) => `
    <div class="ub"><div class="h ${cls}">${label} <span class="rn">${esc(runLabel(run))}</span>
      <span class="tot" data-desc="mem0가 이 유저에게 수행한 메모리 연산 총수 (ADD+UPDATE+DELETE). 저장돼 남은 것은 ${c.stored.toLocaleString()}개. DELETE는 목록에서 빠짐">연산 ${c.ops.toLocaleString()}</span></div>
      ${compBar(c.ops, extSegs(c))}</div>`;
  const compHTML = `<div class="ucomp">
    <div class="t">${esc(S.bundle.user_name)} <span class="muted">· 세션 ${rsA.length} · QA ${nQA}</span></div>
    <div class="ub"><div class="h gold-h">골든 <span class="rn">데이터셋 공통</span>
      <span class="tot" data-desc="이 유저의 골든 메모리 총수: 데이터셋 고유 속성이라 Agent·Generator·Judge 설정과 무관하게 동일합니다">${gC.g.toLocaleString()}</span></div>
      ${compBar(gC.g, goldSegs(gC))}</div>
    ${extBlock("추출 A", "a-h", S.run, eA)}
    ${S.bundleB ? extBlock("추출 B", "b-h", S.runB, extComp(realSessions(S.bundleB))) : ""}
    <div class="qstart"><button class="jbtn" id="btn-queue" data-desc="모든 분석가에게 동일한 순서로 제공되는 표본을 순서대로 라벨합니다. 분석가 간 일치도(IAA)는 이 겹치는 항목들로 계산됩니다">⚖ 검토 시작 (공유 표본)</button>
      <button class="jbtn" id="btn-iaa" data-desc="라벨링 현황과 분석가 간·분석가 vs judge 일치도">📊 IAA</button>
      <button class="jbtn" id="btn-gqa" data-desc="골든 정답 검수 결과: 벤치마크 문항 자체의 품질. judge 판정과는 별개로 봅니다">📝 정답검수</button></div>
  </div>`;
  sb.innerHTML = compHTML + S.bundle.sessions.map((s) => {
    if (s.generated_qa_session) return "";
    const fA = flagsOf(sessionSummary(s), false);
    let flags = fA;
    if (sbMap) {
      const sB = sbMap.get(s.session_id);
      const fB = sB ? flagsOf(sessionSummary(sB), true) : "";
      flags = (fA ? `<i class="ab a">A</i>${fA}` : "") + (fB ? `<i class="ab b">B</i>${fB}` : "");
    }
    return `<div class="side-item ${s.session_id === S.session ? "active" : ""}" data-sid="${s.session_id}">
      <b>S${s.session_id}</b><span class="t">${esc((s.start_time || "").slice(0, 12))}</span>
      <span class="flags">${flags}</span></div>`;
  }).join("");
  $$(".side-item", sb).forEach((el) => (el.onclick = () => { S.session = +el.dataset.sid; renderSessions(); }));
  $("#btn-queue") && ($("#btn-queue").onclick = jmStartQueue);
  $("#btn-iaa") && ($("#btn-iaa").onclick = () => { JM.ctx = null; $("#jmodal").classList.remove("hidden"); $("#jmodal-head").innerHTML = `<b>사람 판정 vs judge: 검토 결과</b><span style="margin-left:auto"></span><button class="jbtn" id="jm-close">✕</button>`; $("#jm-close").onclick = jmClose; jmIAA(); });
  $("#btn-gqa") && ($("#btn-gqa").onclick = () => { JM.ctx = null; $("#jmodal").classList.remove("hidden"); $("#jmodal-head").innerHTML = `<b>골든 정답 검수: 벤치마크 품질</b><span style="margin-left:auto"></span><button class="jbtn" id="jm-close">✕</button>`; $("#jm-close").onclick = jmClose; jmGoldQA(); });

  const s = S.bundle.sessions.find((x) => x.session_id === S.session);
  if (!s || s.generated_qa_session) { $("#content").innerHTML = "<p class='muted'>세션을 선택하세요</p>"; return; }
  const A = anchorTurns(s);
  const B = anchorsForB(s.session_id);
  const sG = goldComp(s), sA = extComp(s);  // 이 세션의 구성 (골든 / 추출 A)

  // QA (워크플로상 최상단)
  const qas = s.questions.map((q, i) => {
    let badges = verdictBadge(q.judge);
    if (B?.session) {
      const qb = B.session.questions?.find((x) => x.question === q.question);
      badges = abPair(verdictBadge(q.judge), verdictBadge(qb?.judge));
    }
    const jq = (run, lab) => jmBtn({ run, uuid: S.uuid, session_id: s.session_id, rec_type: "qa", idx: i, generator: S.generator, judge_name: S.judge }, lab);
    // 골든 정답 자체의 타당성 검수 (judge 판정과 별개, 벤치마크 품질 검수)
    const jgold = jmBtn({ run: S.run, uuid: S.uuid, session_id: s.session_id, rec_type: "gold_qa", idx: i, generator: S.generator }, "정답검수");
    return `<div class="row" data-qa="${i}" data-desc="클릭하면 이 QA의 4자 대조 화면으로 이동. 드래그로 텍스트 선택 후 코멘트도 가능">
      <span>${badges}${B?.session ? jq(S.run, "A") + jq(S.runB, "B") : jq(S.run)}${jgold}</span>
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
        const desc = upd ? "갱신 골든 (근사 앵커·추정): 과거 정보의 업데이트본. Update(C/H/O) 평가 대상"
          : intf ? "미끼(interference) 골든 (근사 앵커·추정): AI 발화에만 있고 user가 확정 안 한 내용. 시스템이 흡수하면 감점(FMR)"
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
    const upd = mp.is_update === "True", intf = mp.memory_source === "interference";
    // B 비교 중엔 같은 골든에 대한 양쪽 judge 판정을 페어로 (골든 자체는 데이터셋 공통)
    const badge = B?.session
      ? abPair(goldenBadge(mp), goldenBadge(bGolden(s.session_id, i, mp.memory_content)))
      : goldenBadge(mp);
    // 골든 판정은 run의 추출 목록을 입력으로 쓰므로 A/B가 서로 다른 판정 상황임
    const rt = mp.judge?.kind === "update" ? "update" : "integrity";
    const jg = (run, lab) => jmBtn({ run, uuid: S.uuid, session_id: s.session_id, rec_type: rt, idx: i, judge_name: S.judge }, lab);
    return `<div class="row${upd ? " upd-row" : intf ? " intf-row" : ""}" data-a="mp:${i}" data-turn="${A.gTurn[i]}">
      <span>${badge}${B?.session ? jg(S.run, "A") + jg(S.runB, "B") : jg(S.run)}</span>
      ${upd ? '<span class="tagchip t-upd" data-k="is_update">↻ upd</span>' : ""}
      ${intf ? '<span class="tagchip t-intf" data-k="memory_source">⚠ 미끼</span>'
        : mp.memory_source !== "system" ? `<span class="tagchip" data-k="memory_source">${esc(mp.memory_source)}</span>` : ""}
      <span class="txt">${esc(mp.memory_content)}</span></div>`;
  }).join("");

  const extRows = s.extracted.map((m, i) => `
    <div class="row${isNoop(m) ? " noop-row" : ""}" data-a="ext:${i}" data-turn="${A.eTurn[i]}">
      <span>${scoreBadge(m.judge?.score)}${jmBtn({ run: S.run, uuid: S.uuid, session_id: s.session_id, rec_type: "accuracy", idx: i, judge_name: S.judge })}</span>${memOps(m)}
      <span class="txt">${esc(m.text)}</span></div>`).join("");

  let extBCard = "";
  if (B?.session) {
    const extBRows = B.session.extracted.map((m, i) => `
      <div class="row${isNoop(m) ? " noop-row" : ""}" data-b-ext="${i}">
        <span>${scoreBadge(m.judge?.score)}${jmBtn({ run: S.runB, uuid: S.uuid, session_id: s.session_id, rec_type: "accuracy", idx: i, judge_name: S.judge })}</span>${memOps(m)}
        <span class="txt">${esc(m.text)}</span></div>`).join("");
    const sB = extComp(B.session);
    extBCard = `<div class="card b-card"><h4>추출 B: ${esc(runLabel(S.runB))} (${B.session.extracted.length})<span class="tot-h" data-desc="이 세션에서 mem0가 수행한 메모리 연산 총수 (ADD+UPDATE+DELETE). 저장돼 남은 것은 ${sB.stored}개">연산 ${sB.ops}</span></h4>
      <div class="scomp">${compBar(sB.ops, extSegs(sB))}</div><div class="body">${extBRows}</div></div>`;
  } else if (S.runB) {
    extBCard = `<div class="card b-card"><h4>추출 B: ${esc(runLabel(S.runB))}</h4><div class="body"><p class="small muted">이 유저/세션 데이터가 B 런에 없음</p></div></div>`;
  }

  $("#content").innerHTML = `
    <div class="hint">S${s.session_id}: QA부터 확인 → 대화 스크롤하며 골든/추출 대조. 행 클릭=우측 상세 · 드래그 선택=코멘트 · 턴 클릭=앵커 상세${s.add_dialogue_duration_ms ? ` · <span data-desc="이 세션의 mem0.add 소요 시간 (fact 추출·update 결정 LLM 콜 포함): 백본 속도 실측">⏱ 투입 ${(s.add_dialogue_duration_ms / 1000).toFixed(1)}s</span>` : ""}${S.bundleB ? ` · <span style="color:var(--bcol);font-weight:700">B=${esc(runLabel(S.runB))} (회색)</span>` : " · 상단 [+ 비교(B)]로 다른 세팅 비교"}</div>
    <div class="legend" data-desc="배지 범례: 채운 원형=judge 판정, 외곽선 사각형=사이드바 세션 문제 카운트">
      <b>범례</b>
      <span><span class="badge b2">2</span>완전</span><span><span class="badge b1">1</span>부분</span><span><span class="badge b0">0</span>실패</span>
      <span><span class="badge bC">C</span>/<span class="badge bH">H</span>/<span class="badge bO">O</span></span>
      <span class="flag flag-g" style="cursor:default" data-desc="사이드바: 이 세션의 미포함(0점) 골든 수">G✗</span>
      <span class="flag flag-q" style="cursor:default" data-desc="사이드바: 이 세션의 오답 QA 수">Q✗</span>
      <span class="anchor-chip g" style="cursor:default">G 골든(금)</span>
      <span class="anchor-chip g upd" style="cursor:default" data-desc="갱신 골든: Update(C/H/O) 평가 대상">G↻ 갱신</span>
      <span class="anchor-chip g intf" style="cursor:default" data-desc="미끼 골든: 흡수하면 감점(FMR)">G⚠ 미끼</span>
      <span class="anchor-chip e" style="cursor:default">A 추출(파랑)</span>
      ${S.bundleB ? '<span class="anchor-chip eb" style="cursor:default">B 추출(회색)</span>' : ""}
      <span class="op UPDATE" style="cursor:default">UPDATE</span><span class="op noop" style="cursor:default" data-desc="${NOOP_DESC}">= 무변경</span><span>= 갱신했다지만 내용이 안 바뀜</span>
      <span class="cmt-chip" style="cursor:default">가</span><span>= 코멘트 (호버/클릭)</span>
    </div>
    <div class="card"><h4 data-k="questions">QA (${s.questions.length})</h4><div class="body">${qas}</div></div>
    <div class="card"><h4 data-k="dialogue_turn">대화 (${s.dialogue.length}턴)
      <span class="small muted" style="margin-left:auto" data-desc="대화 오른쪽에 표시되는 골든/추출 메모리 칼럼의 너비 조절">메모리 표시 너비</span>
      <input type="range" id="anch-w" min="140" max="640" step="10" style="width:110px"></h4>
      <div class="body">${dlg}</div></div>
    <div class="${B?.session ? "three-col" : "two-col"}">
      <div class="card"><h4 data-k="memory_points" class="gold-h">골든 (${s.golden.length})${B?.session ? ' <span class="small muted" style="text-transform:none" data-desc="골든은 데이터셋 공통: 배지는 왼쪽이 A 세팅, 오른쪽이 B 세팅의 judge 판정">공통 · A/B 판정</span>' : ""}</h4>
        <div class="scomp">${compBar(sG.g, goldSegs(sG))}</div><div class="body">${goldenRows}</div></div>
      <div class="card"><h4 data-k="extracted_memories">추출 A: ${esc(runLabel(S.run))} (${s.extracted.length})<span class="tot-h" data-desc="이 세션에서 mem0가 수행한 메모리 연산 총수 (ADD+UPDATE+DELETE). 저장돼 남은 것은 ${sA.stored}개">연산 ${sA.ops}</span></h4>
        <div class="scomp">${compBar(sA.ops, extSegs(sA))}</div><div class="body">${extRows}</div></div>
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
  // 칩 클릭: 우측 상세만 (자동 스크롤 없음. 대화 읽던 위치 유지)
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
  bindJmButtons($("#content"));
  renderCmtMarks();
}

function toggleTurnPanel(s, A, B, ti) {
  const box = $(`#turnx-${ti}`);
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  const a = A.map[ti];
  const dual = !!B?.session;
  // 골든은 데이터셋 공통: B 비교 중엔 양쪽 judge 판정을 페어로
  const secG = a.g.length ? `
    <h5 style="color:var(--gold)">골든 (공통${dual ? " · A/B 판정" : ""}): 이 턴 앵커 (추정)</h5>
    ${a.g.map((gi) => { const mp = s.golden[gi];
      const badge = dual ? abPair(goldenBadge(mp), goldenBadge(bGolden(s.session_id, gi, mp.memory_content))) : goldenBadge(mp);
      return `<div class="row">${badge}<span class="txt">${esc(mp.memory_content)}</span></div>`; }).join("")}` : "";
  const secA = `
    <h5 style="color:var(--accent)">A: ${esc(runLabel(S.run))}: 이 턴 추출 (추정)</h5>
    ${a.e.map((ei) => { const m = s.extracted[ei];
      return `<div class="row${isNoop(m) ? " noop-row" : ""}">${memOps(m)}${scoreBadge(m.judge?.score)}<span class="txt">${esc(m.text)}</span></div>`; }).join("") || "<p class='small muted'>앵커된 추출 없음</p>"}`;
  let secB = "";
  if (dual) {
    const bMap = B.map[ti] || { g: [], e: [] };
    secB = `<h5 style="color:var(--bcol)">B: ${esc(runLabel(S.runB))}: 이 턴 추출 (추정)</h5>
      ${bMap.e.map((ei) => { const m = B.session.extracted[ei];
        return `<div class="row${isNoop(m) ? " noop-row" : ""}">${memOps(m)}${scoreBadge(m.judge?.score)}<span class="txt">${esc(m.text)}</span></div>`; }).join("") || "<p class='small muted'>앵커된 추출 없음</p>"}`;
  }
  box.innerHTML = secG + secA + secB;
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
  visibleComments().forEach((c) => (byAnchor[c.anchor] = byAnchor[c.anchor] || []).push(c));
  Object.entries(byAnchor).forEach(([anchor, list]) => {
    const sel = anchorSelector(anchor);
    if (!sel) return;
    const el = $(`#content ${sel}`);
    if (!el) return;
    el.classList.add("has-cmt");
    const allOther = list.every((c) => !cmtMatches(c));  // 전부 다른 세팅에서 단 코멘트 → 회색 칩
    const chip = document.createElement("span");
    chip.className = `cmt-chip live${allOther ? " other" : ""}`;
    chip.textContent = list.length > 1 ? `${initials(list[0].author)}+${list.length - 1}` : initials(list[0].author);
    chip.dataset.desc = `${allOther ? "[다른 세팅] " : ""}코멘트 ${list.length}개. ${esc(list.map((c) => `${c.author}: ${c.body.slice(0, 40)}`).join(" | "))} (클릭하면 스레드)`;
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
  const bVerdict = ({ s, q }) => {
    const sb = S.bundleB?.sessions.find((x) => x.session_id === s.session_id);
    return sb?.questions?.find((x) => x.question === q.question)?.judge;
  };
  $("#sidebar").innerHTML = `
    <div style="padding:8px"><div class="pill-filter" ${S.bundleB ? 'data-desc="판정 필터: B 비교 중에도 필터는 A 세팅 판정 기준"' : ""}>${filters.map((f) =>
      `<button class="${S.qaFilter === f ? "on" : ""}" data-f="${f}">${f === "all" ? "전체" : f[0]}</button>`).join("")}</div></div>` +
    items.map((it) => `
      <div class="side-item" data-sid="${it.s.session_id}" data-qi="${it.i}">
        ${S.bundleB ? abPair(verdictBadge(it.q.judge), verdictBadge(bVerdict(it))) : verdictBadge(it.q.judge)} <span class="txt small">${esc(it.q.question.slice(0, 60))}</span></div>`).join("");
  $$("#sidebar .pill-filter button").forEach((b) => (b.onclick = () => { S.qaFilter = b.dataset.f; renderQA(); }));
  $$("#sidebar .side-item").forEach((el) => (el.onclick = () => renderQADetail(+el.dataset.sid, +el.dataset.qi, false)));
  $("#content").innerHTML = `<p class="hint">좌측에서 QA를 선택하세요 (판정 필터: 전체/C/H/O${S.bundleB ? ", A 세팅 기준" : ""}). 총 ${allQAs().length}문항.</p>`;
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

  // 답변 생성 레인 표기: A/B 카드 공통 (generator 레인은 상단 선택 하나를 양쪽이 공유)
  const genTxt = ` · gen=${esc(genLabel(S.generator))}`;

  // B 세팅: 답변 + B 자체의 검색 context (context는 Stage A 산물이라 런마다 다름)
  let bCard = "", bCtxCard = "";
  if (S.bundleB) {
    const sb = S.bundleB.sessions.find((x) => x.session_id === sid);
    const qb = sb?.questions?.find((x) => x.question === q.question);
    bCard = `<div class="card b-card"><h4>B: ${esc(runLabel(S.runB))} 답변${genTxt} ${verdictFull(qb?.judge)}</h4>
      <div class="body">${esc(qb?.system_response || "(B 런에 답변 없음)")}</div></div>`;
    if (qb) {
      const itemsB = parseContext(qb.context);
      const listB = itemsB
        ? itemsB.map((c, i) => `<div class="row"><span class="small muted">#${i + 1}</span><span class="txt">${esc(c)}</span></div>`).join("")
        : null;
      const rawB = `<pre class="mono">${esc(typeof qb.context === "string" ? qb.context : JSON.stringify(qb.context, null, 2))}</pre>`;
      bCtxCard = `<div class="card b-card"><h4 data-k="context">B 검색 Context (${itemsB ? itemsB.length + "건" : "원문"})
        ${itemsB ? '<button class="ctx-toggle" id="ctx-toggle-b">원문 보기</button>' : ""}</h4>
        <div class="body"><div id="ctx-list-b">${listB ?? rawB}</div><div id="ctx-raw-b" class="hidden">${rawB}</div></div></div>`;
    }
  }

  const isOracle = S.generator === "oracle";
  $("#content").innerHTML = `
    ${fromSession ? `<span class="backlink" id="btn-back">← 세션 S${sid}로 돌아가기</span>` : ""}
    ${isOracle ? `<div class="oracle-note" data-desc="이 레인은 메모리 시스템의 검색 결과를 쓰지 않습니다. 저장·검색이 완벽했다고 가정한 QA 상한을 재기 위한 대조군입니다">⚠ <b>오라클 레인</b>: 이 답변은 아래 검색 Context를 쓰지 않고 <b>Evidence(정답 근거 골든)</b>만 보고 생성됐습니다. 저장·검색이 완벽했을 때의 상한을 재는 대조군이며, 아래 검색 Context는 참고용으로만 표시됩니다.</div>` : ""}
    <div class="hint">S${sid} · ${esc(q.question_type || "")}: 질문 → 정답 → ${S.bundleB ? "A/B 답변(판정)" : "답변(판정)"} → ${isOracle ? "Evidence(실제 사용) → 검색 context(참고)" : "context"}</div>
    <div class="card"><h4 data-k="question">질문</h4><div class="body">${esc(q.question)}</div></div>
    <div class="card"><h4 data-k="answer">골든 정답</h4><div class="body">${esc(q.answer)}</div></div>
    <div class="card"><h4 data-k="system_response">${S.bundleB ? `A: ${esc(runLabel(S.run))} 답변` : "시스템 답변 (A′)"}${genTxt} ${verdictFull(q.judge)}</h4><div class="body">${esc(q.system_response || "(A′ 미실행)")}</div></div>
    ${bCard}
    <div class="card${isOracle ? " oracle-card" : ""}"><h4 data-k="evidence">Evidence (${(q.evidence || []).length})${isOracle ? ' <span class="small" style="text-transform:none;color:var(--ok);font-weight:800">← 이 레인이 실제로 사용한 입력</span>' : ""}</h4><div class="body">${ev}</div></div>
    <div class="card"><h4 data-k="context">${S.bundleB ? "A " : ""}검색 Context (${items ? items.length + "건" : "원문"})
      ${items ? '<button class="ctx-toggle" id="ctx-toggle">원문 보기</button>' : ""}</h4>
      <div class="body"><div id="ctx-list">${listHTML ?? rawHTML}</div><div id="ctx-raw" class="hidden">${rawHTML}</div></div></div>
    ${bCtxCard}`;
  const bindCtxToggle = (btnId, listId, rawId) => {
    const btn = $(btnId);
    if (!btn) return;
    btn.onclick = () => {
      const showRaw = $(rawId).classList.toggle("hidden");
      $(listId).classList.toggle("hidden", !showRaw);
      btn.textContent = showRaw ? "원문 보기" : "목록 보기";
    };
  };
  bindCtxToggle("#ctx-toggle", "#ctx-list", "#ctx-raw");
  bindCtxToggle("#ctx-toggle-b", "#ctx-list-b", "#ctx-raw-b");
  $("#btn-back") && ($("#btn-back").onclick = () => renderSessions());
  S.session = sid;
  setAnchor(`session:${sid}/qa:${qi}`, q);
}

/* ----- Compare ----- */

function renderCompare() {
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted">A: <b>${esc(runLabel(S.run))}</b></p>
    <p class="small muted">B: <b>${esc(S.runB ? runLabel(S.runB) : "미선택")}</b></p>
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
    <div class="hint">A = ${esc(runLabel(S.run))} / B = ${esc(runLabel(S.runB))}: 유저 ${esc(S.bundle.user_name)}. 주황 = 판정 갈림. QA 행 클릭 = 상세 대조.</div>
    <div class="card"><h4 data-desc="이 유저 전체에 대한 A/B 세팅 요약: 골든 포함률(judge 2점 비율), 추출량, accuracy 0점 수, QA 판정 분포">요약 (유저 전체)</h4><div class="body">
      <div class="row" style="cursor:default"><span class="tagchip">A</span><span class="txt">${esc(runLabel(S.run))}</span><span>${fmtAgg(aA)}</span></div>
      <div class="row" style="cursor:default"><span class="tagchip" style="color:var(--bcol)">B</span><span class="txt">${esc(runLabel(S.runB))}</span><span>${fmtAgg(aB)}</span></div>
    </div></div>
    <div class="card"><h4 data-desc="같은 질문에 대한 A/B 판정 대조: 주황 셀이 판정이 갈린 질문. 행 클릭 시 양쪽 답변·정답·근거 펼침">QA 판정 대조 (${qas.filter((r) => r.diff).length}건 갈림 / ${qas.length})</h4><div class="body">
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
        <p class="small"><b>골든 정답</b>: ${esc(r.q.answer)}</p>
        <p class="small"><b>A 답변</b> ${verdictFull(r.q.judge)}</p><pre class="mono">${esc(r.q.system_response || "(없음)")}</pre>
        <p class="small"><b>B 답변</b> ${verdictFull(r.qb?.judge)}</p><pre class="mono">${esc(r.qb?.system_response || "(B 런에 없음)")}</pre>
        <p class="small"><b>근거 골든</b>: ${(r.q.evidence || []).map((e) => esc(e.memory_content)).join(" · ") || "없음"}</p></td>`;
      tr.after(x);
    };
  });
}

/* ----- Metrics (전체 실험 지표 테이블) ----- */

const METRIC_DEFS = {
  r: "Recall↑: 골든 메모리 포인트(interference 제외) 중 judge가 '완전 포함(2점)'으로 판정한 비율. 추출 커버리지의 핵심 지표",
  wr: "Weighted Recall↑: 부분 포함(1점)에 0.5 가중치를 준 포함률. R과의 격차가 크면 '부분만 건진' 골든이 많다는 뜻",
  acc: "Accuracy↑: 추출 메모리가 당해 세션 대화·골든에 근거하는 정도(2/1/0 가중평균). 괄호=채점된 추출 메모리 수. ⚠ 교차 세션 내용을 담은 UPDATE 재작성본은 구조적으로 불리",
  tp: "Target Precision↑: 필드(주제) 단위로 골든과 대응한다고 판정된 추출 메모리만 모수(괄호)로 한 accuracy 가중평균",
  fmr: "FMR↑: 미끼(interference: AI 발화에만 있고 user가 확정 안 한 내용) 골든 중 시스템이 흡수하지 않은 비율. 높을수록 distractor 저항이 강함",
  f1: "F1↑: R과 Target P의 조화평균. R≪P 레짐에선 사실상 R이 지배 (F1 개선 ≡ R 개선)",
  upd_c: "Update Correct↑: 갱신 요구 골든에 대해 갱신본의 모든 원자 정보·수치가 정확한 비율",
  upd_h: "Update Hallucination↓: 갱신 중 틀린 값을 만들어낸 비율",
  upd_o: "Update Omission↓: 갱신 중 디테일을 누락한 비율 (Qwen 계열의 주 실패 모드)",
  qa_c: "QA Correct↑: 다요소 질문에 대한 답변이 완전 정답인 비율",
  qa_h: "QA Hallucination↓: 답변이 날조인 비율",
  qa_o: "QA Omission↓: 답변이 누락·회피인 비율",
};
const METRIC_COLS = [
  { k: "r", label: "R↑", dir: 1 }, { k: "wr", label: "Weighted R↑", dir: 1 },
  { k: "acc", label: "Acc.↑ (#mem)", dir: 1, n: "acc_n" }, { k: "tp", label: "Target P↑ (#mem)", dir: 1, n: "tp_n" },
  { k: "fmr", label: "FMR↑", dir: 1 }, { k: "f1", label: "F1↑", dir: 1 },
  { k: "upd_c", label: "Upd C↑", dir: 1 }, { k: "upd_h", label: "Upd H↓", dir: -1 }, { k: "upd_o", label: "Upd O↓", dir: -1 },
  { k: "qa_c", label: "QA C↑", dir: 1 }, { k: "qa_h", label: "QA H↓", dir: -1 }, { k: "qa_o", label: "QA O↓", dir: -1 },
];
const metricsCache = new Map();
S.metricsScope = "first4";
S.showGroups = new Set();   // 켜진 숨김 그룹 (custom / bm25 …): runs.yaml의 hide_groups
// 행/열 하이라이트 선택: 행은 run 이름, 열은 칼럼 키. 재렌더에도 유지 (탭 이탈해도 세션 내 유지)
S.metricsSelRows = new Set(); S.metricsSelCols = new Set(); S.metricsSelCells = new Set();
S.metricsColRows = new Set(); S.metricsColCols = new Set();   // 접힌 행/열

async function renderMetrics() {
  // generator·judge는 상단바 선택을 그대로 따른다 (별도 선택기 없음. 화면 전체가 한 조합)
  const groupsParam = [...S.showGroups].sort().join(",");
  const key = `${S.generator}|${S.judge}|${S.metricsScope}|${groupsParam}`;
  if (!metricsCache.has(key)) {
    busy(true, "지표 집계 중 (공식 집계 함수)…");
    try { metricsCache.set(key, await api(`/api/metrics?judge=${S.judge}&scope=${S.metricsScope}&generator=${S.generator}${groupsParam ? `&show=${encodeURIComponent(groupsParam)}` : ""}`)); }
    finally { busy(false); }
  }
  const data = metricsCache.get(key);

  // 스코프 옵션: 공통 4유저 + 개별 유저(이름은 현재 유저 목록에서 매핑)
  const nameOf = (uid) => S.users.find((u) => u.uuid === uid)?.user_name || uid.slice(0, 8);
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted" data-desc="이 테이블의 관측 스택: 바꾸려면 상단바의 Generator/Judge 드롭다운을 사용">
      <b>조합</b> (상단바 연동)<br>gen=${esc(genLabel(S.generator))}<br>judge=${esc(judgeLabel(S.judge))}</p>
    <p class="small muted" style="margin-top:10px"><b>유저 범위</b></p>
    <select id="metrics-scope" style="width:100%">
      <option value="first4" ${S.metricsScope === "first4" ? "selected" : ""}>첫 4유저 (전 실험 공통)</option>
      <option value="all" ${S.metricsScope === "all" ? "selected" : ""}>런별 전체 유저</option>
      ${data.first4.map((u) => `<option value="${u}" ${S.metricsScope === u ? "selected" : ""}>${esc(nameOf(u))}</option>`).join("")}
    </select>
    <p class="small muted" style="margin-top:10px"><b>숨긴 세팅</b></p>
    ${(data.hide_groups || []).map((g) => `
    <label class="small hgrp" data-desc="${esc(g.desc || "")}">
      <input type="checkbox" data-group="${esc(g.key)}" ${S.showGroups.has(g.key) ? "checked" : ""}> ${esc(g.label || g.key)}도 보기</label>`).join("")}
    <p class="small muted" style="margin-top:10px">굵은 값 = 열별 최고(방향 반영). 셀 호버 = 순위·해석, 행 첫 칸 호버 = 런 요약 노트. 이 조합의 라벨이 없는 런은 표에서 제외됨 (레인마다 채점 유저 수가 다름).</p></div>`;
  $("#metrics-scope").onchange = () => { S.metricsScope = $("#metrics-scope").value; renderMetrics(); };
  $$("#sidebar input[data-group]").forEach((el) => (el.onchange = () => {
    el.checked ? S.showGroups.add(el.dataset.group) : S.showGroups.delete(el.dataset.group);
    renderMetrics();
  }));

  const allRows = data.rows.filter((r) => r.metrics);
  const rows = allRows;   // 접기는 DOM에 남겨두고 클래스로만 처리 (재렌더 없이 토글하기 위함)
  // 오라클로 읽을 수 없게 된 지표는 '–'로 가린다 (백엔드가 masked 목록을 내려줌)
  const isMasked = (r, k) => (r.masked || []).includes(k);
  // 오라클 행은 시스템 설정이 아닌 대조군(상한)이므로 어떤 열에서도 백본들과 순위를 겨루지 않는다.
  // QA까지 순위에 넣으면 1위가 항상 오라클 상한(83.66)이 되어 백본 간 비교가 무의미해진다.
  // 값은 그대로 보여주되 순위·최고값 계산에서만 뺀다.
  const QA_KEYS = ["qa_c", "qa_h", "qa_o"];
  const outOfRank = (r) => !!r.oracle;
  // 메모리측 지표는 '주입된 정답'이 만든 수치라 기울임+보라로 구분 표기하고, QA는 정상 표기한다
  //   (오라클 행에서 QA는 실제로 읽는 값이므로 흐리게 만들면 안 된다)
  const isInjected = (r, k) => outOfRank(r) && !QA_KEYS.includes(k);
  const inRank = (r, k) => !isMasked(r, k) && !outOfRank(r);
  // 열별 최고/순위 (방향 반영): 가려진 칸·오라클 주입값은 모수에서 제외해 순위가 오염되지 않게 한다
  const rank = {};
  METRIC_COLS.forEach((c) => {
    const live = rows.filter((r) => inRank(r, c.k));
    const vals = live.map((r) => r.metrics[c.k]);
    const sorted = [...vals].sort((a, b) => c.dir === 1 ? b - a : a - b);
    rank[c.k] = { sorted, best: sorted[0], n: live.length };
  });

  // ── 노이즈 바닥 ────────────────────────────────────────────────────────────
  // 같은 실험을 A′ 생성부터 다시 돌리면 수치가 얼마나 흔들리는지의 실측값(백엔드가 산출물에서 계산).
  // 이보다 작은 행 간 차이는 실체가 아니므로, '최고값 ± 노이즈' 안에 드는 행을 모두 공동 1위로 굵게 한다.
  const NZ = data.noise;
  // 반복 회차는 1유저분만 있다. 질문 단위 독립을 가정하면 유저가 n배면 표준편차는 √n배 작아진다. 추정치다.
  const noiseFor = (k, nUsers) => {
    if (!NZ || NZ[k] == null) return null;
    return NZ[k] / Math.sqrt(Math.max(1, (nUsers || 1) / (NZ.n_users || 1)));
  };
  // 그 행이 자체 반복을 가졌더라도 그것은 '한 루프 안의 회차들'이라 흔들림을 과소평가한다.
  // 배치 간을 포함한 노이즈 바닥과 비교해 <b>더 보수적인 쪽</b>을 밴드로 쓴다.
  const ownSd = (r, k) => (k === "qa_c" && r.sd != null) ? r.sd : null;
  const bandFor = (r, k) => {
    const floor = noiseFor(k, r.metrics?.n_users), own = ownSd(r, k);
    if (own == null) return floor;
    return floor == null ? own : Math.max(own, floor);
  };
  // 두 값이 구분 가능한가는 '값 하나의 SD' 대신 '차이의 표준오차'로 판단한다.
  //   차이의 SE = √(sd_a² + sd_b²), 95% 기준이면 1.96배. 1σ로 자르면 지나치게 엄격해진다.
  const bestRowOf = (k) => rows.find((x) => inRank(x, k) && x.metrics[k] === rank[k].best);
  const coBestGap = (r, k) => {
    const b = bandFor(r, k), bb = bandFor(bestRowOf(k) || r, k);
    return (b == null || bb == null) ? null : 1.96 * Math.sqrt(b * b + bb * bb);
  };
  const isCoBest = (r, c) => {
    if (!inRank(r, c.k)) return false;
    const gap = coBestGap(r, c.k);
    if (gap == null) return r.metrics[c.k] === rank[c.k].best;
    return Math.abs(r.metrics[c.k] - rank[c.k].best) <= gap;   // 노이즈 이내면 공동 1위
  };
  const judgeName = judgeLabel(data.judge);
  const judgeShortName = judgeShort(data.judge);
  const maxIngest = Math.max(...rows.map((r) => r.latency?.ingest_avg_s || 0), 0.1);

  // 메타 열 + 지표 열을 한 목록으로: 숨기기·하이라이트를 열 종류와 무관하게 동일하게 처리
  const META_COLS = [
    { k: "sys", label: "Memory System", desc: "메모리 시스템: 전부 mem0 OSS 0.1.118 classic" },
    { k: "users", label: "#Users", desc: "이 행의 지표가 집계된 유저 수" },
    { k: "backbone", label: "Agent LLM", desc: "memory agent 백본: fact 추출·update 결정을 담당하는 LLM" },
    { k: "prompt", label: "Prompt", desc: "fact 추출 프롬프트: default=mem0 기본 / custom=HaluMem 원본 지침(문단형)" },
    { k: "retriever", label: "Retriever", desc: "메모리 검색기입니다. <b>백본·프롬프트와 독립된 변인</b>입니다. 기본은 임베딩(Qwen3-Embedding-4B + Qdrant dense), BM25는 Qdrant sparse + IDF.<br>⚠ retriever는 QA 검색만 바꾸지 않습니다. mem0는 <b>갱신 결정 전에도 search로 후보를 가져오므로</b>(main.py:378) 저장 내용 자체가 달라집니다.<br>⚠ BM25는 질의어와 토큰이 겹치지 않는 문서를 아예 반환하지 않아 <b>context 개수가 줄어듭니다</b>(실측 19.96→13.83). 컨텍스트 축약만으로 QA가 오르므로 길이 통제 대조군과 함께 읽어야 합니다" },
    { k: "oracle", label: "Oracle 단계", desc: "해당 단계를 '완벽한 정답'으로 대체한 실험인지: <b>프롬프트 종류와 독립된 항목</b>입니다. '없음'=모든 단계를 시스템이 실제로 수행. 오라클 행은 저장물이 골든 자체라 R·Acc·FMR 비교가 무의미하고 QA C만 읽습니다" },
    { k: "lat", label: "Ingest/세션↓", desc: "세션 1개 투입(mem0.add) 평균 시간. fact 추출·update 결정 LLM 콜 포함. ⚠ 유저 병렬 실행 부하가 섞인 실측이라 절대값보단 행 간 상대 비교용" },
    { k: "judge", label: "Judge LLM", desc: "채점 LLM: 행 간 비교는 동일 judge에서만 유효" },
  ];
  const ALL_COLS = [...META_COLS, ...METRIC_COLS.map((c) => ({ k: c.k, label: c.label, desc: METRIC_DEFS[c.k], metric: c }))];
  const cols = ALL_COLS;

  const colCls = (k) => S.metricsSelCols.has(k) ? " mcol-sel" : "";
  const cellCls = (run, k) => S.metricsSelCells.has(`${run}|${k}`) ? " mcell-sel" : "";
  const caret = (kind, id, label) =>
    `<button class="hidecaret" data-fold="${kind}" data-id="${esc(id)}" data-desc="${esc(label)}을(를) 얇게 접습니다. 접힌 줄을 다시 클릭하면 펼쳐집니다">▾</button>`;

  function cellHTML(c, r, m) {
    if (c.metric) {
      if (isMasked(r, c.k)) return { html: `<span class="masked">–</span>`, desc: maskDesc(r, c.k) };
      const v = m[c.k];
      // ⚠ QA만 측정한 행(반복 채점을 --only qa로 돌린 고정 레인 등)은 나머지 지표가 null이다.
      //    오라클 행은 masked 경로로 걸러지지만 오라클이 아닌 QA 전용 행은 여기까지 온다.
      //    가드가 없으면 null.toFixed()로 Metrics 탭 전체가 렌더에 실패한다.
      if (v == null) return { html: `<span class="masked">–</span>`,
        desc: `<b>${esc(c.label)}</b>: 이 행은 <b>QA만 측정</b>했습니다. 답변 생성 레인 자체가 실험 조건이라 반복 채점을 QA로만 돌렸고, 저장물 지표(R·Acc·Upd 등)는 carrier 런의 값과 같습니다.` };
      const rk = rank[c.k].sorted.indexOf(v) + 1;
      const bestRow = bestRowOf(c.k);
      if (isInjected(r, c.k)) return {
        html: `<span class="oorank">${v.toFixed(2)}</span>`,
        desc: `<b>${esc(c.label)}</b> = ${v} <span class="small">(순위 비교 제외)</span><br>이 행은 <b>${esc(oracleLabel(r.oracle))}</b>을(를) 정답으로 주입한 실험이라, 이 값은 <b>주입된 정답이 다음 단계를 얼마나 통과했는지</b>를 뜻합니다. 백본의 능력을 잰 값과는 다릅니다. 실제 백본 행들과 같은 순위표에 놓지 않습니다.<br>${esc(METRIC_DEFS[c.k])}`,
      };
      const nTxt = c.metric.n && m[c.metric.n] != null ? ` <span class="small muted">(${m[c.metric.n].toLocaleString()})</span>` : "";
      // 흔들림 폭을 값 옆에 붙인다. 반복을 실제로 돌린 행은 실측 SD(±), 나머지는 유저 수로 환산한 추정치(~±).
      const band = QA_KEYS.includes(c.k) ? bandFor(r, c.k) : null;
      const own = ownSd(r, c.k);
      const sdTxt = band == null ? ""
        : ` <span class="sdtag${own != null ? " meas" : ""}">${own != null ? "±" : "~±"}${band.toFixed(2)}</span>`;
      const sdDesc = band == null ? ""
        : own != null
          ? `<br><b>${r.repeats.length}회 반복 실측</b> (A′ 생성 + judge 채점을 매번 새로): 회차: ${r.repeats.map((x) => x.toFixed(2)).join(", ")}`
            + (band > own ? `<br>⚠ 이 회차들은 <b>한 루프 안에서 연속 실행</b>돼 흔들림을 과소평가합니다(자체 ±${own.toFixed(2)}). 배치 간을 포함한 <b>±${band.toFixed(2)}</b>를 밴드로 씁니다.` : "")
          : `<br><b>흔들림 추정 ±${band.toFixed(2)}</b>: 이 행은 1회만 돌렸습니다. 실측 행의 독립 실행 ${NZ.n_obs}회에서 잰 ±${NZ[c.k]}(유저 ${NZ.n_users}명)를 이 행의 유저 ${m.n_users}명 기준으로 환산한 <b>추정치</b>입니다.`;
      const co = isCoBest(r, c);
      const gap = coBestGap(r, c.k);
      const coDesc = (co && v !== rank[c.k].best)
        ? `<br><span style="color:var(--accent);font-weight:800">공동 1위</span>. 1위(${rank[c.k].best})와의 차이 ${Math.abs(v - rank[c.k].best).toFixed(2)}p가 구분 한계 ${gap.toFixed(2)}p 이내라 우열을 가릴 수 없습니다.` : "";
      // 오라클 행의 QA는 실제로 읽는 값이므로 정상 표기하되, 순위표에서는 빠졌음을 밝힌다
      const rkTxt = inRank(r, c.k)
        ? `${rk}위/${rank[c.k].n}, ${c.metric.dir === 1 ? "높을수록" : "낮을수록"} 좋음, 최고 ${rank[c.k].best} = ${esc(bestRow ? bestRow.label : "-")}`
        : `<span class="small">대조군(상한): 백본 순위 비교에서 제외</span>`;
      const d = `<b>${esc(c.label)}</b> = ${v} (${rkTxt})<br>${esc(METRIC_DEFS[c.k])}${sdDesc}${coDesc}`;
      return { html: `${v.toFixed(2)}${sdTxt}${nTxt}`, desc: d, bold: co };
    }
    if (c.k === "sys") return { html: `<b>Mem0-Classic-OSS</b>${caret("row", r.run, r.label)}`, desc: r.note || r.label, rowHead: true };
    if (c.k === "users") return { html: String(m.n_users), desc: c.desc };
    if (c.k === "backbone") return { html: `${esc(r.backbone)}${r.backbone_effort ? `<br><span class="small muted">${esc(effortShort(r.backbone_effort))}</span>` : ""}`, desc: r.backbone_effort ? `reasoning effort: ${r.backbone_effort}` : c.desc };
    if (c.k === "prompt") return { html: esc(r.prompt), desc: c.desc };
    if (c.k === "retriever") {
      const v = r.retriever || "";
      const bm = /bm25/i.test(v), cut = /절단/.test(v);
      const html = bm ? `<span class="rtr bm">BM25</span>`
        : `<span class="rtr emb${cut ? " cut" : ""}">임베딩${cut ? "·절단" : ""}</span>`;
      return { html, desc: `<b>${esc(v)}</b><br>${c.desc}` };
    }
    if (c.k === "oracle") return { html: r.oracle ? `<span class="orc">${esc(oracleLabel(r.oracle))}</span>` : `<span class="muted">없음</span>`, desc: c.desc };
    if (c.k === "judge") {
      const p = r.pinned_lane;
      // 두 경우를 구분한다. 설계상 고정된 레인(extra_rows)과, 고른 레인이 없어 대체된 것.
      // 후자는 다른 배치라 행 간 비교가 깨지므로 경고로 표시한다.
      if (!p) return { html: esc(judgeShortName), desc: judgeName };
      if (p.fallback) {
        return { html: `${esc(judgeShort(p.judge))} <span class="pinlane fb">⚠</span>`,
          desc: `<b>⚠ 다른 채점 배치로 대체됨</b><br>이 런에는 선택하신 <b>${esc(judgeLabel(p.want_judge))}</b> 라벨이 없어 <b>${esc(judgeLabel(p.judge))}</b>으로 집계했습니다.<br><b>이 행의 R·Acc·Target P·FMR·F1·UPD를 다른 행과 비교하지 마세요.</b> 배치가 다르면 유저 집합과 문항 수가 다릅니다.<br><span class="small">QA C는 반복 채점 레인에서 따로 계산하므로 영향이 없습니다. 두 행을 제대로 대조하려면 상단바에서 양쪽이 다 가진 judge를 고르세요.</span>` };
      }
      return { html: `${esc(judgeShort(p.judge))} <span class="pinlane">📌</span>`,
        desc: `<b>고정 레인 행</b>: 이 행은 상단바의 generator·judge 선택을 따르지 않습니다.<br>답변 생성 레인 자체가 실험 조건이라, 항상 <b>${esc(genLabel(p.generator))}</b> × <b>${esc(judgeLabel(p.judge))}</b>으로만 집계됩니다.<br><span class="small">Stage A 저장소는 런 <b>${esc(p.run)}</b>의 것을 씁니다.</span>` };
    }
    const lat = r.latency;
    return lat
      ? { html: `<div class="lat-bar" style="width:${(lat.ingest_avg_s / maxIngest * 100).toFixed(0)}%"></div><span class="small">${lat.ingest_avg_s}s</span>`,
          desc: `<b>세션 투입 시간</b>: 평균 ${lat.ingest_avg_s}s · 중앙값 ${lat.ingest_p50_s}s · 세션 ${lat.n_sessions}개 실측. 질문 검색은 평균 ${lat.search_avg_ms}ms로 백본 무관` }
      : { html: "–", desc: c.desc };
  }

  const body = rows.map((r) => {
    const m = r.metrics;
    const tds = cols.map((c) => {
      const cell = cellHTML(c, r, m);
      // 첫 칼럼(Memory System) = 행 토글, 나머지 = 개별 칸 토글 (열 토글은 머리글이 담당)
      const attr = cell.rowHead ? `data-rowtoggle="${esc(r.run)}"` : `data-cell="${esc(r.run)}|${esc(c.k)}"`;
      return `<td class="${colCls(c.k)}${cellCls(r.run, c.k)}${cell.rowHead ? " rowhead" : ""}${S.metricsColCols.has(c.k) ? " ccol" : ""}" data-col="${esc(c.k)}" ${attr} data-desc="${esc(cell.desc || c.desc || "")}" ${cell.bold ? 'style="font-weight:800"' : ""}>${cell.html}</td>`;
    }).join("");
    return `<tr class="${S.metricsSelRows.has(r.run) ? "mrow-sel" : ""}${S.metricsColRows.has(r.run) ? " crow" : ""}">${tds}</tr>`;
  }).join("");

  const anySel = S.metricsSelRows.size || S.metricsSelCols.size || S.metricsSelCells.size;
  const nHidden = S.metricsColRows.size + S.metricsColCols.size;
  // 재렌더로 스크롤이 튀지 않도록 보존: 창 폭에 따라 스크롤 주체가 #content일 수도, 문서일 수도 있다
  const scrollTop = $("#content").scrollTop, winY = window.scrollY;
  const restoreScroll = () => { $("#content").scrollTop = scrollTop; window.scrollTo(0, winY); };
  const batchWarn = NZ && NZ.n_batches > 1 && NZ.qa_c > NZ.qa_c_within;
  const noiseBanner = !NZ ? "" : `
    <div class="noisebar" data-desc="사다리의 실측 행(오라클 없음)을 <b>A′ 답변 생성부터 judge 채점까지 통째로</b> 다시 돌린 <b>독립 실행 ${NZ.n_obs}회</b>(${NZ.n_batches}개 배치)에서 잰 값입니다. 회차별: ${NZ.values.join(", ")}. 산출물에서 실시간 계산하므로 반복을 더 돌리면 자동으로 갱신됩니다.">
      <b>📏 노이즈 바닥</b>: 같은 실험을 다시 돌리기만 해도 <b>QA C가 ±${NZ.qa_c}p</b> 흔들립니다
      <span class="small">(유저 ${NZ.n_users}명 · 독립 실행 ${NZ.n_obs}회 실측 · 최대 폭 ${NZ.qa_c_range}p)</span>.
      <b>이보다 작은 행 간 차이는 해석하지 마세요</b>: 1위와 구분 한계 이내인 행은 <b>공동 1위로 함께 굵게</b> 표시됩니다.
      ${batchWarn ? `<br><span class="small">⚠ 연속 루프로 돌린 ${NZ.n_repeats}회만 보면 ±${NZ.qa_c_within}p로 <b>작게 나오지만</b>, 날짜가 다른 배치를 섞으면 ±${NZ.qa_c}p입니다. 한 루프 안의 회차들은 서버 상태를 공유해 독립 시행이 아닙니다. <b>보수적인 쪽(±${NZ.qa_c}p)을 씁니다.</b></span>` : ""}
      <span class="small">흔드는 쪽은 채점이 아닌 <b>답변 생성</b>입니다 (judge만 반복하면 폭 0.6p).</span>
    </div>`;
  // 선택한 judge 라벨이 없어 다른 배치로 대체된 행. 표 안에 배치가 섞이면 행 간 비교가 깨진다.
  // 칸의 ⚠ 하나로는 눈에 안 띄어서 표 위에 따로 띄운다 (2026-08-19: BM25 행이 4유저 배치,
  // 임베딩 행이 1유저 배치로 잡혀 UPD를 잘못 대조한 사고가 있었다).
  const fb = rows.filter((r) => r.pinned_lane && r.pinned_lane.fallback);
  const laneWarn = !fb.length ? "" : `
    <div class="noisebar warn" data-desc="런마다 채점을 돌린 레인이 다릅니다. 어떤 런은 1유저 배치만, 어떤 런은 4유저 배치만 있습니다. 상단바에서 고른 레인이 없는 런은 표에서 빼는 대신 그 런이 가진 다른 레인으로 집계합니다.">
      <b>⚠ 이 표에 다른 채점 배치가 섞여 있습니다</b>: ${fb.length}개 행이 선택한 judge(<b>${esc(judgeName)}</b>) 라벨이 없어 대체됐습니다.
      <span class="small">${fb.map((r) => `${esc(r.label || r.run)} → <b>${esc(judgeLabel(r.pinned_lane.judge))}</b>`).join(" · ")}</span>
      <br><span class="small"><b>대체된 행과 나머지 행의 R·Acc·Target P·FMR·F1·UPD를 비교하지 마세요.</b> 배치가 다르면 유저 집합과 문항 수가 다릅니다. QA C는 반복 채점 레인에서 따로 계산하므로 영향이 없습니다. 제대로 대조하려면 상단바에서 <b>양쪽이 다 가진 judge</b>를 고르세요.</span>
    </div>`;
  $("#content").innerHTML = `
    <div id="ladder-card"></div>
    ${noiseBanner}${laneWarn}
    <div class="hint">HaluMem Table 3 지표: judge 레코드에서 <b>공식 집계 함수로 실시간 산출</b> (문서 테이블과 동일 수치). 범위: ${S.metricsScope === "first4" ? "전 실험 공통 첫 4유저" : S.metricsScope === "all" ? "런별 전체 유저 (유저 수 다름 주의)" : "유저 " + esc(nameOf(S.metricsScope)) + " 1명"} · judge=${judgeName}
      · 첫 칸 클릭=행, 머리글 클릭=열, 나머지 칸 클릭=그 칸만 하이라이트 · <b>▾</b>=접기(접힌 줄 클릭=펼침) · 칼럼 경계 드래그=폭 조절 <button id="msel-clear" class="ctx-toggle${anySel ? "" : " btn-off"}" style="margin-left:6px">하이라이트 해제</button> <button id="mhide-clear" class="ctx-toggle${nHidden ? "" : " btn-off"}" style="margin-left:6px">접힌 항목 <b id="nfold">${nHidden}</b>개 모두 펼치기</button></div>
    <div class="card"><div class="body" style="overflow-x:auto">
      <table class="cmp resizable" id="metrics-table"><tr>
        ${cols.map((c) => `<th class="${colCls(c.k)}${S.metricsColCols.has(c.k) ? " ccol" : ""}" data-col="${esc(c.k)}" data-desc="${esc(c.desc || "")}<br>클릭=열 하이라이트 · ▾=열 숨기기">${esc(c.label)}${caret("col", c.k, c.label)}</th>`).join("")}
      </tr>${body}</table></div></div>
    ${rows.length < data.rows.length ? `<p class="hint">⚠ ${data.rows.length - rows.length}개 런은 이 judge(${judgeName}) 라벨이 없어 표시 제외</p>` : ""}`;
  restoreScroll();
  requestAnimationFrame(restoreScroll);          // 레이아웃 확정 후 한 번 더
  // 사다리는 비동기로 채워지며 높이를 바꾼다. 그 사이 사용자가 스크롤했다면 건드리지 않는다
  // (늦은 복원이 사용자의 스크롤을 되감는 것을 방지).
  // ⚠ 사다리 렌더를 requestAnimationFrame 안에 두면 안 된다. 백그라운드 탭에서는 rAF가 아예
  //    호출되지 않아 사다리가 영영 비어 있게 된다(탭을 숨긴 채 대시보드를 열면 재현). 즉시 호출하고,
  //    스크롤 보정만 완료 후에 한다.
  const settled = () => [$("#content").scrollTop, window.scrollY];
  const [c0, w0] = settled();
  renderLadder().then(() => {
    const [c1, w1] = settled();
    if (Math.abs(c1 - c0) < 2 && Math.abs(w1 - w0) < 2) restoreScroll();
  });

  // ⚠ 표 조작(하이라이트·접기)은 절대 재렌더하지 않는다. 표를 다시 그리면 스크롤이 튄다.
  //    모두 제자리에서 클래스만 토글하고, 상태 집합은 다음 재렌더(범위 변경 등) 때 복원용으로만 쓴다.
  const sel = (q) => $$(`#content ${q}`);
  const syncBtns = () => {
    const any = S.metricsSelRows.size || S.metricsSelCols.size || S.metricsSelCells.size;
    $("#msel-clear")?.classList.toggle("btn-off", !any);
    const n = S.metricsColRows.size + S.metricsColCols.size;
    $("#mhide-clear")?.classList.toggle("btn-off", !n);
    const el = $("#nfold"); if (el) el.textContent = String(n);
  };
  const rowEl = (run) => sel(`td[data-rowtoggle="${CSS.escape(run)}"]`)[0]?.closest("tr");

  const toggleRow = (run) => {
    const on = S.metricsSelRows.has(run);
    on ? S.metricsSelRows.delete(run) : S.metricsSelRows.add(run);
    rowEl(run)?.classList.toggle("mrow-sel", !on);
    syncBtns();
  };
  const toggleCol = (k) => {
    const on = S.metricsSelCols.has(k);
    on ? S.metricsSelCols.delete(k) : S.metricsSelCols.add(k);
    sel(`[data-col="${CSS.escape(k)}"]`).forEach((el) => el.classList.toggle("mcol-sel", !on));
    syncBtns();
  };
  const toggleCell = (key, td) => {
    const on = S.metricsSelCells.has(key);
    on ? S.metricsSelCells.delete(key) : S.metricsSelCells.add(key);
    td.classList.toggle("mcell-sel", !on);
    syncBtns();
  };
  // 접기/펼치기: DOM은 그대로 두고 클래스만 (행은 얇은 줄, 열은 좁은 띠로 축소)
  const foldRow = (run, on) => {
    on ? S.metricsColRows.add(run) : S.metricsColRows.delete(run);
    rowEl(run)?.classList.toggle("crow", on);
    syncBtns();
  };
  const foldCol = (k, on) => {
    on ? S.metricsColCols.add(k) : S.metricsColCols.delete(k);
    sel(`[data-col="${CSS.escape(k)}"]`).forEach((el) => el.classList.toggle("ccol", on));
    syncBtns();
  };

  sel("td[data-rowtoggle]").forEach((td) => (td.onclick = (e) => {
    if (e.target.closest(".hidecaret")) return;
    const run = td.dataset.rowtoggle;
    if (S.metricsColRows.has(run)) return foldRow(run, false);   // 접힌 행 클릭 = 펼침
    toggleRow(run);
  }));
  sel("th[data-col]").forEach((th) => (th.onclick = (e) => {
    if (e.target.closest(".hidecaret") || e.target.closest(".colrz")) return;
    const k = th.dataset.col;
    if (S.metricsColCols.has(k)) return foldCol(k, false);       // 접힌 열 클릭 = 펼침
    toggleCol(k);
  }));
  sel("td[data-cell]").forEach((td) => (td.onclick = () => {
    const k = td.dataset.col;
    if (S.metricsColCols.has(k)) return foldCol(k, false);
    const run = td.dataset.cell.split("|")[0];
    if (S.metricsColRows.has(run)) return foldRow(run, false);
    toggleCell(td.dataset.cell, td);
  }));
  sel(".hidecaret").forEach((b) => (b.onclick = (e) => {
    e.stopPropagation(); e.preventDefault(); b.blur();           // 포커스 이동으로 인한 스크롤 방지
    b.dataset.fold === "row" ? foldRow(b.dataset.id, true) : foldCol(b.dataset.id, true);
  }));
  $("#msel-clear") && ($("#msel-clear").onclick = () => {
    S.metricsSelRows.clear(); S.metricsSelCols.clear(); S.metricsSelCells.clear();
    sel(".mrow-sel").forEach((el) => el.classList.remove("mrow-sel"));
    sel(".mcol-sel").forEach((el) => el.classList.remove("mcol-sel"));
    sel(".mcell-sel").forEach((el) => el.classList.remove("mcell-sel"));
    syncBtns();
  });
  $("#mhide-clear") && ($("#mhide-clear").onclick = () => {
    [...S.metricsColRows].forEach((r) => foldRow(r, false));
    [...S.metricsColCols].forEach((k) => foldCol(k, false));
  });
  initColResize($("#metrics-table"));
}

/* ----- 표 칼럼 폭 드래그 조절 ----- */

// 칼럼 경계선을 잡고 끌어 폭을 바꾼다. 폭은 표 단위로 localStorage에 남아 다음 방문에도 유지됨.
function initColResize(table) {
  if (!table) return;
  const key = `colw_${table.id}`;
  const saved = JSON.parse(localStorage.getItem(key) || "null");
  const ths = [...table.querySelectorAll("tr:first-child th")];
  if (!ths.length) return;

  // table-layout: fixed로 바꾸려면 초기 폭이 픽셀로 박혀 있어야 한다 (현재 렌더 폭을 그대로 채택)
  const widths = saved && saved.length === ths.length ? saved : ths.map((th) => Math.round(th.getBoundingClientRect().width));
  const apply = () => { ths.forEach((th, i) => (th.style.width = `${widths[i]}px`)); };
  table.style.tableLayout = "fixed";
  table.style.width = `${widths.reduce((a, b) => a + b, 0)}px`;
  apply();

  ths.forEach((th, i) => {
    if (i === ths.length - 1) return;  // 마지막 칼럼은 오른쪽 경계가 없음
    const grip = document.createElement("span");
    grip.className = "colrz";
    grip.title = "드래그해서 칼럼 폭 조절 (더블클릭=초기화)";
    grip.onmousedown = (e) => {
      e.preventDefault(); e.stopPropagation();
      grip.classList.add("on");
      const x0 = e.clientX, w0 = widths[i];
      const move = (ev) => {
        widths[i] = Math.max(44, w0 + (ev.clientX - x0));
        table.style.width = `${widths.reduce((a, b) => a + b, 0)}px`;
        apply();
      };
      const up = () => {
        grip.classList.remove("on");
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        localStorage.setItem(key, JSON.stringify(widths));
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    };
    grip.ondblclick = (e) => { e.stopPropagation(); localStorage.removeItem(key); renderMetrics(); };
    th.appendChild(grip);
  });
}

/* ----- 단계별 오라클 상한 사다리 ----- */

const LADDER_STAGES = ["extraction", "update", "retrieval"];

async function renderLadder() {
  if (!$("#ladder-card")) return;
  let d;
  try { d = await api(`/api/oracle-ladder?scope=${S.metricsScope}`); }
  catch { const b = $("#ladder-card"); if (b) b.innerHTML = ""; return; }
  // ⚠ await 사이에 renderMetrics가 다시 돌면 #content가 통째로 교체돼 앞서 잡아둔 노드가 떨어져 나간다.
  //    그 노드에 쓰면 화면에는 아무것도 안 보인다. 반드시 await '이후' 다시 조회한다.
  const box = $("#ladder-card");
  if (!box) return;
  const lads = d.ladders || [{ key: "", label: "", note: d.note, n_repeats: d.n_repeats, rows: d.rows }];
  if (!lads.length || !lads.some((L) => L.rows.length)) { box.innerHTML = ""; return; }
  // 사다리를 retriever 종류마다 한 벌씩 그린다. 막대 스케일은 전 사다리 공통이라야 눈으로 비교된다.
  const allDone = lads.flatMap((L) => L.rows).filter((r) => r.qa_c != null);
  const max = Math.max(...allDone.map((r) => r.qa_c), 100);

  const ladderTable = (L) => {
  const done = L.rows.filter((r) => r.qa_c != null);
  const rows = L.rows.map((r, i) => {
    const stages = LADDER_STAGES.map((s) => {
      const on = r.stages.includes(s);
      return `<td class="lst ${on ? "on" : ""}" data-desc="${esc(d.stage_names[s])} 단계: ${on ? "이 행에서는 정답으로 대체됨(오라클)" : "실제 시스템이 수행"}">${on ? "오라클" : "실측"}</td>`;
    }).join("");
    const sd = r.sd != null ? `<span class="lsd" data-desc="${r.repeats.length}회 반복(A′ 생성 + judge 채점을 매번 새로) 표본표준편차: 각 회차: ${r.repeats.map((x) => x.toFixed(2)).join(", ")}">±${r.sd.toFixed(2)}</span>` : "";
    const nrep = r.repeats && r.repeats.length ? `<span class="lrep" data-desc="반복 회차 수: 평균값으로 표시됩니다">n=${r.repeats.length}</span>` : "";
    const bar = r.qa_c == null ? `<span class="small muted">미실행</span>`
      : `<div class="lbar"><span style="width:${(r.qa_c / max * 100).toFixed(1)}%"></span>${r.sd != null ? `<i class="lerr" style="left:${((r.qa_c - r.sd) / max * 100).toFixed(1)}%;width:${(2 * r.sd / max * 100).toFixed(1)}%"></i>` : ""}</div><b>${r.qa_c.toFixed(2)}</b>${sd}${nrep}`;
    const dl = r.delta == null ? "" : `<span class="ldelta ${r.delta >= 0 ? "up" : "down"}">${r.delta >= 0 ? "+" : ""}${r.delta.toFixed(2)}</span>`;
    return `<tr class="${r.qa_c == null ? "pend" : ""}">
      <td class="lstep" data-desc="${esc(r.desc)}"><b>${esc(r.label)}</b><br><span class="small muted">${esc(r.run_label || r.run)}</span></td>
      ${stages}
      <td class="lqa">${bar}${r.n_users != null ? `<span class="small muted" data-desc="이 행이 실제로 채점한 유저 수: 모든 행이 같아야 공정한 비교입니다">${r.n_users}u</span>` : ""}</td>
      <td class="lgain" data-desc="직전 단계 대비 QA Correct 증가분: 이 단계를 완벽하게 만들었을 때 얻는 성능">${dl}</td></tr>`;
  }).join("");

  const first = done[0], last = done[done.length - 1];
  const gap = first && last && first !== last ? (last.qa_c - first.qa_c).toFixed(2) : null;
  return `
    <div class="card"><h4 data-desc="mem0 파이프라인의 각 단계를 차례로 '완벽한 정답'으로 대체하며 QA 상한을 잽니다. 행 사이의 증가분이 곧 그 단계의 기여분입니다">
        단계별 오라클 상한 사다리${L.label ? ` <span class="lkey">${esc(L.label)}</span>` : ""}${gap ? ` <span class="small" style="text-transform:none;color:var(--accent);font-weight:800">실측 → 상한 +${gap}p</span>` : ""}</h4>
      <div class="body">
        <table class="cmp ladder"><tr>
          <th>세팅</th><th data-desc="fact 추출 단계">추출</th><th data-desc="ADD/UPDATE/DELETE 갱신 결정 단계">갱신</th>
          <th data-desc="저장소에서 답변 재료를 찾아오는 단계">저장·검색</th>
          <th data-desc="QA Correct: 이 사다리에서 유일하게 의미 있는 지표. 오라클 행은 저장물이 골든 자체라 R·Acc는 100 근처로 붙어 무의미합니다">QA C↑</th>
          <th>직전 대비</th></tr>${rows}</table>
        <p class="small muted" style="margin-top:8px">${L.n_repeats ? `반복 ${L.n_repeats}회 설계 (A′ 생성·judge 채점을 매번 새로 수행, 평균±표준편차) · ` : ""}${esc(L.note)} · 남은 구간(상한 → 100)은 generator 자체와 문항·정답 결함의 몫입니다.</p>
        <p class="small" style="margin-top:4px;color:#8a5600;background:#fff4e6;border-left:3px solid var(--warn);padding:5px 9px;border-radius:0 5px 5px 0" data-desc="마지막 행은 검색 결과 대신 정답 근거(evidence)만 제공합니다. 평균 1.4개 항목으로, 실제 검색(top-20)보다 훨씬 짧고 깨끗한 컨텍스트입니다. 따라서 이 구간의 증가분에는 '검색 정확도'와 '컨텍스트 축약·무관정보 제거' 효과가 섞여 있습니다">
          ⚠ 마지막 행은 <b>정답 근거만 남긴 조건</b>(평균 1.4개 항목)이라 실제 검색(top-20)과 컨텍스트 조건이 다릅니다. 이 구간 증가분은 검색의 기여를 <b>과대평가</b>합니다.</p>
      </div></div>`;
  };

  box.innerHTML = lads.map(ladderTable).join("");
}

/* ----- Digest ----- */

async function renderDigest() {
  const all = await api(`/api/digest/${S.uuid}`);
  // 기본은 현재 세팅(런·generator·judge·B런)에서 단 코멘트만: 토글 켜면 전체 런·전체 세팅
  const base = S.showOtherCmts ? all : all.filter((c) => c.run === S.run && cmtMatches(c));
  const hiddenN = all.length - base.length;
  const scoped = S.digestScope === "session"
    ? base.filter((c) => c.run === S.run && (c.anchor === `session:${S.session}` || c.anchor.startsWith(`session:${S.session}/`)))
    : base;
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted">유저 <b>${esc(S.bundle.user_name)}</b> 코멘트</p>
    <div class="pill-filter" style="margin:0 0 8px">
      <button class="${S.digestScope === "user" ? "on" : ""}" data-sc="user">유저 전체</button>
      <button class="${S.digestScope === "session" ? "on" : ""}" data-sc="session">현재 세션 S${S.session}</button></div>
    <label class="small" style="display:flex;align-items:center;gap:5px;margin:0 0 8px;cursor:pointer"
      data-desc="기본은 지금 보고 있는 세팅(런·generator·judge·B런)에서 단 코멘트만. 켜면 다른 런·다른 세팅의 코멘트도 회색으로 함께 표시">
      <input type="checkbox" id="digest-other-tgl" ${S.showOtherCmts ? "checked" : ""}>
      다른 세팅 코멘트 표시${!S.showOtherCmts && hiddenN ? ` (숨김 ${hiddenN})` : ""}</label>
    <a href="/api/export/${S.uuid}" target="_blank" data-desc="이 유저의 코멘트 전체(모든 세팅 포함)를 Markdown으로 다운로드">📄 Markdown export</a></div>`;
  $$("#sidebar .pill-filter button").forEach((b) => (b.onclick = () => { S.digestScope = b.dataset.sc; renderDigest(); }));
  $("#digest-other-tgl").onchange = () => setShowOtherCmts($("#digest-other-tgl").checked);
  if (!scoped.length) {
    $("#content").innerHTML = `<div class="card"><h4>Digest: 코멘트 모아보기</h4><div class="body">
      <p>선택 범위(${S.digestScope === "user" ? "유저 전체" : `세션 S${S.session}`}${S.showOtherCmts ? "" : " · 현재 세팅"})에 코멘트가 없습니다.${!S.showOtherCmts && hiddenN ? ` 다른 세팅 코멘트 ${hiddenN}개가 숨겨져 있습니다. 좌측 토글로 표시.` : ""}</p>
      <p class="small muted">코멘트 남기기: Sessions/QA 탭에서 항목 클릭 → 우측 코멘트 탭. 또는 텍스트 드래그 선택 → 💬 버튼.<br>
      세션/유저 단위 종합 코멘트는 항목 클릭 없이 우측 코멘트 탭에서 바로 작성하면 됩니다 (앵커 run/session).</p></div></div>`;
    return;
  }
  const byRun = {};
  scoped.forEach((c) => (byRun[c.run] = byRun[c.run] || []).push(c));
  $("#content").innerHTML = Object.entries(byRun).map(([run, list]) => `
    <div class="card"><h4>${esc(runLabel(run))} (${list.length})</h4><div class="body">
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
  $("#cmt-count").textContent = visibleComments().filter((c) => c.anchor === S.anchor).length || "";
  const el = $("#insp-body");
  if (S.itab === "detail") {
    // 골든·QA 앵커는 런마다 원본이 다름 (judge 라벨·갱신 골든의 검색 스냅샷·답변·context)
    // -> B 비교 중엔 Trace 탭처럼 A/B 알약으로 전환해서 봄. 전환 상태는 Trace와 공유(S.traceSide)
    const m = S.anchor.match(/^session:(\d+)\/(mp|qa):(\d+)$/);
    let pills = "", sideChip = anchorSideChip(S.anchor), obj = S.anchorObj;
    if (m && S.bundleB) {
      const side = S.traceSide === "B" ? "B" : "A";
      if (side === "B") {
        const sid = +m[1], idx = +m[3];
        if (m[2] === "mp") {
          obj = bGolden(sid, idx, S.anchorObj?.memory_content);
        } else {
          const sb = S.bundleB.sessions.find((x) => x.session_id === sid);
          obj = sb?.questions?.find((x) => x.question === S.anchorObj?.question) ?? null;
        }
      }
      pills = `<div class="pill-filter" style="margin:8px 0 0" data-desc="상세 JSON을 볼 세팅 선택: 골든/QA는 데이터셋 공통이지만 judge 라벨·검색 스냅샷·답변·context는 런마다 다름">
        <button class="${side === "A" ? "on" : ""}" data-dside="A">A: ${esc(runLabel(S.run))}</button>
        <button class="${side === "B" ? "on" : ""}" data-dside="B">B: ${esc(runLabel(S.runB))}</button></div>`;
      sideChip = "";  // 알약이 세팅 표시를 대신함
    }
    el.innerHTML = `<div class="anchor-label" data-desc="${esc(S.anchor)}">앵커: ${esc(anchorHuman(S.anchor))}</div> ${sideChip}${pills}
      <div class="jt" style="margin-top:8px">${obj != null ? jsonTree(obj) : "<p class='small muted'>B 런에 이 항목이 없습니다 (유저/세션 미포함)</p>"}</div>`;
    $$("button[data-dside]", el).forEach((b) => (b.onclick = () => { S.traceSide = b.dataset.dside; renderInspector(); }));
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
    const think = r.llm?.reasoning ? ` · 🧠 사고 ${String(r.llm.reasoning).length.toLocaleString()}자` : "";
    return (facts || (r.llm?.response || "").slice(0, 60)) + think;
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
  // B 비교 중엔 어느 세팅의 trace를 볼지 선택 (B 요소 클릭 시 자동으로 B로 전환됨)
  const side = S.runB && S.traceSide === "B" ? "B" : "A";
  const traceRun = side === "B" ? S.runB : S.run;
  const pills = S.runB ? `<div class="pill-filter" style="margin:0 0 8px" data-desc="Trace를 볼 세팅 선택: trace는 Stage A 산물이라 런마다 별도">
      <button class="${side === "A" ? "on" : ""}" data-side="A">A: ${esc(runLabel(S.run))}</button>
      <button class="${side === "B" ? "on" : ""}" data-side="B">B: ${esc(runLabel(S.runB))}</button></div>` : "";
  const bindPills = () => $$(".pill-filter button[data-side]", el).forEach((b) =>
    (b.onclick = () => { S.traceSide = b.dataset.side; renderInspector(); }));
  el.innerHTML = pills + "<p class='muted'>trace 로딩…</p>";
  bindPills();
  const cacheKey = `${traceRun}|${S.session}`;
  let recs = S.traceCache.get(cacheKey);
  if (!recs) {
    try { recs = await api(`/api/trace/${traceRun}/${S.uuid}?session=${S.session}`); }
    catch { el.innerHTML = pills + `<p class='muted'>${side} 런(${esc(runLabel(traceRun))})에는 trace가 없습니다</p>`; bindPills(); return; }
    S.traceCache.set(cacheKey, recs);
  }
  el.innerHTML = pills + `<div class="anchor-label">S${S.session} trace${S.runB ? ` · ${side}=${esc(runLabel(traceRun))}` : ""}: ${recs.length}건 (시간순) · 클릭하면 펼침 (한 번에 하나)</div>` +
    recs.map((r, i) => `<div class="tr-rec" data-i="${i}">
      <div class="hdr" data-k="${esc(r.event)}">
        <span class="small muted">#${r.seq}</span>
        <b>${esc(r.event === "llm_call" ? r.purpose : r.event)}</b>
        <span class="tagchip">${esc(r.stage || "")}</span>
        <span class="summ">${esc(traceSummary(r))}</span>
        <span class="small muted">${Math.round(r.duration_ms || 0)}ms</span></div>
      <div class="x hidden"></div></div>`).join("");
  bindPills();
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
      highlightTraceTarget(r, side);
    };
  });
}

function clearTraceHighlight() { $$(".hl-tr").forEach((el) => el.classList.remove("hl-tr")); }

function highlightTraceTarget(r, side = "A") {
  if (S.tab !== "sessions") return;
  const s = S.bundle.sessions.find((x) => x.session_id === S.session);
  if (!s) return;
  // B trace를 보는 중이면 추출 매칭은 B 카드([data-b-ext])에: 골든·질문은 데이터셋 공통이라 그대로
  const sB = side === "B" ? S.bundleB?.sessions.find((x) => x.session_id === S.session) : null;
  const extList = side === "B" ? (sB?.extracted || []) : s.extracted;
  const extSel = (i) => side === "B" ? `[data-b-ext="${i}"]` : `[data-a="ext:${i}"]`;
  const targets = [];
  const findExt = (pred) => extList.forEach((m, i) => { if (pred(m)) targets.push(extSel(i)); });
  const findRow = (kind, pred, list) => {
    list.forEach((item, i) => { if (pred(item)) targets.push(`[data-a="${kind}:${i}"]`); });
  };
  if (r.event === "retrieval" && r.stage === "ingest" && r.retrieval?.query) {
    findExt((m) => m.text === r.retrieval.query);
  } else if (r.event === "retrieval" && r.stage === "qa_retrieval" && r.ref?.question) {
    s.questions.forEach((q, i) => { if (q.question === r.ref.question) targets.push(`[data-qa="${i}"]`); });
  } else if (r.event === "retrieval" && r.stage === "update_probe" && r.ref) {
    const refText = Object.values(r.ref)[0];
    findRow("mp", (mp) => mp.memory_content === refText, s.golden);
  } else if (r.event === "memory_write") {
    (r.writes || []).forEach((w) => findExt((m) => m.text === w.text));
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
    const L = r.llm;
    // reasoning 모델은 사고 과정이 message.reasoning으로 분리 반환된다 (vLLM --reasoning-parser).
    // 옛 런에는 이 필드가 없다. tracing.py에 기록이 추가된 이후 런부터 존재.
    const meta = [
      L.finish_reason ? `finish_reason=${L.finish_reason}` : "",
      L.completion_tokens != null ? `출력 ${L.completion_tokens.toLocaleString()} tok` : "",
      L.reasoning_tokens != null ? `그중 사고 ${L.reasoning_tokens.toLocaleString()} tok` : "",
    ].filter(Boolean).join(" · ");
    return r.llm.messages.map((m) => `<p class="small"><b>${esc(m.role)}</b></p><pre class="mono">${esc(m.content)}</pre>`).join("") +
      (L.reasoning ? `<p class="small"><b data-desc="모델이 답을 내기 전 생성한 사고 과정: 응답 본문(content)에는 포함되지 않습니다">reasoning (사고 과정)</b> <span class="small muted">${esc(String(L.reasoning).length.toLocaleString())}자</span></p><pre class="mono reason">${esc(L.reasoning)}</pre>` : "") +
      `<p class="small"><b>response</b>${meta ? ` <span class="small muted">${esc(meta)}</span>` : ""}</p><pre class="mono">${esc(L.response)}</pre>`;
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
  // ① 대상 세팅(런): 가장 중요: 이 코멘트가 어떤 모델/세팅의 요소를 가리키는가.
  //    A쪽 앵커(mp/ext/turn/qa/session)는 c.run, B쪽 앵커(extb)는 c.run_b가 대상
  const isB = c.anchor.includes("/extb:");
  const target = isB ? (c.run_b || null) : c.run;
  const targetChip = `<span class="tagchip" style="font-weight:800; color:${isB ? "var(--bcol)" : "var(--accent)"}"
    data-desc="이 코멘트의 대상 세팅(런): ${esc(target ? runLabel(target) : "당시 비교(B) 쪽 요소에 단 코멘트인데 B 런 기록이 없는 구버전 코멘트")}${isB && target ? ": 당시 비교(B) 쪽 요소에 단 코멘트" : ""}">▶ ${esc(target ? runLabel(target) : "B 런 (기록 없음)")}</span>`;
  // ② 작성 당시 관측 스택 (generator/judge: 구 코멘트는 빈 값이라 생략)
  //    비교 상대는 대상의 반대편: B에 단 코멘트면 상대=A(c.run), A에 단 코멘트면 상대=B(c.run_b)
  const counterpart = isB ? c.run : c.run_b;
  const cpTxt = counterpart ? ` (당시 비교 상대 ${isB ? "A" : "B"}=${esc(runLabel(counterpart))})` : "";
  const ctxBits = [];
  if (c.generator) ctxBits.push(`gen=${genLabel(c.generator)}`);
  if (c.judge) ctxBits.push(`judge=${judgeLabel(c.judge)}`);
  const ctxChip = ctxBits.length
    ? `<span class="tagchip" data-desc="작성 당시 관측 스택: 답변·라벨이 이 generator/judge 기준이었음${cpTxt}">${esc(ctxBits.join(" · "))}</span>`
    : `<span class="tagchip" data-desc="generator/judge 기록이 없는 구버전 코멘트: 어떤 세팅에서 봤는지 재구성 불가">세팅 기록 없음</span>`;
  const other = !cmtMatches(c);  // 다른 세팅에서 단 코멘트 → 점선 테두리 + 흐리게
  return `<div class="cmt${other ? " other" : ""}"><div class="meta"><span class="author">${esc(c.author)}</span>
    ${c.tag ? `<span class="tagchip">${esc(c.tag)}</span>` : ""}
    ${targetChip}<span data-desc="${esc(c.anchor)}">${esc(anchorHuman(c.anchor))}</span>${ctxChip}<span>${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</span>
    ${canGoto && c.anchor.startsWith("session:") ? `<button class="del goto" style="color:var(--accent)" data-anchor="${esc(c.anchor)}">이동</button>` : ""}
    ${mine ? `<button class="del" data-id="${c.id}">삭제</button>` : ""}</div>
    ${c.quote ? `<div class="cmt-quote">“${esc(c.quote)}”</div>` : ""}
    <div class="body-text">${esc(c.body)}</div></div>`;
}

function renderComments(el) {
  const vis = visibleComments();
  const here = vis.filter((c) => c.anchor === S.anchor);
  const others = vis.filter((c) => c.anchor !== S.anchor);
  const hiddenN = S.comments.length - vis.length;
  el.innerHTML = `
    <div class="anchor-label" data-desc="${esc(S.anchor)}">앵커: ${esc(anchorHuman(S.anchor))}</div> ${anchorSideChip(S.anchor)}
    <label class="small" style="display:flex;align-items:center;gap:5px;margin:6px 0;cursor:pointer"
      data-desc="기본은 지금 보고 있는 세팅(generator·judge·B런)에서 단 코멘트만 표시. 켜면 다른 세팅에서 단 코멘트도 회색으로 함께 표시">
      <input type="checkbox" id="cmt-other-tgl" ${S.showOtherCmts ? "checked" : ""}>
      다른 세팅 코멘트 표시${!S.showOtherCmts && hiddenN ? ` (숨김 ${hiddenN})` : ""}</label>
    <div id="cmt-form" style="margin:8px 0">
      ${S.pendingQuote ? `<div class="cmt-quote">“${esc(S.pendingQuote)}” <button id="quote-clear" style="border:none;background:none;cursor:pointer;color:var(--bad)">×</button></div>` : ""}
      <textarea id="cmt-body" placeholder="관찰/해석을 남겨주세요 (관찰 → 유형 태그 → 시사점)"></textarea>
      <div style="display:flex;gap:6px;margin-top:4px">
        <select id="cmt-tag"><option value="">태그 없음</option><option>강점</option><option>약점</option><option>병목</option><option>judge오판</option><option>추출누락</option><option>재작성drift</option><option>기타</option></select>
        <button class="primary" id="cmt-add">등록</button></div></div>
    <h4 class="small muted">이 앵커 (${here.length})</h4>${here.map((c) => cmtHTML(c)).join("") || "<p class='small muted'>없음</p>"}
    <h4 class="small muted">이 유저의 다른 앵커 (${others.length})</h4>${others.map((c) => cmtHTML(c, true)).join("")}`;
  $("#cmt-other-tgl").onchange = () => setShowOtherCmts($("#cmt-other-tgl").checked);
  $("#quote-clear") && ($("#quote-clear").onclick = () => { S.pendingQuote = ""; renderComments(el); });
  $("#cmt-add").onclick = async () => {
    const body = $("#cmt-body").value.trim();
    if (!body) return;
    if (!S.author) { askName(); return; }
    await api("/api/comments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run: S.run, uuid: S.uuid, anchor: S.anchor, author: S.author,
        tag: $("#cmt-tag").value, body, quote: S.pendingQuote,
        // 작성 당시 관측 세팅 기록: 나중에 어떤 generator/judge 라벨을 보며 단 코멘트인지 재구성 가능
        generator: S.generator, judge: S.judge, run_b: S.runB || "",
      }),
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

/* ---------- 판정 검토 (judge 입력 재현 + 분석가 주석) ---------- */

// judge가 실제로 출력하는 라벨 문자열과 동일하게 맞춤 (비교·집계 일관성)
const LABEL_SETS = {
  integrity: [["2", "완전 포함", "골든의 모든 핵심 정보가 추출 메모리에 담김"], ["1", "부분 포함", "일부만 담김"], ["0", "미포함", "추출 메모리에 없음"]],
  accuracy: [["2", "전부 근거 있음", "이 메모리의 모든 내용이 대화·골든에 근거"], ["1", "부분 근거", "일부만 근거 있음"], ["0", "근거 없음/모순", "대화에 없거나 모순됨"]],
  update: [["Correct", "Correct", "갱신본의 모든 원자 정보·수치가 정확"], ["Hallucination", "Hallucination", "틀린 값을 만들어냄"], ["Omission", "Omission", "디테일을 누락"], ["Other", "Other", "위 셋 중 어디에도 명확히 속하지 않는 갱신 실패 (judge 프롬프트의 4번째 분류)"]],
  qa: [["Correct", "Correct", "정답과 의미가 완전히 동등. ⚠ 정답이 '알 수 없음'인데 시스템도 추측 없이 모른다고 답하면 Correct"], ["Hallucination", "Hallucination", "날조·모순. 정답이 '알 수 없음'인데 단정적 사실을 답한 경우도 포함"], ["Omission", "Omission", "날조는 없으나 필요한 요소를 누락 (다요소 질문은 하나만 빠져도 Omission)"]],
  // 벤치마크 자체 검수: '이 문항의 골든 정답이 타당한가'를 본다 (judge 판정 검토와 별개).
  // ⚠ 세 라벨은 '얼마나 나쁜가'로 갈리지 않는다. **조치**로 갈린다:
  //    그대로 씀 / 정답·채점을 고침 / 문항을 뺌.
  //    '근거 없음'만 유일하게 달성 가능한 QA 상한을 깎으므로 반드시 따로 센다.
  //    (구 라벨 ambiguous는 wrong으로 접어 집계한다. 조치가 같아 쪼갤 실익이 없었음)
  // ⚠ 이 화면은 judge를 평가하지 않는다. 골든이 멀쩡한데 judge만 틀렸으면 valid다.
  //    judge 품질은 판정 검토 화면이 따로 본다.
  //    (2026-08-18: 설명에서 개별 사례 예시를 전부 뺐다. 예시를 달면 그 사례가 정말
  //     그 갈래인지 따지게 되고, 하나만 틀려도 라벨링을 오도한다. 실제로 채점 오류
  //     사례를 wrong 예시로 달아뒀다가 분석가 제보로 발견했다. 사례 분석은 문서에 둔다:
  //     backbone-experiment.md §13. 여기에는 판단 규칙만 둔다)
  gold_qa: [["valid", "타당", "대화에 근거가 있고, 정답 표현도 이것 하나로 좁혀진다. 이대로 채점 기준으로 쓸 수 있다.<br><b>judge가 오답 처리했더라도 골든 자체가 멀쩡하면 '타당'이다.</b> 이 화면은 judge를 평가하지 않는다"],
            ["wrong", "골든 정답이 잘못됨", "<b>대화에 근거는 있는데 골든을 그대로 채점 기준으로 쓸 수 없다.</b> 골든 값이 대화 내용과 어긋나거나, 똑같이 타당한 다른 표현이 있는데 골든이 그중 하나만 인정하는 경우다.<br><b>→ 옆의 대안 정답 칸에 올바른(또는 함께 인정해야 할) 답을 적어주세요.</b><br>골든은 멀쩡한데 judge만 틀린 경우는 여기가 아니라 '타당'이다"],
            ["unanswerable", "대화에 근거 없음", "<b>그 정보가 대화에 아예 없다.</b> 어떤 메모리 시스템도 원리적으로 맞힐 수 없다.<br><b>→ 문항 자체를 벤치마크에서 빼야 한다. 이 라벨만 달성 가능한 QA 상한을 깎는다.</b>"]],
};
// 오라클로 대체한 파이프라인 단계. 추출 프롬프트 종류(default/custom)와 독립된 항목
const ORACLE_STAGE_NAMES = {
  "": "없음",
  "extraction": "추출",
  "extraction+update": "추출+갱신",
  "extraction+update+retrieval": "추출+갱신+검색",
};
const oracleLabel = (v) => ORACLE_STAGE_NAMES[v || ""] || v;

// 오라클 행에서 특정 지표를 '–'로 가리는 이유: 칸에 호버하면 뜬다.
// 계단이 단순하지 않다는 게 요점이다: 오라클을 넣으면 그 단계의 '내용 품질' 지표는 자명해져 죽지만
// '생존율' 지표(R·Weighted R)와 다음 단계 지표(Upd)는 그 단계만 단독으로 재게 되어 오히려 살아난다.
const MASK_WHY = {
  acc: "추출 오라클: 저장물이 골든 원문 그대로라 정확도가 자명하게 높습니다. 백본 능력을 재는 값이 아닙니다",
  tp: "추출 오라클: 저장물이 골든 원문 그대로라 자명하게 높습니다",
  f1: "추출 오라클: Target P가 자명해져 조합 지표인 F1도 읽을 수 없습니다",
  fmr: "추출 오라클: 미끼(interference) 골든을 애초에 주입하지 않으므로 '흡수하지 않은 비율'이 무의미합니다",
  r: "갱신 오라클: 골든이 정의상 전부 저장되어 100입니다. (추출 오라클만 걸린 행에서는 이 값이 <b>살아 있습니다</b>: 완벽히 추출해도 저장까지 살아남지 못한 비율 = 갱신 결정의 손실)",
  wr: "갱신 오라클: 정의상 100입니다",
  upd_c: "갱신 오라클: 갱신을 정답대로 수행하므로 정의상 100 근처입니다. (추출 오라클만 걸린 행에서는 <b>살아 있습니다</b>: 추출이 완벽할 때의 갱신 능력 단독 측정)",
  upd_h: "갱신 오라클: 정의상 0입니다",
  upd_o: "갱신 오라클: 정의상 0입니다",
};
const maskDesc = (r, k) =>
  `<b>읽을 수 없는 지표</b>: 이 행은 <b>${esc(oracleLabel(r.oracle))}</b>을(를) 오라클로 대체한 실험입니다.<br>${MASK_WHY[k] || "오라클 대체로 이 지표가 자명해집니다"}<br><span class="small">오라클 행에서는 <b>QA</b> 지표만 해석합니다.</span>`;

const REC_NAMES = { integrity: "골든 포함 (Integrity)", accuracy: "추출 근거 (Accuracy)", update: "갱신 (Update)", qa: "질의응답 (QA)",
                    gold_qa: "골든 정답 검수 (벤치마크 품질)" };
// judge 채점 4종이 각각 무엇을 묻는지: 유형 이름에 호버하면 뜬다 (분석가가 라벨 의미를 헷갈리지 않도록)
const REC_DESCS = {
  integrity: "<b>골든 포함 (Memory Integrity)</b><br>정답 메모리(골든 MP)가 시스템이 <b>추출한 메모리 목록 안에 담겼는지</b>를 봅니다.<br>judge 라벨 <b>0</b>=미포함 · <b>1</b>=부분 포함 · <b>2</b>=완전 포함.<br><span class='small'>지표 R·Weighted R의 원천. ⚠ interference(미끼) 골든은 여기서 빠지고 FMR로 채점됩니다.</span>",
  accuracy: "<b>추출 근거 (Memory Accuracy)</b><br>시스템이 <b>추출한 메모리가 그 세션 대화·골든에 근거하는지</b>(날조·왜곡 여부)를 봅니다. 방향이 Integrity와 반대입니다. 이쪽은 시스템 산출물이 심판 대상.<br>judge 라벨 <b>0/1/2</b>점.<br><span class='small'>지표 Acc·Target P의 원천.</span>",
  update: "<b>갱신 (Memory Update)</b><br><code>is_update</code> 골든에 대해 시스템 메모리가 <b>갱신된 내용을 정확히 담고 원본을 대체했는지</b>를 봅니다.<br>judge 라벨 <b>Correct · Hallucination · Omission · Other</b>.<br><span class='small'>지표 Upd C/H/O의 원천. judge 간 합의가 가장 낮은 항목(Fleiss κ 0.384)이라 단독 결론 금지.</span>",
  qa: "<b>질의응답 (Question Answering)</b><br>검색 context로 <b>생성된 답변이 골든 정답과 맞는지</b>를 봅니다. 유일하게 답변 생성 레인(generator)에 종속되는 항목입니다.<br>judge 라벨 <b>Correct · Hallucination · Omission</b>.<br><span class='small'>지표 QA C/H/O의 원천. 반복 채점 안정성은 가장 높습니다(κ 0.888).</span>",
  gold_qa: "<b>골든 정답 검수</b><br>골든 정답 자체가 <b>채점 기준으로 타당한지</b>를 봅니다 (벤치마크 품질 검수).<br><span class='small'>judge 판정과 비교하지 않으므로 IAA 집계에서 제외됩니다.</span>",
};
// 유형 이름: 한글·영문 병기 + 호버 설명
const recTag = (t) => `<span class="rectag" data-desc="${esc(REC_DESCS[t] || "")}">${esc(REC_NAMES[t] || t)}</span>`;

const JM = { ctx: null, data: null, my: null, revealed: false, raw: false, queue: null, qi: -1, blind: true, note: "", gt: "", agree: null };

function jmOpen(ctx) {
  JM.ctx = ctx; JM.data = null; JM.my = null; JM.revealed = !JM.blind; JM.raw = false;
  $("#jmodal").classList.remove("hidden");
  jmRender();
  jmLoad();
}
function jmClose() { $("#jmodal").classList.add("hidden"); JM.ctx = null; }

async function jmLoad() {
  const c = JM.ctx;
  try {
    JM.data = await api(`/api/judge-input/${c.run}/${c.uuid}?rec_type=${c.rec_type}&session_id=${c.session_id}&idx=${c.idx}&generator=${encodeURIComponent(c.generator || S.generator)}`);
    // 기존 내 주석 불러오기 (재방문 시 이어서)
    const mine = (await api(`/api/annotations?run=${c.run}&uuid=${c.uuid}&annotator=${encodeURIComponent(S.author)}`))
      .find((a) => a.session_id === c.session_id && a.rec_type === c.rec_type && a.idx === c.idx);
    if (mine) { JM.my = mine.label || null; JM.note = mine.note || ""; JM.agree = mine.agree; JM.gt = mine.gt_answer || ""; JM.revealed = true; }
    else { JM.note = ""; JM.agree = null; JM.gt = ""; }
  } catch (e) { JM.data = { error: String(e.message || e) }; }
  jmRender();
}

function jmFieldsHTML(d) {
  const f = d.fields, t = d.rec_type;
  const list = (arr, cls = "") => (arr || []).length
    ? `<ol class="jl ${cls}">${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>`
    : `<p class="small muted">(없음)</p>`;
  const box = (title, inner, desc) => `<div class="jsec"><h5 ${desc ? `data-desc="${esc(desc)}"` : ""}>${title}</h5>${inner}</div>`;
  if (t === "integrity") return box(`평가 대상 골든 <span class="jtag gold">이게 담겼는가?</span>`, `<div class="jtarget">${esc(f.expected_memory_point)}</div>`)
    + box(`시스템이 이 세션에서 추출한 메모리 (${(f.memories || []).length})`, list(f.memories), "judge는 이 목록 전체를 보고 대상 골든이 담겼는지 판단합니다. 대화 원문은 보지 않습니다");
  if (t === "accuracy") return box(`평가 대상 추출 메모리 <span class="jtag a">이게 근거 있는가?</span>`, `<div class="jtarget">${esc(f.candidate_memory)}</div>`)
    + box(`골든 메모리 (미끼 제외, ${(f.golden_memories || []).length})`, list(f.golden_memories))
    + box(`이 세션 대화 (${(f.dialogue || []).length}턴)`, `<div class="jdlg">${(f.dialogue || []).map((x) =>
        `<div class="jt ${esc(x.role)}"><span class="r"><span class="ts">[${esc(x.timestamp)}]</span>${esc(x.role)}</span><span>${esc(x.content)}</span></div>`).join("")}</div>`,
        "judge는 대화 전체를 근거로 이 메모리가 지지되는지 봅니다. 다른 세션 내용은 보지 못합니다. 프롬프트에는 각 발화가 [타임스탬프]역할: 내용 형식으로 들어갑니다");
  if (t === "update") return box(`평가 대상 갱신 골든 <span class="jtag gold">이게 반영됐는가?</span>`, `<div class="jtarget">${esc(f.updated_memory)}</div>`)
    + box("원본 메모리 (갱신 전)", list(f.original_memory))
    + box(`시스템 메모리 검색 스냅샷 (top-10)`, list(f.memories), "Stage A에서 이 갱신 골든으로 검색한 상위 10건. judge는 이 안에 갱신 내용이 반영됐는지 봅니다");
  return box("질문", `<div class="jtarget">${esc(f.question)}</div>`)
    + box("골든 정답", `<div class="jans">${esc(f.reference_answer)}</div>`)
    + box(`핵심 근거 골든 (${(f.key_memory_points || []).length})`, list(f.key_memory_points))
    + box("시스템 답변", `<div class="jans sys">${esc(f.response) || "<span class='muted'>(답변 없음)</span>"}</div>`);
}

function jmRender() {
  const el = $("#jmodal-body"), c = JM.ctx, d = JM.data;
  if (!c) return;
  const qpos = JM.queue && JM.qi >= 0 ? `<span class="jq">큐 ${JM.qi + 1}/${JM.queue.length}</span>
    <button class="jbtn" id="jm-prev" ${JM.qi <= 0 ? "disabled" : ""}>← 이전</button>
    <button class="jbtn" id="jm-next" ${JM.qi >= JM.queue.length - 1 ? "disabled" : ""}>다음 →</button>` : "";
  $("#jmodal-head").innerHTML = `<b>판정 검토</b>
    <span class="jchip t-${esc(c.rec_type)}">${esc(REC_NAMES[c.rec_type])}</span>
    <span class="jchip">${esc(runLabel(c.run))}</span><span class="jchip">S${c.session_id}</span>
    ${qpos}<span style="margin-left:auto"></span>
    <label class="jsw" data-desc="켜면 judge 판정을 가린 채 먼저 라벨합니다 (앵커링 편향 방지: IAA 신뢰도의 핵심)"><input type="checkbox" id="jm-blind" ${JM.blind ? "checked" : ""}> 블라인드</label>
    <button class="jbtn" id="jm-iaa">📊 IAA</button><button class="jbtn" id="jm-close">✕</button>`;

  if (!d) { el.innerHTML = `<p class="muted" style="padding:20px">judge 입력 재현 중…</p>`; jmBind(); return; }
  if (d.error) { el.innerHTML = `<p style="padding:20px;color:var(--bad)">${esc(d.error)}</p>`; jmBind(); return; }

  const opts = LABEL_SETS[c.rec_type];
  const curJudge = c.judge_name || S.judge;
  const myJudge = d.judge_labels?.[curJudge];
  // 모든 judge를 한 줄씩: 각 judge별로 내 판정과의 일치 여부를 따로 표시 (현재 선택 judge를 맨 위)
  const jList = Object.entries(d.judge_labels || {})
    .sort(([a], [b]) => (a === curJudge ? -1 : b === curJudge ? 1 : 0));
  // 동일 judge를 같은 입력으로 반복 채점한 결과가 갈리면 = 판정이 경계선에 있는 항목
  const reps = REPEAT_JUDGES.map((k) => d.judge_labels?.[k]).filter((v) => v != null);
  const repSet = [...new Set(reps.map(normLab))];
  const unstable = reps.length >= 2 && repSet.length > 1;

  // 골든 정답 검수: judge 대조 없이 '정답 타당성 + 내가 생각하는 정답'만 기록
  if (c.rec_type === "gold_qa") {
    el.innerHTML = `
      <div class="jleft">
        <div class="jtoggle"><button class="jbtn ${JM.raw ? "" : "on"}" id="jm-view">정리된 화면</button><button class="jbtn ${JM.raw ? "on" : ""}" id="jm-rawv">judge가 받은 프롬프트 원문</button></div>
        ${JM.raw ? `<pre class="mono jraw">${esc(d.prompt)}</pre>` : jmFieldsHTML({ ...d, rec_type: "qa" })}
      </div>
      <div class="jright">
        <div class="jsec"><h5 data-desc="벤치마크가 제시한 골든 정답 자체가 채점 기준으로 타당한지를 봅니다 (judge 판정을 검토하는 항목과는 다릅니다)">① 이 골든 정답은 타당한가?</h5>
          <div class="jopts">${LABEL_SETS.gold_qa.map(([v, label, desc]) =>
            `<button class="jopt ${JM.my === v ? "on" : ""}" data-lab="${esc(v)}" data-desc="${esc(desc)}">${esc(label)}</button>`).join("")}</div></div>
        <div class="jsec"><h5>② 내가 생각하는 정답 (선택)</h5>
          <textarea id="jm-gt" placeholder="골든이 부적절하다면, 이 질문의 올바른 정답은 무엇이어야 하는지">${esc(JM.gt || "")}</textarea></div>
        <div class="jsec"><h5>③ 근거 메모 (선택)</h5>
          <textarea id="jm-note" placeholder="왜 그렇게 판단했는지">${esc(JM.note || "")}</textarea></div>
        <button class="primary jsave" id="jm-save">저장${JM.queue ? " 후 다음 →" : ""}</button>
        <div id="jm-msg" class="small muted"></div>
      </div>`;
    jmBind();
    return;
  }

  el.innerHTML = `
    <div class="jleft">
      <div class="jtoggle"><button class="jbtn ${JM.raw ? "" : "on"}" id="jm-view">정리된 화면</button><button class="jbtn ${JM.raw ? "on" : ""}" id="jm-rawv">judge가 받은 프롬프트 원문 (${d.prompt.length.toLocaleString()}자)</button></div>
      ${JM.raw ? `<pre class="mono jraw">${esc(d.prompt)}</pre>` : jmFieldsHTML(d) + `
        <details class="jrubric"><summary data-desc="judge가 프롬프트로 받은 채점 지시문 원문: 위 데이터가 {중괄호} 자리에 들어갑니다. 분석가도 같은 기준으로 판정해야 비교가 성립합니다">judge가 받은 채점 기준 (지시문 원문) 펼치기</summary>
          <pre class="mono">${esc(d.template || "")}</pre></details>`}
    </div>
    <div class="jright">
      <div class="jsec"><h5>① 내 판정</h5>
        <div class="jopts">${opts.map(([v, label, desc]) =>
          `<button class="jopt ${JM.my === v ? "on" : ""}" data-lab="${esc(v)}" data-desc="${esc(desc)}">${esc(label)}</button>`).join("")}</div></div>
      <div class="jsec"><h5 data-desc="각 judge의 판정을 따로 표시하고, 내 판정과의 일치 여부를 judge별로 각각 계산합니다. ⚠ 같은 모델이라도 채점 회차가 다르면 라벨이 다를 수 있습니다 (동일 입력 반복 채점 실측)">② judge별 판정 · 내 판정과의 일치</h5>
        ${reps.length >= 2 ? `<div class="repnote ${unstable ? "unst" : "st"}" data-desc="같은 judge에 완전히 동일한 입력을 ${reps.length}회 넣었을 때의 결과입니다. 갈렸다면 이 항목은 판정 경계선에 있다는 뜻이고, 어느 한쪽이 '정답'이라고 보기 어렵습니다 (반복 실측: 명확한 항목은 2~3%만 흔들리는 반면 경계선 항목은 30~55%가 흔들림)">
          ${unstable ? `⚠ 반복 채점 ${reps.length}회에서 <b>판정이 갈린 항목</b> (${repSet.map((x) => esc(x)).join(" / ")})` : `✓ 반복 채점 ${reps.length}회 모두 동일 판정`}</div>` : ""}
        ${JM.revealed
          ? (jList.length ? `<table class="jtab">${jList.map(([k, v]) => {
              const spec = LABEL_SETS[c.rec_type].map(([x]) => normLab(x));
              const offSpec = v != null && !spec.includes(normLab(v));
              const hit = JM.my != null && labMatch(v, JM.my);
              const cell = JM.my == null ? ""
                : v == null ? `<span class="jmatch na" data-desc="judge가 이 항목을 무효(None) 처리했습니다. 대조 대상이 없습니다">대조 불가</span>`
                : offSpec ? `<span class="jmatch na" data-desc="judge가 프롬프트 규격(${esc(LABEL_SETS[c.rec_type].map(([x]) => x).join(' / '))})에 없는 값을 반환했습니다. 선택지에 없으므로 일치 여부를 판정하지 않습니다. ⚠ HaluMem 공식 집계도 이런 값은 어느 비율에도 세지 않고 분모만 키웁니다">대조 불가</span>`
                : `<span class="jmatch ${hit ? "ok" : "no"}">${hit ? "일치" : "불일치"}</span>`;
              return `<tr class="${k === curJudge ? "cur" : ""}">
                <td class="jn">${k === curJudge ? '<span class="curdot" data-desc="상단바에서 선택 중인 judge: 주석 저장 시 이 judge의 라벨이 기록됩니다">●</span>' : ""}${esc(judgeLabel(k))}</td>
                <td class="jv">${esc(String(v ?? "–"))}${offSpec ? '<span class="offspec" data-desc="프롬프트가 정의하지 않은 라벨">규격외</span>' : ""}</td>
                <td>${cell}</td></tr>`;
            }).join("")}</table>` : `<p class="small muted">이 항목에 대한 judge 라벨이 없습니다</p>`)
          : `<p class="small muted">블라인드 상태입니다. 내 판정을 고르면 공개됩니다.</p>`}</div>
      <div class="jsec"><h5>③ judge 판정에 동의하십니까?</h5>
        <div class="jopts"><button class="jopt ag ${JM.agree === 1 ? "on" : ""}" data-ag="1">👍 동의</button>
          <button class="jopt dg ${JM.agree === 0 ? "on" : ""}" data-ag="0">👎 비동의</button></div></div>
      <div class="jsec"><h5>④ 근거 메모 (선택)</h5>
        <textarea id="jm-note" placeholder="왜 그렇게 판단했는지 (특히 judge와 갈릴 때)">${esc(JM.note || "")}</textarea></div>
      <button class="primary jsave" id="jm-save">저장${JM.queue ? " 후 다음 →" : ""}</button>
      <div id="jm-msg" class="small muted"></div>
    </div>`;
  jmBind();
}

function jmBind() {
  $("#jm-close") && ($("#jm-close").onclick = jmClose);
  $("#jm-blind") && ($("#jm-blind").onchange = (e) => { JM.blind = e.target.checked; if (!JM.blind) JM.revealed = true; jmRender(); });
  $("#jm-view") && ($("#jm-view").onclick = () => { JM.raw = false; jmRender(); });
  $("#jm-rawv") && ($("#jm-rawv").onclick = () => { JM.raw = true; jmRender(); });
  $("#jm-iaa") && ($("#jm-iaa").onclick = jmIAA);
  $("#jm-prev") && ($("#jm-prev").onclick = () => jmGo(JM.qi - 1));
  $("#jm-next") && ($("#jm-next").onclick = () => jmGo(JM.qi + 1));
  $$("#jmodal .jopt[data-lab]").forEach((b) => (b.onclick = () => { JM.my = b.dataset.lab; JM.revealed = true; JM.note = $("#jm-note")?.value || JM.note; JM.gt = $("#jm-gt")?.value || JM.gt; jmRender(); }));
  $$("#jmodal .jopt[data-ag]").forEach((b) => (b.onclick = () => { JM.agree = +b.dataset.ag; JM.note = $("#jm-note")?.value || JM.note; JM.gt = $("#jm-gt")?.value || JM.gt; jmRender(); }));
  $("#jm-save") && ($("#jm-save").onclick = jmSave);
}

async function jmSave() {
  if (!S.author) { askName(); return; }
  const c = JM.ctx, d = JM.data;
  const jn = c.judge_name || S.judge;
  await api("/api/annotations", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      run: c.run, uuid: c.uuid, session_id: c.session_id, rec_type: c.rec_type, idx: c.idx,
      target: String(d.target).slice(0, 2000), generator: c.rec_type === "qa" ? (c.generator || S.generator) : "",
      annotator: S.author, label: JM.my || "", agree: JM.agree,
      judge_name: jn, judge_label: String(d.judge_labels?.[jn] ?? ""), blind: JM.blind ? 1 : 0,
      note: $("#jm-note")?.value || "", gt_answer: $("#jm-gt")?.value || "",
    }),
  });
  if (JM.queue && JM.qi < JM.queue.length - 1) { jmGo(JM.qi + 1); return; }
  $("#jm-msg").textContent = "저장됨";
}

async function jmGo(i) {
  if (!JM.queue || i < 0 || i >= JM.queue.length) return;
  JM.qi = i;
  const q = JM.queue[i];
  jmOpen({ run: q.run, uuid: q.uuid, session_id: q.session_id, rec_type: q.rec_type, idx: q.idx,
           generator: q.generator || S.generator, judge_name: q.judge });
}

async function jmStartQueue() {
  busy(true, "공유 표본 큐 불러오는 중…");
  let d;
  try { d = await api("/api/queue"); } finally { busy(false); }
  JM.queue = d.items; jmGo(0);
}

// judge 라벨 표기: 0/1/2가 분석가 번호로 오독되지 않게 항상 뜻을 붙인다
const labMeta = (t, v) => (LABEL_SETS[t] || []).find((x) => x[0] === String(v));
const labDesc = (t, v) => { const m = labMeta(t, v); return m ? `${m[0]}=${m[1]}` : String(v ?? "–"); };
const labChip = (t, v) => {
  const m = labMeta(t, v);
  return `<span class="lab">${esc(String(v ?? "–"))}${m && /^\d+$/.test(String(v)) ? `<i>${esc(m[1])}</i>` : ""}</span>`;
};

const kFmt = (o) => o.kappa == null ? "–"
  : `${o.kappa}${o.ci ? ` <span class="kci">[${o.ci[0]}–${o.ci[1]}]</span>` : ""}`;
const kGrade = (k) => k == null ? "" : k >= 0.8 ? "거의 완전" : k >= 0.6 ? "견고" : k >= 0.4 ? "보통" : k >= 0.2 ? "약함" : "거의 없음";
const pctCell = (o) => o && o.n ? `${o.agree}% <span class="muted small">(${o.n})</span>` : `<span class="muted">–</span>`;

// 골든 정답 검수 결과 화면.
// IAA 화면과 묻는 것이 다르다: 저쪽은 "judge 채점이 맞았나", 여기는 **"문항이 채점 기준으로 쓸 만한가"**.
// 핵심은 라벨 × 시스템 정답률 교차표다. 분석가가 '근거 없음'이라 한 문항을 실제로 모든 시스템이
// 틀렸다면 그 진단이 데이터로 확인되고, 동시에 QA 점수 중 벤치마크 결함 몫이 정량화된다.
const GQ_LAB = { valid: "타당", wrong: "골든 정답이 잘못됨", unanswerable: "대화에 근거 없음", "동률": "동률(합의 없음)" };
const GQ_FIX = {
  valid: "그대로 채점 기준으로 쓸 수 있음",
  wrong: "<b>정답·채점을 고쳐야 함</b>: 문항은 살리되 올바른 답(또는 함께 인정할 동등 표현)으로 교정",
  unanswerable: "<b>문항을 빼야 함</b>: 대화에 근거가 없어 어떤 시스템도 원리적으로 못 맞힘. 이 라벨만 달성 가능한 QA 상한을 깎는다",
};
// 사람 기준(3인 합의 / 개별 분석가)을 바꿀 때 화면 전체를 다시 그리면 스크롤이 튀고 느리다.
// 필요한 데이터는 이미 응답 안에 다 들어 있으므로, 이 표만 다시 만들어 갈아끼운다.
function iaaMatrixHTML(d) {
      const ref = d.matrices[JM.href] ? JM.href : d.matrix_refs[0];
      const MM = d.matrices[ref], baseM = MM[0], others = MM.slice(1);
      const isCons = ref === d.matrix_refs[0];
      // ⚠ 유형 × 모델 매트릭스로 본다. 모델을 행으로 두면 '한 유형에서 번 것을 다른 유형에서
      //    잃는' 상쇄 구조가 안 보인다. 그걸 놓치면 정반대 결론을 내게 된다.
      // 행(판정 유형)별 최고 일치율: 색(McNemar 유의)과 의미가 겹치지 않게 테두리로 구분한다
      const rowBest = (t) => {
        const vals = [baseM, ...others].map((m) => (t ? m.by_type[t] : m)).filter(Boolean).map((s) => s.agree);
        return vals.length ? Math.max(...vals) : null;
      };
      const cell = (m, t) => {
        const s = t ? m.by_type[t] : m;
        if (!s) return `<td class="muted">–</td>`;
        const v = s.vs_base;
        const sig = v && v.p < 0.05 ? (v.better > v.worse ? " tw" : " tl") : "";
        const best = s.agree === rowBest(t) ? " best" : "";
        const desc = `<b>${esc(m.model)}</b> · ${t ? esc(REC_NAMES[t]) : "전체"}<br>일치 ${s.ok}/${s.n} (${s.agree}%) · κ ${s.kappa ?? "–"}`
          + (s.bias != null ? `<br>편향 ${s.bias > 0 ? "+" : ""}${s.bias}: 사람이 judge보다 ${s.bias > 0.05 ? "<b>관대</b>" : s.bias < -0.05 ? "<b>가혹</b>" : "중립"} (0/1/2 척도)` : "")
          + (s.agree === rowBest(t) ? `<br><b>이 유형에서 최고 일치율</b>` : "")
          + (v ? `<br>기준 대비 개선 <b>${v.better}</b> : 악화 <b>${v.worse}</b> · p=${v.p}`
                 + (v.p < 0.05 ? (v.better > v.worse ? ": <b>유의하게 개선</b>" : ": <b>유의하게 악화</b>") : ": 구분 불가") : "");
        return `<td class="mcell${sig}${best}" data-desc="${esc(desc)}"><b>${s.agree}%</b> <span class="muted small">${s.ok}/${s.n}</span>
          <br><span class="kap">κ ${s.kappa ?? "–"}</span>${
          v ? ` <span class="vs">${v.better}:${v.worse}${v.p < 0.05 ? " ✦" : ""}</span>` : ""}</td>`;
      };
      const fs = (s) => !s || !s.n ? `<td class="muted">–</td>`
        : `<td data-desc="일치 ${s.ok}/${s.n} · κ ${s.kappa ?? "–"}">${s.agree}% <span class="muted small">(${s.n})</span></td>`;
      const row = (t) => `<tr><td>${t ? recTag(t) : "<b>전체</b>"}</td>${[baseM, ...others].map((m) => cell(m, t)).join("")}${
        t ? fs(baseM.by_type[t]?.firm) + fs(baseM.by_type[t]?.split) : `<td class="muted">–</td><td class="muted">–</td>`}</tr>`;
      return `
      <h4 style="margin:18px 0 6px" data-desc="같은 항목을 <b>judge 모델만 바꿔 재채점</b>한 결과입니다. 채점 프롬프트는 원본과 비트 단위로 같고 모델만 다릅니다.<br>⚠ 모든 칸을 <b>같은 항목 집합</b>(재채점 모델 전부가 커버하는 교집합)에서 쟀습니다. 기준 judge는 큐 밖 라벨까지 있어 그대로 재면 쉬운 항목이 섞여 유리해집니다.">사람 판정 vs judge: 유형 × 모델</h4>
      <p class="small hrefsw" data-desc="사람 쪽 기준을 고릅니다. <b>3인 합의</b>는 다수결(동률 제외)이고, 개인을 고르면 그 분석가의 라벨을 그대로 정답으로 놓습니다. 사람마다 judge와 얼마나 맞는지, 그리고 그 순위가 분석가에 따라 바뀌는지를 볼 수 있습니다.">
        <b>사람 기준</b>${d.matrix_refs.map((r) => `<button class="seg${r === ref ? " on" : ""}" data-href="${esc(r)}">${r === d.matrix_refs[0] ? "3인 합의" : esc(r)}</button>`).join("")}</p>
      <div class="jbasis" data-desc="같은 항목을 여러 모델이 판정했으므로 <b>대응표본</b>입니다. 독립표본 CI는 서로 크게 겹쳐 '구분 불가'로 오판하게 되므로 McNemar 정확검정으로 판정합니다.">
        <b>⚠ 유형별로 보세요</b>: 전체 행만 보면 <b>한 유형에서 번 것을 다른 유형에서 잃는 상쇄</b>가 안 보입니다.
        분석가에게 배정된 <b>공유 표본</b>에서 ${isCons ? "3인 <b>합의 라벨</b>" : `<b>${esc(ref)}</b> 님의 라벨`}을 기준으로 각 judge 모델의 판정을 대조합니다.
        <span class="small">칸의 <b>a:b</b>는 기준(gpt-oss-120b) 대비 <b>개선 : 악화</b> 건수, <b>✦</b>는 McNemar p&lt;0.05.
        초록=유의하게 개선, 빨강=유의하게 악화. <b>굵은 테두리</b>는 그 유형에서 <b>최고 일치율</b>입니다(색과 별개. 최고여도 기준 대비 유의하지 않을 수 있습니다). <b>순위 판정에는 κ 대신 개선:악화를</b> 씁니다. κ는 우연 일치를 보정한 절대 수준(0.6↑ 견고)을 볼 때 씁니다.</span>
      </div>
      <table class="cmp jm"><tr><th>판정 유형</th>${[baseM, ...others].map((m) =>
        `<th data-desc="${esc(m.model)}">${esc(m.model.replace(/: 기존$/, ""))}${/기존/.test(m.model) ? '<br><span class="small muted">기준</span>' : ""}</th>`).join("")}
        <th data-desc="기준 judge가 반복 채점에서 <b>매번 같은 라벨</b>을 준 항목에서의 사람 일치율: judge가 확신한 구간">judge 확신 구간</th>
        <th data-desc="기준 judge가 반복 채점에서 <b>갈렸던</b> 항목에서의 사람 일치율: 여기가 낮으면 그 항목이 사람에게도 애매하다는 뜻">judge 흔들린 구간</th></tr>
        ${["integrity", "accuracy", "update", "qa"].filter((t) => baseM.by_type[t]).map(row).join("")}
        <tr class="jm-tot">${row("").slice(4)}</tr></table>
      <p class="small muted" style="margin-top:5px">개별 검토(큐 밖)로 라벨한 주석 ${d.outside_queue}건은 이 집계에서 제외됩니다. 분석가가 의심스러운 항목을 골라 누른 <b>기회 표본</b>이라 섞으면 지표가 왜곡됩니다.</p>`;
}

/* ---------- BEAM ---------- */
/* HaluMem 화면과 데이터 구조가 달라 전용 화면으로 둠. 유저·세션·골든이 없고
   대화 x 능력 x cutoff 세 갈래로만 집계됨. */
S.beamBucket = "100k";

// 점수를 색으로. 0(빨강) ~ 0.5(노랑) ~ 1(초록). 판정 팔레트와 뜻이 겹치지 않게
// 이 화면 안에서만 쓰는 국소 스케일임
function beamHeat(v) {
  if (v == null) return "background:var(--chip)";
  const t = Math.max(0, Math.min(1, v));
  const h = t * 120;                       // 0=빨강, 120=초록
  return `background:hsl(${h} 62% ${92 - t * 14}%);color:hsl(${h} 70% 24%)`;
}

S.beamMode = "overview";   // overview | bucket

// 화면 상단 모드 전환. 두 화면이 같은 자리에 붙는다
const beamModeSwitch = () => `<p class="hrefsw beammode"><b>보기</b>
  <button class="seg${S.beamMode === "overview" ? " on" : ""}" data-bm="overview"
    data-desc="규모 3종 × 답변 프롬프트 2종을 한 화면에서 대조합니다. BEAM 결론이 전부 버킷 간·프롬프트 간 비교라 버튼을 눌러가며 기억으로 맞추면 오독이 납니다">종합</button>
  <button class="seg${S.beamMode === "bucket" ? " on" : ""}" data-bm="bucket"
    data-desc="버킷 하나를 자세히 봅니다. 능력별 문항 목록과 문항 상세(답변 원문·rubric 채점)로 들어갈 수 있습니다">버킷별 상세</button></p>`;

const bindBeamMode = () => $$("#content .beammode button").forEach((b) =>
  (b.onclick = () => { S.beamMode = b.dataset.bm; renderBeam(); }));

// 차이 칸 색. 0을 흰색으로 두고 ±0.4 를 양끝으로 잡는다 (실측 최대가 -0.434)
function deltaHeat(v) {
  if (v == null) return "background:var(--chip)";
  const t = Math.max(-1, Math.min(1, v / 0.4));
  const h = t >= 0 ? 145 : 8;
  return `background:hsl(${h} 60% ${96 - Math.abs(t) * 20}%);color:hsl(${h} 70% 26%)`;
}

async function renderBeam() {
  if (S.beamMode === "overview") return renderBeamOverview();
  return renderBeamBucket();
}

// 규모 × 답변 프롬프트를 한 화면에. BEAM 결론이 전부 이 축들의 대조라 버킷별 화면으로는 안 보인다.
async function renderBeamOverview() {
  const el = $("#content");
  el.innerHTML = `<p class="muted">집계 중…</p>`;
  let d;
  try {
    d = await api(`/api/beam/overview`);
  } catch (e) {
    el.innerHTML = `${beamModeSwitch()}<p><b>불러오기 실패</b></p><p class="small">${esc(e.message)}</p>`;
    bindBeamMode();
    return;
  }
  const CUT = d.cutoffs, SC = d.scales, PR = d.prompts;
  const at = (s, p) => d.buckets.find((b) => b.ready && b.scale === s && b.prompt === p);
  const sc = (b, c) => (b && b.overall[String(c)] ? b.overall[String(c)].score : null);
  const n3 = (v) => (v == null ? "–" : v.toFixed(3));
  const sgn = (v) => (v == null ? "–" : (v >= 0 ? "+" : "") + v.toFixed(3));

  if (!d.buckets.some((b) => b.ready)) {
    el.innerHTML = `${beamModeSwitch()}<p class="muted">채점본이 있는 버킷이 없습니다.</p>`;
    bindBeamMode();
    return;
  }

  // ---- 곡선 미니 그래프. 세로는 0~1 고정이라 행끼리 기울기를 눈으로 비교할 수 있다
  const curve = (b) => {
    const pts = CUT.map((c, i) => {
      const v = sc(b, c);
      return v == null ? null : `${(i / (CUT.length - 1) * 56 + 2).toFixed(1)},${(20 - v * 18).toFixed(1)}`;
    }).filter(Boolean).join(" ");
    return `<svg class="bspark" viewBox="0 0 60 22" preserveAspectRatio="none"><polyline points="${pts}"/></svg>`;
  };

  // ---- 카드 1: 전체 점수 (규모 × 프롬프트 × cutoff)
  const rows1 = [];
  for (const s of SC) {
    for (const p of PR) {
      const b = at(s, p);
      if (!b) { rows1.push(`<tr><td class="brow"><b>${esc(s)}</b><br><span class="small muted">${esc(p)}</span></td>
        <td class="muted" colspan="${CUT.length + 3}">채점본 없음</td></tr>`); continue; }
      const lo = sc(b, CUT[0]), hi = sc(b, CUT[CUT.length - 1]);
      rows1.push(`<tr><td class="brow"><b>${esc(s)}</b><br><span class="small muted">${esc(p)}</span></td>
        <td class="small">${b.n_convs}대화<br>${b.n_questions}문항</td>
        ${CUT.map((c) => {
          const cell = b.overall[String(c)], v = cell ? cell.score : null;
          const mark = cell && cell.full ? `<i class="bfull" data-desc="${esc(`이 칸의 ${cell.full}/${cell.n}건은 저장 메모리가 cutoff 보다 적어 <b>저장소를 전부 준 조건</b>입니다. 검색이 작동하지 않았으므로 검색 예산 효과로 읽으면 안 됩니다.`)}">▚</i>` : "";
          return `<td class="bcell" style="${beamHeat(v)}"><b>${n3(v)}</b>${mark}</td>`;
        }).join("")}
        <td class="bdelta ${hi >= lo ? "up" : "down"}">${sgn(hi != null && lo != null ? hi - lo : null)}</td>
        <td>${curve(b)}</td></tr>`);
    }
  }

  // ---- 카드 2: 프롬프트 효과 (능력 × 규모, top-200)
  const LAST = CUT[CUT.length - 1];
  const abKeys = Object.keys(d.abilities);
  const pdelta = (a, s) => {
    const A = at(s, PR[0]), B = at(s, PR[1]);
    if (!A || !B) return null;
    const x = A.abilities[a]?.[String(LAST)], y = B.abilities[a]?.[String(LAST)];
    return x == null || y == null ? null : y - x;
  };
  const ab2 = abKeys.map((a) => ({ a, vals: SC.map((s) => pdelta(a, s)) }))
    .sort((p, q) => (p.vals.find((v) => v != null) ?? 0) - (q.vals.find((v) => v != null) ?? 0));

  // ---- 카드 3: 능력 × 규모 (대표 프롬프트, top-200) — 단조인지 보려는 표
  const ab3 = abKeys.map((a) => {
    const vals = SC.map((s) => at(s, PR[0])?.abilities[a]?.[String(LAST)] ?? null);
    const ok = vals.every((v) => v != null);
    const mono = ok && (vals.every((v, i) => i === 0 || v >= vals[i - 1]) || vals.every((v, i) => i === 0 || v <= vals[i - 1]));
    return { a, vals, mono, diff: ok ? vals[vals.length - 1] - vals[0] : null };
  }).sort((p, q) => (p.diff ?? 0) - (q.diff ?? 0));
  const nMono = ab3.filter((r) => r.mono).length;

  el.innerHTML = `
    ${beamModeSwitch()}

    <div class="noisebar warn" data-desc="세 버킷의 대화 제목 겹침이 0입니다 (500K∩1M 0/35, 100K∩500K 0/20). 주제 구성도 8종/14종/13종으로 다릅니다.">
      <b>⚠ 규모 간 절대 비교를 하지 마세요.</b> BEAM은 버킷마다 <b>완전히 다른 대화</b>를 씁니다(제목 겹침 0).
      <span class="small">벤치마크 설계가 규모 간 대응 비교를 지원하지 않습니다. 능력 구성을 통제해도 10개 중 8개가 500K에서 위아래로 튀고, 버킷 안에서 저장량과 점수의 상관은 전부 무의미합니다.</span>
      <br><span class="small"><b>읽어도 되는 것</b>: 같은 규모 안에서의 <b>cutoff 곡선 모양</b>과 <b>프롬프트 간극</b>. 둘 다 같은 대화·같은 저장소 위의 비교라 대화 집합 차이가 상쇄됩니다. 판독 근거는 <code>docs/mem0-classic-oss/beam-experiment.md</code> §0·§4-1</span>
    </div>

    <div class="card"><h4 data-desc="행은 규모 × 답변 프롬프트, 열은 cutoff. 곡선은 세로 0~1 고정이라 행끼리 기울기를 눈으로 비교할 수 있습니다">전체 점수 (규모 × 답변 프롬프트 × 검색 예산)</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>규모 · 프롬프트</th><th>규모</th>
      ${CUT.map((c) => `<th data-desc="답변자에게 메모리 ${c}개까지 제공">top-${c}</th>`).join("")}
      <th data-desc="top-${LAST} 빼기 top-${CUT[0]}. 양수면 컨텍스트를 늘릴수록 좋아집니다">차이</th><th>곡선</th></tr>
      ${rows1.join("")}
    </table></div>
    <div class="body"><span class="small muted">▚ = 저장 메모리가 cutoff 보다 적어 저장소를 전부 준 칸. 그 칸은 검색이 작동하지 않았습니다. <b>100K의 top-200에만 붙습니다.</b> 100K에서 곡선이 꺾이는 이유입니다.</span></div>
    </div>

    <div class="card"><h4 data-desc="BEAM 공식 프롬프트에서 mem0 하네스 프롬프트를 뺀 값입니다. 음수면 공식 프롬프트가 낮습니다. top-${LAST} 기준">답변 프롬프트 효과 (능력 × 규모, top-${LAST})</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>능력</th>${SC.map((s) => `<th>${esc(s)}</th>`).join("")}</tr>
      ${ab2.map((r) => `<tr><td class="brow"><b>${esc(d.abilities[r.a] || r.a)}</b><br><span class="small muted">${esc(r.a)}</span></td>
        ${r.vals.map((v) => `<td class="bcell" style="${deltaHeat(v)}"><b>${sgn(v)}</b></td>`).join("")}</tr>`).join("")}
    </table></div>
    <div class="body"><span class="small muted">세 규모 모두 <b>모순 감지</b>가 최대 하락, <b>지시 준수</b>가 그다음입니다. <b>갱신 반영</b>만 0 근처이거나 양수입니다. rubric 1항목이 곧 답이라 짧아져도 안 깎이기 때문입니다 (§7-5).</span></div>
    </div>

    <div class="card"><h4 data-desc="대표 프롬프트(${esc(PR[0])}) 기준 능력별 점수를 규모끼리 늘어놓은 것입니다. 규모 효과가 있다면 단조여야 합니다">능력 × 규모 (${esc(PR[0])}, top-${LAST})</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>능력</th>${SC.map((s) => `<th>${esc(s)}</th>`).join("")}<th>${esc(SC[SC.length-1])}−${esc(SC[0])}</th><th>모양</th></tr>
      ${ab3.map((r) => `<tr><td class="brow"><b>${esc(d.abilities[r.a] || r.a)}</b><br><span class="small muted">${esc(r.a)}</span></td>
        ${r.vals.map((v) => `<td class="bcell" style="${beamHeat(v)}">${n3(v)}</td>`).join("")}
        <td class="bdelta ${(r.diff ?? 0) >= 0 ? "up" : "down"}">${sgn(r.diff)}</td>
        <td class="small">${r.mono ? "<b>단조</b>" : `<span class="muted">비단조</span>`}</td></tr>`).join("")}
    </table></div>
    <div class="body"><span class="small muted"><b>${abKeys.length}개 중 단조는 ${nMono}개뿐입니다.</b> 규모 효과라면 단조여야 합니다. 이 모양은 어느 대화가 그 버킷에 들어갔느냐가 만든 것입니다. 능력들이 같은 대화 집합을 공유하므로 여러 개가 같이 오르내리는 것도 근거가 되지 못합니다.</span></div>
    </div>

    <div class="card"><h4 data-desc="답변 길이 중앙값입니다. mem0 하네스 프롬프트는 길이 지시가 없고 세부를 다 담으라고 하며, BEAM 공식은 '설명 없이 답만 출력'이라고 지시합니다">답변 길이 (문자 수 중앙값)</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>프롬프트</th>${SC.map((s) => `<th>${esc(s)}</th>`).join("")}</tr>
      ${PR.map((p) => `<tr><td class="brow"><b>${esc(p)}</b></td>
        ${SC.map((s) => { const b = at(s, p); return `<td>${b ? `<b>${b.len_median}</b>자` : "–"}</td>`; }).join("")}</tr>`).join("")}
    </table></div>
    <div class="body"><span class="small muted">저장소가 커질수록 <b>${esc(PR[0])}</b> 쪽 답변이 길어집니다. <b>${esc(PR[1])}</b> 쪽은 규모와 무관하게 눌려 있습니다. 규모 간 무엇을 비교하든 이 몫을 먼저 빼야 합니다 (§4-3).</span></div>
    </div>`;

  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted"><b>BEAM 종합</b><br>규모 ${SC.length}종 × 답변 프롬프트 ${PR.length}종 × cutoff ${CUT.length}종</p>
    <p class="small muted" style="margin-top:10px"><b>점수 색</b></p>
    <div class="bleg">${[0, .25, .5, .75, 1].map((v) => `<span style="${beamHeat(v)}">${v.toFixed(2)}</span>`).join("")}</div>
    <p class="small muted" style="margin-top:10px"><b>차이 색</b></p>
    <div class="bleg">${[-.4, -.2, 0, .2].map((v) => `<span style="${deltaHeat(v)}">${v >= 0 ? "+" : ""}${v.toFixed(2)}</span>`).join("")}</div>
    <p class="small muted" style="margin-top:10px"><b>읽는 순서</b><br>① 곡선 모양 → 검색 예산이 언제 듣는가<br>② 프롬프트 효과 → 답변 규약의 몫<br>③ 능력 × 규모 → 규모 효과가 있는가 (없음)</p>
    <p class="small muted" style="margin-top:10px">⚠ 이 화면은 상단바의 Generator·Judge 선택을 따르지 않습니다. 버킷마다 고정된 조합입니다.</p>
    <p class="small muted" style="margin-top:10px">판독 근거는 <code>docs/mem0-classic-oss/beam-experiment.md</code></p>
  </div>`;
  bindBeamMode();
}

async function renderBeamBucket() {
  const el = $("#content");
  el.innerHTML = `<p class="muted">집계 중…</p>`;
  let d;
  try {
    d = await api(`/api/beam?bucket=${encodeURIComponent(S.beamBucket)}`);
  } catch (e) {
    el.innerHTML = `<p><b>불러오기 실패</b></p><p class="small">${esc(e.message)}</p>`;
    return;
  }
  const CUT = d.cutoffs;
  const pick = (d.buckets || []).map((b) =>
    `<button class="seg${b.key === d.bucket ? " on" : ""}" data-bk="${esc(b.key)}"
      ${b.ready ? "" : "disabled"} data-desc="${esc(b.note || "")}${b.ready ? "" : "<br><b>아직 채점본이 없습니다</b>"}"
      >${esc(b.label)}</button>`).join("");

  if (!d.ready) {
    el.innerHTML = `${beamModeSwitch()}<p class="hrefsw"><b>버킷</b>${pick}</p>
      <p class="muted">이 버킷은 아직 채점본이 없습니다.</p>`;
    bindBeamMode();
    bindBeamMode();
  $$("#content .hrefsw:not(.beammode) button").forEach((b) => (b.onclick = () => { S.beamBucket = b.dataset.bk; renderBeam(); }));
    return;
  }

  // cutoff 가 저장소보다 큰 칸을 표시. 그 칸은 검색이 작동하지 않은 조건임
  const cellHTML = (c) => {
    if (!c) return `<td class="muted">–</td>`;
    const fullPct = c.n ? c.full / c.n * 100 : 0;
    const mark = c.full ? `<i class="bfull" data-desc="${esc(
      `이 칸의 ${c.full}/${c.n}건은 저장 메모리가 cutoff 보다 적어 <b>저장소를 전부 준 조건</b>입니다.`
      + `<br>실제 투입 평균 ${c.used}개. 검색이 골라낸 결과가 아니므로 검색 품질로 읽으면 안 됩니다.`)}">▚</i>` : "";
    return `<td class="bcell" style="${beamHeat(c.score)}" data-desc="${esc(
      `평균 ${c.score} · ${c.n}건<br>실제 투입 평균 <b>${c.used}</b>개`
      + (c.full ? `<br>그중 ${c.full}건은 저장소가 모자라 cutoff 를 못 채웠습니다 (${fullPct.toFixed(0)}%)` : ""))}"
      ><b>${c.score.toFixed(3)}</b>${mark}</td>`;
  };
  const deltaHTML = (v) => v == null ? `<td class="muted">–</td>`
    : `<td class="bdelta ${v >= 0 ? "up" : "down"}">${v >= 0 ? "+" : ""}${v.toFixed(3)}</td>`;

  // 능력별 미니 곡선. 값을 0~1 로 보고 세로 위치를 잡음
  const spark = (cells) => {
    const pts = CUT.map((k, i) => {
      const c = cells[String(k)];
      return c ? `${(i / (CUT.length - 1) * 56 + 2).toFixed(1)},${(20 - c.score * 18).toFixed(1)}` : null;
    }).filter(Boolean).join(" ");
    return `<svg class="bspark" viewBox="0 0 60 22" preserveAspectRatio="none"><polyline points="${pts}"/></svg>`;
  };

  const eo = d.event_ordering;
  el.innerHTML = `
    ${beamModeSwitch()}
    <p class="hrefsw" data-desc="버킷마다 대화 집합이 다릅니다(제목 겹침 0). 절대 수치를 버킷 간에 비교하지 마세요."><b>버킷</b>${pick}</p>
    <p class="small muted">${esc(d.note || "")}<br>대화 ${d.n_convs}개 · 문항 ${d.n_questions}개 · 채점 ${d.n_records}건 · 저장 메모리 ${d.stored_min}~${d.stored_max}개</p>

    <div class="jbasis" data-desc="cutoff 는 Stage A' 에서 검색 결과 top-200 을 잘라 만든 조건입니다. 투입은 한 번만 했고 자르기만 달리했습니다.">
      <b>이 표가 묻는 것</b>: 답변자에게 메모리를 몇 개까지 주느냐(cutoff)에 따라 능력별 점수가 어떻게 변하는가.
      <span class="small">▚ 표시는 저장 메모리가 cutoff 보다 적어 <b>저장소를 전부 준</b> 칸입니다. 그 칸은 검색이 작동하지 않았습니다.</span>
    </div>

    <div class="card"><h4 data-desc="행은 능력 10종, 열은 cutoff. 마지막 열은 top-${CUT[CUT.length-1]} 에서 top-${CUT[0]} 을 뺀 값으로, 클수록 검색 예산에 민감한 능력입니다">능력 × 검색 예산</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>능력</th>${CUT.map((k) => `<th data-desc="답변자에게 메모리 ${k}개까지 제공">top-${k}</th>`).join("")}
      <th data-desc="top-${CUT[CUT.length-1]} 빼기 top-${CUT[0]}. 양수면 컨텍스트를 늘릴수록 좋아지는 능력">차이</th><th>추세</th></tr>
      ${d.abilities.map((a) => `<tr data-ab="${esc(a.key)}">
        <td class="brow bclick" data-open="${esc(a.key)}|${esc(a.label)}"
          data-desc="클릭하면 이 능력의 문항 ${CUT.length}벌 채점을 문항 단위로 볼 수 있습니다"><b>${esc(a.label)}</b>
          <br><span class="small muted">${esc(a.key)}</span></td>
        ${CUT.map((k) => cellHTML(a.cells[String(k)])).join("")}
        ${deltaHTML(a.delta)}<td>${spark(a.cells)}</td></tr>`).join("")}
      <tr class="jm-tot"><td class="brow"><b>전체</b></td>
        ${CUT.map((k) => cellHTML(d.overall[String(k)])).join("")}
        ${deltaHTML(d.overall[String(CUT[CUT.length-1])] && d.overall[String(CUT[0])]
          ? d.overall[String(CUT[CUT.length-1])].score - d.overall[String(CUT[0])].score : null)}<td></td></tr>
    </table></div></div>

    ${!eo ? "" : `
    <div class="card"><h4 data-desc="공식 채점 코드가 이 능력만 다른 계열의 지표로 잽니다. 세 정의가 서로 다른 값을 냅니다">event_ordering 지표 정의 (${eo.n}건)</h4>
    <div class="body">
      <table class="cmp"><tr><th>정의</th><th>쓰는 곳</th><th>값</th><th>0인 건</th><th>무엇을 재나</th></tr>
        <tr><td><b>nugget 평균</b></td><td>mem0 하네스</td><td><b>${eo.nugget}</b></td><td>–</td>
          <td class="small">rubric 항목을 언급했는가. <b>순서를 전혀 보지 않음</b></td></tr>
        <tr><td>tau_norm</td><td>BEAM 공식 리포트</td><td>${eo.tau_norm}</td>
          <td>${eo.tau_zero}/${eo.n}</td>
          <td class="small">순서. 언급 안 된 사건이 뒤로 몰려 '순서 맞음'이 되어 <b>적게 말할수록 유리</b></td></tr>
        <tr><td>final (tau×f1)</td><td>계산만 하고 버려짐</td><td>${eo.final_score}</td>
          <td class="bad-n">${eo.f1_zero}/${eo.n}</td>
          <td class="small">순서 + 회수율. 동등성 판정기가 짧은 rubric 라벨과 장황한 응답 줄을 못 맞춰 무너짐</td></tr>
      </table>
      <p class="small muted" style="margin-top:6px">보조 지표: precision ${eo.precision} · recall ${eo.recall} · f1 ${eo.f1}</p>
      <div class="noisebar" data-desc="같은 응답에 nugget 0.5, final 0.0 이 매겨진 사례가 실재합니다. 자세한 근거는 docs/mem0-classic-oss/beam-experiment.md §6">
        <b>대표값은 nugget 평균을 씁니다.</b> 셋 다 결함이 있으나 나머지 9개 능력과 척도가 같아 한 표에 놓을 수 있는 것이 이것뿐입니다.
        공식 수치를 인용할 때는 그쪽이 tau_norm 을 쓴다는 점을 반드시 밝혀야 합니다.
      </div>
    </div></div>`}

    <div class="card"><h4 data-desc="대화마다 주제와 저장 메모리 규모가 다릅니다. 저장이 적은 대화는 큰 cutoff 에서 검색이 작동하지 않습니다">대화별 (${d.n_convs})</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>대화</th><th>주제</th><th>청크</th><th data-desc="투입이 끝난 뒤 저장소에 남은 메모리 수">저장</th>
      ${CUT.map((k) => `<th>top-${k}</th>`).join("")}</tr>
      ${d.convs.map((c) => `<tr><td><b>${esc(c.conv)}</b></td><td class="small">${esc(c.category || "")}</td>
        <td>${c.chunks ?? "–"}</td><td><b>${c.stored ?? "–"}</b></td>
        ${CUT.map((k) => {
          const v = c.cells[String(k)];
          const over = c.stored != null && c.stored < k;
          return v == null ? `<td class="muted">–</td>`
            : `<td class="bcell" style="${beamHeat(v)}" data-desc="${esc(
                over ? `저장 ${c.stored}개로 cutoff ${k}보다 적습니다. <b>저장소를 전부 준 조건</b>입니다` : `평균 ${v}`)}"
                >${v.toFixed(3)}${over ? '<i class="bfull">▚</i>' : ""}</td>`;
        }).join("")}</tr>`).join("")}
    </table></div></div>`;

  // 사이드바가 HaluMem 세션 목록인 채로 남으면 다른 화면처럼 보임. 이 화면 전용 안내로 교체함
  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted"><b>BEAM</b> (ICLR 2026)<br>${esc(d.note || "")}</p>
    <p class="small muted" style="margin-top:10px"><b>점수 색</b></p>
    <div class="bleg">${[0, .25, .5, .75, 1].map((v) =>
      `<span style="${beamHeat(v)}">${v.toFixed(2)}</span>`).join("")}</div>
    <p class="small muted" style="margin-top:10px"><b>▚ 표시</b><br>저장 메모리가 cutoff 보다 적어 저장소를 전부 준 칸입니다. 검색이 작동하지 않았으므로 검색 품질로 읽으면 안 됩니다.</p>
    <p class="small muted" style="margin-top:10px"><b>읽는 법</b><br>전체 행만 보면 평평해 보입니다. 반대 방향 둘이 상쇄되기 때문입니다. 능력별로 나눠 보세요.</p>
    <p class="small muted" style="margin-top:10px">판독 근거는 <code>docs/mem0-classic-oss/beam-experiment.md</code></p>
    <p class="small muted" style="margin-top:10px">⚠ 이 화면은 HaluMem 과 데이터가 달라 상단바의 Generator·Judge 선택을 따르지 않습니다. 버킷마다 고정된 조합으로 집계됩니다.</p>
  </div>`;
  bindBeamMode();
  $$("#content .hrefsw:not(.beammode) button").forEach((b) => (b.onclick = () => { S.beamBucket = b.dataset.bk; renderBeam(); }));
  $$("#content td.bclick").forEach((td) => (td.onclick = () => {
    const [k, lab] = td.dataset.open.split("|");
    beamQuestions(k, lab);
  }));
}

// 능력 하나의 문항 목록. 어느 문항이 cutoff 에 따라 흔들리는지 보려는 화면임
async function beamQuestions(ability, label) {
  $("#jmodal").classList.remove("hidden");
  $("#jmodal-head").innerHTML = `<b>BEAM · ${esc(label)}</b>
    <span class="jchip">${esc(ability)}</span><span style="margin-left:auto"></span>
    <button class="jbtn" id="jm-close">✕</button>`;
  $("#jm-close").onclick = jmClose;
  const el = $("#jmodal-body");
  el.innerHTML = `<p class="muted" style="padding:20px">불러오는 중…</p>`;
  const d = await api(`/api/beam/questions?bucket=${encodeURIComponent(S.beamBucket)}&ability=${encodeURIComponent(ability)}`);
  const CUT = d.cutoffs;
  el.innerHTML = `<div style="padding:16px 20px;overflow-y:auto">
    <div class="jbasis" data-desc="같은 문항을 cutoff 만 바꿔 네 번 답변시키고 각각 채점했습니다. 흔들림이 큰 문항일수록 컨텍스트 양에 민감합니다.">
      문항 ${d.questions.length}개를 <b>흔들림(최대 − 최소)</b>이 큰 순으로 놓았습니다.
      <span class="small">흔들림이 크면 컨텍스트 양이 답을 좌우한다는 뜻이고, 0에 가까우면 몇 개를 주든 결과가 같습니다.</span>
    </div>
    <table class="cmp beam"><tr><th>대화</th><th>주제</th><th>문항</th><th>rubric</th>
      ${CUT.map((k) => `<th>top-${k}</th>`).join("")}<th data-desc="최대 − 최소">흔들림</th><th></th></tr>
      ${d.questions.map((q) => `<tr>
        <td><b>${esc(q.conv)}</b><span class="muted small">·q${q.idx}</span></td>
        <td class="small">${esc(q.category || "")}</td>
        <td class="qtxt small">${esc((q.question || "").slice(0, 90))}</td>
        <td>${q.n_rubric}</td>
        ${CUT.map((k) => {
          const v = q.cells[String(k)];
          const over = q.stored != null && q.stored < k;
          return v == null ? `<td class="muted">–</td>`
            : `<td class="bcell" style="${beamHeat(v)}">${v.toFixed(2)}${over ? '<i class="bfull">▚</i>' : ""}</td>`;
        }).join("")}
        <td class="bdelta ${q.spread >= 0.4 ? "down" : ""}">${(q.spread ?? 0).toFixed(2)}</td>
        <td><button class="jbtn" data-q="${esc(q.conv)}|${esc(ability)}|${q.idx}">상세</button></td>
      </tr>`).join("")}</table>
    <p style="margin-top:14px"><button class="jbtn" id="jm-back">✕ 닫기</button></p></div>`;
  $("#jm-back").onclick = jmClose;
  $$("#jmodal-body button[data-q]").forEach((b) => (b.onclick = () => {
    const [conv, ab, idx] = b.dataset.q.split("|");
    beamDetail(conv, ab, +idx, label);
  }));
}

// 문항 하나를 cutoff 4벌로 나란히. rubric 항목별 채점과 투입된 메모리까지 보여줌
async function beamDetail(conv, ability, idx, label) {
  const el = $("#jmodal-body");
  el.innerHTML = `<p class="muted" style="padding:20px">불러오는 중…</p>`;
  const d = await api(`/api/beam/question?bucket=${encodeURIComponent(S.beamBucket)}`
    + `&conv=${encodeURIComponent(conv)}&ability=${encodeURIComponent(ability)}&idx=${idx}`);
  const CS = d.cutoffs;
  const sc = (v) => `<span class="nsc n${String(v).replace(".", "")}">${v}</span>`;

  el.innerHTML = `<div style="padding:16px 20px;overflow-y:auto">
    <p class="small muted"><b>${esc(d.conv)}</b> · ${esc(d.category || "")} · ${esc(ability)}
      ${d.difficulty ? `· 난이도 ${esc(d.difficulty)}` : ""} · 저장 메모리 <b>${d.stored ?? "–"}</b>개</p>

    <div class="jsec"><h5 data-desc="BEAM 이 제시한 probing question 원문">문항</h5>
      <div class="jtarget">${esc(d.question)}</div></div>

    <div class="jsec"><h5 data-desc="채점은 이 항목들만 봅니다. judge 가 항목마다 0 / 0.5 / 1 을 매기고 그 평균이 문항 점수입니다">rubric (${d.rubric.length}개)</h5>
      <ol class="jl">${d.rubric.map((r) => `<li class="small">${esc(r)}</li>`).join("")}</ol></div>

    ${d.reference ? `<div class="jsec"><h5 data-desc="참고용입니다. 채점에는 쓰이지 않습니다">데이터셋이 제시한 정답</h5>
      <div class="jans small">${esc(String(d.reference))}</div></div>` : ""}

    <div class="jsec"><h5 data-desc="같은 문항을 컨텍스트 양만 바꿔 네 번 답변시킨 결과입니다. 행이 rubric 항목, 열이 cutoff 입니다">rubric × cutoff 채점</h5>
      <table class="cmp beam"><tr><th>rubric</th>${CS.map((c) =>
        `<th data-desc="${esc(`요청 ${c.cutoff}개 · 실제 투입 ${c.used}개`)}">top-${c.cutoff}<br>
          <span class="small muted">${c.used}개 투입</span></th>`).join("")}</tr>
        ${d.rubric.map((r, i) => `<tr><td class="qtxt small">${esc(r)}</td>
          ${CS.map((c) => {
            const n = (c.nugget_scores || [])[i];
            return !n ? `<td class="muted">–</td>`
              : `<td class="bcell" data-desc="${esc(n.reason || "")}">${sc(n.score)}</td>`;
          }).join("")}</tr>`).join("")}
        <tr class="jm-tot"><td><b>문항 점수</b></td>
          ${CS.map((c) => `<td class="bcell" style="${beamHeat(c.score)}"><b>${c.score.toFixed(3)}</b></td>`).join("")}</tr>
      </table></div>

    ${d.cutoffs.some((c) => c.event_ordering) ? `<div class="jsec">
      <h5 data-desc="이 능력만 공식 코드가 다른 계열의 지표를 씁니다. 세 값이 서로 다릅니다">event_ordering 보조 지표</h5>
      <table class="cmp"><tr><th>cutoff</th><th>nugget</th><th>tau_norm</th><th>f1</th><th>final</th></tr>
        ${CS.map((c) => { const e = c.event_ordering || {};
          return `<tr><td>top-${c.cutoff}</td><td><b>${c.score.toFixed(3)}</b></td>
            <td>${e.tau_norm ?? "–"}</td><td class="${e.f1 === 0 ? "bad-n" : ""}">${e.f1 ?? "–"}</td>
            <td>${e.final_score ?? "–"}</td></tr>`; }).join("")}</table></div>` : ""}

    <div class="jsec"><h5>생성된 답변</h5>
      ${CS.map((c) => `<details class="bans"><summary>top-${c.cutoff}
        <span class="muted small">${c.used}개 투입 · 점수 ${c.score.toFixed(3)}</span></summary>
        <div class="jans small">${esc(c.system_response || "(빈 답변)")}</div></details>`).join("")}</div>

    <details class="jrubric"><summary>검색된 메모리 ${d.retrieved.length}개 (상위 순)</summary>
      <div class="bmem">${d.retrieved.map((m, i) => `<div class="bm">
        <span class="bmi">${i + 1}</span>
        <span class="bmt">${esc(m.session_time || "")}</span>
        <span class="bmx">${esc(m.memory || "")}</span></div>`).join("")}</div></details>

    <p style="margin-top:14px"><button class="jbtn" id="jm-back">← 문항 목록</button>
      <button class="jbtn" id="jm-x">✕ 닫기</button></p></div>`;
  $("#jm-back").onclick = () => beamQuestions(ability, label);
  $("#jm-x").onclick = jmClose;
}

async function jmGoldQA() {
  const el = $("#jmodal-body");
  el.innerHTML = `<p class="muted" style="padding:20px">집계 중…</p>`;
  let d;
  // 실패를 삼키면 '집계 중…'에서 영원히 멈춘 것처럼 보인다. 원인을 화면에 그대로 띄운다
  try {
    d = await api("/api/gold-qa?uuid=" + encodeURIComponent(S.uuid || ""));
  } catch (e) {
    el.innerHTML = `<div style="padding:20px"><p><b>불러오기 실패</b></p><p class="small">${esc(e.message)}</p>
      <p style="margin-top:14px"><button class="jbtn" id="jm-back">← 검토 화면으로</button></p></div>`;
    $("#jm-back").onclick = jmRender; return;
  }
  if (!d.n) {
    el.innerHTML = `<div style="padding:20px"><p class="muted">아직 골든 정답 검수 주석이 없습니다.</p>
      <p class="small muted">세션 화면의 QA 항목마다 붙은 <b>정답검수</b> 버튼으로 라벨하면 여기에 쌓입니다.</p>
      <p style="margin-top:14px"><button class="jbtn" id="jm-back">← 검토 화면으로</button></p></div>`;
    $("#jm-back").onclick = jmRender; return;
  }
  const ORDER = ["valid", "wrong", "unanswerable"];
  const chip = (l) => `<span class="gq gq-${l}" data-desc="${esc(GQ_FIX[l] || "")}">${esc(GQ_LAB[l] || l)}</span>`;
  const done = d.n_items, tot = d.total_q || 0;

  // 문제 있음으로 합의된 문항 비율: §13의 '벤치마크 노이즈' 추정을 실측으로 바꾸는 값
  const bad = ORDER.slice(1).reduce((a, l) => a + (d.by_label[l] || 0), 0);

  el.innerHTML = `<div style="padding:16px 20px;overflow-y:auto">
    <div class="jbasis" data-desc="judge 판정 검토는 '채점이 맞았나'를 묻습니다. 이 화면은 '문항이 애초에 채점 기준으로 쓸 만한가'를 묻습니다. 그래서 IAA 집계와 섞지 않고 따로 봅니다.">
      <b>📝 이 화면이 묻는 것</b>: <b>골든 정답 자체가 채점 기준으로 타당한지</b>입니다. judge의 채점 품질을 보는 화면과는 다릅니다.
      <span class="small">라벨 셋은 '문제 있음'을 <b>원인별로</b> 가릅니다. 고치는 방법이 각각 다르기 때문입니다:
      ${ORDER.slice(1).map((l) => `${chip(l)} ${GQ_FIX[l].replace(/<b>|<\/b>/g, "")}`).join(" · ")}</span>
    </div>

    <h4 style="margin:14px 0 6px" data-desc="대상은 ${esc(d.user_name)} 님의 QA 문항 전수입니다 (생성 QA 세션 제외). 분석가별 막대는 각자 라벨한 문항 수">진행 현황: ${esc(d.user_name)} · 문항 ${tot}개</h4>
    <p class="small muted">라벨된 문항 <b>${done}</b>개 (${tot ? (done / tot * 100).toFixed(0) : 0}%) · 주석 ${d.n}건 · 2인 이상 겹친 문항 ${d.pairs.reduce((a, p) => Math.max(a, p.n), 0)}개</p>
    <div class="qprog">${d.progress.map((a) => `<div class="qrow">
      <span class="qwho">${esc(a.annotator)}</span>
      <div class="qbar"><span class="qseg qs-qa" style="width:100%"><i style="width:${tot ? a.n / tot * 100 : 0}%"></i></span></div>
      <span class="qnum"><b>${a.n}</b><span class="muted">/${tot}</span> <span class="qpct${tot && a.n >= tot ? " done" : ""}">${tot ? (a.n / tot * 100).toFixed(0) : 0}%</span></span>
    </div>`).join("")}</div>

    <h4 style="margin:16px 0 6px" data-desc="여러 분석가가 라벨한 문항은 다수결(동률이면 '동률')로 합의 라벨을 정합니다">합의 라벨 분포</h4>
    <p class="confrow">${Object.entries(d.by_label).sort((a, b2) => ORDER.indexOf(a[0]) - ORDER.indexOf(b2[0]))
      .map(([l, n]) => `<span class="cf">${chip(l)} <b>${n}</b><span class="muted">건 ${(n / done * 100).toFixed(0)}%</span></span>`).join("")}</p>
    ${bad ? `<p class="small" data-desc="이 비율이 벤치마크 자체의 결함 몫입니다. 라벨된 문항이 적을 때는 표본 편향(분석가가 의심스러운 문항부터 볼 수 있음)에 주의하세요">
      → 라벨된 ${done}개 중 <b>${bad}개(${(bad / done * 100).toFixed(0)}%)</b>가 문항 결함으로 합의됐습니다.
      ${done < 30 ? `<span class="muted">⚠ 아직 표본이 작습니다 (${done}/${tot}): 전수 대비 비율로 읽지 마세요.</span>` : ""}</p>` : ""}

    ${!d.pairs.length ? "" : `
    <h4 style="margin:16px 0 6px" data-desc="같은 문항을 두 분석가가 모두 라벨한 경우만 집계됩니다. κ는 우연 일치 보정값">분석가 간 일치도</h4>
    <table class="cmp"><tr><th>쌍</th><th>공통 문항</th><th>일치율</th><th>Cohen κ</th></tr>
      ${d.pairs.map((p) => `<tr><td>${esc(p.a)} ↔ ${esc(p.b)}</td><td>${p.n}</td><td>${p.agree}%</td>
        <td>${kFmt(p)} <span class="muted small">${kGrade(p.kappa)}</span></td></tr>`).join("")}</table>`}

    ${!d.cross.length ? "" : `
    <h4 style="margin:18px 0 6px" data-desc="사람이 매긴 문항 품질 라벨과, 그 문항에서 실제 시스템들이 맞혔는지를 교차한 표입니다. '근거 없음'인데 모든 시스템이 틀렸다면 그 진단이 데이터로 확인된 것입니다.">라벨 × 시스템 정답률</h4>
    <div class="jbasis" data-desc="정답률은 각 런의 judge 채점(Correct 비율)입니다. '전멸'은 그 문항을 맞힌 런이 하나도 없는 경우: 원리적으로 불가하다는 주장의 직접 증거입니다.">
      <b>왜 보는가</b>: 라벨이 <b>주관적 인상이 아님</b>을 확인하는 대조입니다.
      <span class="small">'근거 없음'으로 본 문항의 정답률이 '타당' 문항보다 뚜렷이 낮고 <b>전멸</b> 건수가 몰려 있다면, 사람 판정과 시스템 실패가 같은 곳을 가리키는 것입니다.
      반대로 차이가 없다면 그 라벨은 재검토 대상입니다.</span>
    </div>
    <table class="cmp"><tr><th>합의 라벨</th><th>문항</th>${d.runs.map((r) => `<th data-desc="${esc(runLabel(r))}">${esc(runLabel(r))}</th>`).join("")}
      <th data-desc="모든 런이 틀린 문항 수. 시스템 성능보다 문항 자체의 문제일 가능성을 봅니다">전멸</th></tr>
      ${d.cross.map((c) => `<tr><td>${chip(c.label)}</td><td>${c.n}</td>${d.runs.map((r) => {
        const v = c.runs[r];
        return !v ? `<td class="muted">–</td>` : `<td data-desc="${esc(`${runLabel(r)} · ${GQ_LAB[c.label]} 문항 ${v.n}개 중 ${v.ok}개 정답`)}">${v.pct}% <span class="muted small">${v.ok}/${v.n}</span></td>`;
      }).join("")}<td>${c.none_solved ? `<b>${c.none_solved}</b>` : "0"}<span class="muted small">/${c.n}</span></td></tr>`).join("")}</table>`}

    <h4 style="margin:18px 0 6px" data-desc="문항을 클릭하면 검토 화면이 열립니다">검수한 문항 (${d.items.length})</h4>
    <table class="cmp gqi"><tr><th>세션</th><th>질문</th><th>골든 정답</th><th>합의</th><th>분석가</th>
      <th data-desc="이 문항을 맞힌 런 수 / 채점된 런 수">시스템</th><th></th></tr>
      ${d.items.map((it) => `<tr><td>S${it.session_id}<span class="muted small">·q${it.idx}</span></td>
        <td class="qtxt">${esc(it.question || "")}</td>
        <td class="qtxt">${esc(it.answer || "")}${it.gt ? `<br><span class="small" data-desc="분석가가 제안한 대안 정답">✎ ${esc(it.gt)}</span>` : ""}${it.note ? `<br><span class="small muted">${esc(it.note)}</span>` : ""}</td>
        <td>${it.consensus ? chip(it.consensus) : `<span class="muted small">동률</span>`}</td>
        <td class="small">${Object.entries(it.labels).map(([a, l]) => `${esc(a)} ${chip(l)}`).join(" ")}</td>
        <td>${it.n_systems ? `${it.solved_by.length}/${it.n_systems}${it.solved_by.length === 0 ? ' <span class="gq gq-unanswerable">전멸</span>' : ""}` : `<span class="muted">–</span>`}</td>
        <td>${jmBtn({ run: S.run, uuid: d.uuid, session_id: it.session_id, rec_type: "gold_qa", idx: it.idx, generator: S.generator })}</td></tr>`).join("")}</table>

    <p style="margin-top:14px"><button class="jbtn" id="jm-back">← 검토 화면으로</button></p></div>`;
  $("#jm-back").onclick = jmRender;
  bindJmButtons(el);
}

async function jmIAA() {
  const el = $("#jmodal-body");
  el.innerHTML = `<p class="muted" style="padding:20px">집계 중…</p>`;
  let d;
  // 실패를 삼키면 '집계 중…'에서 영원히 멈춘 것처럼 보인다. 원인을 화면에 그대로 띄운다
  try {
    d = await api("/api/iaa");
  } catch (e) {
    el.innerHTML = `<div style="padding:20px"><p><b>불러오기 실패</b></p><p class="small">${esc(e.message)}</p>
      <p style="margin-top:14px"><button class="jbtn" id="jm-back">← 검토 화면으로</button></p></div>`;
    $("#jm-back").onclick = jmRender; return;
  }
  // 렌더 중 예외도 화면에 띄운다. 안 그러면 innerHTML이 안 채워져 '집계 중…'에 갇힌다
  try {
  const jb = d.judge_basis || {};
  const pg = d.progress;
  const TYPES = ["integrity", "accuracy", "update", "qa"];

  // 큐 진척: 표 대신 막대. 유형별 몫만큼 칸을 나누고 그 안을 완료분으로 채운다.
  // (표가 많아 화면이 답답해지므로, 진척처럼 한눈에 볼 값은 그래픽으로 둔다)
  const progressBlock = !pg ? "" : `
    <h4 style="margin:14px 0 6px" data-desc="공유 표본 큐 ${pg.queue_n}건 중 몇 건을 라벨했는지. 분석가 간 일치도는 이 큐에서만 쌓입니다. 막대는 유형별 몫만큼 칸이 나뉘고, 칸 안의 채워진 부분이 완료분입니다">큐 진척</h4>
    <div class="qprog">${pg.annotators.map((a) => {
      const pct = a.in_queue / pg.queue_n * 100;
      return `<div class="qrow">
        <span class="qwho">${esc(a.annotator)}</span>
        <div class="qbar">${TYPES.map((t) => {
          const tot = pg.queue_types[t] || 0, done = a.by_type[t] || 0;
          if (!tot) return "";
          return `<span class="qseg qs-${t}" style="width:${tot / pg.queue_n * 100}%"
            data-desc="${esc(`<b>${REC_NAMES[t]}</b><br>${done} / ${tot}건 완료 (${(done / tot * 100).toFixed(0)}%)`)}"><i style="width:${done / tot * 100}%"></i></span>`;
        }).join("")}</div>
        <span class="qnum"><b>${a.in_queue}</b><span class="muted">/${pg.queue_n}</span> <span class="qpct${pct >= 99.5 ? " done" : ""}">${pct.toFixed(0)}%</span></span>
        ${a.out ? `<span class="qout" data-desc="큐에 없는 항목을 개별 검토로 라벨한 건수: 기회 표본이라 위 집계에서는 제외됩니다">큐 밖 ${a.out}</span>` : ""}
      </div>`;
    }).join("")}
      <div class="qleg">${TYPES.map((t) => `<span class="qk qs-${t}">${esc((REC_NAMES[t] || t).replace(/\s*\(.*\)$/, ""))} <i>${pg.queue_types[t] || 0}</i></span>`).join("")}</div>
    </div>`;

  const rowHTML = (labelHTML, o, extra = "") => `<tr><td>${labelHTML}</td><td>${o.n}</td><td>${o.agree}%</td>
    <td>${kFmt(o)} <span class="muted small">${kGrade(o.kappa)}</span></td>${extra}</tr>`;
  const row = (label, o, extra = "") => rowHTML(esc(label), o, extra);

  el.innerHTML = `<div style="padding:16px 20px;overflow-y:auto">
    <h4 style="margin:0 0 6px">진행 현황</h4>
    <p class="small muted">주석 ${d.total}건 · 라벨 완료 ${d.labeled}건 · 항목 ${d.items}개 (2인 이상 겹친 항목 <b>${d.overlap_items}</b>개) · judge와 대조 가능 ${d.comparable}건 · 👍${d.agree_clicks.agree} 👎${d.agree_clicks.disagree}</p>
    ${d.hidden_labeled ? `<p class="small muted" data-desc="custom 프롬프트 갈래는 정성분석 결과 품질이 낮아 화면에서 숨겼지만, 이미 완료된 판정 검토 작업이 걸려 있어 주석은 지우지 않았습니다. 아래 집계에는 그대로 포함되며, 이전에 보고한 수치가 그대로 재현됩니다.">ℹ 이 중 <b>${d.hidden_labeled}건</b>은 화면에서 숨긴 런(${esc(d.hidden_runs.join(", "))})의 주석입니다. 보고 수치 재현을 위해 집계에는 그대로 포함됩니다.</p>` : ""}

    <div class="jbasis" data-desc="${esc(jb.note || "")}">
      <b>⚖ 'judge 판정'의 정의</b>: <b>${esc(jb.label || "judge")}로 동일 입력을 반복 채점한 결과의 다수결</b>입니다. 단일 채점본을 쓰지 않습니다.
      단일 회차와 대조하면 그 회차에 judge가 우연히 흔들린 값과 분석가를 비교하게 돼 일치율이 깎입니다.
      <span class="small">반복 2회 이상인 항목 <b>${jb.multi_items || 0}</b>개. 그중 만장일치 ${jb.unanimous || 0}, <b>동률이라 합의 없음 ${jb.tie || 0}</b>(대조에서 제외).
      반복본이 없는 항목 ${jb.fallback_items || 0}개는 단일 채점본으로 대조합니다.</span>
    </div>
    ${progressBlock}

    <h4 style="margin:14px 0 6px" data-desc="같은 항목을 두 분석가가 모두 라벨한 경우만 집계됩니다. κ는 우연 일치를 보정한 값: 대괄호는 부트스트랩 95% 신뢰구간">분석가 간 일치도 (IAA)</h4>
    <table class="cmp"><tr><th>쌍</th><th>공통 항목</th><th>일치율</th><th>Cohen κ</th><th>유형별 일치율</th></tr>
      ${d.annotator_pairs.map((p) => row(`${p.a} ↔ ${p.b}`, p,
        `<td class="small">${(p.by_type || []).map((b) => `${recTag(b.rec_type)} ${b.agree}%<span class="muted">(${b.n})</span>`).join(" · ") || "–"}</td>`)).join("")
        || `<tr><td colspan="5" class="muted">겹친 항목 없음. 공유 큐로 라벨하면 채워집니다</td></tr>`}</table>

    ${!(d.matrix_refs || []).length ? "" : `<div id="jm-mtx">${iaaMatrixHTML(d)}</div>`}

    ${d.by_type.filter((p) => (p.confusion || []).some((c) => c.mine !== c.judge)).map((p) => `
      <h4 style="margin:12px 0 4px" data-desc="분석가가 어느 방향으로 judge와 어긋나는지: 한 방향으로 쏠려 있으면 체계적 이견(기준 차이), 흩어져 있으면 경계선 흔들림입니다">${recTag(p.rec_type)} 불일치 방향</h4>
      <p class="small confrow">${p.confusion.filter((c) => c.mine !== c.judge).slice(0, 10)
        .map((c) => `<span class="cf" data-desc="${esc(`<b>${c.annotator}</b> 님이 ${labDesc(p.rec_type, c.mine)}로 봤고, judge 합의는 ${labDesc(p.rec_type, c.judge)}였습니다.`)}"><b class="who">${esc(c.annotator)}</b> ${labChip(p.rec_type, c.mine)} <span class="arr">→</span> judge ${labChip(p.rec_type, c.judge)} <span class="muted">${c.n}건</span></span>`).join("")}</p>`).join("")}
    <h4 style="margin:18px 0 6px" data-desc="완전히 동일한 입력을 같은 judge로 여러 번 채점했을 때의 결과: judge 자체의 재현성입니다 (분석가와 무관)">judge 자기 일관성 (동일 입력 반복 채점)</h4>
    <div id="jc-box" class="small muted">집계 중…</div>
    <p style="margin-top:14px"><button class="jbtn" id="jm-back">← 검토 화면으로</button></p></div>`;
  $("#jm-back").onclick = jmRender;
  // 기준 전환은 표만 갈아끼운다 (전체 재렌더 금지: 스크롤 위치가 유지되고 재요청도 없다)
  const bindHref = () => $$("#jmodal-body .hrefsw button").forEach((b) => (b.onclick = () => {
    JM.href = b.dataset.href;
    $("#jm-mtx").innerHTML = iaaMatrixHTML(d);
    bindHref();
  }));
  bindHref();
  } catch (e) {
    el.innerHTML = `<div style="padding:20px"><p><b>화면 구성 실패</b> <span class="small muted">(데이터 수신은 정상)</span></p><p class="small">${esc(e.message)}</p><pre class="small" style="white-space:pre-wrap;max-height:240px;overflow:auto">${esc(e.stack || "")}</pre><p style="margin-top:14px"><button class="jbtn" id="jm-back">← 검토 화면으로</button></p></div>`;
    $("#jm-back").onclick = jmRender;
    console.error("jmIAA render failed", e, d);
    return;
  }
  try {
    const jc = await api("/api/judge-consistency");
    $("#jc-box").innerHTML = !jc.rows.length ? `<p class="small muted">${esc(jc.note || "반복 채점 세트 없음")}</p>` : `
      <p class="small muted">${esc(runLabel(jc.run))} · ${jc.reps || jc.judges.length}회 반복 · 유저 ${jc.users}명: 같은 입력을 같은 judge로 반복 채점</p>
      <table class="cmp"><tr><th>레코드</th><th>항목</th><th data-desc="모든 회차가 같은 라벨을 준 항목 비율">전회 일치</th><th data-desc="다수결 라벨과 다르게 매긴 판정의 비율">다수결 이탈</th><th data-desc="다수결 라벨별 흔들린 항목 비율: 판정이 애매한 구간에 불안정성이 집중됩니다">라벨별 흔들림</th></tr>
        ${jc.rows.map((r) => `<tr><td>${esc(r.rec_type)}</td><td>${r.n.toLocaleString()}</td><td>${r.unanimous}%</td><td>${r.deviation}%</td>
          <td class="small">${Object.entries(r.by_major).map(([k, v]) => `${esc(k)}: ${v.unstable}% <span class="muted">(${v.n})</span>`).join(" · ")}</td></tr>`).join("")}</table>`;
  } catch (e) { $("#jc-box").innerHTML = `<p class="small muted">불러오기 실패: ${esc(e.message)}</p>`; }
}

// 행에 붙는 진입 버튼
const jmBtn = (ctx, side = "") => `<button class="jopen${ctx.rec_type === "gold_qa" ? " gold" : side ? " s-" + side.toLowerCase() : ""}" data-jm="${esc(JSON.stringify(ctx))}" data-desc="${ctx.rec_type === "gold_qa" ? "이 문항의 골든 정답이 채점 기준으로 타당한지 검수합니다 (judge 판정과 별개 항목)" : `judge가 이 항목을 판정할 때 받았던 입력(${esc(REC_NAMES[ctx.rec_type])} · ${esc(runLabel(ctx.run))})을 그대로 재현해 직접 평가합니다`}">${ctx.rec_type === "gold_qa" ? "정답검수" : "검토" + (side ? " " + side : "")}</button>`;
function bindJmButtons(root = document) {
  $$(".jopen", root).forEach((b) => (b.onclick = (ev) => {
    ev.stopPropagation();
    JM.queue = null; JM.qi = -1;
    jmOpen(JSON.parse(b.dataset.jm));
  }));
}

boot().catch((e) => { document.body.innerHTML = `<pre style="padding:20px;color:#c92a2a">${esc(e.stack || e.message)}</pre>`; });

/* ==================== Memora (ACL 2026 Findings) ====================
   BEAM 화면과 달리 관심사가 '검색 예산'이 아니라 **저장소 행동**임. 삭제까지 재는 첫
   벤치마크라, 데이터셋이 의도한 연산과 mem0 가 실제로 한 연산을 나란히 놓는 것이 핵심임.
   판독 근거는 docs/mem0-classic-oss/memora-experiment.md */

S.memoraPeriod = "weekly";

// 수행률 색. 100%가 기준이고 모자랄수록 붉어짐. 100을 넘는 것(추가)은 회색으로 중립 처리
function rateHeat(v, neutralOver) {
  if (v == null) return "background:var(--chip)";
  if (neutralOver && v > 120) return "background:var(--sunk);color:var(--faint)";
  const t = Math.max(0, Math.min(1, v / 100));
  const h = t * 120;
  return `background:hsl(${h} 62% ${92 - t * 14}%);color:hsl(${h} 70% 24%)`;
}

// 검색 예산(cutoff) 스윕. 저장소·검색은 그대로 두고 답변에 넣는 개수만 바꾼 팔들임.
// 이 카드가 답하는 질문: mem0 의 병목이 저장인가 검색인가.
async function memoraCutoffCard(period) {
  let d;
  try { d = await api(`/api/memora/cutoff?period=${encodeURIComponent(period)}`); }
  catch (e) { return ""; }
  if (!d.ready) return "";
  const T = d.tasks || {}, c = d.coverage || {}, ne = c.natural_experiment || {};
  const n2 = (v) => (v == null ? "–" : v.toFixed(2));
  const sig = (p) => (p != null && p < 0.05);

  return `<div class="card"><h4 data-desc="Stage A를 k=800으로 한 번만 돌리고 답변 단계에서 잘라 만든 팔들입니다. 저장소도 검색 결과도 동일하고 답변에 넣은 개수만 다르므로 팔 사이 비교는 깨끗합니다.">검색 예산(cutoff) 스윕 · ${esc(period)}</h4>
  <div class="body mscroll" style="padding:0">
  <table class="cmp beam"><tr>
    <th data-desc="답변 컨텍스트에 넣은 메모리 개수. 50이 Memora 공식 기본값입니다">cutoff</th>
    <th data-desc="문항의 memory_evidence 조각 중 이 cutoff 안에 들어온 비율. 답변 모델이 실제로 본 재료입니다">근거 도달</th>
    <th>전체 FAMA</th>
    ${Object.keys(T).map((t) => `<th data-desc="${esc(t)} FAMA">${esc(T[t])}</th>`).join("")}
    <th data-desc="remembering의 MPA. 이 실험이 겨냥한 지표입니다">기억 MPA</th>
    <th>답변 길이</th></tr>
    ${d.arms.map((a) => `<tr>
      <td class="brow"><b>${a.cutoff}</b>${a.cutoff === 50 ? '<br><span class="small muted">공식</span>' : ""}</td>
      <td class="bcell" style="${beamHeat(a.coverage == null ? null : a.coverage / 100)}">${a.coverage == null ? "–" : a.coverage.toFixed(1) + "%"}</td>
      <td class="bcell" style="${beamHeat(a.overall.fama / 100)}"><b>${n2(a.overall.fama)}</b></td>
      ${Object.keys(T).map((t) => {
        const v = a.by_task[t] || {};
        return `<td class="bcell" style="${beamHeat(v.fama == null ? null : v.fama / 100)}">${v.fama == null ? "–" : v.fama.toFixed(1)}</td>`;
      }).join("")}
      <td>${n2((a.by_task.remembering || {}).mpa)}</td>
      <td class="small">${a.len_median == null ? "–" : a.len_median.toLocaleString()}</td></tr>`).join("")}
  </table></div>

  ${(() => {
    // "저장소 전량" 지점은 기간마다 다름 (weekly 200 · monthly 800 · quarterly 2000).
    // 800 으로 하드코딩했다가 weekly·quarterly 에서 틀린 값을 보여줬음. 곡선의 제일 큰 키를 씀
    const cc = d.coverage_curve || {};
    const keys = Object.keys(cc).map(Number).filter((x) => !isNaN(x)).sort((a, b) => a - b);
    const top = keys.length ? keys[keys.length - 1] : null;
    const full = top == null ? null : cc[String(top)];
    return `<div class="body"><span class="small">
    <b>저장소가 페르소나당 ${c.store_median == null ? "?" : c.store_median.toLocaleString()}개입니다.</b>
    ${top == null ? "" : `cutoff ${top.toLocaleString()}은 검색 필터가 사실상 없는 상태입니다.`} 그런데도 근거 도달은
    <b>${full == null ? "–" : full.toFixed(1) + "%"}</b>가 상한이고,
    mem0가 한 번이라도 뽑은 것은 <b>${c.extracted == null ? "–" : c.extracted.toFixed(1) + "%"}</b>입니다.
    <b>그 차이는 검색을 아무리 늘려도 못 되찾습니다</b> (뽑았다가 지우거나 덮어쓴 몫).
  </span></div>`;
  })()}

  <div class="jbasis" data-desc="k를 올리면 어떤 문항에는 새 근거가 들어오고, 어떤 문항에는 무관한 메모리만 늘어납니다. 그 둘을 갈라 보면 점수 변화가 근거 때문인지 아닌지 알 수 있습니다.">
    <b>스윕 안의 자연 대조</b> (k${ne.lo} → k${ne.hi}, ${esc(c.task || "remembering")})
    <table class="cmp beam" style="margin-top:8px"><tr><th>문항 갈래</th><th>문항</th><th>MPA 변화</th><th>부호검정</th></tr>
      <tr><td class="brow"><b>근거가 새로 들어옴</b></td><td>${ne.gain?.n ?? "–"}</td>
        <td class="bdelta ${(ne.gain?.mean_d_mpa ?? 0) > 0 ? "up" : "down"}"><b>${ne.gain?.mean_d_mpa >= 0 ? "+" : ""}${n2(ne.gain?.mean_d_mpa)}</b></td>
        <td class="small">개선 ${ne.gain?.up} · 악화 ${ne.gain?.down} · <b${sig(ne.gain?.p) ? ' style="color:var(--ok-ink)"' : ""}>p=${(ne.gain?.p ?? 1).toFixed(4)}</b></td></tr>
      <tr><td class="brow">주변 메모리만 늘어남 <span class="small muted">(대조군)</span></td><td>${ne.control?.n ?? "–"}</td>
        <td class="bdelta">${ne.control?.mean_d_mpa >= 0 ? "+" : ""}${n2(ne.control?.mean_d_mpa)}</td>
        <td class="small">개선 ${ne.control?.up} · 악화 ${ne.control?.down} · p=${(ne.control?.p ?? 1).toFixed(3)}</td></tr>
    </table>
    <span class="small"><b>근거가 도착한 곳에서만 점수가 올랐습니다.</b> 검색 예산이 실제 병목이라는 직접 증거입니다
    (문항별 근거 증가량과 MPA 증가량의 Spearman ρ = ${ne.rho >= 0 ? "+" : ""}${(ne.rho ?? 0).toFixed(3)}).
    대조군은 유의하지 않으므로 <b>긴 컨텍스트가 답변을 희석시킨다는 주장은 하지 않습니다.</b></span>
  </div>

  ${!d.noise ? "" : `<div class="noisebar" data-desc="cutoff ${d.noise.cutoff} 팔은 기존 monthly 레인과 같은 설정입니다. 투입부터 채점까지 전 구간을 다시 돌린 것이라 두 값의 차이가 파이프라인 재실행 노이즈입니다.">
    <b>📏 이 표를 읽는 기준: 과제 단위 재실행 노이즈 ±${n2(d.noise.max_task_fama)}</b>
    <span class="small">cutoff ${d.noise.cutoff} 팔과 기존 ${esc(period)} 레인은 같은 설정인데
    ${Object.keys(T).map((t) => `${esc(T[t])} FAMA ${d.noise.by_task[t]?.fama >= 0 ? "+" : ""}${n2(d.noise.by_task[t]?.fama)}`).join(" · ")}
    만큼 벌어졌습니다 (전체 FAMA ${d.noise.overall.fama >= 0 ? "+" : ""}${n2(d.noise.overall.fama)}).
    <b>한 칸씩의 차이는 전부 이 폭 안입니다.</b> 끝점(k${d.arms[0].cutoff} 대 k${d.arms[d.arms.length - 1].cutoff})과,
    네 점이 모두 단조라는 사실만 읽으세요.</span></div>`}
  </div>`;
}

async function renderMemora() {
  const el = $("#content");
  el.innerHTML = `<p class="muted">집계 중…</p>`;
  let d;
  try {
    d = await api(`/api/memora?period=${encodeURIComponent(S.memoraPeriod)}`);
  } catch (e) {
    el.innerHTML = `<p><b>불러오기 실패</b></p><p class="small">${esc(e.message)}</p>`;
    return;
  }
  const pick = (d.periods || []).map((p) =>
    `<button class="seg${p.key === d.period ? " on" : ""}" data-mp="${esc(p.key)}"
      ${p.ready ? "" : "disabled"} data-desc="${esc(p.note || "")}${p.ready ? "" : "<br><b>아직 채점본이 없습니다</b>"}"
      >${esc(p.label)}</button>`).join("");
  const bind = () => $$("#content .hrefsw button").forEach((b) =>
    (b.onclick = () => { S.memoraPeriod = b.dataset.mp; renderMemora(); }));

  if (!d.ready) {
    el.innerHTML = `<p class="hrefsw"><b>기간</b>${pick}</p>
      <p class="muted">이 기간은 아직 채점본이 없습니다.</p>`;
    bind();
    return;
  }

  const n2 = (v) => (v == null ? "–" : v.toFixed(2));
  const TASKS = d.tasks || {};
  const rho = d.delete_faa_rho;

  // 연산 수행률. 추가는 100%를 크게 넘는 것이 정상이라 따로 설명을 붙임
  const opRow = (label, intent, actual, key, neutral, desc) => {
    const rate = intent ? (100 * actual / intent) : null;
    return `<tr><td class="brow"><b>${label}</b></td>
      <td>${intent.toLocaleString()}</td><td>${actual.toLocaleString()}</td>
      <td class="bcell" style="${rateHeat(rate, neutral)}" data-desc="${esc(desc)}">
        <b>${rate == null ? "–" : rate.toFixed(1) + "%"}</b></td></tr>`;
  };

  el.innerHTML = `
    <p class="hrefsw" data-desc="기간마다 세션 수와 누적 갱신·삭제 횟수가 다릅니다. 대화 집합도 다르므로 기간 간 절대 비교는 하지 마세요."><b>기간</b>${pick}</p>
    <p class="small muted">${esc(d.note || "")}<br>페르소나 ${d.n_personas} · 세션 ${d.n_sessions.toLocaleString()} · 문항 ${d.n_questions} · 평가 기준 ${d.n_criteria.toLocaleString()} · 저장 메모리 ${d.stored.toLocaleString()}개</p>

    <div class="jbasis" data-desc="FAMA = max(0, MPA − λ(1−FAA)). MPA는 넣어야 할 것을 넣은 비율, FAA는 빼야 할 것을 뺀 비율, λ는 문항의 forgetting 기준 비중입니다.">
      <b>이 화면이 묻는 것</b>: 기억한 것만이 아니라 <b>잊어야 할 것을 잊었는가</b>.
      <span class="small"><b>MPA</b>는 정답을 넣었는지만 봅니다. <b>FAMA</b>는 거기서 무효·삭제된 정보를 끌어다 쓴 만큼 깎습니다. 둘의 차이가 <b>페널티</b>입니다.</span>
    </div>

    <div class="noisebar" data-desc="한 문항의 답변을 500토큰으로 자르면 recommending 페널티가 36→28로 줄지만 remembering MPA가 77→60으로 무너집니다(스모크 실측). 반면 문항끼리 견주면 긴 답변일수록 FAA가 오히려 높습니다(r=+0.16~+0.29, 3기간). 층위가 다른 두 관찰이라 길이로 문항 간 점수차를 설명하면 안 됩니다.">
      <b>📏 길이로 점수차를 설명하지 마세요</b>
      <span class="small">이 레인의 답변 길이 중앙값은 <b>${d.len_median == null ? "–" : d.len_median.toLocaleString()}자</b>(최대 ${d.len_max == null ? "–" : d.len_max.toLocaleString()}자)이고, 공식 하네스는 500토큰에서 끊습니다. 한 답변을 잘라보면 페널티가 줄지만, <b>문항끼리 견주면 긴 답변이 오히려 FAA가 높습니다</b>(r=+0.16~+0.29). 길이를 통제해도 MPA↔FAA 상충은 그대로 남습니다(편상관 −0.19~−0.43).</span>
    </div>

${await memoraCutoffCard(d.period)}

    ${!d.compare || d.compare.length < 2 ? "" : `
    <div class="card"><h4 data-desc="기간이 길수록 저장소가 커집니다. 이 벤치마크가 실제로 묻는 축입니다">기간 비교: 저장소가 커질 때</h4>
    <div class="body mscroll" style="padding:0">
    <table class="cmp beam"><tr><th>기간</th>
      <th data-desc="페르소나 한 명당 저장된 메모리 수. 기간이 길수록 커지는 실제 원인 변수입니다">저장/인</th>
      <th data-desc="최종 점수">FAMA</th>
      <th data-desc="넣어야 할 것을 넣은 비율">MPA</th>
      <th data-desc="빼야 할 것을 뺀 비율. 올라간다고 좋아진 것이 아닙니다 - 아래 설명을 보세요">FAA</th>
      <th>페널티</th>
      ${Object.keys(TASKS).map((t) => `<th data-desc="${esc(t)} FAMA">${esc(TASKS[t])}</th>`).join("")}
      <th data-desc="mem0 DELETE 이벤트 수 ÷ 데이터셋 의도 수. 대상 일치는 확인하지 않은 개수 비율입니다">삭제</th></tr>
      ${d.compare.map((c) => `<tr${c.key === d.period ? ' class="jm-tot"' : ""}>
        <td class="brow bclick" data-mperiod="${esc(c.key)}"><b>${esc(c.label)}</b>
          <br><span class="small muted">세션 ${c.sessions.toLocaleString()}</span></td>
        <td class="small">${c.stored_each.toLocaleString()}</td>
        <td class="bcell" style="${beamHeat(c.overall.fama / 100)}"><b>${c.overall.fama.toFixed(2)}</b></td>
        <td class="bcell" style="${beamHeat(c.overall.mpa / 100)}">${c.overall.mpa.toFixed(2)}</td>
        <td class="small">${c.overall.faa == null ? "–" : c.overall.faa.toFixed(2)}</td>
        <td class="bdelta down">−${c.overall.penalty.toFixed(2)}</td>
        ${Object.keys(TASKS).map((t) => {
          const v = c.by_task[t] || {};
          return `<td class="bcell" style="${beamHeat(v.fama == null ? null : v.fama / 100)}">${v.fama == null ? "–" : v.fama.toFixed(1)}</td>`;
        }).join("")}
        <td class="bcell" style="${rateHeat(c.delete_rate, true)}">${c.delete_rate == null ? "–" : c.delete_rate.toFixed(0) + "%"}</td></tr>`).join("")}
    </table></div>
    <div class="body"><span class="small">
      <b>⚠ FAA가 올라가는 것을 성과로 읽지 마세요.</b> 저장소가 커질수록 FAA는 오르고 MPA는 무너집니다.
      같은 원인입니다 - 검색이 정답을 못 꺼내오면 무효 항목도 같이 안 꺼내옵니다. 문항 단위로 봐도
      MPA와 FAA는 서로 반대로 움직이고(Spearman −0.21 / −0.47 / −0.26), 답변 길이를 통제해도 남습니다
      (편상관 −0.19 / −0.43 / −0.22). <b>페널티가 줄어드는 것도 잘 잊어서가 아니라 꺼내온 것이 없어서입니다.</b>
      <br><br><b>기간끼리 문항이 짝지어지지 않습니다.</b> 문항 stem 교집합이 71~82개이고 문구까지 같은 것은 26~27개뿐입니다
      (&quot;this week&quot;가 &quot;this month&quot;로 바뀌는 식). 기간별 집계끼리 견주는 것은 이 벤치마크의 설계 의도지만,
      <b>차이를 특정 문항에 귀속하지는 마세요.</b>
    </span></div></div>`}

    <div class="card"><h4 data-desc="과제 이름을 클릭하면 그 과제의 문항 목록을 FAMA 낮은 순으로 봅니다">과제별</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>과제</th><th>문항</th>
      <th data-desc="FAMA = max(0, MPA − λ(1−FAA)). 최종 점수">FAMA</th>
      <th data-desc="넣어야 할 것을 넣은 비율. 망각을 안 보는 종래 지표">MPA</th>
      <th data-desc="빼야 할 것을 뺀 비율. forgetting 기준이 있는 문항만으로 평균냅니다">FAA</th>
      <th data-desc="MPA − FAMA. 무효 메모리를 끌어다 쓴 대가">페널티</th>
      <th data-desc="이 과제의 평가 기준 수. 넣기(memory_presence) / 빼기(forgetting_absence)">기준 넣기/빼기</th></tr>
      ${Object.keys(TASKS).map((t) => {
        const v = d.by_task[t] || {};
        return `<tr><td class="brow bclick" data-mtask="${esc(t)}"
          data-desc="클릭하면 문항 목록을 봅니다"><b>${esc(TASKS[t])}</b>
          <br><span class="small muted">${esc(t)}</span></td>
          <td>${v.n ?? "–"}</td>
          <td class="bcell" style="${beamHeat(v.fama == null ? null : v.fama / 100)}"><b>${n2(v.fama)}</b></td>
          <td class="bcell" style="${beamHeat(v.mpa == null ? null : v.mpa / 100)}">${n2(v.mpa)}</td>
          <td ${v.n_forget ? "" : `class="muted" data-desc="${esc("이 과제에는 forgetting_absence 기준이 하나도 없습니다(λ=0). 잰 값이 없으므로 비워 둡니다.")}"`}>${v.n_forget ? n2(v.faa) : "–"}</td>
          <td class="bdelta ${v.n_forget && (v.penalty ?? 0) > 0 ? "down" : ""}"
            ${v.n_forget ? "" : `data-desc="${esc("λ=0이라 FAMA가 MPA와 같아집니다. 잘해서 0이 아니라 뺄 것이 없어서 0입니다.")}"`}
            >${v.n_forget ? "−" + (v.penalty ?? 0).toFixed(2) : "–"}</td>
          <td class="small muted">${(v.n_presence || 0)} / ${(v.n_forget || 0)}</td></tr>`;
      }).join("")}
      <tr class="jm-tot"><td class="brow"><b>전체</b></td><td>${d.overall.n}</td>
        <td class="bcell" style="${beamHeat(d.overall.fama / 100)}"><b>${n2(d.overall.fama)}</b></td>
        <td class="bcell" style="${beamHeat(d.overall.mpa / 100)}">${n2(d.overall.mpa)}</td>
        <td>${n2(d.overall.faa)}</td>
        <td class="bdelta down">−${n2(d.overall.penalty)}</td>
        <td class="small muted">${d.overall.n_presence} / ${d.overall.n_forget}</td></tr>
    </table></div></div>

    <div class="card"><h4 data-desc="데이터셋이 각 세션에서 의도한 메모리 연산 횟수와, mem0가 실제로 발생시킨 연산 횟수를 나란히 놓은 것입니다. 대상까지 맞는지는 확인하지 않은 개수 비교입니다">연산 발생비 (데이터셋 의도 대비 개수)</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>연산</th><th>데이터셋 의도</th><th>mem0 실제</th>
      <th data-desc="실제 ÷ 의도. 100%라도 '지워야 할 그것'을 지웠다는 뜻은 아닙니다. 개수만 맞춘 값입니다">발생비</th></tr>
      ${opRow("삭제 DELETE", d.intent.delete || 0, d.actual.DELETE || 0, "d", true,
              "이 벤치마크의 핵심입니다. HaluMem에는 삭제가 없어 못 보던 갈래입니다. 100%를 넘는 것도 문제일 수 있습니다 - 지우라고 하지 않은 것까지 지운 것이기 때문입니다.")}
      ${opRow("갱신 UPDATE", d.intent.update || 0, d.actual.UPDATE || 0, "u", true,
              "HaluMem에서 무효 UPDATE가 99.5%였습니다(§14). 개수만으로는 유효성을 알 수 없으니 수행률은 참고값입니다.")}
      ${opRow("추가 ADD", d.intent.add || 0, d.actual.ADD || 0, "a", true,
              "100%를 크게 넘는 것이 정상입니다. 세션에 지정된 연산은 하나지만 mem0는 15턴 대화에 섞인 부수적 사실도 전부 뽑습니다. 회색은 중립 표시입니다.")}
    </table></div>
    <div class="body"><span class="small muted"><b>⚠ 이것은 수행률이 아니라 개수 비율입니다.</b> mem0의 DELETE 이벤트를 데이터셋이 지목한 삭제 대상과 짝지어 확인하지 않았습니다. 100%라도 엉뚱한 것을 지웠을 수 있고, 100%를 넘으면 지우라고 하지 않은 것까지 지운 것입니다. <b>대상 일치는 아직 재지 않았습니다.</b><br>추가가 100%를 크게 넘는 것은 설계상 당연합니다. 세션마다 지정된 연산은 하나지만 mem0는 15턴 대화에 섞인 부수적 사실도 전부 뽑습니다.</span></div>
    </div>

    <div class="card"><h4 data-desc="페르소나마다 대화 내용과 메모리 연산 수가 다릅니다. 순위를 말하기 전에 이 폭을 먼저 보세요">페르소나별 (${d.n_personas})</h4>
    <div class="body" style="padding:0">
    <table class="cmp beam"><tr><th>페르소나</th><th>세션</th><th>저장</th>
      <th>FAMA</th><th>MPA</th><th>FAA</th><th>페널티</th>
      <th data-desc="mem0의 DELETE 이벤트 수 ÷ 데이터셋이 의도한 삭제 수. 대상 일치는 확인하지 않은 개수 비율이라 100%를 넘기도 합니다">삭제 발생비</th>
      <th data-desc="답변 길이 중앙값">길이</th></tr>
      ${d.personas.slice().sort((a, b) => (a.fama ?? 0) - (b.fama ?? 0)).map((p) => `<tr>
        <td class="brow"><b>${esc(p.persona)}</b></td>
        <td class="small">${p.sessions ?? "–"}</td><td class="small">${p.stored ?? "–"}</td>
        <td class="bcell" style="${beamHeat(p.fama == null ? null : p.fama / 100)}"><b>${n2(p.fama)}</b></td>
        <td>${n2(p.mpa)}</td><td>${n2(p.faa)}</td>
        <td class="bdelta ${(p.penalty ?? 0) > 0 ? "down" : ""}">−${n2(p.penalty)}</td>
        <td class="bcell" style="${rateHeat(p.delete_rate, true)}" data-desc="${esc(`의도 ${p.delete_intent} → 실제 ${p.delete_actual}`)}">${p.delete_rate == null ? "–" : p.delete_rate.toFixed(0) + "%"}</td>
        <td class="small">${p.len_median == null ? "–" : p.len_median.toLocaleString()}</td></tr>`).join("")}
    </table></div>
    <div class="body"><span class="small muted">${d.fama_sd == null ? "" : `FAMA 페르소나 간 <b>SD ${d.fama_sd}</b>.`}
      <b>이 표를 순위로 읽지 마세요.</b> 같은 페르소나(academic_researcher)를 같은 문항으로 전 구간 다시 돌렸더니 전체 FAMA가 <b>2.75</b>, 과제 단위로는 <b>12.67</b> 움직였습니다(weekly 실측). 페르소나당 문항이 15개뿐이라 <b>재실행 노이즈가 페르소나 간 차이와 같은 자릿수</b>입니다. 이 표는 분포의 폭을 보는 용도입니다.</span></div>
    </div>

    <div class="card"><h4 data-desc="삭제 이벤트가 많이 난 페르소나가 무효 언급도 적은지 봅니다. 페널티의 원인이 저장소인지 답변 규약인지 가르는 첫 단서입니다">삭제 발생비 ↔ forgetting 정확도</h4>
    <div class="body">
      ${rho == null
        ? `<p class="muted small">페르소나가 3개 미만이라 상관을 내지 않습니다.</p>`
        : `<p><b>Spearman ρ = ${rho >= 0 ? "+" : ""}${rho.toFixed(3)}</b> <span class="small muted">(페르소나 ${d.personas.filter((p) => p.delete_rate != null).length}개)</span></p>
           <p class="small">양수이고 크면 <b>삭제가 많이 일어난 페르소나가 무효 언급도 적다</b>는 뜻입니다. 0 근처면 삭제 개수로는 forgetting 성적을 설명할 수 없다는 뜻이고, 원인을 <b>검색이나 답변 규약</b>에서 찾아야 합니다.</p>
           <p class="small muted">⚠ 페르소나 10개짜리 순위 상관이고, x축이 대상 일치를 확인하지 않은 개수 비율입니다. 부호와 크기만 읽고 유의성을 주장하지 마세요.</p>`}
    </div></div>

    ${d.parse_fail ? `<p class="hint">⚠ 판정 실패 ${d.parse_fail}건 (파싱 또는 호출 실패). 오답으로 처리됐습니다</p>` : ""}`;

  $("#sidebar").innerHTML = `<div style="padding:10px">
    <p class="small muted"><b>Memora</b> (ACL 2026 Findings)<br>${esc(d.note || "")}</p>
    <p class="small muted" style="margin-top:10px"><b>FAMA</b><br><code>max(0, MPA − λ(1−FAA))</code><br>λ = 망각 기준 수 ÷ 전체 기준 수</p>
    <p class="small muted" style="margin-top:10px"><b>점수 색</b></p>
    <div class="bleg">${[0, .25, .5, .75, 1].map((v) => `<span style="${beamHeat(v)}">${(v * 100).toFixed(0)}</span>`).join("")}</div>
    <p class="small muted" style="margin-top:10px"><b>읽는 순서</b><br>① 기간 비교 → 저장소가 커지면 무엇이 무너지나<br>② 과제별 → 무너지는 방식이 과제마다 다름<br>③ 연산 발생비 → 저장소가 어떻게 요동치나<br>④ 페르소나별 → 폭만 보고 순위는 보지 않음</p>
    <p class="small muted" style="margin-top:10px">⚠ 기간별 집계 비교는 이 벤치마크의 설계 의도입니다. 다만 문항이 짝지어지지 않으니 <b>차이를 특정 문항에 귀속하지 마세요.</b></p>
    <p class="small muted" style="margin-top:10px">⚠ <b>FAA 상승 = 개선 아님.</b> 검색이 정답을 못 꺼내면 무효 항목도 같이 안 나옵니다. FAA와 페널티는 MPA와 함께 읽으세요.</p>
    <p class="small muted" style="margin-top:10px">⚠ 재실행 노이즈가 큽니다. 같은 페르소나 재실행에서 과제 FAMA가 12.67 움직인 실측이 있습니다. <b>페르소나 순위·소수점 차이를 읽지 마세요.</b></p>
    <p class="small muted" style="margin-top:10px">⚠ 이 화면은 상단바의 Generator·Judge 선택을 따르지 않습니다.</p>
    <p class="small muted" style="margin-top:10px">판독 근거는 <code>docs/mem0-classic-oss/memora-experiment.md</code></p>
  </div>`;
  bind();
  $$("#content td.bclick[data-mtask]").forEach((td) =>
    (td.onclick = () => memoraQuestions(td.dataset.mtask, TASKS[td.dataset.mtask])));
  $$("#content td.bclick[data-mperiod]").forEach((td) =>
    (td.onclick = () => { S.memoraPeriod = td.dataset.mperiod; renderMemora(); }));
}

// 과제 하나의 문항 목록. FAMA 낮은 순이라 실패부터 보임
async function memoraQuestions(task, label) {
  $("#jmodal").classList.remove("hidden");
  $("#jmodal-head").innerHTML = `<b>Memora · ${esc(label)}</b>
    <span class="jchip">${esc(task)}</span><span style="margin-left:auto"></span>
    <button class="jbtn" id="jm-close">✕</button>`;
  $("#jm-close").onclick = jmClose;
  const el = $("#jmodal-body");
  el.innerHTML = `<p class="muted" style="padding:20px">불러오는 중…</p>`;
  const d = await api(`/api/memora/questions?period=${encodeURIComponent(S.memoraPeriod)}&task=${encodeURIComponent(task)}`);
  el.innerHTML = `<div style="padding:16px 20px;overflow-y:auto">
    <div class="jbasis" data-desc="FAMA 낮은 순입니다. 기준 충족은 '넣어야 할 것 / 빼야 할 것'을 각각 몇 개 맞혔는지입니다.">
      문항 ${d.questions.length}개를 <b>FAMA 낮은 순</b>으로 놓았습니다.
      <span class="small">넣기(presence)는 다 맞혔는데 빼기(forget)에서 깨진 문항이 <b>무효 메모리를 끌어다 쓴 사례</b>입니다.</span>
    </div>
    <table class="cmp beam"><tr><th>페르소나</th><th>문항</th>
      <th>FAMA</th><th>MPA</th><th>FAA</th><th>λ</th>
      <th data-desc="넣어야 할 기준 중 맞힌 수">넣기</th>
      <th data-desc="빼야 할 기준 중 맞힌 수">빼기</th><th>길이</th><th></th></tr>
      ${d.questions.map((q) => `<tr>
        <td class="small"><b>${esc(q.persona)}</b></td>
        <td class="qtxt small">${esc((q.question || "").slice(0, 80))}</td>
        <td class="bcell" style="${beamHeat(q.fama / 100)}"><b>${q.fama.toFixed(1)}</b></td>
        <td>${q.mpa.toFixed(0)}</td><td>${q.faa.toFixed(0)}</td><td class="small">${q.lam.toFixed(2)}</td>
        <td class="small">${q.n_presence_ok}/${q.n_presence}</td>
        <td class="small${q.n_forget && q.n_forget_ok < q.n_forget ? " bad-n" : ""}">${q.n_forget_ok}/${q.n_forget}</td>
        <td class="small">${q.len == null ? "–" : q.len.toLocaleString()}</td>
        <td><button class="jbtn" data-mq="${esc(q.persona)}|${esc(q.question_id)}">상세</button></td>
      </tr>`).join("")}</table>
    <p style="margin-top:14px"><button class="jbtn" id="jm-back">✕ 닫기</button></p></div>`;
  $("#jm-back").onclick = jmClose;
  $$("#jmodal-body button[data-mq]").forEach((b) => (b.onclick = () => {
    const [persona, qid] = b.dataset.mq.split("|");
    memoraDetail(persona, qid, label);
  }));
}

// 문항 하나. 기준별 판정과 답변 원문. 어떤 기준에서 깨졌는지 보는 화면
async function memoraDetail(persona, qid, label) {
  const el = $("#jmodal-body");
  el.innerHTML = `<p class="muted" style="padding:20px">불러오는 중…</p>`;
  const d = await api(`/api/memora/question?period=${encodeURIComponent(S.memoraPeriod)}`
    + `&persona=${encodeURIComponent(persona)}&question_id=${encodeURIComponent(qid)}`);
  const crit = (t) => d.criteria.filter((c) => c.type === t);
  const block = (title, t, hint) => {
    const cs = crit(t);
    if (!cs.length) return "";
    return `<div class="jsec"><h5 data-desc="${esc(hint)}">${title} (${cs.filter((c) => c.ok).length}/${cs.length})</h5>
      <table class="cmp beam"><tr><th>기준</th><th>기대</th><th>판정</th><th></th></tr>
        ${cs.map((c) => `<tr>
          <td class="qtxt small">${esc(c.text)}</td>
          <td class="small">${esc(c.expected)}</td>
          <td class="small">${esc(String(c.got))}</td>
          <td class="${c.ok ? "" : "bad-n"}" data-desc="${esc(c.reason || "")}">${c.ok ? "✓" : "✗"}</td>
        </tr>`).join("")}</table></div>`;
  };
  el.innerHTML = `<div style="padding:16px 20px;overflow-y:auto">
    <p class="small muted"><b>${esc(d.persona)}</b> · ${esc(d.task)} · ${esc(d.question_id)}</p>
    <div class="jsec"><h5>문항</h5><div class="jtarget">${esc(d.question)}</div></div>
    <div class="jsec"><h5 data-desc="FAMA = max(0, MPA − λ(1−FAA))">점수</h5>
      <p><b>FAMA ${(d.fama * 100).toFixed(1)}</b>
        <span class="small muted">= max(0, MPA ${(d.mpa * 100).toFixed(1)} − λ ${d["lambda"].toFixed(3)} × (1 − FAA ${(d.faa * 100).toFixed(1)}%))</span></p></div>
    ${block("넣어야 할 것 (memory_presence)", "memory_presence", "정답에 포함돼야 할 정보입니다. 기대 답이 yes입니다.")}
    ${block("빼야 할 것 (forgetting_absence)", "forgetting_absence", "삭제·무효화된 정보라 언급하면 안 됩니다. 기대 답이 no입니다. 여기서 깨지면 무효 메모리를 끌어다 쓴 것입니다.")}
    <div class="jsec"><h5 data-desc="채점에 들어간 답변 원문입니다">시스템 답변 (${(d.system_response || "").length.toLocaleString()}자)</h5>
      <div class="jans small" style="white-space:pre-wrap">${esc(d.system_response || "(빈 답변)")}</div></div>
    <div class="jsec"><h5 data-desc="이 문항에 대해 mem0가 검색해 온 메모리입니다. 여기에 무효 항목이 섞여 있으면 저장소 문제입니다">검색된 메모리 (${(d.retrieved || []).length})</h5>
      <ol class="jl">${(d.retrieved || []).slice(0, 30).map((m) => `<li class="small">${esc(m.memory)}
        <span class="muted">${m.session_date ? "· " + esc(m.session_date) : ""}${m.score != null ? " · " + m.score.toFixed(2) : ""}</span></li>`).join("")}</ol></div>
    <p style="margin-top:14px"><button class="jbtn" id="jm-back2">← 목록</button>
      <button class="jbtn" id="jm-close2">✕ 닫기</button></p></div>`;
  $("#jm-back2").onclick = () => memoraQuestions(d.task, label);
  $("#jm-close2").onclick = jmClose;
}
