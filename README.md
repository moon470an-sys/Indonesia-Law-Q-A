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

### 2-3. ChromaDB 서버 (별도 데몬, 권장)

기본값 `RAG_CHROMA_MODE=http`에서는 `rag_server`와 ingest가 **별도 ChromaDB 서버 프로세스**에 접속합니다. 그래야 `rag_server` 재시작이 10GB 인덱스 reload 없이 즉시 끝납니다.

수동으로 띄울 때:
```powershell
.\.venv\Scripts\chroma.exe run --path D:\rag_data\chroma_db --host 127.0.0.1 --port 8001
```

**Windows 서비스로 영구 등록 (권장)** — `NSSM`을 사용합니다.

```powershell
winget install --id NSSM.NSSM            # NSSM 1회 설치
.\scripts\install_chroma_service.bat     # 관리자 cmd/PowerShell 에서
nssm start ChromaDB-IndonesiaLaw         # 즉시 시작
sc query ChromaDB-IndonesiaLaw           # 상태 확인
```

서비스 등록 후엔 PC 로그온/재부팅 시 자동 시작되고, 죽으면 5초 후 자동 재시작됩니다. 로그는 `logs\chroma_service.log` / `.err`.

`persistent` 모드로 되돌리려면 `.env`에서 `RAG_CHROMA_MODE=persistent`로 변경하고 `rag_server`를 재시작.

### 2-4. 헌법 PDF 인덱싱

대상 폴더: `D:\인도네시아 법령 원문\헌법` (이미 존재)

```powershell
python ingest.py
```

처음 실행 시 임베딩 모델(약 1GB)이 `D:\hf_cache`에 다운로드됩니다.

### 2-5. FastAPI 서버 실행

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

## 8. 부팅/로그온 시 자동 시작 (Windows Task Scheduler)

PC를 켜면 백엔드와 터널이 자동으로 뜨고, 데스크톱 바로가기 클릭 한 번으로 페이지가 백엔드 URL이 채워진 채 열리도록 만듭니다.

### 등록 (1회만)

PowerShell에서:
```powershell
cd "C:\Users\yoonseok.moon\OneDrive - (주) ST International\Projects\인도네시아 법령 RAG"
.\auto_start\register_task.ps1
```

### 즉시 1회 테스트

```powershell
Start-ScheduledTask -TaskName "Indonesia Law RAG"
# 약 30초~2분 뒤 데스크톱에 "인도네시아 헌법 Q&A.url" 바로가기가 생기거나 갱신됩니다
Get-Content "logs\start.log" -Tail 20
```

### 동작 흐름

1. 로그온 → 30초 지연 후 `start_rag.ps1` 실행 (창 안 보임)
2. `uvicorn` 기동 → `/health` 정상 확인
3. `cloudflared tunnel --url http://127.0.0.1:8000` 기동 → 터널 URL 추출
4. `logs/` 폴더에 모든 stdout/err 기록
5. 데스크톱에 `인도네시아 헌법 Q&A.url` 바로가기를 새 URL로 갱신
6. 사용자: 바로가기 클릭 → `?api=...` 파라미터로 백엔드 URL 자동 입력 → 토큰 1회 입력하면 끝

### 중지 / 해제

```powershell
# 임시 중지 (프로세스만 종료, 다음 로그온 시 다시 시작됨)
.\auto_start\stop_rag.ps1

# 자동 시작 완전 해제
.\auto_start\unregister_task.ps1
```

### 로그 위치

| 파일 | 내용 |
|---|---|
| `logs\start.log` | 자동 시작 스크립트 자체 로그 |
| `logs\uvicorn.log` / `.err` | FastAPI 서버 출력 |
| `logs\cloudflared.log` / `.err` | 터널 출력 (URL은 .err 파일에 있음) |
| `.tunnel_url` | 가장 최근 터널 URL (디버깅용) |

---

## 9. 다음 단계 (확장 시)

- 헌법 외 다른 법령(UU/PP/Perpres) 폴더 추가 → `RAG_SOURCE_DIR` 환경변수만 바꿔서 `ingest.py` 재실행
- 컬렉션을 법령 종류별로 분리 (`indonesia_constitution`, `indonesia_uu`, …)
- 재순위 모델(reranker) 추가로 검색 품질 향상
- Cloudflare Access로 이메일 인증 추가 (본인만 접근)

---

## 10. ChromaDB 마이그레이션 (PersistentClient → HttpClient)

`rag_server.py`와 ingest 스크립트가 같은 프로세스 안에서 ChromaDB(10GB)를 직접 매핑하던 구조에서, 별도 데몬으로 분리합니다. **uvicorn 재시작 시 데이터 reload를 없애는** 게 목적입니다 (재시작 ~30분 → 수 초).

### 절차

1. `.venv`에 chromadb가 설치돼 있는지 확인 (`.venv\Scripts\chroma.exe` 존재).
2. NSSM 설치 후 ChromaDB 서비스 등록:
   ```powershell
   winget install --id NSSM.NSSM
   .\scripts\install_chroma_service.bat   # 관리자 권한
   nssm start ChromaDB-IndonesiaLaw
   ```
3. `chroma run`은 기존 `D:\rag_data\chroma_db\` 디렉터리를 그대로 읽으므로 **재인덱싱 불필요**.
4. `.env`를 다음으로 갱신:
   ```
   RAG_CHROMA_MODE=http
   RAG_CHROMA_HOST=127.0.0.1
   RAG_CHROMA_PORT=8001
   ```
5. 컬렉션 8개가 그대로 보이는지 확인 (예: PowerShell):
   ```powershell
   curl http://127.0.0.1:8001/api/v1/collections   # ChromaDB v0.4.x
   # 또는
   curl http://127.0.0.1:8001/api/v2/collections   # v0.5+
   ```
   응답에 `indonesia_uud / indonesia_uu / indonesia_pp / indonesia_perpres / indonesia_permen / indonesia_kepmen / indonesia_perda / indonesia_lainnya` 8개가 나오면 정상.
6. `rag_server`를 재시작 (`Restart-Service` / watchdog의 uvicorn 재기동). 이제 uvicorn은 ChromaDB를 메모리에 매핑하지 않으므로 **수 초 안에 healthy**.

### 롤백

문제 발생 시 `.env`에서 한 줄만 바꾸고 `rag_server` 재시작:
```
RAG_CHROMA_MODE=persistent
```
기존 PersistentClient 경로가 fallback으로 살아 있으므로 즉시 원복.

### 알려진 호환 이슈

- ChromaDB 클라이언트 버전과 서버(`chroma run`) 버전이 다르면 컬렉션 포맷 mismatch 가능. `rag_server` 시작 로그에 `client_ver=… server_ver=…`이 찍히고 다르면 경고. 같은 `.venv`의 `chroma.exe`로 서버를 띄우면 항상 일치.

---

## 11. Health endpoint 분리 (Stage 2 변경)

`rag_server.py`는 두 종류의 health 엔드포인트를 노출합니다.

| Endpoint | 의미 | 사용처 | 응답 |
|---|---|---|---|
| `/health/live` (`/healthz` alias) | 프로세스 살아있음. 의존성 체크 없음. 즉시 200. | watchdog, cloudflared keep-alive | `{"ok":true,"alive":true}` |
| `/health/ready` | SentenceTransformer + ChromaDB 워밍업 완료 여부. | 프록시/프론트엔드의 라우팅 결정 | 준비 완료: 200 / 준비 중: 503 (body에 `error`, `warmup_started_ts`) |
| `/health` | 컬렉션별 청크 수 등 사람용 데이터 응답 (캐시 24h). | 프론트엔드 UI | `{"ok":true,"collection_count":...}` |

### 워밍업 동작

- uvicorn 부팅 직후 FastAPI `startup` 이벤트가 `asyncio.to_thread`로 워밍업을 시작.
- 워밍업 = SentenceTransformer 1회 encode + 모든 `indonesia_*` 컬렉션에 peek.
- 완료까지 평균 30~120초. 그 동안 `/health/live`는 200, `/health/ready`는 503.

### watchdog 설정 가이드

`auto_start/watchdog.ps1`의 `Test-UvicornHealth`는 **반드시 `/health/live`만** polling 해야 합니다. `/health/ready`를 polling하면 워밍업 중에 watchdog가 서버를 죽이고 재시작하는 cycle이 다시 생깁니다.

`Test-UvicornHealth` 내부:
```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8000/health/live" -TimeoutSec 5
```
(또는 호환을 위해 `/healthz` 유지 가능 — 둘은 같은 핸들러.)

응급 처치로 늘려둔 "5회 연속 실패해야 재시작" 임계값은 이제 **2회로 원복 가능**합니다. /health/live는 ChromaDB 접근 없이 즉답하므로 정상이라면 빠르게 응답합니다.

### 롤백

`/health/live`, `/health/ready`는 추가만 됐고 기존 `/healthz`는 그대로이므로 Stage 2 단독 롤백은 워밍업이 부담될 때만 필요. 그 경우 `_on_startup`을 빼고 `_state["ready"] = True`로 초기화하면 readiness가 항상 200.

---

## 12. 설정 핫리로드 (Stage 3 변경)

토큰 검증 같은 정책은 `.env`만 고치고 admin 엔드포인트 한 번 치면 적용됩니다. 더 이상 "토큰 한 줄 바꾸려고 10GB 다시 로딩"하지 않습니다.

### 핫리로드 대상

| 환경변수 | 의미 | 핫리로드 |
|---|---|---|
| `CLIENT_API_TOKEN` | 단일 토큰 (호환 유지) | ✅ |
| `RAG_TOKENS` | 콤마 분리 다중 토큰 | ✅ |
| `RAG_REQUIRE_TOKEN` | 검증 on/off 명시 토글 | ✅ |
| `RAG_ADMIN_KEY` | admin 엔드포인트 보호 키 | ❌ (재시작 필요) |
| `ANTHROPIC_API_KEY`, `RAG_CHROMA_*`, `ALLOWED_ORIGINS`, 모델 경로 | 인프라성 | ❌ (재시작 필요) |

### 사용법

`.env`에서 정책값 수정 후:
```powershell
curl -Method POST -Headers @{"X-Admin-Key"="${env:RAG_ADMIN_KEY}"} `
  http://127.0.0.1:8000/admin/reload-config
```
응답:
```json
{
  "reloaded": ["require_token", "tokens"],
  "current": { "require_token": true, "token_count": 2 }
}
```

요청마다 `require_token`이 `_config` dict를 lookup하므로 다음 요청부터 즉시 새 정책 적용.

### 보안 모델

- `RAG_ADMIN_KEY`가 비어 있으면 admin 엔드포인트는 항상 503. 실수로 무방비 노출되지 않게.
- `RAG_ADMIN_KEY`는 충분히 긴 무작위 문자열 권장 (`CLIENT_API_TOKEN`과 동일 PowerShell 명령).
- admin 호출은 가능하면 localhost에서만 (cloudflared 터널 통해서 노출하지 말 것).

### 동시성

`_config` 갱신과 require_token 검증은 모두 `threading.Lock`으로 보호. dict snapshot을 잡은 뒤 비교만 하므로 lock 보유 시간은 마이크로초 단위.
