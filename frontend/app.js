"use strict";

const LS_KEYS = {
  url: "rag.apiUrl",
  token: "rag.apiToken",
  topK: "rag.topK",
  history: "rag.history",
  theme: "rag.theme",
  fontSize: "rag.fontSize",
  corpusSort: "rag.corpusSort",
  draft: "rag.draft",
};

const LONG_ANSWER_CHARS = 800; // 이 이상이면 접기 버튼 노출
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
  corpusSelectAll: document.getElementById("corpusSelectAll"),
  corpusSelectNone: document.getElementById("corpusSelectNone"),
  exampleChips: document.getElementById("exampleChips"),
  historyToolbar: document.getElementById("historyToolbar"),
  historySearch: document.getElementById("historySearch"),
  historyCount: document.getElementById("historyCount"),
  exportBtn: document.getElementById("exportBtn"),
  starredOnlyBtn: document.getElementById("starredOnlyBtn"),
  themeToggle: document.getElementById("themeToggle"),
  scrollTop: document.getElementById("scrollTop"),
  citeTip: document.getElementById("citeTip"),
  helpToggle: document.getElementById("helpToggle"),
  helpModal: document.getElementById("helpModal"),
  legendGrid: document.getElementById("legendGrid"),
  statsSection: document.getElementById("statsSection"),
  statsGrid: document.getElementById("statsGrid"),
  historySort: document.getElementById("historySort"),
  fontMinus: document.getElementById("fontMinus"),
  fontPlus: document.getElementById("fontPlus"),
  corpusSort: document.getElementById("corpusSort"),
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

// fetch with timeout — 죽은 URL/네트워크 문제로 무한 대기하지 않도록 모든 헬스/터널 fetch에 적용
function fetchWithTimeout(url, opts = {}, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => clearTimeout(timer));
}

async function fetchAutoUrl() {
  try {
    const r = await fetchWithTimeout(`tunnel.json?t=${Date.now()}`, { cache: "no-store" }, 5000);
    if (!r.ok) return "";
    const text = (await r.text()).replace(/^﻿/, ""); // BOM 방어
    const data = JSON.parse(text);
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

  setStatus("연결 확인 중…", "info"); // tunnel.json fetch가 길어져도 사용자에게 "막힘"으로 보이지 않게.

  const params = new URLSearchParams(location.search);
  const urlFromQuery = params.get("api");
  // ?reset=1 로 stale localStorage 청소
  if (params.get("reset")) {
    localStorage.removeItem(LS_KEYS.url);
    history.replaceState(null, "", location.pathname);
  }
  if (urlFromQuery) {
    // 옛날엔 localStorage에 박았는데 그게 죽은 URL을 stale 상태로 누적시킴.
    // 이제는 sessionStorage(브라우저 탭 단위 임시)에만 두고, 다음 새로고침이면 tunnel.json 자동 갱신본을 우선 사용.
    sessionStorage.setItem(LS_KEYS.url, urlFromQuery);
    history.replaceState(null, "", location.pathname);
  }

  const queryUrl = normalizeUrl(sessionStorage.getItem(LS_KEYS.url));
  const storedUrl = normalizeUrl(localStorage.getItem(LS_KEYS.url));
  const autoUrl = await fetchAutoUrl();

  // 우선순위: tunnel.json fresh URL > ?api= (sessionStorage) > 사용자 수동 설정 localStorage > config 기본값.
  // tunnel.json이 살아있으면 항상 그걸 신뢰. (watchdog가 매번 새 URL 발급 후 push하므로)
  els.apiUrl.value = autoUrl || queryUrl || storedUrl || cfg.defaultApiUrl || "";
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
  // POST/PUT 등 body를 보내는 호출용 (Content-Type + 옵션 토큰)
  const h = { "Content-Type": "application/json" };
  if (els.apiToken.value) h["X-Api-Token"] = els.apiToken.value;
  return h;
}

function getHeaders() {
  // GET 호출용: Content-Type을 안 박아서 CORS preflight 회피.
  // 토큰이 있을 때만 X-Api-Token을 보냄 (없으면 preflight 자체 없음).
  // 토큰이 있어도 헤더가 하나만 있으면 그 헤더가 simple/non-simple인지에 따라 preflight 발생할 수 있음.
  // X-Api-Token은 custom header라 항상 preflight 발생 → 토큰 있을 때만 부담.
  if (els.apiToken.value) return { "X-Api-Token": els.apiToken.value };
  return undefined;  // headers 자체 미설정 → 100% simple GET → preflight 없음
}

async function tryHealthOnce(base) {
  // quick=1 → 서버는 캐시 있으면 캐시, 없으면 warming=true 즉답.
  // 15s timeout: cloudflared QuickTunnel은 가끔 preflight + GET이 도합 5-10s 걸림.
  // 8s timeout이면 정상 응답도 abort돼서 "signal is aborted without reason" 에러로 표시됨.
  const opts = {};
  const h = getHeaders();
  if (h) opts.headers = h;
  const r = await fetchWithTimeout(`${base}/health?quick=1`, opts, 15000);
  const data = await r.json();
  // warming 응답이면 잠시 후 한번 더 (캐시 채워졌을 가능성). 실패해도 warming 결과 그대로 반환.
  if (data && data.warming) {
    await new Promise((r) => setTimeout(r, 2500));
    try {
      const r2 = await fetchWithTimeout(`${base}/health?quick=1`, opts, 15000);
      const d2 = await r2.json();
      if (d2 && !d2.warming) return d2;
    } catch {}
  }
  return data;
}

let _warmingRetries = 0;
const _WARMING_MAX_RETRIES = 20; // 4s × 20 = 80s. /health cold start의 최악(8 컬렉션 × 10s) 커버.

function applyHealth(data) {
  if (data.ok && data.warming) {
    // 백엔드 cold start — 캐시 채워지는 중. 코퍼스는 안 그리고, 잠시 후 자동 재시도.
    setStatus("연결 OK · 인덱스 로딩 중…", "info");
    els.corpus.hidden = true;
    if (_warmingRetries < _WARMING_MAX_RETRIES) {
      _warmingRetries++;
      setTimeout(() => { checkHealth().catch(() => {}); }, 4000);
    } else {
      setStatus("연결 OK · 인덱스 로딩이 지연되고 있습니다. 잠시 후 새로고침해 주세요.", "warn");
    }
  } else if (data.ok && data.collection_count > 0) {
    _warmingRetries = 0;
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

let lastCorpusData = null; // 마지막 health 응답 캐시 (정렬 변경 시 재렌더용)

function renderCorpus(perCollection, total) {
  lastCorpusData = { perCollection, total };
  const entries = Object.entries(perCollection || {}).filter(([, n]) => n > 0);
  if (!entries.length) {
    els.corpus.hidden = true;
    return;
  }
  const mode = els.corpusSort?.value || "default";
  const koOf = (name) => COLLECTION_META[name]?.ko || name;
  switch (mode) {
    case "most":
      entries.sort(([, a], [, b]) => b - a);
      break;
    case "least":
      entries.sort(([, a], [, b]) => a - b);
      break;
    case "alpha":
      entries.sort(([a], [b]) => koOf(a).localeCompare(koOf(b), "ko"));
      break;
    case "default":
    default:
      // 법령 위계순 (미리 정의된 순서)
      entries.sort(([a], [b]) => {
        const ia = COLLECTION_ORDER.indexOf(a);
        const ib = COLLECTION_ORDER.indexOf(b);
        if (ia === -1 && ib === -1) return a.localeCompare(b);
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        return ia - ib;
      });
      break;
  }
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
      autosizeQuestion();
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

// 인도네시아 법령 참조 패턴 강조 (분류별 컬러)
const LAW_REF_PATTERNS = [
  // UUD 1945, UUD NRI 1945
  { re: /\bUUD(?:\s+NRI)?\s+\d{4}\b/g, cls: "cat-uud" },
  // UU 13/2003, UU No.13/2003, UU Nomor 13 Tahun 2003
  { re: /\bUU(?:\s+(?:No\.?|Nomor))?\s*\d+(?:\s+Tahun\s+\d{4}|\/\d{4})\b/g, cls: "cat-uu" },
  // PP 5/2021, PP No. 5/2021
  { re: /\bPP(?:\s+(?:No\.?|Nomor))?\s*\d+(?:\s+Tahun\s+\d{4}|\/\d{4})\b/g, cls: "cat-pp" },
  { re: /\bPerpres(?:\s+(?:No\.?|Nomor))?\s*\d+(?:\s+Tahun\s+\d{4}|\/\d{4})\b/gi, cls: "cat-perpres" },
  { re: /\bPermen[a-zA-Z]*\s*(?:No\.?\s*)?\d+(?:\s+Tahun\s+\d{4}|\/\d{4})\b/gi, cls: "cat-permen" },
  { re: /\bKepmen[a-zA-Z]*\s*(?:No\.?\s*)?\d+(?:\s+Tahun\s+\d{4}|\/\d{4})\b/gi, cls: "cat-kepmen" },
  { re: /\bPerda(?:\s+(?:No\.?|Nomor))?\s*\d+(?:\s+Tahun\s+\d{4}|\/\d{4})\b/gi, cls: "cat-perda" },
  // Pasal 6, Pasal 6A, Pasal 6 ayat (2)
  { re: /\bPasal\s+\d+[A-Z]?(?:\s+ayat\s+\(\d+\))?\b/g, cls: "law-art" },
];

function renderInline(line) {
  // 먼저 escape, 그 다음 안전한 인라인 마크다운만 변환.
  let out = escapeHtml(line);
  // **bold**
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // *italic*
  out = out.replace(/(?<![*\w])\*([^*\n]+?)\*(?!\w)/g, '<em>$1</em>');
  // `inline code`
  out = out.replace(/`([^`]+)`/g, '<code class="ic">$1</code>');
  // 인도네시아 법령 패턴 자동 강조 (카테고리별 컬러)
  for (const { re, cls } of LAW_REF_PATTERNS) {
    out = out.replace(re, (m) => `<span class="law-ref ${cls}">${m}</span>`);
  }
  // 외부 URL 자동 링크화
  out = out.replace(/(?<![">])(https?:\/\/[^\s<)]+[^\s<.,;:!?)])/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer" class="a-link">$1</a>');
  // [1], [2] 출처 인덱스 → ref 칩
  out = out.replace(/\[(\d{1,2})\]/g, '<a href="#" class="src-ref" data-idx="$1">[$1]</a>');
  // (Pasal X, 출처: ...) → 인용 칩 (이미 law-ref로 감싸진 Pasal 텍스트가 () 안에 있어도 무방)
  out = out.replace(CITE_RE, '<span class="cite">$1</span>');
  return out;
}

function estimateReadingTime(text) {
  // 한국어/인도네시아어 혼합 본문. 대충 분당 350자/200단어 기준으로 보수적 추정.
  const t = String(text || "");
  const chars = t.replace(/\s/g, "").length;
  const minutes = Math.max(1, Math.round(chars / 350));
  return { chars, minutes };
}

function isTableSeparator(line) {
  // 마크다운 테이블 구분선: |---|---| 또는 | :--- | :---: | ---: |
  if (!/^\s*\|/.test(line)) return false;
  const inner = line.replace(/^\s*\||\|\s*$/g, "");
  const cells = inner.split("|").map((s) => s.trim());
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
}
function parseTableRow(line) {
  return line.replace(/^\s*\||\|\s*$/g, "").split("|").map((s) => s.trim());
}

function renderAnswer(answer) {
  // Claude 출력 마크다운 일부 렌더링: 볼드/이탤릭/인라인코드/리스트/헤딩/blockquote/테이블/단락.
  const lines = String(answer || "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let buf = [];
  let mode = null; // "ul" | "ol" | "bq" | null
  let tableRows = []; // 테이블 누적 (헤더 + 데이터 행)

  const flushPara = () => {
    if (buf.length) {
      blocks.push(`<p>${buf.map(renderInline).join("<br>")}</p>`);
      buf = [];
    }
  };
  const flushList = () => {
    if (buf.length) {
      const tag = mode;
      const items = buf.map((t) => `<li>${renderInline(t)}</li>`).join("");
      blocks.push(`<${tag}>${items}</${tag}>`);
    }
    buf = [];
  };
  const flushBq = () => {
    if (buf.length) {
      blocks.push(`<blockquote class="a-bq">${buf.map(renderInline).join("<br>")}</blockquote>`);
    }
    buf = [];
  };
  const flushTable = () => {
    if (tableRows.length >= 2) {
      const [header, ...dataRows] = tableRows;
      const thead = `<thead><tr>${header.map((c) => `<th>${renderInline(c)}</th>`).join("")}</tr></thead>`;
      const tbody = dataRows.length
        ? `<tbody>${dataRows.map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join("")}</tr>`).join("")}</tbody>`
        : "";
      blocks.push(`<div class="a-table-wrap"><table class="a-table">${thead}${tbody}</table></div>`);
    } else if (tableRows.length === 1) {
      // separator 없으면 그냥 단락으로 떨어뜨림
      buf.push(tableRows[0].join(" | "));
      flushPara();
    }
    tableRows = [];
  };
  const flush = () => {
    if (mode === "ul" || mode === "ol") flushList();
    else if (mode === "bq") flushBq();
    else if (mode === "table") flushTable();
    else flushPara();
    mode = null;
  };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trimEnd();
    if (!line.trim()) {
      flush();
      continue;
    }
    const ulM = line.match(/^\s*[-*]\s+(.+)$/);
    const olM = line.match(/^\s*\d+\.\s+(.+)$/);
    const hM = line.match(/^\s*(#{1,3})\s+(.+)$/);
    const bqM = line.match(/^\s*>\s?(.*)$/);
    const isPipeRow = /^\s*\|.*\|\s*$/.test(line);

    if (hM) {
      flush();
      const level = Math.min(hM[1].length + 2, 4);
      blocks.push(`<h${level} class="a-h">${renderInline(hM[2])}</h${level}>`);
      continue;
    }
    // 테이블: 헤더 행 + 다음 줄이 separator일 때만 테이블 진입
    if (isPipeRow && mode !== "table") {
      const next = (lines[i + 1] || "").trimEnd();
      if (isTableSeparator(next)) {
        flush();
        mode = "table";
        tableRows.push(parseTableRow(line));
        i++; // separator 건너뜀
        continue;
      }
    }
    if (mode === "table") {
      if (isPipeRow) {
        tableRows.push(parseTableRow(line));
        continue;
      } else {
        flush();
        // fall through to 일반 처리
      }
    }
    if (bqM) {
      if (mode !== "bq") flush();
      mode = "bq";
      buf.push(bqM[1]);
      continue;
    }
    if (ulM) {
      if (mode !== "ul") flush();
      mode = "ul";
      buf.push(ulM[1]);
      continue;
    }
    if (olM) {
      if (mode !== "ol") flush();
      mode = "ol";
      buf.push(olM[1]);
      continue;
    }
    // 일반 단락 라인
    if (mode === "ul" || mode === "ol" || mode === "bq" || mode === "table") flush();
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
    <li class="src-card ${hueCls}" data-cat="${escapeHtml(catKo)}" data-idx="${i + 1}">
      <header class="src-top">
        <span class="src-idx">#${i + 1}</span>
        ${catKo ? `<span class="src-cat">${escapeHtml(catKo)}</span>` : ""}
        <span class="src-score-wrap" title="유사도 ${score.toFixed(3)}">
          <span class="src-score-bar"><span style="width:${scorePct}%"></span></span>
          <span class="src-score-num">${score.toFixed(2)}</span>
        </span>
        <button type="button" class="src-copy" title="이 출처 정보 복사" aria-label="이 출처 정보 복사">📋</button>
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

function buildSourceFilters(sources) {
  // 출처가 2개 이상의 카테고리에 분포할 때만 필터 칩 노출.
  const cats = new Map(); // catKo → count
  for (const s of sources) {
    const k = categoryKo(s.category) || "기타";
    cats.set(k, (cats.get(k) || 0) + 1);
  }
  if (cats.size < 2) return "";
  const chips = [...cats.entries()]
    .sort(([, a], [, b]) => b - a)
    .map(([k, n]) => {
      const cls = categoryHueClass(k);
      return `<button type="button" class="src-filter ${cls}" data-cat="${escapeHtml(k)}" aria-pressed="true">${escapeHtml(k)} <span class="src-filter-n">${n}</span></button>`;
    })
    .join("");
  return `<div class="src-filters">${chips}</div>`;
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
  const { q, a, sources = [], critique = null, scope = [], elapsedMs, ts, id } = item;
  const wrap = document.createElement("article");
  wrap.className = "qa";
  wrap.dataset.id = id;
  const srcHtml = sources.map(renderSource).join("");

  const scopeText = scope.length
    ? scope.map((c) => COLLECTION_META[c]?.ko || c).join(", ")
    : "전체 법령";
  const elapsed = elapsedMs ? `${(elapsedMs / 1000).toFixed(1)}s` : "";

  // 출처 카테고리 분포: "출처 5건 · [법률]3 [헌법]1 [정부령]1"
  const catCounts = new Map();
  for (const s of sources) {
    const k = categoryKo(s.category) || "기타";
    catCounts.set(k, (catCounts.get(k) || 0) + 1);
  }
  const breakdownHtml = catCounts.size
    ? [...catCounts.entries()]
        .sort(([, a], [, b]) => b - a)
        .map(([k, n]) => {
          const cls = categoryHueClass(k);
          return `<span class="src-mini ${cls}" title="${escapeHtml(k)} ${n}건">${escapeHtml(k)} ${n}</span>`;
        })
        .join("")
    : "";
  const sourcesMeta = `<span class="qa-meta-item">🔎 출처 ${sources.length}건${breakdownHtml ? ` <span class="src-breakdown">${breakdownHtml}</span>` : ""}</span>`;
  const reading = estimateReadingTime(a);
  const readingItem = reading.chars > 50
    ? `<span class="qa-meta-item" title="본문 ${reading.chars}자 · 예상 읽기 시간">📖 ${reading.minutes}분 (${formatCount(reading.chars)}자)</span>`
    : "";
  const metaBits = [
    `<span class="qa-meta-item">📂 ${escapeHtml(scopeText)}</span>`,
    sourcesMeta,
    readingItem,
    elapsed ? `<span class="qa-meta-item">⏱ ${elapsed}</span>` : "",
    ts ? `<span class="qa-meta-item qa-meta-ts" data-ts="${ts}">🕘 ${escapeHtml(formatRelativeTime(ts))}</span>` : "",
  ].filter(Boolean).join("");

  const isLong = String(a || "").length >= LONG_ANSWER_CHARS;
  const collapseBtn = isLong
    ? `<button class="qa-collapse" type="button" aria-pressed="false" aria-label="답변 접기/펼치기" title="긴 답변 접기/펼치기">▴ 접기</button>`
    : "";
  const starredCls = item.starred ? "is-starred" : "";
  // Phase 5: critique 정보 (신뢰도 뱃지 + 이슈 리스트)
  let critiqueHtml = "";
  if (critique && typeof critique === "object") {
    const conf = critique.confidence || "unknown";
    const confClass = { high: "ok", medium: "warn", low: "err", unknown: "info" }[conf] || "info";
    const confLabel = { high: "✓ 신뢰도 높음", medium: "⚠ 부분 미흡", low: "⚠ 검증 필요", unknown: "검증 미실행" }[conf] || conf;
    const issues = Array.isArray(critique.issues) ? critique.issues : [];
    const verifiedCount = critique.verified_citations_count || 0;
    const summary = critique.summary || "";
    const issuesHtml = issues.length
      ? `<ul class="critique-issues">${issues.map(i => `<li><span class="ci-type">${escapeHtml(i.type || "")}</span> ${escapeHtml(i.description || "")}</li>`).join("")}</ul>`
      : "";
    critiqueHtml = `
      <details class="critique critique-${confClass}" ${issues.length ? "open" : ""}>
        <summary class="critique-summary">${escapeHtml(confLabel)} · 인용 검증 ${verifiedCount}건${issues.length ? ` · 이슈 ${issues.length}건` : ""}${summary ? " — " + escapeHtml(summary) : ""}</summary>
        ${issuesHtml}
      </details>`;
  }
  wrap.innerHTML = `
    <header class="qa-head">
      <h3 class="q" title="클릭 → 입력창에 다시 채우기">Q. ${escapeHtml(q)}</h3>
      <div class="qa-actions">
        <button class="qa-star ${starredCls}" type="button" aria-label="즐겨찾기" aria-pressed="${item.starred ? "true" : "false"}" title="즐겨찾기에 ${item.starred ? "해제" : "추가"}">${item.starred ? "★" : "☆"}</button>
        ${collapseBtn}
        <button class="qa-copy" type="button" aria-label="답변 복사" title="답변을 클립보드에 복사">📋 복사</button>
        <button class="qa-del" type="button" aria-label="이 질문 삭제" title="삭제">✕</button>
      </div>
    </header>
    <div class="qa-meta">${metaBits}</div>
    <div class="a">${renderAnswer(a)}</div>
    ${critiqueHtml}
    <details class="sources" ${sources.length ? "open" : ""}>
      <summary>🔎 검색된 출처 ${sources.length}건</summary>
      ${buildSourceFilters(sources)}
      <ul class="src-list">${srcHtml}</ul>
    </details>
  `;

  // 답변 본문 자동 TOC: 헤딩이 2개 이상이면 .a 맨 위에 목차 삽입
  const headingNodes = wrap.querySelectorAll(".a .a-h");
  if (headingNodes.length >= 2) {
    const aDiv = wrap.querySelector(".a");
    const toc = document.createElement("nav");
    toc.className = "a-toc";
    toc.setAttribute("aria-label", "답변 목차");
    const tocTitle = document.createElement("div");
    tocTitle.className = "a-toc-title";
    tocTitle.textContent = `📑 목차 (${headingNodes.length}개 섹션)`;
    toc.appendChild(tocTitle);
    const ol = document.createElement("ol");
    ol.className = "a-toc-list";
    headingNodes.forEach((h, i) => {
      const aid = `${item.id}-h${i}`;
      h.id = aid;
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = `#${aid}`;
      link.textContent = h.textContent;
      link.addEventListener("click", (e) => {
        e.preventDefault();
        h.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      li.appendChild(link);
      ol.appendChild(li);
    });
    toc.appendChild(ol);
    aDiv.insertBefore(toc, aDiv.firstChild);
  }

  // 인용 칩 인터랙션: 클릭 → 점프, 호버 → 미리보기 툴팁
  const findSourceForCite = (cite) => {
    const text = cite.textContent || "";
    const filenameMatch = text.match(/출처:\s*([^,)]+)/);
    const fname = filenameMatch ? filenameMatch[1].trim() : "";
    if (!fname) return null;
    const cards = wrap.querySelectorAll(".src-card");
    for (const card of cards) {
      const name = card.querySelector(".src-name")?.title || card.querySelector(".src-name")?.textContent || "";
      if (name && (name.includes(fname) || fname.includes(name.replace(/…/, "")))) {
        return card;
      }
    }
    return null;
  };

  wrap.querySelectorAll(".cite").forEach((cite) => {
    cite.style.cursor = "pointer";
    cite.title = "클릭 → 매칭 출처로 이동, 호버 → 미리보기";
    cite.addEventListener("click", () => {
      const target = findSourceForCite(cite);
      if (!target) return;
      const details = wrap.querySelector("details.sources");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.remove("pulse");
      void target.offsetWidth;
      target.classList.add("pulse");
    });
    cite.addEventListener("mouseenter", () => showCiteTip(cite, findSourceForCite(cite)));
    cite.addEventListener("mouseleave", hideCiteTip);
    cite.addEventListener("focus", () => showCiteTip(cite, findSourceForCite(cite)));
    cite.addEventListener("blur", hideCiteTip);
  });

  // 삭제 버튼
  wrap.querySelector(".qa-del")?.addEventListener("click", () => {
    deleteHistoryItem(id);
  });

  // Q 클릭 → 입력창에 다시 채우기
  wrap.querySelector(".q")?.addEventListener("click", () => {
    els.question.value = q;
    els.question.focus();
    autosizeQuestion();
    els.question.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // 답변 안의 출처 인덱스 [N] → 같은 카드의 출처 #N으로 점프 + 펄스
  wrap.querySelectorAll("a.src-ref").forEach((ref) => {
    ref.addEventListener("click", (e) => {
      e.preventDefault();
      const idx = ref.dataset.idx;
      const target = wrap.querySelector(`.src-card[data-idx="${CSS.escape(idx)}"]`);
      if (!target) return;
      const details = wrap.querySelector("details.sources");
      if (details && !details.open) details.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.remove("pulse");
      void target.offsetWidth;
      target.classList.add("pulse");
    });
  });

  // 개별 출처 복사 버튼
  wrap.querySelectorAll(".src-copy").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const card = btn.closest(".src-card");
      if (!card) return;
      const idx = card.dataset.idx;
      const i = Number(idx) - 1;
      const src = sources[i];
      if (!src) return;
      const cat = categoryKo(src.category) || "";
      const text = `[${cat}] ${src.source} · p.${src.page} · ${src.article || "조항 미확인"} (유사도 ${(src.score || 0).toFixed(2)})`;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "✅";
        setTimeout(() => { btn.textContent = "📋"; }, 1200);
      } catch {
        btn.textContent = "❌";
        setTimeout(() => { btn.textContent = "📋"; }, 1200);
      }
    });
  });

  // 즐겨찾기 토글
  const starBtn = wrap.querySelector(".qa-star");
  if (starBtn) {
    starBtn.addEventListener("click", () => {
      const idx = historyItems.findIndex((x) => x.id === item.id);
      if (idx === -1) return;
      historyItems[idx].starred = !historyItems[idx].starred;
      item.starred = historyItems[idx].starred;
      saveHistoryItems(historyItems);
      starBtn.textContent = item.starred ? "★" : "☆";
      starBtn.classList.toggle("is-starred", !!item.starred);
      starBtn.setAttribute("aria-pressed", item.starred ? "true" : "false");
      starBtn.title = `즐겨찾기에 ${item.starred ? "해제" : "추가"}`;
      applyHistoryFilter(); // 필터가 즐겨찾기만 모드면 즉시 반영
      updateHistoryToolbar();
    });
  }

  // 접기/펼치기 버튼
  const collapseBtnEl = wrap.querySelector(".qa-collapse");
  if (collapseBtnEl) {
    collapseBtnEl.addEventListener("click", () => {
      const collapsed = wrap.classList.toggle("qa-collapsed");
      collapseBtnEl.setAttribute("aria-pressed", collapsed ? "true" : "false");
      collapseBtnEl.textContent = collapsed ? "▾ 펼치기" : "▴ 접기";
    });
  }

  // 출처 카테고리 필터 칩 (Q&A별)
  const filterBtns = wrap.querySelectorAll(".src-filter");
  if (filterBtns.length) {
    const applyFilter = () => {
      const enabled = new Set(
        [...filterBtns]
          .filter((b) => b.getAttribute("aria-pressed") !== "false")
          .map((b) => b.dataset.cat)
      );
      wrap.querySelectorAll(".src-card").forEach((card) => {
        const c = card.dataset.cat || "";
        card.hidden = enabled.size === 0 ? false : !enabled.has(c);
      });
    };
    filterBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const cur = btn.getAttribute("aria-pressed") !== "false";
        btn.setAttribute("aria-pressed", cur ? "false" : "true");
        applyFilter();
      });
    });
  }

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

// Phase 7: 현재 대화 세션 id. null이면 새 대화 시작, 값 있으면 follow-up.
let currentConversationId = null;
let currentConversationTurn = 0;

function resetConversation() {
  currentConversationId = null;
  currentConversationTurn = 0;
  updateConversationBadge();
  setStatus("새 대화를 시작합니다", "info");
}

function updateConversationBadge() {
  let badge = document.getElementById("conv-badge");
  if (!badge) {
    // 헤더의 askBtn 근처에 추가
    const host = els.askBtn?.parentElement;
    if (!host) return;
    badge = document.createElement("button");
    badge.id = "conv-badge";
    badge.type = "button";
    badge.className = "conv-badge";
    badge.title = "현재 대화 세션. 클릭 시 새 대화 시작.";
    badge.addEventListener("click", resetConversation);
    host.appendChild(badge);
  }
  if (!currentConversationId) {
    badge.textContent = "🆕 새 대화";
    badge.dataset.active = "false";
  } else {
    badge.textContent = `💬 대화 ${currentConversationId.slice(0, 6)} · turn ${currentConversationTurn}`;
    badge.dataset.active = "true";
  }
}

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

function sortedHistoryView() {
  const mode = els.historySort?.value || "latest";
  const arr = [...historyItems];
  switch (mode) {
    case "oldest":  arr.sort((a, b) => (a.ts || 0) - (b.ts || 0)); break;
    case "fastest": arr.sort((a, b) => (a.elapsedMs || Infinity) - (b.elapsedMs || Infinity)); break;
    case "slowest": arr.sort((a, b) => (b.elapsedMs || 0) - (a.elapsedMs || 0)); break;
    case "latest":
    default: arr.sort((a, b) => (b.ts || 0) - (a.ts || 0)); break;
  }
  return arr;
}

function renderHistoryAll() {
  els.history.innerHTML = "";
  if (!historyItems.length) {
    renderHistoryEmpty();
    updateHistoryToolbar();
    return;
  }
  for (const item of sortedHistoryView()) {
    els.history.appendChild(buildQaCard(item));
  }
  applyHistoryFilter();
  updateHistoryToolbar();
}

function addHistoryItem(item) {
  historyItems.unshift(item);
  if (historyItems.length > HISTORY_LIMIT) historyItems.length = HISTORY_LIMIT;
  saveHistoryItems(historyItems);
  const sortMode = els.historySort?.value || "latest";
  let newCard;
  if (sortMode === "latest") {
    if (els.history.querySelector(".empty-state")) els.history.innerHTML = "";
    newCard = buildQaCard(item);
    els.history.prepend(newCard);
  } else {
    renderHistoryAll();
    newCard = els.history.querySelector(`[data-id="${CSS.escape(item.id)}"]`);
  }
  // 새 카드 도착 펄스 (1회)
  if (newCard) {
    newCard.classList.add("qa-fresh");
    setTimeout(() => newCard.classList.remove("qa-fresh"), 1400);
  }
  applyHistoryFilter();
  updateHistoryToolbar();
}

function deleteHistoryItem(id) {
  historyItems = historyItems.filter((x) => x.id !== id);
  saveHistoryItems(historyItems);
  const card = els.history.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (card) card.remove();
  if (!historyItems.length) renderHistoryEmpty();
  applyHistoryFilter();
  updateHistoryToolbar();
}

function clearAllHistory() {
  historyItems = [];
  saveHistoryItems(historyItems);
  renderHistoryEmpty();
  if (els.historySearch) els.historySearch.value = "";
  updateHistoryToolbar();
}

function updateHistoryToolbar() {
  if (!els.historyToolbar) return;
  if (historyItems.length === 0) {
    els.historyToolbar.hidden = true;
    return;
  }
  els.historyToolbar.hidden = false;
  const q = (els.historySearch?.value || "").trim();
  if (!q) {
    els.historyCount.textContent = `총 ${historyItems.length}개 저장됨`;
  } else {
    const visible = els.history.querySelectorAll(".qa:not([hidden])").length;
    els.historyCount.textContent = `${visible} / ${historyItems.length}개 일치`;
  }
}

function escapeRegex(s) {
  return String(s || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function clearSearchHighlights(rootEl) {
  rootEl.querySelectorAll("mark.search-mark").forEach((mark) => {
    const text = document.createTextNode(mark.textContent);
    mark.parentNode.replaceChild(text, mark);
  });
  rootEl.normalize();
}

function highlightTextIn(rootEl, query) {
  if (!query) return;
  const re = new RegExp(escapeRegex(query), "gi");
  // 검색 대상 영역만 (스니펫은 너무 무거우니 제외, q + a + src-name + src-article만)
  const targets = rootEl.querySelectorAll(".q, .a, .src-name, .src-article");
  for (const t of targets) {
    const walker = document.createTreeWalker(t, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => {
        if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
        // mark 안에 다시 매칭되지 않도록
        if (n.parentNode && n.parentNode.nodeName === "MARK") return NodeFilter.FILTER_REJECT;
        return re.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) nodes.push(n);
    for (const node of nodes) {
      const frag = document.createDocumentFragment();
      const text = node.nodeValue;
      let lastIdx = 0;
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(text)) !== null) {
        if (m.index > lastIdx) frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
        const mark = document.createElement("mark");
        mark.className = "search-mark";
        mark.textContent = m[0];
        frag.appendChild(mark);
        lastIdx = m.index + m[0].length;
        if (m.index === re.lastIndex) re.lastIndex++; // 빈 매칭 무한루프 방지
      }
      if (lastIdx < text.length) frag.appendChild(document.createTextNode(text.slice(lastIdx)));
      node.parentNode.replaceChild(frag, node);
    }
  }
}

function applyHistoryFilter() {
  if (!els.historySearch) return;
  const q = (els.historySearch.value || "").trim();
  const qLower = q.toLowerCase();
  const starredOnly = els.starredOnlyBtn?.getAttribute("aria-pressed") === "true";
  const cards = els.history.querySelectorAll(".qa");

  // 빈 검색결과 placeholder 제거 (뒤에서 필요시 다시 삽입)
  els.history.querySelectorAll(".no-match").forEach((n) => n.remove());

  // 모든 카드의 기존 하이라이트 먼저 제거
  cards.forEach((c) => clearSearchHighlights(c));

  // ID로 historyItems 매핑 → starred 여부 확인용
  const starredIds = new Set(historyItems.filter((x) => x.starred).map((x) => x.id));

  let visibleCount = 0;
  cards.forEach((card) => {
    const id = card.dataset.id;
    let visible = true;
    if (starredOnly && !starredIds.has(id)) visible = false;
    if (visible && qLower) {
      const text = card.textContent.toLowerCase();
      if (!text.includes(qLower)) visible = false;
    }
    card.hidden = !visible;
    if (visible && qLower) {
      highlightTextIn(card, q);
    }
    if (visible) visibleCount++;
  });
  if (visibleCount === 0 && cards.length > 0) {
    const note = document.createElement("div");
    note.className = "no-match";
    const reason = starredOnly && qLower
      ? `즐겨찾기 + "${escapeHtml(q)}" 조건에 해당하는 항목이 없습니다`
      : starredOnly
        ? `즐겨찾기로 표시한 항목이 없습니다`
        : `"${escapeHtml(q)}" 와 일치하는 항목이 없습니다`;
    note.innerHTML = `
      <div class="empty-icon">🔍</div>
      <p class="empty-title">${reason}</p>
      <p class="empty-sub">필터/검색어를 조정해 보세요.</p>`;
    els.history.appendChild(note);
  }
  updateHistoryToolbar();
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
      <span class="qa-meta-item qa-progress">⏳ <span class="qa-progress-stage">벡터 검색 중…</span></span>
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

// 응답 대기 동안 단계 메시지를 점진적으로 갱신 (실제 백엔드는 streaming 안 함 — UX feedback용)
function startProgressStages(skeleton) {
  const stageEl = skeleton.querySelector(".qa-progress-stage");
  if (!stageEl) return () => {};
  // 시간대별 메시지: 초기 검색, 그 다음 LLM 생성, 그 다음 길어지면 안내
  const stages = [
    { at: 0,    text: "벡터 검색 중…" },
    { at: 2000, text: "관련 조항 정리 중…" },
    { at: 3500, text: "Claude가 답변 생성 중…" },
    { at: 15000, text: "Claude가 답변 생성 중… (긴 답변일 수 있어요)" },
    { at: 30000, text: "응답 지연 — 잠시만 기다려주세요…" },
  ];
  const timers = [];
  for (const s of stages.slice(1)) {
    timers.push(setTimeout(() => { stageEl.textContent = s.text; }, s.at));
  }
  return () => timers.forEach((t) => clearTimeout(t));
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

// SSE 클라이언트 — /query/stream consume.
// callbacks: { sources, token, done, error, conversation, intent, critique, tool_call, ... }
async function postQueryStream(base, q, topK, callbacks, options = {}) {
  const body = { question: q, top_k: topK };
  if (selectedCategories.size) body.categories = [...selectedCategories];
  if (options.conversationId) body.conversation_id = options.conversationId;
  const ctrl = new AbortController();
  const timeoutId = setTimeout(() => ctrl.abort(), 300000); // 5분 안전망
  try {
    const resp = await fetch(`${base}/query/stream`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`HTTP ${resp.status} ${text.slice(0, 200)}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE event 단위 split. event 사이 \n\n.
      const events = buffer.split("\n\n");
      buffer = events.pop(); // 마지막 미완 이벤트는 buffer로 유지
      for (const ev of events) {
        if (!ev.trim()) continue;
        let eventName = null;
        let dataStr = "";
        for (const line of ev.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
        }
        if (!eventName || !dataStr) continue;
        let parsed;
        try {
          parsed = JSON.parse(dataStr);
        } catch (e) {
          console.warn("SSE parse fail", e, dataStr.slice(0, 100));
          continue;
        }
        const handler = callbacks[eventName];
        if (handler) handler(parsed);
      }
    }
  } finally {
    clearTimeout(timeoutId);
  }
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
  const scopeLabel = selectedCategories.size
    ? [...selectedCategories].map((n) => COLLECTION_META[n]?.ko || n).join(", ")
    : "전체 법령";
  setStatus(`Claude가 ${scopeLabel} 문서를 검토하는 중…`, "info");

  const t0 = performance.now();
  const scope = [...selectedCategories];

  // 빈 상태 제거하고 스켈레톤 prepend + 단계 메시지 시작
  if (els.history.querySelector(".empty-state")) els.history.innerHTML = "";
  const skeleton = makeSkeletonCard(q, scope);
  els.history.prepend(skeleton);
  const stopStages = startProgressStages(skeleton);

  // streaming UI 상태
  let partialAnswer = "";
  let receivedSources = [];
  let renderRaf = 0;
  const answerEl = skeleton.querySelector(".a");
  const stageEl = skeleton.querySelector(".qa-progress-stage");
  const flushAnswer = () => {
    if (renderRaf) return;
    renderRaf = requestAnimationFrame(() => {
      answerEl.innerHTML = renderAnswer(partialAnswer) + '<span class="streaming-cursor">▌</span>';
      renderRaf = 0;
    });
  };

  let receivedCritique = null;
  let receivedConversation = null;
  const streamCallbacks = {
    conversation: (data) => {
      receivedConversation = data;
      currentConversationId = data?.conversation_id || currentConversationId;
      currentConversationTurn = data?.turn || currentConversationTurn;
      updateConversationBadge();
      if (stageEl && data?.is_followup) {
        stageEl.textContent = `이전 대화 이어가기 (turn ${data.turn})… 벡터 검색 중`;
      }
    },
    sources: (data) => {
      receivedSources = data.sources || [];
      stopStages();
      if (stageEl) stageEl.textContent = `Claude가 답변 작성 중… (출처 ${receivedSources.length}개 확보)`;
      // 스켈레톤 라인 제거 — 답변 영역을 빈 상태 + cursor로
      answerEl.innerHTML = '<span class="streaming-cursor">▌</span>';
    },
    token: (data) => {
      partialAnswer += data.text || "";
      flushAnswer();
    },
    critique: (data) => {
      // Phase 5: 답변 끝난 후 self-critique 결과
      receivedCritique = data;
      if (stageEl) {
        const conf = data?.confidence || "unknown";
        const map = { high: "✓ 신뢰도 높음", medium: "⚠ 부분 미흡", low: "⚠ 검증 필요", unknown: "검증 미실행" };
        const txt = map[conf] || conf;
        const issueCount = (data?.issues || []).length;
        stageEl.textContent = `검증: ${txt}${issueCount ? ` (이슈 ${issueCount})` : ""}`;
      }
    },
    verifier: (data) => {
      // Phase 5b: deterministic citation verifier (정규식+metadata 매칭). LLM critique과 별도.
      // critique 객체에 합쳐 history에 같이 저장.
      if (data && receivedCritique) receivedCritique.verifier = data;
      else if (data) receivedCritique = { verifier: data };
      if (stageEl) {
        const tot = data?.total || 0;
        const unv = data?.unverified || 0;
        if (tot > 0) {
          const cur = stageEl.textContent || "";
          stageEl.textContent = `${cur} · 인용검증 ${tot - unv}/${tot}${unv ? ` (미검증 ${unv})` : ""}`;
        }
      }
    },
    regenerating: (data) => {
      // Phase 5b: critique이 hallucination/bad_citation/low confidence를 감지 →
      // 서버가 답변을 다시 생성하기 시작. 답변 영역을 리셋하고 stage 메시지로 알림.
      if (renderRaf) cancelAnimationFrame(renderRaf);
      partialAnswer = "";
      answerEl.innerHTML = '<span class="streaming-cursor">▌</span>';
      const reason = data?.reason || "검수 결과 재작성";
      if (stageEl) stageEl.textContent = `답변 재작성 중… (${reason})`;
      // critique은 retry 직후 새 critique 이벤트로 다시 받음.
      receivedCritique = null;
    },
    done: (data) => {
      if (renderRaf) cancelAnimationFrame(renderRaf);
      answerEl.innerHTML = renderAnswer(partialAnswer);
    },
    error: (data) => {
      throw new Error(data.message || "stream error");
    },
  };

  const runStream = async () => {
    await postQueryStream(base, q, topK, streamCallbacks, {
      conversationId: currentConversationId,
    });
  };

  try {
    try {
      await runStream();
    } catch (e) {
      // cloudflared URL이 바뀐 경우: tunnel.json refetch 후 한 번 재시도
      const newAuto = await fetchAutoUrl();
      if (newAuto && newAuto !== base) {
        els.apiUrl.value = newAuto;
        localStorage.removeItem(LS_KEYS.url);
        setStatus(`URL 자동 갱신 (${newAuto}) 재시도 중…`, "info");
        base = newAuto;
        // 누적 상태 초기화 후 재시도
        partialAnswer = "";
        receivedSources = [];
        answerEl.innerHTML = `
          <div class="skeleton-line" style="width: 96%"></div>
          <div class="skeleton-line" style="width: 88%"></div>
          <div class="skeleton-line" style="width: 70%"></div>
          <div class="skeleton-line" style="width: 92%"></div>
          <div class="skeleton-line" style="width: 60%"></div>`;
        await runStream();
      } else {
        throw e;
      }
    }
    const elapsedMs = performance.now() - t0;
    stopStages();
    skeleton.remove();
    addHistoryItem({
      id: `qa_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      q,
      a: partialAnswer,
      sources: receivedSources,
      critique: receivedCritique,
      scope,
      elapsedMs,
      ts: Date.now(),
    });
    els.question.value = "";
    autosizeQuestion();
    clearDraft();
    setStatus("답변 생성 완료", "ok");
  } catch (e) {
    stopStages();
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

els.saveBtn?.addEventListener("click", saveSettings);
els.healthBtn?.addEventListener("click", checkHealth);
els.askBtn.addEventListener("click", askQuestion);
// Phase 7: 페이지 로드 시 대화 뱃지 초기 표시
updateConversationBadge();
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

// 질문창 자동 높이 조절 (입력 길이에 따라 늘어나도록, 최대 12줄)
function autosizeQuestion() {
  els.question.style.height = "auto";
  const max = 12 * 22;
  const next = Math.min(els.question.scrollHeight, max);
  els.question.style.height = `${next}px`;
}

// 입력 텍스트 임시 저장 (장문 작성 중 새로고침해도 보존)
let draftDebounce = 0;
function saveDraft() {
  try {
    const v = els.question.value;
    if (v) localStorage.setItem(LS_KEYS.draft, v);
    else localStorage.removeItem(LS_KEYS.draft);
  } catch {}
}
function clearDraft() {
  try { localStorage.removeItem(LS_KEYS.draft); } catch {}
}
function restoreDraft() {
  try {
    const v = localStorage.getItem(LS_KEYS.draft) || "";
    if (v && !els.question.value) {
      els.question.value = v;
      autosizeQuestion();
    }
  } catch {}
}

els.question.addEventListener("input", () => {
  autosizeQuestion();
  clearTimeout(draftDebounce);
  draftDebounce = setTimeout(saveDraft, 400);
});

function computeUsageStats() {
  if (!historyItems.length) return null;
  const n = historyItems.length;
  const catCounts = new Map();      // 인용된 출처 카테고리 카운트
  const askedCats = new Map();      // 사용자가 범위로 지정한 카테고리 카운트
  let totalMs = 0, validMs = 0, minMs = Infinity, maxMs = 0;
  let firstTs = Infinity, lastTs = 0;
  let totalChars = 0;
  for (const item of historyItems) {
    if (item.ts) { firstTs = Math.min(firstTs, item.ts); lastTs = Math.max(lastTs, item.ts); }
    if (item.elapsedMs > 0) {
      totalMs += item.elapsedMs;
      validMs++;
      minMs = Math.min(minMs, item.elapsedMs);
      maxMs = Math.max(maxMs, item.elapsedMs);
    }
    totalChars += String(item.a || "").length;
    for (const s of (item.sources || [])) {
      const k = categoryKo(s.category) || "기타";
      catCounts.set(k, (catCounts.get(k) || 0) + 1);
    }
    for (const c of (item.scope || [])) {
      const k = COLLECTION_META[c]?.ko || c;
      askedCats.set(k, (askedCats.get(k) || 0) + 1);
    }
  }
  const avgMs = validMs ? totalMs / validMs : 0;
  const topCited = [...catCounts.entries()].sort(([, a], [, b]) => b - a)[0];
  const topAsked = [...askedCats.entries()].sort(([, a], [, b]) => b - a)[0];
  return { n, avgMs, minMs: minMs === Infinity ? 0 : minMs, maxMs, firstTs, lastTs, totalChars, topCited, topAsked, catCounts };
}

function fmtMs(ms) { return `${(ms / 1000).toFixed(1)}s`; }
function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function renderUsageStats() {
  if (!els.statsSection || !els.statsGrid) return;
  const st = computeUsageStats();
  if (!st) { els.statsSection.hidden = true; return; }
  els.statsSection.hidden = false;
  const topCitedHtml = st.topCited
    ? `<span class="${categoryHueClass(st.topCited[0])} stat-pill">${escapeHtml(st.topCited[0])} ${st.topCited[1]}건</span>`
    : "—";
  const topAskedHtml = st.topAsked
    ? `<span class="stat-pill">${escapeHtml(st.topAsked[0])}</span>`
    : `<span class="stat-pill">전체 검색</span>`;
  const periodHtml = (st.firstTs !== Infinity)
    ? (fmtDate(st.firstTs) === fmtDate(st.lastTs)
        ? `${fmtDate(st.firstTs)}`
        : `${fmtDate(st.firstTs)} ~ ${fmtDate(st.lastTs)}`)
    : "—";

  els.statsGrid.innerHTML = `
    <div class="stat-cell"><div class="stat-label">저장된 Q&A</div><div class="stat-val">${st.n}<span class="stat-suffix">건</span></div></div>
    <div class="stat-cell"><div class="stat-label">평균 응답</div><div class="stat-val">${fmtMs(st.avgMs)}</div><div class="stat-sub">최단 ${fmtMs(st.minMs)} · 최장 ${fmtMs(st.maxMs)}</div></div>
    <div class="stat-cell"><div class="stat-label">답변 총 분량</div><div class="stat-val">${formatCount(st.totalChars)}<span class="stat-suffix">자</span></div></div>
    <div class="stat-cell"><div class="stat-label">사용 기간</div><div class="stat-val stat-val-sm">${escapeHtml(periodHtml)}</div></div>
    <div class="stat-cell"><div class="stat-label">가장 자주 인용된 분류</div><div class="stat-val stat-val-sm">${topCitedHtml}</div></div>
    <div class="stat-cell"><div class="stat-label">가장 많이 범위 지정한 분류</div><div class="stat-val stat-val-sm">${topAskedHtml}</div></div>
  `;

  // 카테고리 인용 분포 미니 차트 — stat-grid 아래에 별도 행으로 삽입
  const totalCites = [...st.catCounts.values()].reduce((a, b) => a + b, 0);
  if (totalCites > 0) {
    const chartRows = [...st.catCounts.entries()]
      .sort(([, a], [, b]) => b - a)
      .map(([k, n]) => {
        const pct = Math.max(1, Math.round((n / totalCites) * 100));
        const cls = categoryHueClass(k);
        return `<div class="cite-bar-row ${cls}">
          <span class="cite-bar-name">${escapeHtml(k)}</span>
          <span class="cite-bar-track"><span class="cite-bar-fill" style="width:${pct}%"></span></span>
          <span class="cite-bar-num">${n}<span class="cite-bar-pct"> (${pct}%)</span></span>
        </div>`;
      })
      .join("");
    const chartHtml = `<div class="cite-chart">
      <div class="cite-chart-head">인용된 분류 분포</div>
      ${chartRows}
    </div>`;
    els.statsGrid.insertAdjacentHTML("afterend", chartHtml);
    // 이미 존재하면 제거 — afterend는 매번 새로 추가하니까 이전 차트 제거
    const allCharts = els.statsSection.querySelectorAll(".cite-chart");
    if (allCharts.length > 1) {
      for (let i = 0; i < allCharts.length - 1; i++) allCharts[i].remove();
    }
  }
}

// 본문 폰트 사이즈 (0=작게, 1=기본, 2=크게)
const FONT_SCALES = ["sm", "md", "lg"];
function applyFontSize(idx) {
  const i = Math.max(0, Math.min(FONT_SCALES.length - 1, idx));
  document.documentElement.setAttribute("data-font", FONT_SCALES[i]);
  try { localStorage.setItem(LS_KEYS.fontSize, String(i)); } catch {}
  if (els.fontMinus) els.fontMinus.disabled = i === 0;
  if (els.fontPlus) els.fontPlus.disabled = i === FONT_SCALES.length - 1;
}
function currentFontIdx() {
  const cur = document.documentElement.getAttribute("data-font");
  const i = FONT_SCALES.indexOf(cur);
  return i === -1 ? 1 : i;
}
els.fontMinus?.addEventListener("click", () => applyFontSize(currentFontIdx() - 1));
els.fontPlus?.addEventListener("click", () => applyFontSize(currentFontIdx() + 1));

// 키보드 단축키: Ctrl/Cmd 와 함께 + / - 로도 조절 (브라우저 zoom 충돌 피해 단독 키도 옵션으로 제공)
document.addEventListener("keydown", (e) => {
  const isInput = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (isInput || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "+" || e.key === "=") {
    applyFontSize(currentFontIdx() + 1);
  } else if (e.key === "-" || e.key === "_") {
    applyFontSize(currentFontIdx() - 1);
  }
});

// 도움말 모달
function openHelp() {
  if (!els.helpModal) return;
  if (els.legendGrid && !els.legendGrid.dataset.filled) {
    els.legendGrid.innerHTML = COLLECTION_ORDER.map((name) => {
      const meta = COLLECTION_META[name];
      const cls = categoryHueClass(name);
      return `<div class="legend-item ${cls}">
        <span class="legend-dot"></span>
        <span class="legend-name">${escapeHtml(meta.ko)}</span>
        <span class="legend-abbr">${escapeHtml(meta.abbr)}</span>
      </div>`;
    }).join("");
    els.legendGrid.dataset.filled = "1";
  }
  renderUsageStats();
  els.helpModal.hidden = false;
  document.body.classList.add("modal-open");
}
function closeHelp() {
  if (!els.helpModal) return;
  els.helpModal.hidden = true;
  document.body.classList.remove("modal-open");
}
els.helpToggle?.addEventListener("click", () => {
  if (els.helpModal.hidden) openHelp(); else closeHelp();
});
els.helpModal?.querySelectorAll("[data-close]").forEach((el) => {
  el.addEventListener("click", closeHelp);
});
// '?' 키로 도움말 열기/닫기, Esc로 닫기
document.addEventListener("keydown", (e) => {
  if (e.key === "?" && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
    e.preventDefault();
    if (els.helpModal && els.helpModal.hidden) openHelp(); else closeHelp();
  }
  if (e.key === "Escape" && els.helpModal && !els.helpModal.hidden) {
    closeHelp();
  }
});

// 인용 칩 호버 시 떠오르는 툴팁 (전역 단일 요소)
function showCiteTip(cite, srcCard) {
  if (!els.citeTip || !srcCard) return;
  const meta = srcCard.querySelector(".src-name")?.title || srcCard.querySelector(".src-name")?.textContent || "";
  const article = srcCard.querySelector(".src-article")?.textContent || "";
  const page = srcCard.querySelector(".src-page")?.textContent || "";
  const cat = srcCard.querySelector(".src-cat")?.textContent || "";
  const snippet = srcCard.querySelector(".src-snippet")?.textContent || "";
  const tipMeta = els.citeTip.querySelector(".cite-tip-meta");
  const tipSnip = els.citeTip.querySelector(".cite-tip-snippet");
  if (tipMeta) {
    tipMeta.innerHTML = `${cat ? `<span class="cite-tip-cat">${escapeHtml(cat)}</span>` : ""}
      <span class="cite-tip-name" title="${escapeHtml(meta)}">${escapeHtml(meta)}</span>
      <span class="cite-tip-loc">${escapeHtml(page)} · ${escapeHtml(article)}</span>`;
  }
  if (tipSnip) tipSnip.textContent = snippet;

  // 카드의 카테고리 컬러 변수도 복사 (카테고리별 보더 컬러)
  const styles = getComputedStyle(srcCard);
  els.citeTip.style.setProperty("--cat-color", styles.getPropertyValue("--cat-color"));

  els.citeTip.hidden = false;
  // 위치 계산: cite 아래 + 화면 안에 들어가도록 클램프
  const rect = cite.getBoundingClientRect();
  const tipRect = els.citeTip.getBoundingClientRect();
  const margin = 8;
  const vw = window.innerWidth;
  let left = rect.left + window.scrollX;
  let top = rect.bottom + window.scrollY + 6;
  if (left + tipRect.width > vw - margin) left = vw - tipRect.width - margin;
  if (left < margin) left = margin;
  els.citeTip.style.left = `${Math.max(margin, left)}px`;
  els.citeTip.style.top = `${top}px`;
}
function hideCiteTip() {
  if (els.citeTip) els.citeTip.hidden = true;
}

// 위로 스크롤 부동 버튼
function updateScrollTopVisibility() {
  if (!els.scrollTop) return;
  els.scrollTop.hidden = window.scrollY < 400;
}
window.addEventListener("scroll", updateScrollTopVisibility, { passive: true });
els.scrollTop?.addEventListener("click", () => {
  window.scrollTo({ top: 0, behavior: "smooth" });
});
// 't' 단축키: 위로 스크롤 (입력창 포커스 시는 제외)
document.addEventListener("keydown", (e) => {
  if (e.key.toLowerCase() === "t" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
});

// 테마 토글: 다크(기본) ↔ 라이트, localStorage에 저장
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light") {
    root.setAttribute("data-theme", "light");
    if (els.themeToggle) {
      els.themeToggle.textContent = "☀️";
      els.themeToggle.title = "다크 모드로 전환";
    }
  } else {
    root.removeAttribute("data-theme");
    if (els.themeToggle) {
      els.themeToggle.textContent = "🌙";
      els.themeToggle.title = "라이트 모드로 전환";
    }
  }
}
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  const next = current === "light" ? "dark" : "light";
  applyTheme(next);
  try { localStorage.setItem(LS_KEYS.theme, next); } catch {}
}
els.themeToggle?.addEventListener("click", toggleTheme);

// Markdown 내보내기: 현재 (검색 필터로) 표시되는 항목들만 .md 파일로 다운로드.
function exportHistoryAsMarkdown() {
  const q = (els.historySearch?.value || "").trim().toLowerCase();
  const matchesQuery = (item) => {
    if (!q) return true;
    const blob = `${item.q}\n${item.a}\n${(item.sources || []).map((s) => `${s.source} ${s.article}`).join(" ")}`;
    return blob.toLowerCase().includes(q);
  };
  const items = historyItems.filter(matchesQuery);
  if (!items.length) {
    setStatus("내보낼 항목이 없습니다", "warn");
    return;
  }
  const lines = [];
  lines.push(`# 인도네시아 법령 Q&A — ${items.length}개 질의응답`);
  lines.push("");
  lines.push(`*생성일: ${new Date().toLocaleString("ko-KR")}*`);
  if (q) lines.push(`*검색 필터: "${q}"*`);
  lines.push("");
  lines.push("---");
  lines.push("");

  items.forEach((item, idx) => {
    const ts = item.ts ? new Date(item.ts).toLocaleString("ko-KR") : "";
    const scope = (item.scope && item.scope.length)
      ? item.scope.map((c) => COLLECTION_META[c]?.ko || c).join(", ")
      : "전체 법령";
    lines.push(`## ${idx + 1}. ${item.q}`);
    lines.push("");
    lines.push(`> 📂 ${scope}  ·  🔎 출처 ${(item.sources || []).length}건${ts ? `  ·  🕘 ${ts}` : ""}${item.elapsedMs ? `  ·  ⏱ ${(item.elapsedMs / 1000).toFixed(1)}s` : ""}`);
    lines.push("");
    lines.push(item.a || "");
    lines.push("");
    if (item.sources && item.sources.length) {
      lines.push("### 출처");
      lines.push("");
      item.sources.forEach((s, i) => {
        const cat = categoryKo(s.category) || "";
        const article = s.article || "조항 미확인";
        const score = (s.score || 0).toFixed(2);
        lines.push(`${i + 1}. **${cat}** · \`${s.source}\` · p.${s.page} · ${article} (유사도 ${score})`);
        if (s.snippet) {
          lines.push("");
          lines.push("   > " + s.snippet.replace(/\n/g, "\n   > "));
        }
        lines.push("");
      });
    }
    lines.push("---");
    lines.push("");
  });

  const md = lines.join("\n");
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-");
  a.href = url;
  a.download = `indonesia-law-qa_${stamp}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  setStatus(`${items.length}개 항목을 Markdown으로 내보냈습니다`, "ok");
}
els.exportBtn?.addEventListener("click", exportHistoryAsMarkdown);

// 코퍼스 전체 / 해제 버튼
els.corpusSelectAll?.addEventListener("click", () => {
  const cards = els.corpusGrid.querySelectorAll(".corpus-card");
  cards.forEach((card) => {
    const name = card.dataset.col;
    if (name) {
      selectedCategories.add(name);
      card.setAttribute("aria-pressed", "true");
    }
  });
  updateScopeIndicator();
});
els.corpusSort?.addEventListener("change", () => {
  try { localStorage.setItem(LS_KEYS.corpusSort, els.corpusSort.value); } catch {}
  if (lastCorpusData) renderCorpus(lastCorpusData.perCollection, lastCorpusData.total);
});
els.corpusSelectNone?.addEventListener("click", () => {
  selectedCategories.clear();
  els.corpusGrid.querySelectorAll(".corpus-card").forEach((card) => {
    card.setAttribute("aria-pressed", "false");
  });
  updateScopeIndicator();
});

// 히스토리 검색 — 250ms 디바운싱 (TreeWalker 하이라이트 비용 줄임)
let searchDebounce = 0;
els.historySearch?.addEventListener("input", () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => applyHistoryFilter(), 250);
});

// 정렬 변경 → 전체 재렌더 (sort는 latest/oldest/elapsed 기반, 데이터 변경 없음)
els.historySort?.addEventListener("change", () => {
  if (!historyItems.length) return;
  renderHistoryAll();
});

// 즐겨찾기만 토글
els.starredOnlyBtn?.addEventListener("click", () => {
  const cur = els.starredOnlyBtn.getAttribute("aria-pressed") === "true";
  els.starredOnlyBtn.setAttribute("aria-pressed", cur ? "false" : "true");
  applyHistoryFilter();
});

// 페이지 로드 시 테마 → 폰트 사이즈 → 코퍼스 정렬 → 히스토리 → 예시 → 헬스체크.
(function initTheme() {
  let saved = "";
  try { saved = localStorage.getItem(LS_KEYS.theme) || ""; } catch {}
  if (!saved && window.matchMedia) {
    saved = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  applyTheme(saved || "dark");
})();
(function initFontSize() {
  let saved = 1;
  try {
    const v = parseInt(localStorage.getItem(LS_KEYS.fontSize) || "", 10);
    if (!isNaN(v)) saved = v;
  } catch {}
  applyFontSize(saved);
})();
(function initCorpusSort() {
  let saved = "default";
  try { saved = localStorage.getItem(LS_KEYS.corpusSort) || "default"; } catch {}
  if (els.corpusSort) els.corpusSort.value = saved;
})();

historyItems = loadHistoryItems();
renderHistoryAll();
renderExamples(); // 초기 chip 렌더 (헬스체크 응답 전에도 보이도록)
restoreDraft();
loadSettings().then(() => checkHealth());

// 히스토리에 표시된 상대시간(NN분 전)을 1분마다 갱신.
setInterval(() => {
  els.history.querySelectorAll(".qa-meta-ts").forEach((el) => {
    const ts = Number(el.dataset.ts);
    if (ts) el.textContent = `🕘 ${formatRelativeTime(ts)}`;
  });
}, 60_000);
