"use strict";

/* =========================================================================
   SILENT.CHANGES — feed controller
   ========================================================================= */

let ALL = [];                 // 정규화된 전체 엔트리 (최신순, seq 부여)
let activeFilter = "all";     // all | submarine | linked | stable | recurring
let activeSev = null;         // major | minor | trivial | null
let activeSort = "newest";    // newest | severity | changes
let query = "";
let expandedAll = false;

const SEV_LABEL = { major: "큰 변화", minor: "보통", trivial: "사소함" };
const SEV_RANK = { major: 3, minor: 2, trivial: 1 };
const MONTHS = {
  january: 0, february: 1, march: 2, april: 3, may: 4, june: 5,
  july: 6, august: 7, september: 8, october: 9, november: 10, december: 11,
};
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const $ = (sel) => document.querySelector(sel);

/* ---------- 초기화 ---------- */
async function init() {
  showSkeleton();
  let feed;
  try {
    const res = await fetch("data.json?_=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    feed = await res.json();
  } catch (e) {
    $("#generated-at").textContent = "SYNC 실패";
    $("#feed").innerHTML =
      '<div class="empty"><b>피드를 불러오지 못했습니다.</b><br>파이프라인이 한 번 실행되면 데이터가 채워집니다.</div>';
    return;
  }

  ALL = normalize(feed.entries || []);
  $("#generated-at").textContent =
    "SYNC " + formatGenerated(feed.generated_at) + " · " + ALL.length + " RECORDS";

  renderHero();
  buildStats();
  buildSpark();
  wireControls();
  render();
  handleHash();
  window.addEventListener("hashchange", handleHash);
  wireKeyboard();
  wireToTop();
}

/* ---------- 정규화 ---------- */
function normalize(entries) {
  const norm = entries.map((e) => {
    const ts = parsePostedAt(e.posted_at) || Date.parse(e.scraped_at) || 0;
    const fileTotal = (e.files_changed || []).reduce((s, f) => s + (Number(f.count) || 0), 0);
    const magnitude = fileTotal || (e.changes || []).length || 1;
    return { ...e, _ts: ts, _mag: magnitude };
  });
  // 최신순으로 정렬한 뒤 일련번호(파일 번호) 부여 — 가장 최근이 가장 큰 번호
  norm.sort((a, b) => b._ts - a._ts);
  const n = norm.length;
  norm.forEach((e, i) => { e._seq = n - i; });
  return norm;
}

function parsePostedAt(s) {
  if (!s) return null;
  // 예: "Friday, 12 June 2026 - 12:06 PM EDT"
  const m = String(s).match(
    /(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM))?/i
  );
  if (!m) return null;
  const day = +m[1];
  const mon = MONTHS[m[2].toLowerCase()];
  if (mon === undefined) return null;
  const year = +m[3];
  let hour = m[4] ? +m[4] : 12;
  const min = m[5] ? +m[5] : 0;
  const ap = m[6] ? m[6].toUpperCase() : null;
  if (ap === "PM" && hour < 12) hour += 12;
  if (ap === "AM" && hour === 12) hour = 0;
  return Date.UTC(year, mon, day, hour, min);
}

/* ---------- 히어로(최신 가로챈 전송) ---------- */
function renderHero() {
  const el = $("#hero");
  if (!ALL.length) { el.hidden = true; return; }
  const e = ALL[0];
  const sev = e.severity || "minor";
  const kind = e.locked
    ? '<span class="badge badge-lock">🔒 공개 대기</span>'
    : e.is_submarine
    ? '<span class="badge badge-intel">🌊 잠수함 패치</span>'
    : '<span class="badge badge-signal">🔗 공지 연결</span>';
  const headline = e.summary_ko || "(요약 없음)";

  el.innerHTML = `
    <article class="intercept">
      <span class="bracket tl" aria-hidden="true"></span>
      <span class="bracket br" aria-hidden="true"></span>
      <div class="hero-eyebrow">
        <span class="live">LATEST INTERCEPT</span>
        <span>${esc(timeAgo(e._ts))}</span>
        <span>· ${esc(e.eft_version || "버전 미상")}</span>
      </div>
      <h2 class="hero-h" id="hero-headline">${esc(headline)}</h2>
      <div class="hero-meta">
        ${kind}
        <span class="sev ${sev}">${SEV_LABEL[sev] || sev}</span>
        ${stabBadge(e.stability)}
      </div>
      <div class="hero-foot">
        <button class="hero-cta" type="button" id="hero-cta">전체 해석 열기 ▾</button>
        <span class="hero-id">FILE №${String(e._seq).padStart(3, "0")} · ID ${esc(e.entry_id || "—")}</span>
      </div>
    </article>`;
  el.hidden = false;

  $("#hero-cta").addEventListener("click", () => {
    activeFilter = "all"; activeSev = null; query = ""; activeSort = "newest";
    syncControls();
    render();
    openEntry(e.entry_id, true);
  });

  if (!reduceMotion) scramble($("#hero-headline"), headline);
}

/* 헤드라인 디크립트(복호화) 연출 */
function scramble(node, finalText) {
  const glyphs = "█▓▒░#@%&$/\\<>=+*АБВГ01";
  const chars = [...finalText];
  let frame = 0;
  const total = 16;
  const id = setInterval(() => {
    frame++;
    const reveal = Math.floor((frame / total) * chars.length);
    node.textContent = chars
      .map((c, i) => (i < reveal || c === " " ? c : glyphs[(i + frame) % glyphs.length]))
      .join("");
    if (frame >= total) { clearInterval(id); node.textContent = finalText; }
  }, 34);
}

/* ---------- 통계(카운트업) ---------- */
function buildStats() {
  const el = $("#stats");
  const n = (fn) => ALL.filter(fn).length;
  const defs = [
    { filter: "all", label: "전체 기록", value: ALL.length },
    { filter: "submarine", label: "🌊 잠수함", value: n((e) => e.is_submarine) },
    { filter: "stable", label: "📌 안정 유지", value: n((e) => e.stability === "stable") },
    { filter: "recurring", label: "🔁 반복", value: n((e) => e.stability === "recurring") },
  ];
  el.innerHTML = defs
    .map(
      (d) => `<button class="stat" data-filter="${d.filter}" aria-label="${esc(d.label)} ${d.value}건">
        <span class="stat-num" data-to="${d.value}">0</span>
        <span class="stat-label">${d.label}</span>
      </button>`
    )
    .join("");
  el.querySelectorAll(".stat-num").forEach(countUp);
}

function countUp(node) {
  const to = +node.dataset.to || 0;
  if (reduceMotion || to === 0) { node.textContent = to; return; }
  const dur = 650; let start = null;
  const step = (t) => {
    if (start === null) start = t;
    const p = Math.min((t - start) / dur, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    node.textContent = Math.round(to * eased);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------- 활동 스파크라인 ---------- */
function buildSpark() {
  const el = $("#spark-bars");
  const items = [...ALL].sort((a, b) => a._ts - b._ts); // 시간 오름차순
  const max = Math.max(1, ...items.map((e) => e._mag));
  el.innerHTML = items
    .map((e) => {
      const h = Math.max(8, Math.round((e._mag / max) * 100));
      const sev = e.severity || "minor";
      const title = `${e.summary_ko ? e.summary_ko.slice(0, 40) : "변경"} · ${e._mag}`;
      return `<button class="spark-bar sev-${sev}" style="height:${h}%" data-id="${esc(e.entry_id)}" title="${esc(title)}" aria-label="${esc(title)}"></button>`;
    })
    .join("");
  el.querySelectorAll(".spark-bar").forEach((b) =>
    b.addEventListener("click", () => openEntry(b.dataset.id, true))
  );
}

/* ---------- 컨트롤 ---------- */
function wireControls() {
  $("#search").addEventListener("input", (ev) => {
    query = ev.target.value.trim().toLowerCase();
    render();
  });
  $("#sort").addEventListener("change", (ev) => { activeSort = ev.target.value; render(); });
  $("#expand-all").addEventListener("click", toggleExpandAll);

  $("#filters").querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.sev) {
        activeSev = activeSev === btn.dataset.sev ? null : btn.dataset.sev;
      } else {
        activeFilter = btn.dataset.filter;
      }
      syncControls();
      render();
    });
  });
  $("#stats").querySelectorAll(".stat").forEach((btn) => {
    btn.addEventListener("click", () => {
      activeFilter = btn.dataset.filter;
      syncControls();
      render();
    });
  });
}

function syncControls() {
  document.querySelectorAll(".chip[data-filter]").forEach((b) =>
    b.classList.toggle("active", b.dataset.filter === activeFilter));
  document.querySelectorAll(".stat").forEach((b) =>
    b.classList.toggle("active", b.dataset.filter === activeFilter));
  document.querySelectorAll(".sev-chip").forEach((b) =>
    b.classList.toggle("active", b.dataset.sev === activeSev));
  $("#sort").value = activeSort;
}

function toggleExpandAll() {
  expandedAll = !expandedAll;
  const btn = $("#expand-all");
  btn.setAttribute("aria-pressed", String(expandedAll));
  btn.textContent = expandedAll ? "전체 접기" : "전체 펼치기";
  document.querySelectorAll(".card").forEach((c) => c.classList.toggle("open", expandedAll));
}

/* ---------- 필터/정렬/렌더 ---------- */
function matchesFilter(e) {
  if (activeFilter === "submarine" && !e.is_submarine) return false;
  if (activeFilter === "linked" && e.is_submarine) return false;
  if (activeFilter === "stable" && e.stability !== "stable") return false;
  if (activeFilter === "recurring" && e.stability !== "recurring") return false;
  if (activeSev && (e.severity || "minor") !== activeSev) return false;
  return true;
}

function matchesQuery(e) {
  if (!query) return true;
  const hay = [
    e.summary_ko,
    (e.tags || []).join(" "),
    (e.changes || []).map((c) => c.key_path + " " + c.explanation_ko).join(" "),
    e.eft_version,
  ].join(" ").toLowerCase();
  return hay.includes(query);
}

function sortItems(items) {
  const arr = [...items];
  if (activeSort === "severity") {
    arr.sort((a, b) => (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0) || b._ts - a._ts);
  } else if (activeSort === "changes") {
    arr.sort((a, b) => b._mag - a._mag || b._ts - a._ts);
  } else {
    arr.sort((a, b) => b._ts - a._ts);
  }
  return arr;
}

function render() {
  const feed = $("#feed");
  const items = sortItems(ALL.filter((e) => matchesFilter(e) && matchesQuery(e)));

  const total = ALL.length;
  $("#result-count").textContent =
    items.length === total
      ? `${total}건 표시`
      : `${items.length} / ${total}건 표시${query ? ` · "${query}"` : ""}`;

  if (!items.length) {
    feed.innerHTML = '<div class="empty"><b>조건에 맞는 기록이 없습니다.</b><br>필터를 초기화하거나 다른 키워드로 검색해 보세요.</div>';
    return;
  }
  feed.innerHTML = items.map(card).join("");
  feed.querySelectorAll(".card").forEach((c) => {
    if (expandedAll) c.classList.add("open");
    c.querySelector(".card-head").addEventListener("click", () => c.classList.toggle("open"));
  });
  feed.querySelectorAll(".tag").forEach((t) =>
    t.addEventListener("click", (ev) => {
      ev.stopPropagation();
      $("#search").value = t.dataset.tag;
      query = t.dataset.tag.toLowerCase();
      render();
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    })
  );
  feed.querySelectorAll(".share-btn").forEach((b) =>
    b.addEventListener("click", (ev) => { ev.stopPropagation(); copyLink(b); })
  );
}

/* ---------- 카드 ---------- */
function card(e) {
  const sev = e.severity || "minor";
  const kind = e.locked
    ? '<span class="badge badge-lock">🔒 공개 대기</span>'
    : e.is_submarine
    ? '<span class="badge badge-intel">🌊 잠수함</span>'
    : '<span class="badge badge-signal">🔗 공지 연결</span>';
  const back = e.backfilled ? '<span class="badge badge-backfill">📚 과거 백필</span>' : "";
  const tags = (e.tags || [])
    .map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("");
  const files = (e.files_changed || []).map((f) => `${esc(f.path)} (${f.count})`).join(", ");

  return `
  <article class="card" id="e-${esc(e.entry_id)}" data-sev="${esc(sev)}" data-stab="${esc(e.stability || "")}">
    <div class="card-head">
      <div class="card-top">
        <span class="file-no">FILE №${String(e._seq).padStart(3, "0")}</span>
        <span class="card-top-spacer"></span>
        <span class="chev" aria-hidden="true">⌄</span>
      </div>
      <div class="badges">
        ${kind}
        ${stabBadge(e.stability)}
        ${back}
        <span class="sev ${sev}">${SEV_LABEL[sev] || sev}</span>
        ${tags}
      </div>
      <h2>${esc(e.summary_ko || "(요약 없음)")}</h2>
      <div class="card-meta">
        <span>🗓 <b>${esc(e.posted_at || "날짜 미상")}</b></span>
        <span>🎮 ${esc(e.eft_version || "버전 미상")}</span>
        ${files ? `<span>📄 ${esc(files)}</span>` : ""}
      </div>
    </div>
    <div class="card-body">
      <div class="card-body-inner">
        ${lockedBlock(e)}
        ${e.locked ? "" : patchNoteBlock(e)}
        ${stabilityBlock(e)}
        ${(e.changes || []).map(changeBlock).join("")}
        ${rawBlock(e)}
        <div class="share-row">
          <button class="share-btn" type="button" data-id="${esc(e.entry_id)}">🔗 링크 복사</button>
        </div>
      </div>
    </div>
  </article>`;
}

function lockedBlock(e) {
  if (!e.locked) return "";
  return `<div class="panel-block locked-block">
    🔒 <b>공개 대기 중</b> — 원본(tarkov-changes)이 게시 후 12시간 동안 비공개라
    아직 변경 내용을 받지 못했습니다. 실제 변경은 존재하며, 잠금이 풀리면 다음 자동 갱신 때
    한글 해석이 채워집니다.
    <div class="reason">원본에서 확인: ${esc((e.files_changed || []).map((f) => f.path + " (" + f.count + ")").join(", ") || "변경 감지됨")}</div>
  </div>`;
}

function stabBadge(stab) {
  const M = {
    recurring: '<span class="badge badge-recurring">🔁 반복 이벤트</span>',
    stable: '<span class="badge badge-signal">📌 안정 유지</span>',
    superseded: '<span class="badge badge-super">♻️ 이후 갱신됨</span>',
  };
  return M[stab] || "";
}

function stabilityBlock(e) {
  if (!e.stability || !e.stability_detail_ko) return "";
  const ICON = { recurring: "🔁", stable: "📌", superseded: "♻️" };
  return `<div class="panel-block stability ${esc(e.stability)}">
    ${ICON[e.stability] || "📊"} <b>안정성 자동 판정</b>
    <div class="reason">${esc(e.stability_detail_ko)}</div>
  </div>`;
}

function patchNoteBlock(e) {
  const pn = e.patch_note || {};
  if (e.is_submarine) {
    return `<div class="panel-block patchnote submarine">
      🌊 <b>잠수함 패치</b> — 공식 패치노트에서 대응 공지를 찾지 못했습니다.
      <div class="reason">${esc(pn.reason_ko || "")}</div>
    </div>`;
  }
  const link = pn.url
    ? `<a href="${esc(pn.url)}" target="_blank" rel="noopener">${esc(pn.title || "공식 패치노트")} ↗</a>`
    : esc(pn.title || "");
  return `<div class="panel-block patchnote linked">
    🔗 <b>관련 공지</b>: ${link}
    <div class="reason">${esc(pn.reason_ko || "")}</div>
  </div>`;
}

function changeBlock(c) {
  const ba = (c.before_ko || c.after_ko)
    ? `<div class="ba"><span class="before">${esc(c.before_ko || "—")}</span><span class="arrow">→</span><span class="after">${esc(c.after_ko || "—")}</span></div>`
    : "";
  return `<div class="change">
    ${c.key_path ? `<div class="keypath">${esc(c.key_path)}</div>` : ""}
    ${ba}
    ${c.explanation_ko ? `<div class="explain">${esc(c.explanation_ko)}</div>` : ""}
    ${c.impact_ko ? `<div class="impact">${esc(c.impact_ko)}</div>` : ""}
  </div>`;
}

function rawBlock(e) {
  if (!e.raw_text) return "";
  return `<details class="raw-toggle">
    <summary>원문 diff 보기</summary>
    <pre>${esc(e.raw_text)}</pre>
  </details>`;
}

/* ---------- 딥링크 / 공유 ---------- */
function openEntry(id, scrollTo) {
  const card = document.getElementById("e-" + id);
  if (!card) return;
  card.classList.add("open");
  if (scrollTo) {
    card.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
    card.classList.remove("flash");
    void card.offsetWidth; // 리플로우로 애니메이션 재시작
    card.classList.add("flash");
  }
}

function handleHash() {
  const id = location.hash.replace(/^#e-?/, "");
  if (id) openEntry(id, true);
}

function copyLink(btn) {
  const id = btn.dataset.id;
  const url = location.origin + location.pathname + "#e-" + id;
  const done = () => {
    const orig = btn.textContent;
    btn.textContent = "✓ 복사됨";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 1600);
  };
  if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, done);
  else { history.replaceState(null, "", "#e-" + id); done(); }
}

/* ---------- 키보드 ---------- */
function wireKeyboard() {
  document.addEventListener("keydown", (ev) => {
    const tag = (ev.target.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || tag === "select";
    if (ev.key === "/" && !typing) { ev.preventDefault(); $("#search").focus(); }
    else if (ev.key === "Escape" && tag === "input") {
      ev.target.value = ""; query = ""; render(); ev.target.blur();
    } else if (ev.key.toLowerCase() === "e" && !typing) {
      toggleExpandAll();
    }
  });
}

/* ---------- 맨 위로 ---------- */
function wireToTop() {
  const btn = $("#to-top");
  window.addEventListener("scroll", () => { btn.hidden = window.scrollY < 600; }, { passive: true });
  btn.addEventListener("click", () =>
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" }));
}

/* ---------- 유틸 ---------- */
function showSkeleton() {
  $("#feed").innerHTML = Array.from({ length: 4 }, () => '<div class="skeleton"></div>').join("");
}

function timeAgo(ts) {
  if (!ts) return "시점 미상";
  const diff = Date.now() - ts;
  const min = Math.round(diff / 60000);
  if (min < 1) return "방금 전";
  if (min < 60) return min + "분 전";
  const hr = Math.round(min / 60);
  if (hr < 24) return hr + "시간 전";
  const day = Math.round(hr / 24);
  if (day < 30) return day + "일 전";
  const mon = Math.round(day / 30);
  if (mon < 12) return mon + "개월 전";
  return Math.round(mon / 12) + "년 전";
}

function formatGenerated(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
  } catch { return iso; }
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

init();
