"use strict";

const LS_KEYS = {
  url: "rag.apiUrl",
  token: "rag.apiToken",
  topK: "rag.topK",
};

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
};

function loadSettings() {
  const cfg = window.APP_CONFIG || {};
  els.apiUrl.value = localStorage.getItem(LS_KEYS.url) || cfg.defaultApiUrl || "";
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

async function checkHealth() {
  const base = apiBase();
  if (!base) {
    setStatus("백엔드 URL을 먼저 입력하세요", "err");
    return;
  }
  try {
    setStatus("연결 확인 중…", "info");
    const r = await fetch(`${base}/health`, { headers: authHeaders() });
    const data = await r.json();
    if (data.ok && data.collection_count > 0) {
      setStatus(`연결 OK · 청크 ${data.collection_count}개 로드됨`, "ok");
    } else if (data.ok) {
      setStatus("연결 OK · DB 비어있음 (ingest.py 실행 필요)", "warn");
    } else {
      setStatus(`서버 오류: ${data.error || "unknown"}`, "err");
    }
  } catch (e) {
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

function renderAnswer(answer) {
  // 줄바꿈만 보존 (간단 마크다운). 본문은 신뢰 가능한 LLM 출력이지만 안전하게 escape 후 <br>.
  return escapeHtml(answer).replaceAll("\n", "<br>");
}

function renderItem(q, a, sources) {
  const wrap = document.createElement("article");
  wrap.className = "qa";
  const srcHtml = (sources || [])
    .map((s, i) => {
      const article = s.article || "조항 미확인";
      const score = (s.score || 0).toFixed(3);
      return `
        <li>
          <div class="src-meta">
            <span class="src-idx">[${i + 1}]</span>
            <span class="src-name">${escapeHtml(s.source)}</span>
            <span class="src-page">p.${s.page}</span>
            <span class="src-article">${escapeHtml(article)}</span>
            <span class="src-score">유사도 ${score}</span>
          </div>
          <pre class="src-snippet">${escapeHtml(s.snippet)}</pre>
        </li>`;
    })
    .join("");

  wrap.innerHTML = `
    <h3 class="q">Q. ${escapeHtml(q)}</h3>
    <div class="a">${renderAnswer(a)}</div>
    <details class="sources">
      <summary>🔎 검색된 출처 ${sources?.length || 0}건</summary>
      <ul>${srcHtml}</ul>
    </details>
  `;
  els.history.prepend(wrap);
}

async function askQuestion() {
  const q = els.question.value.trim();
  if (!q) return;
  const base = apiBase();
  if (!base) {
    setStatus("백엔드 URL을 먼저 입력하세요", "err");
    return;
  }

  els.askBtn.disabled = true;
  els.askBtn.textContent = "답변 생성 중…";
  setStatus("Claude가 헌법 문서를 검토하는 중…", "info");

  try {
    const r = await fetch(`${base}/query`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        question: q,
        top_k: parseInt(els.topK.value || "5", 10),
      }),
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`HTTP ${r.status} ${text.slice(0, 200)}`);
    }
    const data = await r.json();
    renderItem(q, data.answer, data.sources || []);
    els.question.value = "";
    setStatus("답변 생성 완료", "ok");
  } catch (e) {
    setStatus(`요청 실패: ${e.message}`, "err");
  } finally {
    els.askBtn.disabled = false;
    els.askBtn.textContent = "질문하기";
  }
}

document.querySelectorAll(".chip").forEach((btn) => {
  btn.addEventListener("click", () => {
    els.question.value = btn.dataset.q || "";
    els.question.focus();
  });
});

els.saveBtn.addEventListener("click", saveSettings);
els.healthBtn.addEventListener("click", checkHealth);
els.askBtn.addEventListener("click", askQuestion);
els.clearBtn.addEventListener("click", () => {
  els.history.innerHTML = "";
});
els.question.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    askQuestion();
  }
});

loadSettings();
checkHealth();
