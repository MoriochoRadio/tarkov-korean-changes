"use strict";

let ALL = [];
let activeFilter = "all";
let query = "";

const SEV_LABEL = { major: "큰 변화", minor: "보통", trivial: "사소함" };
const TAG_PREFIX = "#";

async function init() {
  try {
    const res = await fetch("data.json?_=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    const feed = await res.json();
    ALL = feed.entries || [];
    document.getElementById("generated-at").textContent =
      "마지막 업데이트: " + formatGenerated(feed.generated_at) + " · 총 " + (feed.count || ALL.length) + "건";
  } catch (e) {
    document.getElementById("generated-at").textContent = "데이터를 불러오지 못했습니다.";
    document.getElementById("feed").innerHTML =
      '<div class="empty">아직 data.json 이 없습니다. 파이프라인을 한 번 실행하면 채워집니다.</div>';
    return;
  }
  wireControls();
  render();
}

function wireControls() {
  document.getElementById("search").addEventListener("input", (e) => {
    query = e.target.value.trim().toLowerCase();
    render();
  });
  document.querySelectorAll(".filter").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      activeFilter = btn.dataset.filter;
      render();
    });
  });
}

function matchesFilter(e) {
  if (activeFilter === "submarine") return e.is_submarine;
  if (activeFilter === "linked") return !e.is_submarine;
  if (activeFilter === "stable") return e.stability === "stable";
  if (activeFilter === "recurring") return e.stability === "recurring";
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

function render() {
  const feed = document.getElementById("feed");
  const items = ALL.filter((e) => matchesFilter(e) && matchesQuery(e));
  if (!items.length) {
    feed.innerHTML = '<div class="empty">조건에 맞는 변경이 없습니다.</div>';
    return;
  }
  feed.innerHTML = items.map(card).join("");
  feed.querySelectorAll(".card-head").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("open"));
  });
}

function card(e) {
  const sev = e.severity || "minor";
  const subBadge = e.is_submarine
    ? '<span class="sub-badge">🌊 잠수함 패치</span>'
    : '<span class="linked-badge">🔗 공지 연결됨</span>';
  const STAB_BADGE = {
    recurring: '<span class="event-badge">🔁 반복 이벤트</span>',
    stable: '<span class="stable-badge">📌 안정 유지</span>',
    superseded: '<span class="superseded-badge">♻️ 이후 갱신됨</span>',
  };
  const stabBadge = STAB_BADGE[e.stability] || "";
  const backfillBadge = e.backfilled ? '<span class="backfill-badge">📚 과거 백필</span>' : "";
  const tags = (e.tags || []).map((t) => `<span class="tag">${TAG_PREFIX}${esc(t)}</span>`).join("");
  const files = (e.files_changed || []).map((f) => `${esc(f.path)} (${f.count})`).join(", ");

  return `
  <article class="card">
    <div class="card-head">
      <span class="toggle-hint">펼치기 ▾</span>
      <div class="badges">
        ${subBadge}
        ${stabBadge}
        ${backfillBadge}
        <span class="sev ${sev}">${SEV_LABEL[sev] || sev}</span>
        ${tags}
      </div>
      <h2>${esc(e.summary_ko || "(요약 없음)")}</h2>
      <div class="card-meta">
        <span>🗓 <b>${esc(e.posted_at || "날짜 미상")}</b></span>
        <span>🎮 ${esc(e.eft_version || "버전 미상")}</span>
        <span>📄 ${esc(files || "")}</span>
      </div>
    </div>
    <div class="card-body">
      ${patchNoteBlock(e)}
      ${stabilityBlock(e)}
      ${(e.changes || []).map(changeBlock).join("")}
      ${rawBlock(e)}
    </div>
  </article>`;
}

function stabilityBlock(e) {
  if (!e.stability || !e.stability_detail_ko) return "";
  const ICON = { recurring: "🔁", stable: "📌", superseded: "♻️" };
  return `<div class="stability ${esc(e.stability)}">
    ${ICON[e.stability] || "📊"} <b>안정성 자동 판정</b>
    <div class="reason">${esc(e.stability_detail_ko)}</div>
  </div>`;
}

function patchNoteBlock(e) {
  const pn = e.patch_note || {};
  if (e.is_submarine) {
    return `<div class="patchnote submarine">
      🌊 <b>잠수함 패치</b> — 공식 패치노트에서 대응되는 공지를 찾지 못했습니다.
      <div class="reason">${esc(pn.reason_ko || "")}</div>
    </div>`;
  }
  const link = pn.url ? `<a href="${esc(pn.url)}" target="_blank" rel="noopener">${esc(pn.title || "공식 패치노트")} ↗</a>` : esc(pn.title || "");
  return `<div class="patchnote linked">
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

function formatGenerated(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return iso;
  }
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

init();
