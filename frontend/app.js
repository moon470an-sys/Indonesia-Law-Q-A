"use strict";

const LS_KEYS = {
  url: "rag.apiUrl",
  token: "rag.apiToken",
  topK: "rag.topK",
  history: "rag.history",
};
const HISTORY_LIMIT = 30;

const els = {
  apiUrl: document.getElementById("apiUrl"),
  apiToken: document.getElementById("apiToken"),
  topK: document.getElementById("topK"),
  saveBtn: document.getElementById("saveBtn"),
  healthBtn: document.getElementById("healthBtn"),
  askBtn: document.getElementById("askBtn"),
  clearBtn: document.getElementById("clearBtn"),
  question: document.getElementById("question"),
  history: document.getElementById("history"),
  status: document.getElementById("status"),
  corpus: document.getElementById("corpus"),
  corpusGrid: document.getElementById("corpusGrid"),
  corpusTotal: document.getElementById("corpusTotal"),
  exampleChips: document.getElementById("exampleChips"),
};

// indonesia_* 컬렉션명 → 한국어 표시명 + 약어 매핑.
const COLLECTION_META = {
  indonesia_constitution: { ko: "헌법", abbr: "UUD" },
  indonesia_uu:           { ko: "법률", abbr: "UU" },
  indonesia_pp:           { ko: "정부령", abbr: "PP" },
  indonesia_perpres:      { ko: "대통령령", abbr: "Perpres" },
  indonesia_permen:       { ko: "장관령", abbr: "Permen" },
  indonesia_kepmen:       { ko: "장관결정", abbr: "Kepmen" },
  indonesia_perda:        { ko: "지방조례", abbr: "Perda" },
  indonesia_lainnya:      { ko: "기타", abbr: "Lainnya" },
};
const COLLECTION_ORDER = [
  "indonesia_constitution",
  "indonesia_uu",
  "indonesia_pp",
  "indonesia_perpres",
  "indonesia_permen",
  "indonesia_kepmen",
  "indonesia_perda",
  "indonesia_lainnya",
];

const selectedCategories = new Set(); // 사용자가 클릭으로 선택한 컬렉션명들

// 카테고리별 큐레이션 예시 질문. cats가 비어있으면 항상 표시 (전체).
const EXAMPLE_QUESTIONS = [
  // 헌법
  { label: "대통령의 권한", q: "인도네시아 헌법에서 대통령의 권한은 무엇인가?", cats: ["indonesia_constitution"] },
  { label: "국민 기본권", q: "인도네시아 헌법상 국민의 기본권은 어떻게 규정되어 있는가?", cats: ["indonesia_constitution"] },
  { label: "의회 구성 (MPR/DPR/DPD)", q: "인도네시아 의회(MPR, DPR, DPD)의 구성과 역할은?", cats: ["indonesia_constitution"] },
  { label: "헌법 개정 절차", q: "인도네시아 헌법 개정 절차는 어떻게 되는가?", cats: ["indonesia_constitution"] },
  // 법률 UU
  { label: "노동법 주요 내용", q: "인도네시아 노동법(UU 13/2003)의 주요 내용은?", cats: ["indonesia_uu"] },
  { label: "PT 회사 설립 요건", q: "회사법상 유한책임회사(PT) 설립 요건과 절차는?", cats: ["indonesia_uu"] },
  { label: "외국인투자법", q: "외국인투자법(UU Penanaman Modal)의 주요 규정은?", cats: ["indonesia_uu"] },
  { label: "조세 일반규정", q: "조세일반규정법(KUP)에서 납세자의 권리와 의무는?", cats: ["indonesia_uu"] },
  // 정부령 PP
  { label: "환경영향평가(AMDAL)", q: "환경영향평가(AMDAL)의 절차와 대상 사업은?", cats: ["indonesia_pp"] },
  { label: "토지수용 절차", q: "공익을 위한 토지수용 절차에 관한 정부령은 무엇이 있는가?", cats: ["indonesia_pp"] },
  // 대통령령 Perpres
  { label: "외국인근로자 채용", q: "외국인근로자(TKA) 채용에 관한 대통령령의 주요 내용은?", cats: ["indonesia_perpres"] },
  { label: "부동산 외국인 소유", q: "외국인의 부동산 소유에 관한 대통령령은?", cats: ["indonesia_perpres"] },
  // 장관령 Permen
  { label: "산업안전보건", q: "산업안전보건 관련 노동부 장관령의 주요 규정은?", cats: ["indonesia_permen"] },
  { label: "수입 라이선스(API)", q: "수입업자 식별번호(API) 발급 요건은?", cats: ["indonesia_permen"] },
  { label: "할랄 인증 절차", q: "할랄 인증 의무 대상 품목과 인증 절차는?", cats: ["indonesia_permen"] },
  // 장관결정 Kepmen
  { label: "최저임금 결정", q: "주별 최저임금(UMP) 결정 절차와 기준은?", cats: ["indonesia_kepmen"] },
  // 지방조례 Perda
  { label: "사업 인허가 (자카르타)", q: "자카르타 특별주에서 사업 인허가 관련 조례는?", cats: ["indonesia_perda"] },
  // 기타
  { label: "대통령훈령 효력", q: "대통령훈령(Inpres)의 법적 효력과 위계는?", cats: ["indonesia_lainnya"] },
  // 일반 (전체 범위)
  { label: "법령 위계", q: "인도네시아 법령의 위계(헌법 > 법률 > 정부령 ...)와 충돌 시 우선순위는?", cats: [] },
  { label: "외국인 사업 형태", q: "외국인이 인도네시아에서 사업할 때 가능한 법적 형태(PT PMA, 대표사무소 등)는?", cats: [] },
];

function formatCount(n) {
  return Number(n || 0).toLocaleString("ko-KR");
}

async function fetchAutoUrl() {
  try {
    const r = await fetch(`tunnel.json?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) return "";
    const data = await r.json();
    return (data.url || "").trim().replace(/\/+$/, "");
  } catch {
    return "";
  }
}

function normalizeUrl(u) {
  return (u || "").trim().replace(/\/+$/, "");
}

async function loadSettings() {
  const cfg = window.APP_CONFIG || {};

  const params = new URLSearchParams(location.search);
  const urlFromQuery = params.get("api");
  if (urlFromQuery) {
    localStorage.setItem(LS_KEYS.url, urlFromQuery);
    history.replaceState(null, "", location.pathname);
  }
  // ?reset=1 로 stale localStorage 청소
  if (params.get("reset")) {
    localStorage.removeItem(LS_KEYS.url);
    history.replaceState(null, "", location.pathname);
  }

  const storedUrl = normalizeUrl(localStorage.getItem(LS_KEYS.url));
  const autoUrl = await fetchAutoUrl();

  // tunnel.json이 발급된 fresh URL을 가지고 있으면 항상 그걸 우선.
  // localStorage 우선이면 옛 죽은 URL이 새 URL을 덮는 stale 문제가 생김.
  // 사용자가 ?api=...로 명시 지정한 경우는 위에서 storedUrl로 덮어 씌워졌으니 보존됨.
  els.apiUrl.value = autoUrl || storedUrl || cfg.defaultApiUrl || "";
  els.apiToken.value = localStorage.getItem(LS_KEYS.token) || "";
  els.topK.value = localStorage.getItem(LS_KEYS.topK) || cfg.defaultTopK || 5;
}

function saveSettings() {
  localStorage.setItem(LS_KEYS.url, els.apiUrl.value.trim());
  localStorage.setItem(LS_KEYS.token, els.apiToken.value);
  localStorage.setItem(LS_KEYS.topK, els.topK.value);
  setStatus("설정 저장됨", "ok");
}

function setStatus(msg, kind = "info") {
  els.status.textContent = msg;
  els.status.dataset.kind = kind;
}

function apiBase() {
  return (els.apiUrl.value || "").replace(/\/+$/, "");
}

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (els.apiToken.value) h["X-Api-Token"] = els.apiToken.value;
  return h;
}

async function tryHealthOnce(base) {
  const r = await fetch(`${base}/health`, { headers: authHeaders() });
  const data = await r.json();
  return data;
}

function applyHealth(data) {
  if (data.ok && data.collection_count > 0) {
    setStatus(`연결 OK · 청크 ${formatCount(data.collection_count)}개 로드됨`, "ok");
    renderCorpus(data.collections || {}, data.collection_count);
  } else if (data.ok) {
    setStatus("연결 OK · DB 비어있음 (ingest.py 실행 필요)", "warn");
    els.corpus.hidden = true;
  } else {
    setStatus(`서버 오류: ${data.error || "unknown"}`, "err");
    els.corpus.hidden = true;
  }
}

function renderCorpus(perCollection, total) {
  const entries = Object.entries(perCollection || {}).filter(([, n]) => n > 0);
  if (!entries.length) {
    els.corpus.hidden = true;
    return;
  }
  // 미리 정의된 순서에 따라 정렬, 그 외는 뒤에 추가.
  entries.sort(([a], [b]) => {
    const ia = COLLECTION_ORDER.indexOf(a);
    const ib = COLLECTION_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  const max = entries.reduce((m, [, n]) => Math.max(m, n), 1);

  els.corpusTotal.textContent = `총 ${formatCount(total)}개 청크 · ${entries.length}개 카테고리`;
  els.corpusGrid.innerHTML = entries
    .map(([name, count]) => {
      const meta = COLLECTION_META[name] || { ko: name.replace(/^indonesia_/, ""), abbr: name };
      const pct = Math.max(2, Math.round((count / max) * 100));
      const pressed = selectedCategories.has(name) ? "true" : "false";
      const hueCls = categoryHueClass(name);
      return `
        <div class="corpus-card ${hueCls}" data-col="${escapeHtml(name)}" role="button" tabindex="0" aria-pressed="${pressed}" title="${escapeHtml(meta.ko)} (${escapeHtml(meta.abbr)})">
          <div class="cc-name">${escapeHtml(meta.ko)}</div>
          <div class="cc-abbr">${escapeHtml(meta.abbr)}</div>
          <div class="cc-count">${formatCount(count)}<span class="cc-count-suffix">청크</span></div>
          <div class="cc-bar"><span style="width: ${pct}%"></span></div>
        </div>`;
    })
    .join("");
  els.corpus.hidden = false;

  els.corpusGrid.querySelectorAll(".corpus-card").forEach((card) => {
    const name = card.dataset.col;
    const toggle = () => {
      if (selectedCategories.has(name)) selectedCategories.delete(name);
      else selectedCategories.add(name);
      card.setAttribute("aria-pressed", selectedCategories.has(name) ? "true" : "false");
      updateScopeIndicator();
    };
    card.addEventListener("click", toggle);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
  });
  updateScopeIndicator();
}

function updateScopeIndicator() {
  // 선택된 카테고리가 있으면 ask 영역에 작은 표시. 미니멀하게 placeholder 변경.
  if (!selectedCategories.size) {
    els.question.placeholder = "인도네시아 법령에 관해 질문하세요…";
  } else {
    const names = [...selectedCategories]
      .map((n) => (COLLECTION_META[n]?.ko) || n)
      .join(", ");
    els.question.placeholder = `[${names}] 범위에서 질문하세요…`;
  }
  renderExamples();
}

function renderExamples() {
  if (!els.exampleChips) return;
  let visible;
  if (!selectedCategories.size) {
    // 전체 표시 (모든 카테고리 + 일반)
    visible = EXAMPLE_QUESTIONS;
  } else {
    // 선택된 카테고리에 매칭 + 일반(cats:[])도 포함
    visible = EXAMPLE_QUESTIONS.filter((ex) => {
      if (!ex.cats.length) return true;
      return ex.cats.some((c) => selectedCategories.has(c));
    });
  }
  els.exampleChips.innerHTML = visible
    .map((ex) => {
      const cat = ex.cats[0]; // 첫 번째 카테고리로 색상 부여 (일반은 색 없음)
      const hueCls = cat ? categoryHueClass(cat) : "";
      return `<button class="chip ${hueCls}" data-q="${escapeHtml(ex.q)}">${escapeHtml(ex.label)}</button>`;
    })
    .join("");
  els.exampleChips.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      els.question.value = btn.dataset.q || "";
      els.question.focus();
    });
  });
}

async function checkHealth() {
  let base = apiBase();
  if (!base) {
    setStatus("백엔드 URL을 먼저 입력하세요", "err");
    return;
  }
  setStatus("연결 확인 중…", "info");
  try {
    applyHealth(await tryHealthOnce(base));
    return;
  } catch (e) {
    // 첫 실패 → tunnel.json refetch 후 재시도. 죽은 URL은 어차피 보존 의미가 없으므로 차이만 있으면 무조건 갱신.
    const newAuto = await fetchAutoUrl();
    if (newAuto && newAuto !== base) {
      els.apiUrl.value = newAuto;
      localStorage.removeItem(LS_KEYS.url);
      setStatus(`URL 자동 갱신 (${newAuto}) 재시도 중…`, "info");
      try {
        applyHealth(await tryHealthOnce(newAuto));
        return;
      } catch (e2) {
        setStatus(`연결 실패 (자동 갱신 후도): ${e2.message}`, "err");
        return;
      }
    }
    setStatus(`연결 실패: ${e.message}`, "err");
  }
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// 인용 패턴: (Pasal 6A, 출처: 파일명) 또는 (출처: 파일명) → 칩 처리.
const CITE_RE = /\(([^()]*?(?:Pasal|출처)[^()]*?)\)/g;

function renderInline(line) {
  // 먼저 escape, 그 다음 안전한 인라인 마크다운만 변환.
  let out = escapeHtml(line);
  // **bold**
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // (Pasal X, 출처: ...) → 인용 칩
  out = out.replace(CITE_RE, '<span class="cite">$1</span>');
  return out;
}

function renderAnswer(answer) {
  // Claude 출력 마크다운 일부를 렌더링: 볼드, 순서/비순서 리스트, 헤딩, 단락.
  const lines = String(answer || "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let buf = [];
  let listType = null; // "ul" | "ol" | null
  const flushPara = () => {
    if (buf.length) {
      blocks.push(`<p>${buf.map(renderInline).join("<br>")}</p>`);
      buf = [];
    }
  };
  const flushList = () => {
    if (listType && buf.length) {
      const items = buf.map((t) => `<li>${renderInline(t)}</li>`).join("");
      blocks.push(`<${listType}>${items}</${listType}>`);
    }
    buf = [];
    listType = null;
  };
  const flush = () => {
    if (listType) flushList();
    else flushPara();
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flush();
      continue;
    }
    const ulM = line.match(/^\s*[-*]\s+(.+)$/);
    const olM = line.match(/^\s*\d+\.\s+(.+)$/);
    const hM = line.match(/^\s*(#{1,3})\s+(.+)$/);

    if (hM) {
      flush();
      const level = Math.min(hM[1].length + 2, 4); // ## → h4 등 너무 크지 않게
      blocks.push(`<h${level} class="a-h">${renderInline(hM[2])}</h${level}>`);
      continue;
    }
    if (ulM) {
      if (listType !== "ul") flush();
      listType = "ul";
      buf.push(ulM[1]);
      continue;
    }
    if (olM) {
      if (listType !== "ol") flush();
      listType = "ol";
      buf.push(olM[1]);
      continue;
    }
    // 일반 단락 라인
    if (listType) flushList();
    buf.push(line);
  }
  flush();
  return blocks.join("");
}

// rag_server는 category에 폴더명(한국어) 또는 컬렉션명을 넣을 수 있음. 한국어로 정규화.
function categoryKo(cat) {
  if (!cat) return "";
  if (COLLECTION_META[cat]) return COLLECTION_META[cat].ko;
  // 폴더명에서 prefix가 있을 수 있음 (예: "헌법", "법률_UU")
  const head = String(cat).split(/[_\s]/)[0];
  return head || cat;
}

function categoryHueClass(cat) {
  // 한국어 또는 컬렉션명 둘 다 매핑.
  const k = categoryKo(cat);
  const map = {
    "헌법": "cat-uud",
    "법률": "cat-uu",
    "정부령": "cat-pp",
    "대통령령": "cat-perpres",
    "장관령": "cat-permen",
    "장관결정": "cat-kepmen",
    "지방조례": "cat-perda",
    "기타": "cat-lainnya",
  };
  return map[k] || "cat-lainnya";
}

function shortenFileName(name, max = 64) {
  const s = String(name || "");
  if (s.length <= max) return s;
  // 가운데 줄임표
  const head = Math.ceil((max - 1) / 2);
  const tail = Math.floor((max - 1) / 2);
  return s.slice(0, head) + "…" + s.slice(-tail);
}

function renderSource(s, i) {
  const article = s.article || "조항 미확인";
  const score = Number(s.score) || 0;
  const scorePct = Math.max(0, Math.min(100, Math.round(score * 100)));
  const cat = s.category || "";
  const catKo = categoryKo(cat);
  const hueCls = categoryHueClass(cat);
  return `
    <li class="src-card ${hueCls}">
      <header class="src-top">
        <span class="src-idx">#${i + 1}</span>
        ${catKo ? `<span class="src-cat">${escapeHtml(catKo)}</span>` : ""}
        <span class="src-score-wrap" title="유사도 ${score.toFixed(3)}">
          <span class="src-score-bar"><span style="width:${scorePct}%"></span></span>
          <span class="src-score-num">${score.toFixed(2)}</span>
        </span>
      </header>
      <div class="src-name" title="${escapeHtml(s.source || "")}">${escapeHtml(shortenFileName(s.source))}</div>
      <div class="src-meta">
        <span class="src-page">p.${escapeHtml(String(s.page ?? "?"))}</span>
        <span class="src-sep">·</span>
        <span class="src-article">${escapeHtml(article)}</span>
      </div>
      <pre class="src-snippet">${escapeHtml(s.snippet || "")}</pre>
    </li>`;
}

function formatRelativeTime(ts) {
  if (!ts) return "";
  const diff = Date.now() - ts;
  if (diff < 60_000) return "방금 전";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}분 전`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}시간 전`;
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function buildQaCard(item) {
  const { q, a, sources = [], scope = [], elapsedMs, ts, id } = item;
  const wrap = document.createElement("article");
  wrap.className = "qa";
  wrap.dataset.id = id;
  const srcHtml = sources.map(renderSource).join("");

  const scopeText = scope.length
    ? scope.map((c) => COLLECTION_META[c]?.ko || c).join(", ")
    : "전체 법령";
  const elapsed = elapsedMs ? `${(elapsedMs / 1000).toFixed(1)}s` : "";
  const metaBits = [
    `<span class="qa-meta-item">📂 ${escapeHtml(scopeText)}</span>`,
    `<span class="qa-meta-item">🔎 출처 ${sources.length}건</span>`,
    elapsed ? `<span class="qa-meta-item">⏱ ${elapsed}</span>` : "",
    ts ? `<span class="qa-meta-item qa-meta-ts" data-ts="${ts}">🕘 ${escapeHtml(formatRelativeTime(ts))}</span>` : "",
  ].filter(Boolean).join("");

  wrap.innerHTML = `
    <header class="qa-head">
      <h3 class="q" title="클릭 → 입력창에 다시 채우기">Q. ${escapeHtml(q)}</h3>
      <div class="qa-actions">
        <button class="qa-copy" type="button" aria-label="답변 복사" title="답변을 클립보드에 복사">📋 복사</button>
        <button class="qa-del" type="button" aria-label="이 질문 삭제" title="삭제">✕</button>
      </div>
    </header>
    <div class="qa-meta">${metaBits}</div>
    <div class="a">${renderAnswer(a)}</div>
    <details class="sources" ${sources.length ? "open" : ""}>
      <summary>🔎 검색된 출처 ${sources.length}건</summary>
      <ul class="src-list">${srcHtml}</ul>
    </details>
  `;

  // 인용 칩 → 동일 카드 내 매칭되는 출처로 점프
  wrap.querySelectorAll(".cite").forEach((cite) => {
    cite.style.cursor = "pointer";
    cite.title = "클릭 → 매칭 출처로 이동";
    cite.addEventListener("click", () => {
      const text = cite.textContent || "";
      const filenameMatch = text.match(/출처:\s*([^,)]+)/);
      const fname = filenameMatch ? filenameMatch[1].trim() : "";
      if (!fname) return;
      const cards = wrap.querySelectorAll(".src-card");
      let target = null;
      for (const card of cards) {
        const name = card.querySelector(".src-name")?.title || card.querySelector(".src-name")?.textContent || "";
        if (name && (name.includes(fname) || fname.includes(name.replace(/…/, "")))) {
          target = card;
          break;
        }
      }
      if (!target) return;
      const details = wrap.querySelector("details.sources");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.remove("pulse");
      void target.offsetWidth; // reflow → 애니 재시작
      target.classList.add("pulse");
    });
  });

  // 삭제 버튼
  wrap.querySelector(".qa-del")?.addEventListener("click", () => {
    deleteHistoryItem(id);
  });

  // Q 클릭 → 입력창에 다시 채우기
  wrap.querySelector(".q")?.addEventListener("click", () => {
    els.question.value = q;
    els.question.focus();
    els.question.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // 답변 복사 버튼
  const copyBtn = wrap.querySelector(".qa-copy");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const sourceLines = sources.map((s, i) =>
        `[${i + 1}] ${categoryKo(s.category) || ""} · ${s.source} · p.${s.page} · ${s.article || ""} (유사도 ${(s.score || 0).toFixed(2)})`
      ).join("\n");
      const fullText = `Q. ${q}\n\n${a}\n\n— 출처 —\n${sourceLines}`;
      const original = copyBtn.textContent;
      try {
        await navigator.clipboard.writeText(fullText);
        copyBtn.textContent = "✅ 복사됨";
        copyBtn.classList.add("copied");
      } catch {
        copyBtn.textContent = "❌ 실패";
      }
      setTimeout(() => {
        copyBtn.textContent = original;
        copyBtn.classList.remove("copied");
      }, 1600);
    });
  }

  return wrap;
}

function loadHistoryItems() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_KEYS.history) || "[]");
    if (!Array.isArray(raw)) return [];
    return raw;
  } catch { return []; }
}

function saveHistoryItems(items) {
  try {
    localStorage.setItem(LS_KEYS.history, JSON.stringify(items.slice(0, HISTORY_LIMIT)));
  } catch { /* quota exceeded — silently drop */ }
}

let historyItems = [];

function renderHistoryEmpty() {
  if (historyItems.length) return;
  els.history.innerHTML = `
    <div class="empty-state">
      <div class="empty-icon">💬</div>
      <p class="empty-title">아직 질문이 없습니다</p>
      <p class="empty-sub">위 입력창에 질문을 입력하거나, 예시 질문 버튼을 눌러 시작하세요.<br>
      답변과 출처는 브라우저에 자동 저장되어 다음 방문 때도 유지됩니다.</p>
    </div>`;
}

function renderHistoryAll() {
  els.history.innerHTML = "";
  if (!historyItems.length) {
    renderHistoryEmpty();
    return;
  }
  for (const item of historyItems) {
    els.history.appendChild(buildQaCard(item));
  }
}

function addHistoryItem(item) {
  historyItems.unshift(item);
  if (historyItems.length > HISTORY_LIMIT) historyItems.length = HISTORY_LIMIT;
  saveHistoryItems(historyItems);
  // 빈 상태 표시 제거 후 카드 prepend
  if (els.history.querySelector(".empty-state")) els.history.innerHTML = "";
  els.history.prepend(buildQaCard(item));
}

function deleteHistoryItem(id) {
  historyItems = historyItems.filter((x) => x.id !== id);
  saveHistoryItems(historyItems);
  const card = els.history.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (card) card.remove();
  if (!historyItems.length) renderHistoryEmpty();
}

function clearAllHistory() {
  historyItems = [];
  saveHistoryItems(historyItems);
  renderHistoryEmpty();
}

// 로딩 스켈레톤: askQuestion 동안 임시 카드 표시
function makeSkeletonCard(q, scope) {
  const wrap = document.createElement("article");
  wrap.className = "qa qa-skeleton";
  const scopeText = scope.length
    ? scope.map((c) => COLLECTION_META[c]?.ko || c).join(", ")
    : "전체 법령";
  wrap.innerHTML = `
    <h3 class="q">Q. ${escapeHtml(q)}</h3>
    <div class="qa-meta">
      <span class="qa-meta-item">📂 ${escapeHtml(scopeText)}</span>
      <span class="qa-meta-item">⏳ Claude가 답변 생성 중…</span>
    </div>
    <div class="a">
      <div class="skeleton-line" style="width: 96%"></div>
      <div class="skeleton-line" style="width: 88%"></div>
      <div class="skeleton-line" style="width: 70%"></div>
      <div class="skeleton-line" style="width: 92%"></div>
      <div class="skeleton-line" style="width: 60%"></div>
    </div>`;
  return wrap;
}

async function postQueryOnce(base, q, topK) {
  const body = { question: q, top_k: topK };
  if (selectedCategories.size) body.categories = [...selectedCategories];
  const r = await fetch(`${base}/query`, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status} ${text.slice(0, 200)}`);
  }
  return r.json();
}

async function askQuestion() {
  const q = els.question.value.trim();
  if (!q) return;
  let base = apiBase();
  if (!base) {
    setStatus("백엔드 URL을 먼저 입력하세요", "err");
    return;
  }
  const topK = parseInt(els.topK.value || "5", 10);

  els.askBtn.disabled = true;
  els.askBtn.textContent = "답변 생성 중…";
  const scope = selectedCategories.size
    ? [...selectedCategories].map((n) => COLLECTION_META[n]?.ko || n).join(", ")
    : "전체 법령";
  setStatus(`Claude가 ${scope} 문서를 검토하는 중…`, "info");

  const t0 = performance.now();
  const scope = [...selectedCategories];

  // 빈 상태 제거하고 스켈레톤 prepend
  if (els.history.querySelector(".empty-state")) els.history.innerHTML = "";
  const skeleton = makeSkeletonCard(q, scope);
  els.history.prepend(skeleton);

  try {
    let data;
    try {
      data = await postQueryOnce(base, q, topK);
    } catch (e) {
      // cloudflared URL이 바뀐 경우: tunnel.json refetch 후 한 번 재시도
      const newAuto = await fetchAutoUrl();
      if (newAuto && newAuto !== base) {
        els.apiUrl.value = newAuto;
        localStorage.removeItem(LS_KEYS.url);
        setStatus(`URL 자동 갱신 (${newAuto}) 재시도 중…`, "info");
        base = newAuto;
        data = await postQueryOnce(base, q, topK);
      } else {
        throw e;
      }
    }
    const elapsedMs = performance.now() - t0;
    skeleton.remove();
    addHistoryItem({
      id: `qa_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      q,
      a: data.answer,
      sources: data.sources || [],
      scope,
      elapsedMs,
      ts: Date.now(),
    });
    els.question.value = "";
    setStatus("답변 생성 완료", "ok");
  } catch (e) {
    skeleton.remove();
    if (!historyItems.length) renderHistoryEmpty();
    setStatus(`요청 실패: ${e.message}`, "err");
  } finally {
    els.askBtn.disabled = false;
    els.askBtn.textContent = "질문하기";
  }
}

// 키보드 단축키: Ctrl/Cmd+K → 질문 입력창 포커스, Esc → 입력 비우기 (포커스 시)
document.addEventListener("keydown", (e) => {
  const isInput = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    els.question.focus();
    els.question.select();
    return;
  }
  if (e.key === "Escape" && document.activeElement === els.question) {
    if (els.question.value) {
      els.question.value = "";
      e.preventDefault();
    }
  }
});

els.saveBtn.addEventListener("click", saveSettings);
els.healthBtn.addEventListener("click", checkHealth);
els.askBtn.addEventListener("click", askQuestion);
els.clearBtn.addEventListener("click", () => {
  if (!historyItems.length) return;
  if (confirm(`저장된 ${historyItems.length}개 질문을 모두 삭제하시겠습니까?`)) {
    clearAllHistory();
  }
});
els.question.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    askQuestion();
  }
});

// 페이지 로드 시 저장된 히스토리 즉시 복원, 그 후 백엔드 헬스체크.
historyItems = loadHistoryItems();
renderHistoryAll();
renderExamples(); // 초기 chip 렌더 (헬스체크 응답 전에도 보이도록)
loadSettings().then(() => checkHealth());

// 히스토리에 표시된 상대시간(NN분 전)을 1분마다 갱신.
setInterval(() => {
  els.history.querySelectorAll(".qa-meta-ts").forEach((el) => {
    const ts = Number(el.dataset.ts);
    if (ts) el.textContent = `🕘 ${formatRelativeTime(ts)}`;
  });
}, 60_000);
