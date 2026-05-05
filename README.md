# 인도네시아 헌법 RAG (GitHub Pages + 로컬 백엔드)

GitHub Pages에 배포된 프론트엔드(`https://moon470an-sys.github.io/Indonesia-Law-Q-A/`)에서 질문을 입력하면, **본인 PC에서 돌아가는 로컬 RAG 백엔드**가 인도네시아 헌법 PDF를 근거로 Claude에 답변을 생성하게 합니다. DB(ChromaDB)와 PDF는 로컬에만 보관됩니다.

```
[브라우저]  ──HTTPS──▶  [GitHub Pages: frontend/]
                              │ fetch
                              ▼
                    [Cloudflare Tunnel]
                              │
                              ▼
        [로컬 PC] FastAPI (rag_server.py)
                  ├─ ChromaDB (D:\rag_data\chroma_db)
                  └─ Anthropic Claude API
```

테스트 범위(1단계): `D:\인도네시아 법령 원문\헌법` 폴더의 PDF만 인덱싱.

---

## 0. 보안 — 가장 먼저 할 일

채팅창/공유 문서/저장소에 한 번이라도 노출된 API 키는 즉시 폐기합니다.

1. https://console.anthropic.com/settings/keys 접속
2. 노출된 키 옆 메뉴 → **Revoke**
3. **Create Key** 로 새 키 발급 (이 키는 어디에도 붙여 넣지 말고 다음 단계의 `.env`에만 입력)
4. `.gitignore`로 `.env`는 깃 추적에서 제외되어 있으므로 절대 커밋되지 않습니다

---

## 1. 프로젝트 구조

```
Indonesia-Law-Q-A/                 ← GitHub 저장소 루트
├─ frontend/                       ← GitHub Pages에 배포 (정적)
│  ├─ index.html
│  ├─ app.js
│  ├─ style.css
│  └─ config.js
├─ backend는 같은 저장소의 루트 파이썬 파일들 (서버에 푸시할 필요 없음)
├─ ingest.py                       ← PDF → ChromaDB 임베딩
├─ rag_server.py                   ← FastAPI (CORS + 토큰 인증)
├─ app.py                          ← (옵션) Streamlit 로컬 UI
├─ requirements.txt
├─ .env.example                    ← 키 템플릿
├─ .env                            ← (직접 생성, 깃 제외)
├─ .gitignore
└─ .github/workflows/pages.yml     ← frontend → Pages 자동 배포
```

---

## 2. 로컬 백엔드 준비 (Windows + PowerShell)

### 2-1. 의존성 설치

```powershell
cd "C:\Users\yoonseok.moon\OneDrive - (주) ST International\Projects\인도네시아 법령 RAG"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2-2. `.env` 생성

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` 안에 새로 발급받은 키와 토큰을 채웁니다:

```
ANTHROPIC_API_KEY=sk-ant-새로_발급받은_키
CLIENT_API_TOKEN=직접_만든_긴_랜덤_문자열
ALLOWED_ORIGINS=https://moon470an-sys.github.io,http://localhost:8501,http://127.0.0.1:8501
```

랜덤 토큰을 만드는 PowerShell 한 줄:
```powershell
[Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Max 256 } | ForEach-Object { [byte]$_ }))
```

### 2-3. 헌법 PDF 인덱싱

대상 폴더: `D:\인도네시아 법령 원문\헌법` (이미 존재)

```powershell
python ingest.py
```

처음 실행 시 임베딩 모델(약 1GB)이 `D:\hf_cache`에 다운로드됩니다.

### 2-4. FastAPI 서버 실행

```powershell
uvicorn rag_server:app --host 127.0.0.1 --port 8000
```

확인: 다른 창에서
```powershell
curl http://127.0.0.1:8000/health
```
→ `{"ok": true, "collection_count": 숫자, ...}`

---

## 3. Cloudflare Tunnel — 로컬 PC를 인터넷에 노출 (무료)

GitHub Pages(브라우저)는 `127.0.0.1`에 직접 접근할 수 없으므로 터널이 필요합니다. Cloudflare Tunnel은 무료이고 ngrok보다 안정적입니다.

### 3-1. cloudflared 설치

```powershell
winget install --id Cloudflare.cloudflared
```
(또는 https://github.com/cloudflare/cloudflared/releases 에서 `.msi` 다운로드)

### 3-2. 빠른 테스트용 임시 터널 (인증 없이 즉시 사용)

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

콘솔에 `https://random-name.trycloudflare.com` 형태의 URL이 출력됩니다. 이 URL이 백엔드의 공개 주소입니다. (창을 닫으면 URL이 사라집니다.)

### 3-3. (권장) 영구 고정 도메인 — Cloudflare 계정 + 도메인 필요

```powershell
cloudflared tunnel login
cloudflared tunnel create indonesia-law-rag
cloudflared tunnel route dns indonesia-law-rag rag.본인도메인.com
cloudflared tunnel run --url http://127.0.0.1:8000 indonesia-law-rag
```

---

## 4. GitHub 저장소 → GitHub Pages 배포

### 4-1. 저장소 초기 푸시 (한 번만)

```powershell
cd "C:\Users\yoonseok.moon\OneDrive - (주) ST International\Projects\인도네시아 법령 RAG"
git init
git add .
git status   # ← .env가 목록에 없는지 반드시 확인!
git commit -m "Initial commit: Indonesia Constitution RAG"
git branch -M main
git remote add origin https://github.com/moon470an-sys/Indonesia-Law-Q-A.git
git push -u origin main
```

### 4-2. GitHub Pages 활성화 (저장소에서 1회 설정)

1. https://github.com/moon470an-sys/Indonesia-Law-Q-A → **Settings → Pages**
2. **Source**: `GitHub Actions` 선택
3. `frontend/` 변경이 푸시될 때마다 `.github/workflows/pages.yml` 이 자동 배포

배포 완료 후 접속 URL:
```
https://moon470an-sys.github.io/Indonesia-Law-Q-A/
```

---

## 5. 사용 방법

1. 위 URL을 브라우저로 엽니다
2. 우상단 ⚙️ **백엔드 연결 설정**을 펼칩니다
3. **백엔드 URL**: 3-2에서 받은 `https://...trycloudflare.com` (또는 본인 도메인)
4. **클라이언트 토큰**: `.env`의 `CLIENT_API_TOKEN`과 동일한 값
5. **설정 저장** → **연결 테스트** (상단 상태가 "연결 OK · 청크 N개"가 되면 정상)
6. 질문 입력 후 **질문하기** 또는 `Ctrl+Enter`

URL/토큰은 브라우저 localStorage에만 저장되며, 다시 열어도 유지됩니다.

---

## 6. 흐름도

```
사용자
  ↓ 질문
GitHub Pages JS
  ↓ POST /query  (X-Api-Token 헤더)
Cloudflare Tunnel
  ↓
로컬 FastAPI
  ├─ 임베딩(질문) → ChromaDB Top-K 검색
  ├─ Claude API에 [헌법 청크 + 질문] 전달
  └─ 답변 + 출처 반환
  ↑
GitHub Pages JS  ←  답변 본문 + 인용 조항(Pasal 6A 등) 표시
```

---

## 7. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 프론트에서 "연결 실패" | `cloudflared` 가 떠 있는지, 백엔드 URL이 정확한지 확인. 브라우저 콘솔(F12)에서 CORS 에러 확인 |
| `401 invalid token` | 프론트의 토큰 입력값 ≠ `.env`의 `CLIENT_API_TOKEN`. 둘을 동일하게 맞춤 |
| `ChromaDB 디렉토리가 없습니다` | `python ingest.py`를 먼저 실행 |
| `ANTHROPIC_API_KEY가 설정되지 않았습니다` | `.env`가 프로젝트 루트에 있고 키가 채워졌는지 확인 |
| 빈 답변 / 관련 없는 답변 | 헌법 PDF 외 자료 부족 (현재 1단계 한계). 청크 크기·Top-K 조정 |
| `git push` 시 `.env`가 보임 | 즉시 `git rm --cached .env && git commit && git push` 로 제거하고 키 즉시 폐기·재발급 |

---

## 8. 다음 단계 (확장 시)

- 헌법 외 다른 법령(UU/PP/Perpres) 폴더 추가 → `RAG_SOURCE_DIR` 환경변수만 바꿔서 `ingest.py` 재실행
- 컬렉션을 법령 종류별로 분리 (`indonesia_constitution`, `indonesia_uu`, …)
- 재순위 모델(reranker) 추가로 검색 품질 향상
- Cloudflare Access로 이메일 인증 추가 (본인만 접근)
