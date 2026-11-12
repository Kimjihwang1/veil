/* Veil 관리자 화면.
 *
 * 빌드 도구를 쓰지 않는다. node 를 안 깔아도 되고, 파일을 저장하면 새로고침만
 * 하면 된다. FastAPI 가 같은 서버에서 내보내므로 CORS 도 안 탄다.
 *
 * 2026-11-03 디자인('Outlier 관리자 v3 진한파랑')으로 전면 교체했다. 화면 9개:
 * 검토 피드(검토 대기 / 숨김 / 통과) · 인사이트 · 활동 · 관리 기준 · AI 프롬프트 ·
 * 자동 감지 · 채널. 서버 쪽 약속(조치 흐름, 유튜브 반영 여부를 서버가 정한다는 것,
 * 조치자 이름은 세션에서 읽는다는 것)은 예전 화면과 같다.
 */

// 화면이 이유 없이 비어 보이는 게 제일 나쁘다. 잡히지 않은 오류는
// 콘솔이 아니라 화면에 띄운다 — F12 를 열지 않아도 원인을 알 수 있게.
window.addEventListener("error", (e) => showFatal(e.message));
window.addEventListener("unhandledrejection", (e) => showFatal(e.reason?.message || e.reason));

const API = "/api";
const $ = (s, r = document) => r.querySelector(s);
const view = $("#view");

function showFatal(msg) {
  if (!view) return;
  view.innerHTML = `<div class="empty-card"><b>화면을 그리다 멈췄어요</b>
    <span class="mono">${esc(String(msg)).slice(0, 300)}</span></div>`;
}

// ── 공용 ──────────────────────────────────────────────

async function api(path, opts) {
  const r = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 401) {
    // 자동으로 넘기지 않는다. 로그인이 실패하면 튕겨 나왔다가 다시 보내지는
    // 무한 왕복이 되고, 그러면 원인이 뭔지 볼 기회가 없다.
    showLogin();
    const e = new Error("로그인이 필요합니다");
    e.unauthorized = true; // route() 가 오류 화면으로 덮지 않게 표시해둔다
    throw e;
  }
  if (!r.ok) {
    const body = await r.text();
    let msg = body.slice(0, 200);
    try { msg = JSON.parse(body).detail || msg; } catch { /* 본문이 JSON 이 아니면 그대로 */ }
    const e = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    e.status = r.status;
    throw e;
  }
  return r.status === 204 ? null : r.json();
}

// 구글에서 돌아오다 실패하면 /app?login_error=... 로 온다. 왜 안 됐는지를 여기서 말해준다.
const 로그인오류 = {
  cancelled: "로그인을 취소하셨습니다.",
  access_denied: "구글에서 거부됐습니다. 미검증 앱이라 '테스트 사용자'로 등록된 계정만 됩니다.",
  state: "로그인 절차가 중간에 끊겼습니다 (서버가 재시작됐거나 너무 오래 걸렸습니다). 다시 시도해주세요.",
  password_account: "이 이메일은 비밀번호로 가입한 계정입니다. 첫 화면에서 이메일과 비밀번호로 로그인하세요.",
};

function showLogin() {
  stopRefresh();
  feed.active = false;
  view.className = "wrap w720";
  const code = new URLSearchParams(location.search).get("login_error");
  view.innerHTML = `
    ${code ? `<div class="banner bad"><span>${esc(로그인오류[code] || code)}</span></div>` : ""}
    <div class="empty-card">
      <b>로그인이 필요해요</b>
      <span>채널 데이터는 로그인한 사람에게만 보여요. 이메일·비밀번호 또는 Google 계정으로 들어옵니다.</span>
      <button class="btn blue lg" style="margin-top:10px" onclick="location.href='/'">로그인하러 가기</button>
    </div>`;
}

/** 화면에 넣기 전에 반드시 통과시킨다. 댓글 본문은 남이 쓴 글이라 그대로 넣으면 태그가 실행된다. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

const num = (n) => (n ?? 0).toLocaleString("ko-KR");
const initialOf = (s) => esc(([...String(s || "?").trim()][0] || "?").toUpperCase());

// 아바타 배경. 같은 이름은 어디서나 같은 색이다.
const AV = ["#e6e8ec", "#f2e3e9", "#e0ece8", "#efe8da", "#e2e6f1", "#f1e2e2"];
function avOf(s) {
  let h = 0;
  for (const ch of String(s || "?")) h = (h * 31 + ch.codePointAt(0)) % 9973;
  return AV[h % AV.length];
}

/** '3분 전' 처럼. 관리자는 절대시각보다 얼마나 됐는지를 먼저 본다. */
function ago(iso) {
  if (!iso) return "";
  const t = new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z").getTime();
  const m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  const d = Math.floor(h / 24);
  return d < 30 ? `${d}일 전` : new Date(t).toLocaleDateString("ko-KR");
}
const toDate = (iso) => (iso ? new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z") : null);
const hhmm = (d) => `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2400);
}

function loading() {
  view.innerHTML = `<div class="loading">불러오는 중…</div>`;
}
function failed(e) {
  view.innerHTML = `<div class="empty-card"><b>불러오지 못했어요</b>
    <span class="mono">${esc(e.message)}</span></div>`;
}

const head = (title, sub = "") =>
  `<div class="head"><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ""}</div>`;

// 위험도 1~5 (services/severity.py). 숫자가 클수록 위험하다. 5·4·3·1 색은 옛 4단계 디자인 값 그대로.
const SEV = {
  5: ["5 긴급", "#cf2c56", "#cf2c56"],
  4: ["4 심각", "#ad5216", "#ad5216"],
  3: ["3 보통", "#8a6a10", "#8a6a10"],
  2: ["2 주의", "#5d6b85", "#5d6b85"],
  1: ["1 낮음", "#6b7079", "#636873"],
};
const sevOf = (x) => SEV[x.level] || SEV[1];
const LABEL = { harmful: ["유해", "#cf2c56"], ambiguous: ["애매", "#8a6a10"], safe: ["정상", "#1a7f4b"] };

// ── 클릭 위임 ─────────────────────────────────────────
//
// 화면을 통째로 다시 그리는 방식이라, 버튼마다 onclick 을 다시 붙이지 않고
// data-do="이름" 으로 모아서 받는다. 화면마다 actions 를 갈아 끼운다.

let actions = {};
view.addEventListener("click", (e) => {
  const el = e.target.closest("[data-do]");
  if (!el || !view.contains(el)) return;
  const fn = actions[el.dataset.do];
  if (fn) fn(el, e);
});

// ── 채널 ──────────────────────────────────────────────

let channels = [];
let currentChannel = 0;
const queueCount = {}; // 채널별 검토 대기 수. 채널 원형 줄의 배지.

const channelId = () => currentChannel;
const cur = () => channels.find((c) => c.id === currentChannel);
const channelName = () => cur()?.channel_title || "";

async function loadChannels() {
  channels = await api("/channels");
  const saved = Number(localStorage.getItem("channel") || 0);
  currentChannel = channels.some((c) => c.id === saved) ? saved : channels[0]?.id || 0;
}

/** 채널이 바뀌었으면 true. 유튜버가 초대 링크로 다른 창에서 연결을 끝냈을 수 있다. */
async function syncChannels() {
  const sig = () => channels.map((c) => `${c.id}:${c.connected}`).join();
  const before = sig();
  try { await loadChannels(); } catch { return false; }
  return sig() !== before;
}

// ── 왼쪽 아래: 지금 채널 · 워크스페이스, 누르면 메뉴 ──────
//
// 위에 있던 채널 원형 줄을 여기로 옮겼다 (2026-11-03). 채널이 화면 어디에도 안 보이면
// 지금 어느 채널을 보고 있는지 모르게 되므로, 이 칸에 채널 이름을 늘 띄운다.

let me = null;

function paintMe() {
  const c = cur();
  $("#me-name").textContent = c ? c.channel_title : "채널 없음";
  $("#me-ws").textContent = me ? me.workspace_name : " ";
  $("#me-av").textContent = c ? [...(c.channel_title || "?")][0].toUpperCase() : "+";
  $("#me").title = me ? `${me.email} · 채널 바꾸기 · 로그아웃` : "";
  if (!$("#menu").hidden) paintMenu();
}

function paintMenu() {
  const name = me ? me.name || me.email.split("@")[0] : "";
  $("#menu").innerHTML = `
    ${me ? `<div class="mh"><b>${esc(name)}</b><span>${esc(me.email)}</span></div>` : ""}
    <div class="ml">채널</div>
    ${channels.map((c) => {
      const n = queueCount[c.id] || 0;
      const on = c.id === currentChannel;
      return `<button class="mi${on ? " cur" : ""}" role="menuitemradio" aria-checked="${on}" data-ch="${c.id}">
        <span class="ci">${initialOf(c.channel_title)}</span>
        <span class="nm">${esc(c.channel_title)}</span>
        ${n ? `<span class="cnt" title="검토 대기">${num(n)}</span>` : ""}
        ${on ? `<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>` : ""}
      </button>`;
    }).join("")}
    <a class="mi" role="menuitem" href="#/channels" data-close><span class="ci add">+</span><span class="nm">채널 추가</span></a>
    <hr>
    <button class="mi out" role="menuitem" data-logout><span class="nm">로그아웃</span></button>`;
}

function setMenu(open) {
  const m = $("#menu");
  m.hidden = !open;
  $("#me").setAttribute("aria-expanded", String(open));
  if (open) paintMenu();
}

$("#me").addEventListener("click", (e) => {
  e.stopPropagation();
  setMenu($("#menu").hidden);
});
$("#menu").addEventListener("click", async (e) => {
  e.stopPropagation();
  const ch = e.target.closest("[data-ch]");
  if (ch) {
    setMenu(false);
    const id = Number(ch.dataset.ch);
    if (id === currentChannel) return;
    currentChannel = id;
    localStorage.setItem("channel", String(id));
    feed.reset();
    paintMe();
    paintNav();
    return route();
  }
  if (e.target.closest("[data-close]")) return setMenu(false);
  if (e.target.closest("[data-logout]")) {
    setMenu(false);
    await api("/auth/logout", { method: "POST" }).catch(() => null);
    location.href = "/";
  }
});
document.addEventListener("click", () => { if (!$("#menu").hidden) setMenu(false); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#menu").hidden) { setMenu(false); $("#me").focus(); }
});

/** 각 채널의 검토 대기 수. 실패하면 배지를 안 띄운다. */
async function refreshCounts() {
  await Promise.all(
    channels.map((c) =>
      api(`/channels/${c.id}/stats?period=all`)
        .then((s) => { queueCount[c.id] = s.unreviewed; })
        .catch(() => { queueCount[c.id] = 0; })
    )
  );
  paintNav();
  paintMe();
}

function paintNav() {
  const b = $("#badge");
  const n = queueCount[currentChannel] || 0;
  b.hidden = !n;
  b.textContent = num(n);
}

function markNav(base) {
  const feedOn = FEED.has(base);
  document.querySelectorAll("#nav a").forEach((a) => {
    const on = a.hasAttribute("data-feed") ? feedOn : a.getAttribute("href") === base;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

// ── 검토 피드 (검토 대기 / 숨김 / 통과) ──────────────
//
// 영상별로 묶고, 펼친 묶음 하나만 목록 + 상세로 연다. 위험한 것부터, 같은 등급이면
// 많이 퍼진 것부터 — 순서는 서버가 정한다 (review.py).

const PAGE = 200;
const FILTERS = [["all", "전체"], ["urgent", "4·5단계"], ["ambiguous", "애매"], ["rule", "등록어"], ["held", "유튜브 보류"], ["evidence", "증거 저장됨"]];
const urgent = (x) => x.level >= 4;
// 애매한 자해처럼 4·5단계이기도 한 댓글은 4·5단계에만 둔다. 두 칸에 다 세면 같은 댓글이 겹쳐 보인다.
const fit = (f, x) =>
  f === "all" ||
  (f === "urgent" && urgent(x)) ||
  (f === "ambiguous" && x.label === "ambiguous" && !urgent(x)) ||
  (f === "rule" && x.decided_by === "rule") ||
  (f === "held" && !!x.youtube_held) ||
  (f === "evidence" && !!x.evidence);
// 유튜브가 공개하지 않고 잡아 둔 댓글 (review.py QueueItem.youtube_held)
const heldOf = (x) => (x.youtube_held === "likelySpam" ? "유튜브 스팸 의심" : x.youtube_held ? "유튜브 보류" : "");
const videoOf = (x) => x.video_title || x.video_id || "영상 정보 없음";

const feed = {
  active: false,
  kind: "queue",
  rows: [],
  total: 0,
  counts: { queue: 0, hidden: 0, passed: 0 },
  filter: "all",
  video: null, // 인사이트의 '확인' 에서 넘어오면 그 영상만
  openVideo: null,
  selId: null,
  checked: new Set(),
  confirmingId: null,
  bulkConfirm: false,
  leaving: null,
  fresh: new Set(),
  fetching: false,
  lastRun: null,
  busy: false,
  reset() {
    Object.assign(this, {
      filter: "all", video: null, openVideo: null, selId: null, confirmingId: null,
      bulkConfirm: false, leaving: null,
    });
    this.checked = new Set();
    this.fresh = new Set();
  },
};

const simCache = new Map(); // comment_id → 비슷했던 댓글 HTML. 다시 그릴 때 깜빡이지 않게.

function listAll() {
  const k = feed.kind;
  const f = k === "queue"
    ? feed.rows.filter((x) => fit(feed.filter, x) && (!feed.video || videoOf(x) === feed.video))
    : feed.rows;
  const order = [];
  const by = {};
  f.forEach((x) => {
    const v = videoOf(x);
    if (!by[v]) { by[v] = []; order.push(v); }
    by[v].push(x);
  });
  return { order, by };
}
function openV() {
  if (feed.openVideo === "__none__") return null;
  const { order } = listAll();
  return order.includes(feed.openVideo) ? feed.openVideo : order[0] ?? null;
}
function openList() {
  const v = openV();
  return v === null ? [] : listAll().by[v] || [];
}
function sel() {
  const l = openList();
  return l.find((x) => x.comment_id === feed.selId) || l[0] || null;
}

function tagOf(x) {
  if (feed.kind === "passed") return x.label === "safe" ? "AI 정상" : "관리자 유지";
  if (feed.kind === "hidden") return x.youtube_synced === true ? "유튜브 반영" : "기록만, 유튜브 미반영";
  return x.label === "ambiguous" ? "AI가 확신 못 함" : "";
}

function rowHtml(x, focusId) {
  const id = x.comment_id;
  const s = sevOf(x);
  const cls = [
    "row",
    id === focusId ? "on" : feed.checked.has(id) ? "chk" : "",
    feed.leaving?.id === id ? "out" : "",
  ].join(" ");
  return `
    <div class="${cls}" data-do="pick" data-id="${id}" id="r-${id}">
      <input type="checkbox" aria-label="선택" data-do="check" data-id="${id}"${feed.checked.has(id) ? " checked" : ""}>
      <div style="min-width:0;display:flex;flex-direction:column;gap:4px">
        <div class="meta">
          <span class="sev" style="color:${s[2]}"><i style="background:${s[1]}"></i>${s[0]}</span>
          <span class="cat">${esc(x.category || "분류 없음")}</span>
          ${feed.fresh.has(id) ? `<span class="new">새 댓글</span>` : ""}
          <span class="when">${ago(x.published_at)}</span>
        </div>
        <span class="body">${esc(x.content)}</span>
        <span class="by">${esc(x.author_name || "익명")} · 좋아요 ${num(x.like_count)}</span>
      </div>
    </div>`;
}

function articleHtml(x) {
  const k = feed.kind;
  const id = x.comment_id;
  const s = sevOf(x);
  const synced = x.youtube_synced === true;
  const out = feed.leaving?.id === id ? (feed.leaving.dir > 0 ? " out-r" : " out-l") : "";
  const name = x.author_name || "(보관기한 지나 삭제됨)";
  const tag = tagOf(x);
  const by = x.decided_by === "rule" ? "등록어" : x.decided_by === "prefilter" ? "사전 필터" : "AI";
  const held = k === "queue" ? heldOf(x) : "";
  const syncNote = k === "hidden"
    ? synced
      ? "유튜브에서 가려졌어요. API로는 되돌릴 수 없고, 스튜디오에서 직접 다시 공개해야 합니다."
      : "우리 기록은 숨김인데 유튜브에는 반영되지 않았어요. 권한 만료나 미연동이 원인입니다."
    : held
      ? `${held}로 잡혀 있어 지금 댓글창에 안 보여요. [공개]를 누르면 유튜브에 공개되고, [숨김]을 누르면 거부됩니다.`
      : "";
  const showHide = k !== "hidden" || !synced;
  const acts = feed.confirmingId === id
    ? `<span class="q">유튜브에서 가리면 API로 되돌릴 수 없어요. 숨길까요?</span>
       <button class="btn blue" data-do="yes" data-id="${id}">숨기기</button>
       <button class="btn" data-do="no">취소</button>`
    : `${showHide ? `<button class="btn blue" data-do="hide" data-id="${id}">${k === "hidden" ? "유튜브에 다시 보내기" : "숨김"}</button>` : ""}
       ${k === "queue" ? `<button class="btn dark" data-do="keep" data-id="${id}">${held ? "공개" : "유지"}</button>` : ""}
       <button class="btn" data-do="ban" data-id="${id}">작성자 차단</button>
       ${x.evidence
         ? `<span class="pill line" title="증거 보관함에 있어요">증거 저장됨</span>`
         : `<button class="btn" data-do="evidence" data-id="${id}" title="지금 원문·작성자·시각·링크를 고정해 보관함에 둡니다">증거로 저장</button>`}
       ${k === "queue"
         ? `<span class="keys"><kbd>H</kbd><kbd>K</kbd><kbd>B</kbd><span style="margin-left:2px">J 다음</span></span>`
         : ""}`;
  return `
    <article class="art${out}">
      <div class="who">
        <div class="av" style="background:${avOf(name)}">${initialOf(name)}</div>
        <span class="nm">${esc(name)} <span>${ago(x.published_at)}</span></span>
        <span class="sevb" style="background:${s[1]}">${s[0]}</span>
      </div>
      ${x.parent_content ? `<div class="parent">답글 단 댓글: ${esc(x.parent_content)}</div>` : ""}
      <p class="quote">${esc(x.content)}</p>
      <div class="tags">
        <span class="pill">${esc(x.category || "분류 없음")}</span>
        ${feed.fresh.has(id) ? `<span class="pill blue">새 댓글</span>` : ""}
        ${x.rule_value ? `<span class="pill lt">등록어 ${esc(x.rule_value)}</span>` : ""}
        ${held ? `<span class="pill warn">${held}</span>` : ""}
        ${tag ? `<span class="pill line">${tag}</span>` : ""}
        <span class="stats"><span>좋아요 <b>${num(x.like_count)}</b></span>${
          x.total_reply_count ? `<span>답글 <b>${num(x.total_reply_count)}</b></span>` : ""}</span>
      </div>
      <p class="reason"><b>${by}</b>${esc(x.reason || "근거 없음")}</p>
      <div id="sims-${id}">${simCache.get(id) ?? ""}</div>
      ${syncNote ? `<p class="note-box">${syncNote}</p>` : ""}
      <hr>
      <div class="acts">${acts}</div>
    </article>`;
}

function groupHtml(v, items, isOpen, focus) {
  const sc = {};
  items.forEach((x) => { sc[x.level] = (sc[x.level] || 0) + 1; });
  const parts = feed.kind === "queue"
    ? [5, 4].filter((k) => sc[k]).map((k) => `${SEV[k][0]} ${sc[k]}`)
    : [];
  const meta = [`댓글 ${num(items.length)}건`, ...parts].join(" · ");
  const nChecked = items.filter((x) => feed.checked.has(x.comment_id)).length;
  const allOn = items.length > 0 && nChecked === items.length;
  const k = feed.kind;
  const bulk = !nChecked
    ? ""
    : `<div class="bulk"><b>${nChecked}건 선택</b>${
        feed.bulkConfirm
          ? `<span class="q">숨기면 유튜브에서 되돌릴 수 없어요. 숨길까요?</span>
             <button class="btn blue sm" data-do="bulkYes">숨기기</button>
             <button class="btn sm" data-do="bulkNo">취소</button>`
          : `${k !== "hidden" ? `<button class="btn blue sm" data-do="bulk" data-a="hide">숨김</button>` : ""}
             ${k === "queue" ? `<button class="btn dark sm" data-do="bulk" data-a="keep">유지</button>` : ""}
             ${k === "queue" ? `<button class="btn sm" data-do="bulk" data-a="ban">작성자 차단</button>` : ""}
             ${k === "hidden" ? `<button class="btn blue sm" data-do="bulk" data-a="resend">유튜브에 다시 보내기</button>` : ""}
             <button class="clear" data-do="clearChecked">선택 해제</button>`
      }</div>`;
  return `
    <section class="group${isOpen ? " open" : ""}">
      <button class="g-head" data-do="toggle" data-v="${esc(v)}" aria-expanded="${isOpen}">
        <span class="ic" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 16 16"><path d="M4 2.5v11l9.5-5.5z" fill="#f3f4f6"/></svg></span>
        <span class="tt"><b>${esc(v)}</b><span>${meta}</span></span>
        <span class="tg">${isOpen ? "접기" : "펼치기"}</span>
      </button>
      ${isOpen ? `
      <div class="g-body">
        <div class="g-list">
          <div class="g-bar">
            <label><input type="checkbox" data-do="checkAll"${allOn ? " checked" : ""}>${allOn ? "전체 해제" : "전체 선택"}</label>
            <small>${num(items.length)}건 · J/K로 이동</small>
          </div>
          ${bulk}
          <div class="rows" id="rows">${items.map((x) => rowHtml(x, focus?.comment_id)).join("")}</div>
        </div>
        <div class="g-detail" id="detail">${focus ? articleHtml(focus) : ""}</div>
      </div>` : ""}
    </section>`;
}

const EMPTY = {
  queue: ["다 봤어요", "새 댓글은 매시간 자동으로 들어와요. 위험한 것부터 여기에 쌓입니다."],
  queueFiltered: ["이 조건에 맞는 댓글이 없어요", "다른 필터를 골라 보세요."],
  hidden: ["가린 댓글이 없어요", "검토 피드에서 숨김을 누르면 여기로 옵니다."],
  passed: ["통과한 댓글이 없어요", "AI가 정상으로 본 댓글이 여기에 쌓입니다."],
};

function renderFeed() {
  const k = feed.kind;
  const rowsBox = $("#rows");
  const keepScroll = rowsBox ? rowsBox.scrollTop : 0;
  const { order, by } = listAll();
  const ov = openV();
  const focus = sel();
  const c = cur();

  const more = Math.max(feed.total - feed.rows.length, 0);
  const tabs = [["queue", "검토 대기"], ["hidden", "숨김"], ["passed", "통과"]]
    .map(([key, label]) => `<a href="#/${key}" class="${key === k ? "on" : ""}${key === "queue" ? " q" : ""}" role="tab"
         aria-selected="${key === k}">${label}<span>${num(feed.counts[key])}</span></a>`)
    .join("");

  const toolbar = k !== "queue" ? "" : `
    <div class="toolbar">
      <div class="filters">
        ${feed.video ? `<button class="chipbtn vid" data-do="clearVideo" aria-label="영상 필터 해제">${esc(feed.video)}<span aria-hidden="true">✕</span></button>` : ""}
        ${FILTERS.map(([f, label]) => {
          // 전체는 서버가 센 수. 나머지는 받아 온 것 중에서 센 수라, 덜 받았으면 '+' 를 붙인다.
          const n = f === "all" ? num(feed.total) : num(feed.rows.filter((x) => fit(f, x)).length) + (more ? "+" : "");
          return `<button class="chipbtn${feed.filter === f ? " on" : ""}" data-do="filter" data-f="${f}">${label}<span>${n}</span></button>`;
        }).join("")}
      </div>
      <div class="right">
        <small>마지막 조회 ${feed.lastRun ? ago(feed.lastRun) : "없음"}</small>
        <button class="btn blue md" data-do="fetchNow"${feed.fetching ? " disabled aria-busy=\"true\"" : ""}>${feed.fetching ? "불러오는 중" : "조회"}</button>
      </div>
    </div>
    ${feed.fetching ? `<div class="fetching" role="status"><b>새 댓글을 가져오는 중이에요</b>
      <span>유튜브에서 댓글을 모아 AI가 판별한 뒤 위험한 순서로 넣습니다.</span></div>` : ""}`;

  const banner = c && !c.connected
    ? `<div class="banner"><span><b>${esc(c.channel_title)}</b>은 유튜브 권한이 없어요. 숨겨도 우리 기록에만 남습니다.</span>
         <a class="btn dark md" href="#/channels" style="display:grid;place-items:center;text-decoration:none">연동하기</a></div>`
    : "";

  const empty = !order.length
    ? (() => {
        const [t, b] = EMPTY[k === "queue" && (feed.filter !== "all" || feed.video) ? "queueFiltered" : k];
        return `<div class="empty-card"><b>${t}</b><span>${b}</span></div>`;
      })()
    : "";

  view.innerHTML = `
    <div class="tabs" role="tablist">${tabs}</div>
    ${toolbar}
    ${banner}
    ${order.map((v) => groupHtml(v, by[v], v === ov, v === ov ? focus : null)).join("")}
    ${empty}
    ${more ? `<div class="more-row"><button class="btn md" data-do="more">${num(Math.min(more, PAGE))}건 더 불러오기</button>
               <small>${num(feed.total)}건 중 ${num(feed.rows.length)}건 받음</small></div>` : ""}`;

  const nb = $("#rows");
  if (nb) nb.scrollTop = keepScroll;
  if (focus) loadSimilar(focus.comment_id);
}

/** 비슷했던 댓글. 늦게 와도 되므로 본문을 먼저 그리고 나서 채운다. */
async function loadSimilar(id) {
  if (simCache.has(id)) return;
  simCache.set(id, "");
  try {
    const sims = await api(`/comments/${id}/similar?limit=3`);
    const verdict = (s) =>
      s.reviewed
        ? `${{ hide: "숨김", keep: "유지", ban_author: "차단" }[s.action] || s.action}${s.actor ? ` · ${esc(s.actor)}` : ""}`
        : { hidden: "숨김", queued: "검토 대기", passed: "통과" }[s.status] || s.status;
    const html = sims.length
      ? `<div class="sims"><span>비슷했던 댓글</span>${sims
          .map((s) => `<div class="sim"><span class="s">${s.similarity.toFixed(2)}</span>
              <span class="t">${esc(s.content)}</span><span class="v">${verdict(s)}</span></div>`)
          .join("")}</div>`
      : "";
    simCache.set(id, html);
    const box = $(`#sims-${id}`);
    if (box) box.innerHTML = html;
  } catch {
    simCache.delete(id);
  }
}

function scrollRowIntoView(id) {
  const el = $(`#r-${id}`);
  const p = $("#rows");
  if (!el || !p) return;
  const top = el.offsetTop;
  if (top < p.scrollTop || top + el.offsetHeight > p.scrollTop + p.clientHeight) {
    p.scrollTo({ top: top - 40, behavior: "smooth" });
  }
}

function move(dir) {
  const l = openList();
  const s = sel();
  if (!s) return;
  const n = l[Math.max(0, Math.min(l.length - 1, l.indexOf(s) + dir))];
  feed.selId = n.comment_id;
  feed.confirmingId = null;
  renderFeed();
  scrollRowIntoView(n.comment_id);
}

const API_ACTION = { hide: "hide", keep: "keep", ban: "ban_author", resend: "hide" };
const DONE_KO = { hide: "숨겼어요", keep: "유지했어요", ban: "작성자 채널을 차단했어요" };

/** 서버에 조치를 보낸다. 유튜브에 실제로 반영됐는지는 서버가 읽어서 정한다. */
const send = (id, action) =>
  api(`/comments/${id}/action`, { method: "POST", body: JSON.stringify({ action: API_ACTION[action] }) });

function dropRows(ids) {
  const set = new Set(ids);
  const l = openList();
  const i = l.findIndex((x) => set.has(x.comment_id));
  feed.rows = feed.rows.filter((x) => !set.has(x.comment_id));
  feed.total = Math.max(feed.total - ids.length, 0);
  feed.counts[feed.kind] = Math.max(feed.counts[feed.kind] - ids.length, 0);
  const rest = l.filter((x) => !set.has(x.comment_id));
  feed.selId = (rest[Math.min(Math.max(i, 0), rest.length - 1)] || {}).comment_id ?? null;
  ids.forEach((id) => feed.checked.delete(id));
}

async function act(action, id, confirmed = false) {
  const k = feed.kind;
  const x = openList().find((r) => r.comment_id === id);
  if (!x || feed.leaving || feed.busy) return;
  if (action === "keep" && k !== "queue") return;

  // 숨김 목록의 '유튜브에 다시 보내기'. 목록에서 빠지지 않고 표시만 바뀐다.
  if (k === "hidden" && action === "hide") {
    if (x.youtube_synced === true) return;
    feed.busy = true;
    try {
      const r = await send(id, "hide");
      x.youtube_synced = r.youtube_synced;
      toast(r.youtube_synced ? "유튜브에 반영했어요" : (r.note || "유튜브에 반영하지 못했어요").replace(/^\[|\]$/g, ""));
    } catch (e) {
      toast(`보내지 못했어요: ${e.message}`);
    } finally {
      feed.busy = false;
      renderFeed();
    }
    return;
  }

  if (action === "hide" && !confirmed) {
    feed.selId = id;
    feed.confirmingId = id;
    return renderFeed();
  }

  // 숨김 목록에서 차단하면 댓글은 숨김 그대로라 목록에서 빼지 않는다 (ACTION_STATUS).
  const stays = k === "hidden";
  feed.busy = true;
  feed.confirmingId = null;
  if (!stays) feed.leaving = { id, dir: action === "keep" ? 1 : -1 };
  renderFeed();
  const wait = new Promise((r) => setTimeout(r, stays ? 0 : 280));
  try {
    const [r] = await Promise.all([send(id, action), wait]);
    feed.leaving = null;
    feed.fresh.delete(id);
    if (!stays) dropRows([id]);
    // 반영돼야 하는데 안 됐으면 서버가 남긴 이유를 그대로 보여준다.
    // "숨겼어요" 로 덮으면 관리자는 유튜브에서도 가려진 줄 안다.
    // 유튜브 보류 댓글의 [공개] 는 유튜브로 나가는 조치라 반영 여부를 같이 알린다.
    const 공개 = action === "keep" && !!x.youtube_held;
    const 완료 = 공개 ? "유튜브에 공개했어요" : DONE_KO[action];
    const 안됨 = (action !== "keep" || 공개) && r.youtube_synced === false;
    toast(안됨 ? `${공개 ? "공개하지 못했어요" : DONE_KO[action]}. ${cur()?.connected ? (r.note || "유튜브 반영 실패").replace(/^\[|\]$/g, "") : "권한이 없어 기록에만 남았어요"}` : 완료);
    queueCount[currentChannel] = feed.kind === "queue" ? feed.counts.queue : queueCount[currentChannel];
    paintNav();
  } catch (e) {
    feed.leaving = null;
    toast(`처리하지 못했어요: ${e.message}`);
  } finally {
    feed.busy = false;
    renderFeed();
    const s = sel();
    if (s) scrollRowIntoView(s.comment_id);
  }
}

async function bulkAct(action, confirmed = false) {
  const k = feed.kind;
  const items = openList().filter((x) => feed.checked.has(x.comment_id));
  if (!items.length || feed.busy) return;
  if (action === "hide" && !confirmed) {
    feed.bulkConfirm = true;
    return renderFeed();
  }
  feed.busy = true;
  feed.bulkConfirm = false;
  let failed = 0;
  let notSynced = 0;
  const done = [];
  for (const x of items) {
    if (action === "resend" && x.youtube_synced === true) continue;
    try {
      const r = await send(x.comment_id, action);
      if (action === "resend") x.youtube_synced = r.youtube_synced;
      else done.push(x.comment_id);
      if (action !== "keep" && r.youtube_synced === false) notSynced += 1;
    } catch {
      failed += 1;
    }
  }
  if (action === "resend") feed.checked = new Set();
  else dropRows(done);
  feed.busy = false;
  const base = action === "resend"
    ? `${items.length - failed - notSynced}건을 유튜브에 반영했어요`
    : action === "ban" ? `${done.length}건의 ${DONE_KO.ban}` : `${done.length}건을 ${DONE_KO[action]}`;
  toast([base, notSynced && action !== "resend" ? `유튜브 미반영 ${notSynced}건` : "",
         notSynced && action === "resend" ? `${notSynced}건은 여전히 미반영` : "",
         failed ? `실패 ${failed}건` : ""].filter(Boolean).join(" · "));
  if (k === "queue") { queueCount[currentChannel] = feed.counts.queue; paintNav(); }
  renderFeed();
}

const listPath = (kind = feed.kind) => `/channels/${channelId()}/${kind}?limit=${PAGE}`;
const totalOf = (s, kind) => (kind === "queue" ? s.unreviewed : kind === "hidden" ? s.hidden : s.passed);

async function viewFeed(kind) {
  if (feed.kind !== kind || feed.channel !== channelId()) {
    const video = feed.channel === channelId() ? feed.video : null;
    feed.reset();
    if (kind === "queue") feed.video = video;
  }
  feed.kind = kind;
  feed.channel = channelId();
  feed.active = true;
  loading();
  const [list, stats, watch] = await Promise.all([
    api(listPath(kind)),
    api(`/channels/${channelId()}/stats?period=all`).catch(() => null),
    api(`/channels/${channelId()}/watch`).catch(() => null),
  ]);
  feed.rows = list;
  feed.total = stats ? totalOf(stats, kind) : list.length;
  feed.counts = stats
    ? { queue: stats.unreviewed, hidden: stats.hidden, passed: stats.passed }
    : { ...feed.counts, [kind]: list.length };
  feed.lastRun = watch?.last_run_at || null;
  if (stats) { queueCount[currentChannel] = stats.unreviewed; paintNav(); }

  actions = {
    pick: (el) => {
      const id = Number(el.dataset.id);
      if (feed.selId === id) return;
      feed.selId = id;
      feed.confirmingId = null;
      renderFeed();
    },
    check: (el, e) => {
      e.stopPropagation();
      const id = Number(el.dataset.id);
      if (el.checked) feed.checked.add(id); else feed.checked.delete(id);
      feed.bulkConfirm = false;
      renderFeed();
    },
    checkAll: () => {
      const items = openList();
      const allOn = items.every((x) => feed.checked.has(x.comment_id));
      feed.checked = allOn ? new Set() : new Set(items.map((x) => x.comment_id));
      feed.bulkConfirm = false;
      renderFeed();
    },
    toggle: (el) => {
      const v = el.dataset.v;
      feed.openVideo = v === openV() ? "__none__" : v;
      Object.assign(feed, { selId: null, confirmingId: null, bulkConfirm: false });
      feed.checked = new Set();
      renderFeed();
    },
    filter: (el) => {
      feed.filter = el.dataset.f;
      Object.assign(feed, { selId: null, confirmingId: null });
      renderFeed();
    },
    clearVideo: () => { feed.video = null; feed.selId = null; renderFeed(); },
    hide: (el) => act("hide", Number(el.dataset.id)),
    yes: (el) => act("hide", Number(el.dataset.id), true),
    no: () => { feed.confirmingId = null; renderFeed(); },
    keep: (el) => act("keep", Number(el.dataset.id)),
    ban: (el) => act("ban", Number(el.dataset.id)),
    evidence: (el) => saveEvidence(Number(el.dataset.id)),
    bulk: (el) => bulkAct(el.dataset.a),
    bulkYes: () => bulkAct("hide", true),
    bulkNo: () => { feed.bulkConfirm = false; renderFeed(); },
    clearChecked: () => { feed.checked = new Set(); feed.bulkConfirm = false; renderFeed(); },
    fetchNow: fetchNow,
    more: loadMore,
  };
  renderFeed();
  startRefresh();
}

/** [증거로 저장] — 지금 모습 그대로 보관함에 고정한다. 피드에서 빠지지 않는다. */
async function saveEvidence(id) {
  try {
    await api(`/comments/${id}/evidence`, { method: "POST", body: "{}" });
    const x = feed.rows.find((r) => r.comment_id === id);
    if (x) x.evidence = true;
    toast("증거 보관함에 저장했어요");
    renderFeed();
  } catch (e) {
    toast(`저장하지 못했어요: ${e.message}`);
  }
}

/** [조회] — 지금 이 채널의 새 댓글을 가져와 판별한다. */
async function fetchNow() {
  if (feed.fetching) return;
  feed.fetching = true;
  renderFeed();
  try {
    const r = await api(`/channels/${channelId()}/fetch`, { method: "POST" });
    feed.lastRun = new Date().toISOString();
    await pullFresh();
    toast(r.collected
      ? r.queued ? `새 댓글 ${num(r.collected)}건을 불러왔어요. 검토할 것 ${num(r.queued)}건` : `새 댓글 ${num(r.collected)}건, 모두 정상이에요`
      : "새 댓글이 없어요");
  } catch (e) {
    // 403 동의 전 · 409 이미 도는 중 · 429 방금 눌렀음 — 서버 문장을 그대로 보여준다.
    toast([403, 409, 429].includes(e.status) ? e.message : `불러오지 못했어요: ${e.message}`);
  } finally {
    feed.fetching = false;
    if (feed.active) renderFeed();
  }
}

async function loadMore(btn) {
  btn.disabled = true;
  btn.textContent = "불러오는 중…";
  try {
    const next = await api(`${listPath()}&offset=${feed.rows.length}`);
    const known = new Set(feed.rows.map((r) => r.comment_id));
    feed.rows = feed.rows.concat(next.filter((r) => !known.has(r.comment_id)));
    renderFeed();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "다시 시도";
    toast(`불러오지 못했어요: ${e.message}`);
  }
}

/** 새로 들어온 댓글만 앞에 붙인다. 보던 걸 뺏지 않는다. */
async function pullFresh() {
  const kind = feed.kind;
  const [fresh, stats] = await Promise.all([
    api(listPath(kind)),
    api(`/channels/${channelId()}/stats?period=all`).catch(() => null),
  ]);
  if (!feed.active || feed.kind !== kind) return 0;
  if (stats) {
    feed.total = totalOf(stats, kind);
    feed.counts = { queue: stats.unreviewed, hidden: stats.hidden, passed: stats.passed };
    queueCount[currentChannel] = stats.unreviewed;
    paintNav();
  }
  const known = new Set(feed.rows.map((r) => r.comment_id));
  const added = fresh.filter((r) => !known.has(r.comment_id));
  if (added.length) {
    added.forEach((r) => feed.fresh.add(r.comment_id));
    feed.rows = added.concat(feed.rows);
  }
  return added.length;
}

// 자동 갱신. 유튜브가 댓글 알림을 안 주므로 서버도 화면도 폴링이다.
// DB 만 읽어서 유튜브 쿼터를 안 쓴다. 20초였는데 시연 때 답답해서 5초로 줄였다 (2026-11-04).
const REFRESH_MS = 5000;
let refreshTimer = null;
function stopRefresh() {
  clearInterval(refreshTimer);
  refreshTimer = null;
}
function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(async () => {
    if (document.hidden || feed.busy || feed.leaving || feed.fetching) return;
    try {
      const n = await pullFresh();
      if (!feed.active) return;
      if (n) toast(`새 댓글 ${n}건이 들어왔어요`);
      // 탭 숫자는 새 댓글이 없어도 바뀐다 (다른 관리자가 처리했을 수 있다). 확인 중이면 건드리지 않는다.
      if (!feed.confirmingId && !feed.bulkConfirm) renderFeed();
    } catch { /* 잠깐 실패해도 다음 주기에 다시 시도한다 */ }
  }, REFRESH_MS);
}

// 단축키. H 숨김 · K 유지 · B 차단 · J/↓ 다음 · ↑ 이전 · Esc 취소
document.addEventListener("keydown", (e) => {
  if (!feed.active || document.querySelector(".modal-back")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.closest("textarea, select, input:not([type=checkbox])")) return;
  const k = e.key.toLowerCase();
  const s = sel();
  if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (k === "h" && s) act("hide", s.comment_id);
  else if (k === "k" && s) { if (feed.kind === "queue") act("keep", s.comment_id); else move(-1); }
  else if (k === "b" && s) act("ban", s.comment_id);
  else if (e.key === "Escape" && (feed.confirmingId || feed.bulkConfirm)) {
    feed.confirmingId = null; feed.bulkConfirm = false; renderFeed();
  }
});

// ── 인사이트 ──────────────────────────────────────────

const PERIOD_NAME = { today: "오늘", "7d": "최근 7일 동안", "30d": "최근 30일 동안" };
const PERIOD_LEAD = { today: "오늘은", "7d": "최근 7일 동안", "30d": "최근 30일 동안" };
const SEV_DONUT = [["5", "긴급", "#cf2c56"], ["4", "심각", "#e8806a"], ["3", "보통", "#e3b04b"], ["2", "주의", "#b3bfd8"], ["1", "낮음", "#8fa6ec"]];

/** /channels/{id}/watch 응답을 한 줄 상태로. 인사이트 아래 띠와 자동 감지 화면이 같이 쓴다. */
function monitorLine(w) {
  if (!w) return { on: false, state: "자동 감지 상태를 모르겠어요", times: "" };
  // 채널에서 켜 둬도 서버 감시가 꺼졌거나 권한·동의가 없으면 실제로는 안 돈다. 켜짐이라고 하지 않는다.
  const on = w.enabled && w.server_enabled && w.eligible;
  const mins = w.interval_min;
  const last = toDate(w.last_run_at);
  const next = toDate(w.next_run_at);
  return {
    on,
    mins,
    state: on ? `자동 감지 켜짐 · ${fmtDur(mins)}마다` : w.enabled ? "켜 두었지만 지금은 쉬는 중" : "자동 감지 꺼짐",
    times: on
      ? `마지막 분석 ${last ? hhmm(last) : "없음"}${next ? ` · 다음 예정 ${hhmm(next)}` : ""}`
      : `마지막 분석 ${last ? hhmm(last) : "없음"} · 검토 피드의 조회로만 불러와요`,
  };
}
const fmtDur = (m) => (m % 60 === 0 ? `${m / 60}시간` : m > 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${m}분`);

async function viewDashboard() {
  const P = ["today", "7d", "30d"].includes(localStorage.getItem("period")) ? localStorage.getItem("period") : "7d";
  loading();
  const [d, w] = await Promise.all([
    api(`/channels/${channelId()}/insights?period=${P}`),
    api(`/channels/${channelId()}/watch`).catch(() => null),
  ]);
  const harm = d.categories.reduce((a, [, n]) => a + n, 0);
  const types = d.categories.slice(0, 4);
  const tmax = Math.max(1, ...types.map(([, n]) => n));
  const sev = SEV_DONUT.map(([k, name, color]) => ({ name, color, n: d.levels[k] || 0 }));
  const total = sev.reduce((a, x) => a + x.n, 0);
  const gap = sev.filter((x) => x.n).length > 1 ? 0.8 : 0;
  let acc = 0;
  const stops = [];
  sev.forEach((x) => {
    if (!x.n) return;
    const a = (acc / total) * 100;
    acc += x.n;
    const b = (acc / total) * 100;
    stops.push(`${x.color} ${a}% ${b - gap}%`, `#fdfdfe ${b - gap}% ${b}%`);
  });
  const bmax = Math.max(1, ...d.trend.map((t) => t.n));
  const videos = d.videos.filter((v) => v.queued > 0).slice(0, 3);
  const m = monitorLine(w);
  const trendSub = { today: "최근 24시간 · 4시간 단위 유해 판정 건수", "7d": "최근 7일 · 날짜별 유해 판정 건수", "30d": "최근 30일 · 6일 단위 유해 판정 건수" }[P];

  view.innerHTML = `
    <div class="head"><p style="font-size:13px;font-weight:600">${esc(channelName())}</p></div>
    <div class="periods" role="tablist">
      ${[["today", "오늘"], ["7d", "최근 7일"], ["30d", "최근 30일"]].map(([k, l]) =>
        `<button role="tab" aria-selected="${k === P}" class="${k === P ? "on" : ""}" data-do="period" data-p="${k}">${l}</button>`).join("")}
    </div>
    <div class="summary"><b>댓글 요약</b><span>${
      harm ? `${PERIOD_LEAD[P]} '${esc(types[0][0])}' 유형이 가장 많았어요. 검토 대기 ${num(d.unreviewed)}건을 확인해 주세요.`
           : `${PERIOD_LEAD[P]} 유해 댓글이 없었어요.`}</span></div>
    <div class="kpis">
      <div class="kpi"><span class="l">분석한 댓글</span><span class="v"><b>${num(d.analyzed)}</b><span>건</span></span>
        <span class="s">${PERIOD_NAME[P]} AI가 판별</span></div>
      <div class="kpi blue"><span class="l">검토 대기</span><span class="v"><b>${num(d.unreviewed)}</b><span>건</span></span></div>
      <div class="kpi"><span class="l">조치한 댓글</span><span class="v"><b>${num(d.hidden)}</b><span>건</span></span>
        <span class="s">직접 숨김 ${num(d.hidden_manual)} · 등록어 자동 ${num(d.hidden - d.hidden_manual)}</span></div>
      <div class="kpi"><span class="l">검토 필요 비율</span><span class="v"><b>${d.analyzed ? Math.round((d.queued / d.analyzed) * 100) : 0}</b><span>%</span></span>
        <span class="s">검토 대기 ÷ 분석한 댓글</span></div>
    </div>
    <div class="two">
      <div class="card">
        <h2>위험도 분포</h2>
        <div class="donut-wrap">
          <div class="donut" style="background:${total ? `conic-gradient(${stops.join(", ")})` : "#ebecef"}">
            <div><b>${num(d.analyzed)}</b><span>분석한 댓글</span></div>
          </div>
          <div class="legend">${sev.map((x) => `<div><i style="background:${x.color}"></i><span>${x.name}</span><b>${num(x.n)}건</b></div>`).join("")}</div>
        </div>
      </div>
      <div class="card">
        <div class="head" style="gap:4px"><h2>유해 판정 댓글 추이</h2><p style="font-size:13px">${trendSub}</p></div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <div class="bars">${d.trend.map((t, i) => `<div class="${i === d.trend.length - 1 ? "last" : ""}" title="${esc(t.label)} 유해 판정 ${t.n}건">
              <span>${t.n}</span><i style="height:${(t.n / bmax) * 78}%"></i></div>`).join("")}</div>
          <div class="bar-labels">${d.trend.map((t, i) => `<span class="${i === d.trend.length - 1 ? "last" : ""}">${esc(t.label)}</span>`).join("")}</div>
        </div>
      </div>
    </div>
    <div class="two">
      <div class="card">
        <h2>유해 유형 순위</h2>
        ${types.length
          ? `<div class="types">${types.map(([name, n]) => `<div><span class="nm">${esc(name)}</span>
               <span class="tr"><i style="width:${(n / tmax) * 100}%"></i></span><span class="ct">${num(n)}건</span></div>`).join("")}</div>`
          : `<p class="small-note">이 기간에는 유해 댓글이 없었어요.</p>`}
      </div>
      <div class="card" style="gap:8px">
        <h2>확인이 필요한 영상</h2>
        ${videos.length
          ? `<div class="vids">${videos.map((v, i) => `<div><div class="t"><b>${esc(v.title)}</b>
               <span>검토 대기 ${num(v.queued)}건 · 분석 ${num(v.analyzed)}건</span></div>
               <button class="btn md" data-do="video" data-i="${i}">확인</button></div>`).join("")}</div>`
          : `<p class="small-note">검토할 댓글이 남은 영상이 없어요.</p>`}
      </div>
    </div>
    <div class="monbar">
      <span class="state ${m.on ? "on" : "off"}"><i></i>${m.state}</span>
      <span style="color:var(--muted)">${m.times}</span>
      <a class="btn sm" href="#/monitor" style="display:grid;place-items:center;text-decoration:none;font-weight:700">주기 설정</a>
    </div>`;

  actions = {
    period: (el) => { localStorage.setItem("period", el.dataset.p); viewDashboard(); },
    video: (el) => {
      const v = videos[Number(el.dataset.i)];
      feed.reset();
      feed.channel = channelId();
      feed.video = v.title;
      location.hash = "#/queue";
    },
  };
}

// ── 관리 기준 ─────────────────────────────────────────

async function viewRules() {
  loading();
  // 자동 숨김·채널 기준을 못 불러와도 등록어는 보여준다. 한쪽 실패로 화면 전체가 사라지면 원인을 모른다.
  const [list, auto, ctx] = await Promise.all([
    api(`/channels/${channelId()}/rules`),
    api(`/channels/${channelId()}/auto-hide`).catch(() => null),
    api(`/channels/${channelId()}/context`).catch(() => null),
  ]);

  view.innerHTML = `
    ${head("관리 기준", esc(channelName()))}
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
      <section class="card pad26">
        <div class="head"><h2>등록어</h2><p class="lead">AI보다 먼저 적용돼요. 적은 글자 그대로만 잡습니다.</p></div>
        <div class="head">
          <label class="lbl" for="word">단어 또는 표현</label>
          <div class="word-row">
            <input id="word" class="field" autocomplete="off">
            <select id="waction" class="field" aria-label="처리 방식"><option value="review">검토 큐로</option><option value="block">바로 숨김</option></select>
            <button class="btn dark" data-do="add" style="height:42px">추가</button>
          </div>
        </div>
        ${list.length
          ? `<div class="rchips">${list.map((r) => `<span class="rchip${r.action === "block" ? " block" : ""}">${esc(r.rule_value)}
               <small>${r.action === "block" ? "바로 숨김" : "검토"}</small>
               <button data-do="del" data-id="${r.id}" aria-label="${esc(r.rule_value)} 삭제">✕</button></span>`).join("")}</div>`
          : `<span class="small-note">등록된 단어가 없어요. 없어도 AI 판별은 정상으로 돕니다.</span>`}
      </section>

      <section class="card pad26">
        <div class="head"><h2>채널 판단 기준</h2><p class="lead">AI 판별에 참고로 붙어요. 영상이 바뀌어도 그대로인 것만 적으세요.</p></div>
        ${ctx
          ? `<textarea id="ctx" class="field" rows="8" aria-label="채널 판단 기준">${esc(ctx.context)}</textarea>
             <div class="spread"><small id="ctxinfo">판정 지문 ${esc(ctx.prompt_version)}</small>
               <button class="btn dark" data-do="saveCtx">저장</button></div>`
          : `<span class="small-note">불러오지 못했어요. 서버를 재시작해 보세요.</span>`}
      </section>

      <section class="card pad26" style="gap:10px">
        <div class="head"><h2>자동 숨김</h2><p class="lead">켜면 확인 없이 바로 가려져요. 되돌릴 수 없어서 기본은 전부 꺼짐입니다.</p></div>
        <div id="autohide">${auto ? autoRows(auto.options) : `<span class="small-note">설정을 불러오지 못했어요.</span>`}</div>
      </section>
    </div>`;

  const add = async () => {
    const v = $("#word").value.trim();
    if (!v) return;
    try {
      await api(`/channels/${channelId()}/rules`, {
        method: "POST",
        // 적은 글자 그대로만 잡는다. 초성·변형은 LLM 이 문맥으로 본다 (2026-09-30 결정).
        body: JSON.stringify({ rule_value: v, action: $("#waction").value, expand_variants: false }),
      });
      toast(`'${v}' 등록했어요`);
      viewRules();
    } catch (e) { toast(`등록하지 못했어요: ${e.message}`); }
  };
  $("#word").onkeydown = (e) => { if (e.key === "Enter") add(); };

  let saving = false;
  actions = {
    add,
    del: async (el) => {
      await api(`/channels/${channelId()}/rules/${el.dataset.id}`, { method: "DELETE" });
      toast("삭제했어요");
      viewRules();
    },
    saveCtx: async (el) => {
      const before = ctx.prompt_version;
      el.disabled = true;
      try {
        const next = await api(`/channels/${channelId()}/context`, {
          method: "PUT", body: JSON.stringify({ context: $("#ctx").value }),
        });
        $("#ctxinfo").textContent = next.prompt_version === before
          ? `판정 지문 ${next.prompt_version} (그대로)`
          : `판정 지문 ${before} → ${next.prompt_version}, 다음 판별부터`;
        toast("기준을 저장했어요");
      } catch (e) { toast(`저장하지 못했어요: ${e.message}`); }
      finally { el.disabled = false; }
    },
    // 화면의 현재 상태를 그대로 읽어 보낸다. 연달아 눌러도 마지막 상태가 저장된다.
    auto: async (el) => {
      if (saving) return;
      const name = el.dataset.name;
      const turningOn = el.getAttribute("aria-checked") !== "true";
      const on = [...view.querySelectorAll("#autohide .ah")]
        .filter((r) => (r === el ? turningOn : r.getAttribute("aria-checked") === "true"))
        .map((r) => r.dataset.name);
      saving = true;
      try {
        const next = await api(`/channels/${channelId()}/auto-hide`, { method: "PUT", body: JSON.stringify({ categories: on }) });
        $("#autohide").innerHTML = autoRows(next.options);
        toast(turningOn ? `'${name}'은 이제 확인 없이 가려져요` : `'${name}'은 검토 피드로 와요`);
      } catch (e) { toast(`바꾸지 못했어요: ${e.message}`); }
      finally { saving = false; }
    },
  };
}

const autoRows = (options) =>
  options.map((o) => `
    <button class="ah" role="switch" aria-checked="${o.enabled}" data-do="auto" data-name="${esc(o.name)}">
      <span class="tx"><b>${esc(o.name)}</b><span>${esc(o.description)}</span></span>
      <span class="sw"><i></i></span>
    </button>`).join("");

// ── 증거 보관함 ───────────────────────────────────────
//
// 고소용으로 고정해 둔 댓글 (api/evidence.py). 판별 즉시 자동으로 남는 것(4·5단계 유해)과
// 피드에서 [증거로 저장] 을 누른 것. 유튜브 정책상 30일이 지나면 지워져서, 그 안에
// 내려받아야 한다 — 남은 날을 맨 위와 줄마다 보여 주는 이유다.

async function viewEvidence() {
  loading();
  const rows = await api(`/channels/${channelId()}/evidence`);
  // 같은 사람이 거듭 단 것은 한데 모은다. 반복 여부는 처벌 수위를 정할 때도 본다.
  const groups = new Map();
  rows.forEach((e) => {
    const k = e.author_channel_id || `이름:${e.author_name || "익명"}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(e);
  });
  const soonest = rows.reduce((m, e) => (e.days_left != null && (m == null || e.days_left < m) ? e.days_left : m), null);
  const base = `${API}/channels/${channelId()}`;
  actions = {
    drop: async (el) => {
      const ok = await confirmModal({
        title: "보관함에서 뺄까요?",
        body: "이 증거 기록만 지웁니다. 댓글과 판정, 조치 이력은 그대로 남아요.",
        ok: "빼기", danger: true,
      });
      if (!ok) return;
      try {
        await api(`/evidence/${el.dataset.id}`, { method: "DELETE" });
        toast("보관함에서 뺐어요");
        viewEvidence();
      } catch (e) { toast(`빼지 못했어요: ${e.message}`); }
    },
  };
  const item = (e) => {
    const s = e.severity ? SEV[e.severity] : null;
    const when = e.published_at ? toDate(e.published_at).toLocaleString("ko-KR") : "";
    return `<div class="ev" style="display:flex;flex-direction:column;gap:6px;padding:14px 0;border-top:1px solid var(--line)">
      ${e.parent_content ? `<div class="parent">답글 단 댓글: ${esc(e.parent_content)}</div>` : ""}
      <p class="quote" style="margin:0">${esc(e.content)}</p>
      <div class="tags" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        ${s ? `<span class="pill" style="background:${s[1]};color:#fff">${s[0]}</span>` : ""}
        ${e.category ? `<span class="pill">${esc(e.category)}</span>` : ""}
        <span class="pill line">${e.source === "auto" ? "판별 즉시 자동" : `${esc((e.saved_by || "").split("@")[0])} 저장`}</span>
        ${e.days_left != null ? `<span class="pill ${e.days_left <= 7 ? "warn" : "line"}">${e.days_left}일 뒤 파기</span>` : ""}
        ${e.comment_url ? `<a href="${esc(e.comment_url)}" target="_blank" rel="noopener" style="font-size:13px">댓글 열기</a>` : ""}
        <button class="btn sm" data-do="drop" data-id="${e.id}" style="margin-left:auto">보관함에서 빼기</button>
      </div>
      <span class="small-note" style="font-size:12.5px">작성 ${esc(when)}${e.note ? ` · 메모: ${esc(e.note)}` : ""}${e.reasoning ? ` · AI: ${esc(e.reasoning)}` : ""}</span>
    </div>`;
  };
  view.innerHTML = `
    ${head("증거 보관함", `${num(rows.length)}건 · 작성자 ${num(groups.size)}명`)}
    <div class="banner${soonest != null && soonest <= 7 ? " bad" : ""}" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <span style="flex:1;min-width:240px;line-height:1.6">유튜브 정책상 Veil은 댓글을 30일까지만 보관해요.
        고소에 쓰려면 그 안에 내려받아 따로 보관하세요${soonest != null ? ` — 가장 이른 파기까지 <b>${soonest}일</b>` : ""}.
        4·5단계로 판정된 유해 댓글은 판별하는 순간 자동으로 저장돼요.</span>
      <a class="btn md" href="${base}/evidence.csv" download style="display:grid;place-items:center;text-decoration:none">엑셀(CSV) 내려받기</a>
      <a class="btn dark md" href="${base}/evidence/report" target="_blank" rel="noopener" style="display:grid;place-items:center;text-decoration:none">인쇄 · PDF</a>
    </div>
    ${rows.length
      ? [...groups.values()].map((list) => {
          const a = list[0];
          return `<div class="card" style="margin-top:14px;padding:16px 22px">
            <div style="display:flex;align-items:center;gap:10px">
              <div class="av" style="background:${avOf(a.author_name || "?")};width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:700">${initialOf(a.author_name)}</div>
              <b>${esc(a.author_name || "익명")}</b>
              <span class="small-note">${num(list.length)}건</span>
              ${a.author_url ? `<a href="${esc(a.author_url)}" target="_blank" rel="noopener" style="margin-left:auto;font-size:13px">작성자 채널</a>` : ""}
            </div>
            ${list.map(item).join("")}
          </div>`;
        }).join("")
      : `<div class="empty-card" style="margin-top:14px"><b>보관한 증거가 없어요</b>
          <span>검토 피드에서 [증거로 저장]을 누르거나, 4·5단계 유해 댓글이 들어오면 여기에 쌓여요.</span></div>`}`;
}

// ── 활동 ──────────────────────────────────────────────

const AK ={ hide: ["숨김", "#2350c8"], keep: ["유지", "#16171b"], ban_author: ["차단", "#2350c8"] };

async function viewHistory() {
  loading();
  const rows = await api(`/channels/${channelId()}/history?limit=100`);
  actions = {};
  view.innerHTML = `
    ${head("활동", `${num(rows.length)}건, 최신순`)}
    ${rows.length
      ? `<div class="card hist">${rows.map((h) => {
          const [ko, color] = AK[h.action] || [h.action, "#16171b"];
          const actor = h.actor || "자동";
          // 유지는 유튜브에 보낼 게 없다. 숨김·차단만 반영 여부를 본다.
          const pill = h.action === "keep"
            ? `<span class="pill">변경 없음</span>`
            : h.youtube_synced ? `<span class="pill">유튜브 반영</span>` : `<span class="pill warn" title="${esc(h.note || "")}">기록만</span>`;
          return `<div class="hrow">
            <div class="av">${initialOf(actor)}</div>
            <div class="tx">
              <span><b>${esc(actor)}</b> 님이 <b>${esc(h.author_name || "익명")}</b>의 댓글을 <b style="color:${color}">${ko}</b>했어요<span class="ago">${ago(h.executed_at)}</span></span>
              <span class="c">${esc(h.content)}</span>
            </div>
            ${pill}
          </div>`;
        }).join("")}</div>
        <p class="small-note">유튜브 반영은 실제로 가려졌다는 뜻이에요. 기록만은 채널 권한이 없거나 유튜브 호출이 실패해 우리 쪽에만 남은 것입니다.</p>`
      : `<div class="empty-card"><b>아직 처리한 댓글이 없어요</b><span>검토 피드에서 숨김·유지·차단을 누르면 여기에 남아요.</span></div>`}`;
}

// ── 채널 ──────────────────────────────────────────────

const 연동오류 = {
  cancelled: "연동을 취소하셨습니다.",
  access_denied: "구글에서 거부됐습니다. 미검증 앱이라 '테스트 사용자'에 등록된 계정만 됩니다.",
  no_refresh_token: "이미 허용해둔 계정이라 권한을 새로 못 받았습니다. 구글 계정 설정 > 보안 > 서드파티 앱에서 접근을 지운 뒤 다시 해주세요.",
  no_channel: "이 계정에 유튜브 채널이 없습니다.",
  already_connected: "이 채널은 이미 다른 워크스페이스에 연결되어 있습니다.",
  google_failed: "구글과 통신하지 못했습니다. 잠시 뒤 다시 해주세요.",
  expired: "연결 시간이 지났습니다 (구글 화면에서 10분 넘게 머물렀거나 서버가 재시작됨). 다시 해주세요.",
};

async function viewChannels() {
  loading();
  await syncChannels();
  const [states, invites] = await Promise.all([
    Promise.all(channels.map((c) =>
      api(`/channels/${c.id}/consent`).then((x) => ({ ...c, agreed: x.agreed })).catch(() => ({ ...c, agreed: false })))),
    api("/channels/invites").catch(() => []),
  ]);

  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  // 다시 연결하면 권한이 끊긴 동안 못 보낸 숨김을 서버가 바로 다시 보낸다 (services/resend.py).
  const resent = Number(q.get("resent") || 0);
  const resentOk = Number(q.get("resent_ok") || 0);
  const resentMsg = !resent ? ""
    : resentOk === resent ? ` 권한이 끊긴 동안 못 보낸 숨김 ${num(resent)}건도 유튜브에 반영했어요.`
      : ` 못 보낸 숨김 ${num(resent)}건 중 ${num(resentOk)}건만 반영됐어요. 숨김 탭에서 다시 보내 주세요.`;
  const notice = q.get("connected")
    ? `<div class="banner${resent && resentOk < resent ? " bad" : ""}"><span>채널 ${esc(q.get("connected"))}개를 연결했어요. 이제 숨김이 유튜브에 반영됩니다.${resentMsg}</span></div>`
    : q.get("error")
      ? `<div class="banner bad"><span>${esc(연동오류[q.get("error")] || q.get("error"))}</span></div>`
      : "";

  const inviteState = (i) =>
    i.used_at ? `연결됨${i.result ? `, ${esc(i.result)}` : ""}`
      : toDate(i.expires_at) < new Date() ? "기한 지남"
        : `대기 중, ${toDate(i.expires_at).toLocaleDateString("ko-KR", { month: "long", day: "numeric" })}까지`;

  view.innerHTML = `
    ${notice}
    <div class="ch-top"><a class="btn blue lg" href="/api/channels/connect/start" style="display:grid;place-items:center;text-decoration:none">채널 연결</a></div>
    ${states.length ? `<div class="chgrid">${states.map((c) => `
      <div class="chcard">
        <div class="top">
          <span class="ring${queueCount[c.id] ? " on" : ""}"><span>${initialOf(c.channel_title)}</span></span>
          <div class="nm"><b>${esc(c.channel_title)}</b><span>${
            c.connected
              ? c.connected_by ? `${esc(c.connected_by)} 님이 ${c.connected_at ? toDate(c.connected_at).toLocaleDateString("ko-KR") : ""} 연결` : "연결됨"
              : "숨김이 유튜브에 반영되지 않아요"}</span></div>
        </div>
        <div class="row2">
          <span class="pill${c.connected ? "" : " warn"}">${c.connected ? "유튜브 권한 있음" : "유튜브 권한 없음"}</span>
          <span class="pill${c.agreed ? "" : " warn"}">${c.agreed ? "AI 판별 동의" : "동의 전"}</span>
        </div>
        <div class="row2">
          <a class="btn dark md" href="/api/channels/connect/start" style="display:grid;place-items:center;text-decoration:none"
             title="${c.connected ? "권한이 만료됐거나 취소됐을 때. 채널 주인 계정으로 다시 허용" : ""}">${c.connected ? "다시 연결" : "유튜브 연동"}</a>
          <button class="btn md" data-do="consent" data-id="${c.id}" data-now="${c.agreed}">${c.agreed ? "동의 철회" : "AI 판별 동의"}</button>
          <button class="btn red md" data-do="off" data-id="${c.id}" data-name="${esc(c.channel_title)}">연동 해제</button>
        </div>
      </div>`).join("")}</div>`
      : `<div class="empty-card"><b>아직 연결한 채널이 없어요</b>
           <span>채널 주인의 구글 계정으로 유튜브 권한을 받는 단계예요. 채널이 다른 계정에 있으면 그 계정을 고르세요.</span></div>`}
    <section class="card pad26">
      <div class="head"><h2>유튜버에게 연결 링크 보내기</h2>
        <p class="lead" style="max-width:62ch">받은 사람은 가입 없이 자기 구글 계정으로 권한만 누르면 돼요. 한 번 쓰면 끝나고 7일 뒤 만료됩니다.</p></div>
      <div class="head">
        <label class="lbl" for="inv-note">받는 사람 메모</label>
        <div class="word-row">
          <input id="inv-note" class="field" style="flex:1 1 220px" autocomplete="off">
          <button class="btn dark" data-do="invite" style="height:42px">링크 만들기</button>
        </div>
      </div>
      ${invites.map((i) => `
        <div class="inv">
          <div class="tx"><span><b>${esc(i.note || "메모 없음")}</b><small>${inviteState(i)}</small></span>
            <span class="u">${esc(i.url)}</span></div>
          ${i.used_at ? "" : `<button class="btn sm" data-do="copy" data-url="${esc(i.url)}">복사</button>`}
        </div>`).join("")}
    </section>`;

  const copy = async (url, made) => {
    try {
      await navigator.clipboard.writeText(url);
      toast(made ? "링크를 만들어 복사했어요. 유튜버에게 보내세요" : "링크를 복사했어요");
    } catch {
      toast(made ? "링크를 만들었어요. 아래에서 복사하세요" : "복사하지 못했어요. 링크를 직접 선택해 주세요");
    }
  };

  actions = {
    consent: async (el) => {
      const 켜는중 = el.dataset.now !== "true";
      if (켜는중 && !(await consentModal())) return;
      await api(`/channels/${el.dataset.id}/consent`, { method: "PUT", body: JSON.stringify({ agreed: 켜는중 }) });
      toast(켜는중 ? "동의했어요. 다음 실행부터 판별합니다" : "동의를 철회했어요");
      viewChannels();
    },
    off: async (el) => {
      const yes = await confirmModal({
        title: "연동을 해제할까요?",
        body: `<b>${el.dataset.name}</b>에서 수집한 댓글, 판정, 조치 이력이 전부 삭제됩니다. 되돌릴 수 없어요.`,
        ok: "연동 해제",
        danger: true,
      });
      if (!yes) return;
      const r = await api(`/channels/${el.dataset.id}/disconnect`, { method: "POST" });
      toast(`연동을 해제했어요. 댓글 ${num(r.deleted_comments)}건을 삭제했습니다`);
      await loadChannels();
      viewChannels();
    },
    invite: async () => {
      try {
        const inv = await api("/channels/invites", { method: "POST", body: JSON.stringify({ note: $("#inv-note").value.trim() }) });
        await copy(inv.url, true);
        viewChannels();
      } catch (e) { toast(`링크를 만들지 못했어요: ${e.message}`); }
    },
    copy: (el) => copy(el.dataset.url, false),
  };
}

// ── 창 ────────────────────────────────────────────────

function modal(html, bind) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
    document.body.appendChild(back);
    const done = (v) => { back.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") done(false); };
    document.addEventListener("keydown", onKey);
    back.addEventListener("click", (e) => { if (e.target === back) done(false); });
    bind(back, done);
  });
}

/** AI 판별 동의. 유튜브 API 정책상 채널 관리자의 명시 동의가 있어야 자동 판별을 돌린다. */
function consentModal() {
  return modal(`
    <h3>AI 판별 동의</h3>
    <p>이 채널의 새 댓글을 매시간 모아 분석하고 확인할 순서를 정해요. 댓글을 가리는 건 관리자가 누를 때만입니다.</p>
    <ul><li>댓글 원문은 30일 뒤 자동으로 지워집니다</li><li>채널 기준과 자동 숨김은 언제든 바꿀 수 있습니다</li></ul>
    <label class="agree"><input type="checkbox" id="agree-chk">AI 자동 분석에 동의합니다</label>
    <div class="foot"><button class="btn" id="m-no">취소</button><button class="btn blue" id="m-yes" disabled>동의하고 시작</button></div>`,
    (back, done) => {
      $("#agree-chk", back).onchange = (e) => { $("#m-yes", back).disabled = !e.target.checked; };
      $("#m-no", back).onclick = () => done(false);
      $("#m-yes", back).onclick = () => done(true);
      $("#agree-chk", back).focus();
    });
}

function confirmModal({ title, body, ok = "확인", danger = false }) {
  return modal(`
    <h3>${esc(title)}</h3><p>${body}</p>
    <div class="foot"><button class="btn" id="m-no">취소</button>
      <button class="btn ${danger ? "blue" : "dark"}" id="m-yes" ${danger ? 'style="background:var(--critical)"' : ""}>${esc(ok)}</button></div>`,
    (back, done) => {
      $("#m-no", back).onclick = () => done(false);
      $("#m-yes", back).onclick = () => done(true);
      $("#m-yes", back).focus();
    });
}

// ── AI 프롬프트 ───────────────────────────────────────
//
// 왼쪽은 판별 프롬프트(채널별로 저장), 오른쪽은 댓글 하나로 GPT 를 실제로 불러 보는 시험.
// 시험은 저장 전 초안으로 돈다 — 고친 게 판정을 바꾸는지 저장하기 전에 본다.
// 한 번에 0.17~0.31원. 채널당 하루 100번까지 (channel_tools.TEST_DAILY).

const SAMPLES = [
  "오늘 라이브 ㄹㅈㄷ였다",
  "얼굴 보니까 왜 저러는지 알겠네 ㅋㅋ",
  "편집자 일 안 하냐 진짜 수준 떨어진다",
  "주소 대충 알아냈다 이번 주말에 한번 찾아가볼게",
  "구독자 늘리는 방법 궁금하면 프로필 링크 확인하세요",
];

const promptState = { channel: null, text: "", saved: "", def: "", testText: "", testVideo: "", testing: false, result: null, err: "", log: [] };

async function viewPrompt() {
  const P = promptState;
  if (P.channel !== channelId()) {
    Object.assign(P, { channel: channelId(), testText: "", testVideo: "", result: null, err: "", log: [] });
  }
  loading();
  const p = await api(`/channels/${channelId()}/prompt`);
  P.text = p.template;
  P.saved = p.template;
  P.def = p.default_template;

  const resultHtml = () => {
    if (P.testing) return `<span style="font-size:14px;color:var(--ink3)">AI가 댓글을 읽고 있어요.</span>`;
    if (P.err) return `<span role="alert" style="font-size:14px;line-height:1.6;color:var(--critical)">${esc(P.err)}</span>`;
    const r = P.result;
    if (!r) return `<span class="small-note" style="font-size:14px">댓글을 넣고 판별하기를 누르면 결과가 여기에 나와요.</span>`;
    if (!r.label) {
      return `<span role="alert" style="font-size:14px;line-height:1.6;color:var(--critical)">판별하지 못했어요. ${esc(r.error || "")}</span>
        ${r.raw ? `<details class="raw"><summary>응답 원문 보기</summary><pre>${esc(r.raw)}</pre></details>` : ""}`;
    }
    const [lk, lc] = LABEL[r.label] || ["판단 불가", "#636873"];
    const [sk, sc] = SEV[r.level] || ["등급 없음", "#636873"];
    return `
      <div class="res-tags">
        <span class="pill" style="background:${lc}">${lk}</span>
        <span class="pill" style="background:${sc}">${sk}</span>
        <span class="pill cat">${esc(r.category || "분류 없음")}</span>
        <span style="margin-left:auto;font-size:13px;color:var(--muted)">확신 <b class="mono" style="color:var(--ink)">${r.confidence != null ? r.confidence.toFixed(2) : "-"}</b></span>
      </div>
      <p style="margin:0;font-size:14.5px;line-height:1.65;text-wrap:pretty">${esc(r.reason || "")}</p>
      ${r.raw ? `<details class="raw"><summary>응답 원문 보기</summary><pre>${esc(r.raw)}</pre></details>` : ""}
      <span class="small-note" style="font-size:12.5px">오늘 ${num(r.tests_left)}번 더 시험할 수 있어요</span>`;
  };
  const paintResult = () => {
    $("#test-res").innerHTML = `<h2>판별 결과</h2>${resultHtml()}`;
    $("#test-log").hidden = !P.log.length;
    $("#test-log").innerHTML = `<span style="font-size:14px;font-weight:800">최근 시험</span>
      <div class="log">${P.log.map((r, i) => {
        const [lk, lc] = LABEL[r.label] || ["판단 불가", "#636873"];
        return `<button data-do="pickLog" data-i="${i}"><span class="pill" style="background:${lc}">${lk}</span><span class="t">${esc(r.text)}</span></button>`;
      }).join("")}</div>`;
    const btn = $("#run-test");
    btn.disabled = P.testing;
    btn.textContent = P.testing ? "판별 중" : "판별하기";
    btn.style.opacity = P.testing || !P.testText.trim() ? 0.55 : 1;
  };
  const info = () => `${P.text.length.toLocaleString("ko-KR")}자${P.text === P.saved ? "" : " · 저장 안 됨"}`;

  view.innerHTML = `
    ${head("AI 프롬프트", "판별에 쓰는 지시문을 고치고, 댓글을 넣어 바로 시험해 보세요.")}
    <div class="prompt-grid">
      <section class="card" style="gap:12px">
        <div class="spread"><h2>판별 프롬프트</h2><small id="p-info">${info()}</small></div>
        <p class="lead" style="font-size:13px">{채널 기준} 자리에는 관리 기준 탭에 적은 채널 판단 기준이 들어가요.</p>
        <textarea id="p-text" class="field code" rows="22" spellcheck="false" aria-label="판별 프롬프트">${esc(P.text)}</textarea>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn md" data-do="reset">기본값으로</button>
          <button class="btn dark md" data-do="save">저장</button>
        </div>
      </section>
      <div class="col">
        <section class="card" style="gap:12px">
          <h2>댓글 시험하기</h2>
          <input id="t-video" class="field" placeholder="영상 제목 (선택)" aria-label="영상 제목" value="${esc(P.testVideo)}">
          <textarea id="t-text" class="field" rows="4" placeholder="판별해 볼 댓글을 넣어 주세요" aria-label="댓글" style="font-size:15px;line-height:1.6">${esc(P.testText)}</textarea>
          <div class="samples"><span>예시</span>${SAMPLES.map((t, i) =>
            `<button data-do="sample" data-i="${i}">${esc(t.length > 16 ? `${t.slice(0, 16)}…` : t)}</button>`).join("")}</div>
          <div style="display:flex;align-items:center;gap:10px">
            <span class="small-note" style="font-size:12.5px">Ctrl + Enter로 바로 판별 · 한 번에 약 0.2~0.3원</span>
            <button id="run-test" class="btn blue" data-do="run" style="margin-left:auto;height:42px;padding:0 22px">판별하기</button>
          </div>
        </section>
        <section class="card" id="test-res" aria-live="polite" style="gap:12px;min-height:150px"></section>
        <section class="card" id="test-log" style="padding:18px 24px;gap:6px" hidden></section>
      </div>
    </div>`;
  paintResult();

  const run = async () => {
    const text = P.testText.trim();
    if (!text || P.testing) return;
    P.testing = true; P.err = ""; P.result = null;
    paintResult();
    try {
      const r = await api(`/channels/${channelId()}/prompt/test`, {
        method: "POST",
        body: JSON.stringify({ text, video_title: P.testVideo.trim() || null, template: P.text === P.saved ? null : P.text }),
      });
      P.result = { ...r, text };
      if (r.label) P.log = [P.result, ...P.log].slice(0, 5);
    } catch (e) {
      P.err = e.unauthorized ? "" : `AI 호출에 실패했어요. ${e.message}`;
    } finally {
      P.testing = false;
      if ($("#test-res")) paintResult();
    }
  };

  $("#p-text").oninput = (e) => { P.text = e.target.value; $("#p-info").textContent = info(); };
  $("#t-text").oninput = (e) => { P.testText = e.target.value; paintResult(); };
  $("#t-video").oninput = (e) => { P.testVideo = e.target.value; };
  $("#t-text").onkeydown = (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); run(); }
  };

  actions = {
    run,
    sample: (el) => { P.testText = SAMPLES[Number(el.dataset.i)]; $("#t-text").value = P.testText; paintResult(); },
    pickLog: (el) => {
      const r = P.log[Number(el.dataset.i)];
      P.testText = r.text; P.result = r; P.err = "";
      $("#t-text").value = r.text;
      paintResult();
    },
    reset: () => {
      P.text = P.def;
      $("#p-text").value = P.text;
      $("#p-info").textContent = info();
      toast("기본 프롬프트로 되돌렸어요. 저장해야 적용돼요");
    },
    save: async (el) => {
      el.disabled = true;
      try {
        const r = await api(`/channels/${channelId()}/prompt`, { method: "PUT", body: JSON.stringify({ template: P.text }) });
        P.text = r.template; P.saved = r.template;
        $("#p-text").value = P.text;
        $("#p-info").textContent = info();
        toast(r.is_default ? "기본 프롬프트로 저장했어요. 다음 판별부터 적용돼요" : "프롬프트를 저장했어요. 다음 판별부터 적용돼요");
      } catch (e) { toast(`저장하지 못했어요: ${e.message}`); }
      finally { el.disabled = false; }
    },
  };
}

// ── 자동 감지 ─────────────────────────────────────────
//
// 채널마다 켜고 끄고, 몇 분마다 돌지 정한다. 서버는 1분마다 깨어 때가 된 채널만 돈다
// (watcher.run_forever). 꺼 두면 검토 피드의 [조회] 로만 불러온다.

const PRESETS = [[10, "10분"], [30, "30분"], [60, "1시간"], [180, "3시간"]];

async function viewMonitor() {
  loading();
  let w = await api(`/channels/${channelId()}/watch`);
  let draftH = String(Math.floor(w.interval_min / 60));
  let draftM = String(w.interval_min % 60);
  let err = "";

  const paint = () => {
    const m = monitorLine(w);
    const on = w.enabled;
    const base = toDate(w.next_run_at) || new Date();
    const upcoming = on ? [0, 1, 2, 3].map((k) => hhmm(new Date(base.getTime() + k * w.interval_min * 60000))) : [];
    const warn = !w.server_enabled
      ? "서버 전체 감시가 꺼져 있어요 (WATCH_ENABLED=false). 켜 두어도 돌지 않습니다."
      : !w.eligible
        ? "이 채널은 유튜브 권한과 AI 판별 동의가 둘 다 있어야 자동으로 돌아요. 지금은 검토 피드의 조회로만 불러옵니다."
        : "";
    view.innerHTML = `
      ${head("자동 감지", "새 댓글을 몇 분마다 불러와 판별할지 정해요.")}
      ${warn ? `<div class="banner bad"><span>${warn}</span><a class="btn dark md" href="#/channels" style="display:grid;place-items:center;text-decoration:none">채널 설정</a></div>` : ""}
      <div class="card mon-head" style="flex-direction:row">
        <span class="tx">
          <span class="state ${m.on ? "on" : "off"}"><i></i>${m.state}</span>
          <small>${m.times}</small>
        </span>
        <button class="switch-btn" role="switch" aria-checked="${on}" aria-label="자동 감지" data-do="toggle"><span class="sw big"><i></i></span></button>
      </div>
      <div class="card${on ? "" : " off-dim"}">
        <div class="head" style="gap:4px"><h2>실행 주기</h2><p style="font-size:13px">시간과 분으로 입력해요. 5분부터 24시간까지 정할 수 있어요.</p></div>
        <div class="interval">
          <input id="mon-h" class="field" aria-label="시간" inputmode="numeric" value="${esc(draftH)}"${on ? "" : " disabled"}${err ? ' style="border-color:var(--critical)"' : ""}>
          <span>시간</span>
          <input id="mon-m" class="field" aria-label="분" inputmode="numeric" value="${esc(draftM)}"${on ? "" : " disabled"}${err ? ' style="border-color:var(--critical)"' : ""}>
          <span>분마다</span>
          <i class="sep"></i>
          ${PRESETS.map(([mins, label]) => `<button class="chipbtn${on && w.interval_min === mins ? " on" : ""}" data-do="preset" data-m="${mins}"${on ? "" : " disabled"}>${label}</button>`).join("")}
        </div>
        ${err ? `<span class="err" role="alert">${err}</span>` : ""}
        <div class="head" style="gap:10px;padding-top:14px;border-top:1px solid var(--line2)">
          <span style="font-size:13.5px;font-weight:700">다음 실행 예정</span>
          ${upcoming.length ? `<div class="upcoming">${upcoming.map((t) => `<span>${t}</span>`).join("")}</div>` : ""}
          <span class="small-note">${on
            ? `하루 약 ${Math.floor(1440 / w.interval_min)}번 실행돼요. 새 댓글이 없으면 판별도 비용도 생기지 않아요. 오늘 AI 호출 ${num(w.llm_calls_today)} / ${num(w.llm_daily_cap)}`
            : "꺼져 있는 동안에는 검토 피드의 조회 버튼으로만 불러와요."}</span>
        </div>
      </div>`;
    const apply = () => setInterval_();
    const key = (e) => { if (e.key === "Enter") e.target.blur(); };
    const h = $("#mon-h"), mm = $("#mon-m");
    h.oninput = (e) => { draftH = e.target.value.replace(/[^0-9]/g, "").slice(0, 2); e.target.value = draftH; };
    mm.oninput = (e) => { draftM = e.target.value.replace(/[^0-9]/g, "").slice(0, 4); e.target.value = draftM; };
    h.onblur = apply; mm.onblur = apply; h.onkeydown = key; mm.onkeydown = key;
  };

  const save = async (body, msg) => {
    try {
      w = await api(`/channels/${channelId()}/watch`, { method: "PUT", body: JSON.stringify(body) });
      draftH = String(Math.floor(w.interval_min / 60));
      draftM = String(w.interval_min % 60);
      err = "";
      paint();
      toast(msg(w));
    } catch (e) { toast(`바꾸지 못했어요: ${e.message}`); }
  };
  const setInterval_ = () => {
    const n = (parseInt(draftH, 10) || 0) * 60 + (parseInt(draftM, 10) || 0);
    if (n < 5 || n > 1440) { err = "5분에서 24시간 사이로 정해 주세요"; return paint(); }
    if (n === w.interval_min) { draftH = String(Math.floor(n / 60)); draftM = String(n % 60); err = ""; return paint(); }
    save({ interval_min: n }, (x) => `자동 감지 주기를 ${fmtDur(x.interval_min)}으로 바꿨어요`);
  };

  actions = {
    toggle: () => save({ enabled: !w.enabled }, (x) =>
      x.enabled ? `자동 감지를 켰어요. ${fmtDur(x.interval_min)}마다 분석해요` : "자동 감지를 껐어요"),
    preset: (el) => {
      if (!w.enabled) return;
      save({ interval_min: Number(el.dataset.m) }, (x) => `자동 감지 주기를 ${fmtDur(x.interval_min)}으로 바꿨어요`);
    },
  };
  paint();
}

// ── 로그인한 사람 ─────────────────────────────────────

async function showMe() {
  try {
    me = await api("/auth/me");
    paintMe();
  } catch { /* 로그인 전이면 api() 가 이미 안내 화면을 그렸다 */ }
}

// ── 화면 전환 ─────────────────────────────────────────

const ROUTES = {
  "#/queue": () => viewFeed("queue"),
  "#/hidden": () => viewFeed("hidden"),
  "#/passed": () => viewFeed("passed"),
  "#/dashboard": viewDashboard,
  "#/history": viewHistory,
  "#/evidence": viewEvidence,
  "#/rules": viewRules,
  "#/prompt": viewPrompt,
  "#/monitor": viewMonitor,
  "#/channels": viewChannels,
};
const FEED = new Set(["#/queue", "#/hidden", "#/passed"]);
const TITLE = {
  "#/queue": "검토 피드", "#/hidden": "숨김", "#/passed": "통과", "#/dashboard": "인사이트", "#/history": "활동", "#/evidence": "증거 보관함",
  "#/rules": "관리 기준", "#/prompt": "AI 프롬프트", "#/monitor": "자동 감지", "#/channels": "채널",
};
const WIDTH = {
  "#/queue": 1120, "#/hidden": 1120, "#/passed": 1120, "#/dashboard": 1040, "#/history": 720, "#/evidence": 900,
  "#/rules": 1100, "#/prompt": 1120, "#/monitor": 820, "#/channels": 1040,
};
const routeBase = () => location.hash.split("?")[0];

/** 아직 채널이 없을 때. 새로 가입한 사람이 처음 보는 화면이라 오류 메시지가 뜨면 안 된다. */
function showNoChannel() {
  view.className = "wrap w720";
  actions = {};
  view.innerHTML = `
    <div class="empty-card">
      <b>유튜브 채널을 연결해 주세요</b>
      <span>연결하면 댓글을 모아 위험한 것부터 보여드려요. 다음은 채널 주인의 구글 계정으로 유튜브 권한을 받는 단계예요.</span>
      <a class="btn blue lg" href="#/channels" style="margin-top:10px;display:grid;place-items:center;text-decoration:none">채널 연결하기</a>
    </div>`;
}

async function route() {
  // 연동에서 돌아오면 '#/channels?connected=1' 처럼 뒤에 값이 붙는다. 앞부분만 떼어 고른다.
  const base = routeBase();
  const hash = ROUTES[base] ? base : "#/queue";
  if (base !== hash) return (location.hash = hash);

  stopRefresh();
  feed.active = false;
  actions = {};
  markNav(hash);
  document.title = `${TITLE[hash]} · Veil`;

  if (!channels.length) await syncChannels();
  paintMe();
  if (!channels.length && hash !== "#/channels") return showNoChannel();
  // 화면마다 본문 폭이 다르다 (디자인 값).
  view.className = `wrap w${WIDTH[hash]}`;
  try {
    await ROUTES[hash]();
  } catch (e) {
    // 401 은 api() 가 이미 로그인 안내를 그려뒀다. 덮어쓰면 안 된다.
    if (!e.unauthorized) failed(e);
  }
  window.scrollTo(0, 0);
}

(async function start() {
  try {
    await loadChannels();
  } catch (e) {
    return e.unauthorized ? undefined : failed(e);
  }
  window.addEventListener("hashchange", route);
  // 다른 창에서 채널 연결을 마치고 돌아오면 목록을 다시 받는다.
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && (await syncChannels())) route();
  });
  showMe();
  await route();
  refreshCounts();
})();
