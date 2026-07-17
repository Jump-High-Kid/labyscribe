# labyscribe v2 설계 — 로컬 웹 UI 에디션

- 작성일: 2026-07-17 · 개정: 2026-07-17(codex 교차감사 2패스 반영, 아래 §13)
- 상태: 설계 확정(브레인스토밍 합의 + 교차감사 2패스) · 구현 전
- 선행: v1(Phase 0~6 완주) — 결정론적 추출 코어 + 로컬 stdio MCP + PyInstaller/`.mcpb`
- SSOT 연계: `~/Projects/labyscribe/CLAUDE.md`(정체성·2층 아키텍처) · 로드맵 `Labylinx/docs/.../2026-07-14-video-summary-mcp-share-plan.md`

---

## 1. 배경·목표 (WHY)

v1은 **로컬 stdio MCP**로 완성됐지만, 실제 도달 대상이 **Claude Desktop 유저**로 좁다. 목표는 **일반 ChatGPT/Claude 사용자(무료~기본 유료 플랜)**가 별도 서버·계정·심사 없이 유튜브 영상을 labyscribe의 무손실 추출+요약 계약으로 요약하게 하는 것.

브레인스토밍에서 확정된 **판단 근거(웹 조사)**:
- **원격 MCP는 부적합** — 셀프호스팅 원격은 "내 컴퓨터를 인터넷 서버로 상시 노출 + OAuth 2.1"이 필요해 "서버 의존 0·설치 쉬움"과 정면 충돌. 폰 Claude 앱은 Anthropic 클라우드 경유라 공개 HTTPS가 필수(→ VPN/Tailscale 탈락).
- **MCP 공개 배포도 개인엔 막힘** — 커넥터 디렉토리 등재는 **Team/Enterprise 조직 + 개인정보처리방침·심사** 필요. 개인 플랜 불가.
- **ChatGPT는 MCP가 사실상 봉쇄** — Developer Mode(Pro+)·읽기전용·무료 미지원.
- **보편 인터페이스 = 수동 전달** — 무료 플랜 포함 모든 챗봇에서 되는 건 **복붙 + 파일 업로드**. 2026년 **Markdown이 AI 대화의 보편 교환 포맷**.

→ 결론: **MCP를 주 채널에서 접고, 로컬 웹 UI + 수동 전달(Markdown)로 전환.** GitHub 릴리스로 심사 없이 배포.

### 정체성 (유지 — 불변)
labyscribe는 여전히 **추출·정제·프롬프트 계약만** 진다. 요약·번역은 사용자 챗봇이 수행(비용 0 = "무료 메커니즘"). 검증된 순수 정제 로직(`parse_vtt`·`select_track`·`quality_ok`)을 **그대로 계승**. 바뀌는 건 **전달 채널**(stdio MCP → 로컬 웹 UI)과 그를 위한 **오케스트레이션 확장**뿐.

---

## 2. 범위

### 이번 범위 (v2)
1. **로컬 웹 UI** — 더블클릭 실행 → `127.0.0.1` 웹서버 기동 → 브라우저 자동 열기. URL 입력·챕터 목록·복사 버튼.
2. **챕터 분할** — yt-dlp `chapters` 메타 기반 의미 단위 분할, 없으면 시간/바이트 폴백.
3. **Markdown 출력** — 파트별 `part-NN.md` + 통합 `transcript.md` + 요약 프롬프트. (v1 `transcript.txt`는 **유지**하고 `.md`를 **추가** — §6.1)
4. **클립보드 순환** — "파트 N 복사" / "다음 파트" 로 챗봇에 순차 투입.
5. **자유 저장 폴더** — 백엔드 네이티브 대화상자로 승인한 폴더(capability)에 저장(옵시디언 볼트 지정 시 볼트 저장 자동 성립).
6. **프롬프트 인젝션·Markdown 탈출 방어** — 외부 영상 메타·본문을 안전하게 감싸는 계약 + 적대적 테스트(완전 방어 불가 명시·잔여위험 정의).
7. **최소 경로 설치** — PyInstaller 단독 실행파일(다운로드→더블클릭), 소스 설치 폴백.

### 명시적 범위 밖 (Phase 경계 존중 — 당겨오지 말 것)
- **STT(무자막 영상)** — ffmpeg 의존·연산 무거움. 로컬 웹 UI가 나중에 수용 가능하나 이번 X.
- **원격/폰 접근** — 접음(서버 부담·OAuth·심사).
- **구글 드라이브·Notion·메일 저장** — 외부 OAuth/API 의존 → "의존 0" 정체성 상충. 후속.
- **로컬 stdio MCP(v1)** — **보존**(삭제 금지). Claude Desktop 유저는 기존 설정으로 계속 사용. v2 새 개발은 웹 UI 중심.
- **다중 사용자·동시 다작업 스케일** — v2도 **단일 사용자 로컬** 전제(위협모델 §8). 대규모 동시성·job 오케스트레이션은 범위 밖.
- **파일시스템 TOCTOU/reparse 완전방어·redirect-hop SSRF 심층** — v1 containment 수준 계승. 심층은 **코드 층 교차감사로 이월**(§8·§11).

---

## 3. 사용자 흐름

```
1. 실행파일 더블클릭  (or `labyscribe-web` 명령)
2. 로컬 웹서버 기동 → 브라우저가 http://127.0.0.1:PORT 자동 열림
3. 유튜브 URL 붙여넣고 [추출]  → 추출 중 로딩 표시(동기 완료 후 결과)
4. 파트별 .md 생성(part-01.md…) + 화면에 파트 목록(챕터명 표시)
5. [요약 프롬프트 복사] → 챗봇에 먼저 투입 (또는 Custom GPT/Project 상시화)
6. [파트 1 복사] → 챗봇 붙여넣기 → [다음 파트] → … 순환
7. (선택) [폴더 선택]으로 승인한 저장 폴더/옵시디언 볼트에 .md 세트 저장
```

무료 플랜 대응: 긴 영상은 파트 단위로 나눠 넣어 컨텍스트 한계 회피. 각 파트 파일은 **자족 헤더**(`[3/7] 챕터명 · 직전까지 맥락`)를 달아 파트만 봐도 맥락 유지.

---

## 4. 아키텍처

### 4.1 큰 그림 — 순수 로직 계승 + 오케스트레이션 확장
```
[더블클릭 실행 = webapp 진입점]
  → 로컬 웹서버 기동 (127.0.0.1:PORT · starlette/uvicorn = 직접 의존 고정 §7)
  → webbrowser.open 으로 브라우저 자동 열기
  ┌───────────────────────────────────────┐
  │ 프론트 (정적 HTML/JS · 인라인 or web/static) │
  │  URL 입력 · [추출] · 파트 목록 · 복사/저장 버튼 │
  └───────────────────────────────────────┘
       ↕ 로컬 HTTP API (JSON · localhost 전용 · nonce 전면)
[백엔드]
  · [신규] webapp.py    starlette 라우트 + 서버 기동 + 브라우저 열기 + nonce + 폴더 대화상자 (IO 경계)
  · [신규] chapters.py  cue+타임스탬프 → 챕터 경계 분할 (순수함수 · raise 금지)
  · [신규] render_md.py 파트/메타 → 안전한 Markdown 렌더 (순수 · 이스케이프·동적 경계)
  · [확장] extract.py   run_ytdlp_json 에 chapters 수집 추가 · 순수 정제 로직 불변
  · [확장] storage.py   v2 저장 함수 추가(.md·parts/) · v1 transcript.txt 계약 보존
  · [계승] paging.py    바이트 상한 재분할(챕터 폴백/과대 챕터 재분할)
```

**핵심 원칙(정정)**: **순수 정제 로직(`parse_vtt`·`select_track`·`quality_ok`)은 불변.** 그러나 **오케스트레이션은 명시적으로 확장**한다 — `run_ytdlp_json`에 `chapters` 수집, `storage`에 v2 저장 경로. "그대로"가 아니라 **기존 계약(종료코드·raw 불변·atomic)을 보존하는 v2 어댑터를 추가**하는 것.

### 4.2 순수/IO 경계 (프로젝트 2층 계약 계승)
- **순수함수층**(단위테스트·raise 금지):
  - `chapters.split(cues, chapters_meta, byte_cap) → parts[]` — cue의 **타임스탬프를 보존**받아(§5.1) 챕터 경계에 배정. 메타 없음/손상 시 폴백 신호 반환(판정은 상위).
  - `render_md.part(part, meta) → str` — 메타 이스케이프 + **동적 데이터 경계**(§5.3).
- **오케스트레이션/IO층**: `webapp.py`가 `run_extract`(저장) → `chapters.split` → `render_md` → API 응답 배선. 기존 `ExtractResult.exit_code` 계약을 그대로 에러 매핑에.

### 4.3 API 엔드포인트 (로컬 전용 · JSON · **모든 API에 nonce 필수** · 결과는 result_id 결속)
| 메서드·경로 | 입력 | 출력(allowlist 투영) |
|---|---|---|
| `GET /` | — | 프론트 HTML(기동 nonce 주입). **유일한 nonce 비요구 경로** |
| `POST /api/extract` | `{url, lang?}` + nonce | `{result_id, title, parts:[{part_no, chapter_no, title, bytes}], summary_prompt, status}` (동기 완료) |
| `GET /api/part/{result_id}/{part_no}` | nonce | `{part_no, chapter_no, title, markdown}` (클립보드용) |
| `POST /api/save` | `{result_id, capability_id}` + nonce | `{saved_names[], status}` (표시명만) |
| `POST /api/pick-folder` | nonce | `{capability_id, display_name}` (승인 폴더 등록 §6.2 · **절대경로 미반환**) |

- **result_id(정정)**: 추출마다 예측불가 `result_id` 발급, **세션 nonce에 결속**. `video_id`는 유튜브 공개값이라 lang/hash 다른 결과를 구분 못 하므로 조회·저장 키로 쓰지 않는다. 프로세스 인메모리 매핑(재시작=무효, 재추출로 복구).
- **진행상태**: `/api/extract`는 **동기 완료 응답**. 프론트는 로딩 스피너만. 비동기 job·SSE·취소는 **과설계로 배제**.
- 절대경로·내부 경로키 **미노출** = v1 `_assemble` allowlist 투영 계승. `pick-folder`·`save`도 **표시명만** 반환.
- 에러 = v1 종료코드 계약을 HTTP/JSON 에러로 매핑(NO_SUBTITLE·EMPTY_TRANSCRIPT·BAD_INPUT 재사용).

---

## 5. 챕터 분할 상세

### 5.1 소스·타임스탬프 보존·귀속 규칙
- `run_ytdlp_json`이 `chapters` 필드(`[{start_time, end_time, title}]`)를 함께 수집.
- **cue 타임스탬프 보존**: `chapters.split`은 **렌더 이전의 cue 중간모델**(`_parse_vtt_cues`의 `(start, end, text)` 리스트)을 입력. 정제(dedup·시간가드·태그strip)는 cue 모델 위에서 그대로.
- **cue 귀속 규칙(결정적 불변조건 — 정정)**:
  - 경계는 **반개구간** `[start, end)`. cue는 그 **start 시각**이 속한 챕터에 귀속(경계 걸침 모호성 제거).
  - **첫 챕터 이전·마지막 이후·챕터 간 공백**의 cue → **직전(또는 최근접) 챕터**에 흡수, 어디에도 없으면 **폴백 파트**(파트 0 또는 인접)로. → **모든 cue가 정확히 한 파트에 귀속**(무손실·무중복) 보장.

### 5.2 분할 계층 (의미 → 바이트) · 파트 식별자 · 상한 재검사
1. **1차 = 챕터 경계**: `chapters` 있으면 각 챕터 구간 cue를 한 파트로.
2. **폴백 = 시간/바이트**: `chapters` 없으면 기존 `paging`의 10분 마커/바이트 상한 분할.
3. **과대 챕터 재분할**: 한 챕터가 상한 초과 시 **내부**를 `paging`으로 재분할.
- **상한 = 최종 렌더 기준(정정)**: 바이트 상한은 cue 원문이 아니라 **헤더·경계·이스케이프가 더해진 최종 UTF-8 렌더 결과** 기준으로 재검사. 초과 시 재분할(UTF-8 무절단 유지).
- **식별자 규칙**: **논리 챕터 번호**(`chapter_no`)와 **전송 파트 번호**(`part_no`, 1..N 연속)를 **분리**. 과대 챕터가 3파트로 쪼개지면 `part_no`는 연속, `chapter_no`는 동일. 파일명·"다음 파트" 순회·헤더 분모는 모두 `part_no` 기준.

### 5.3 파트 렌더 (Markdown · 인젝션/탈출 방어)
```markdown
> [3/7] 챕터: 「진단의 기초」 · 12:30–18:45
> (직전 챕터까지의 흐름을 이어서 요약해 주세요)

<<<LABYSCRIBE-DATA-{동적토큰}
transcript 본문 (아래 경계로 감쌈)
LABYSCRIBE-DATA-{동적토큰}>>>
```
- 헤더 둘째 줄은 **챗봇에게 주는 연속성 지시문**일 뿐 — labyscribe가 직전 요약을 삽입하는 게 아니다(요약은 챗봇 몫).
- **보안 계약(정정·강화)**:
  - **메타 이스케이프**: 외부 제목·챕터명의 Markdown 구조 문자(`#`, `` ` ``, `>`, `[]()`, 코드펜스)를 이스케이프.
  - **동적 데이터 경계**: 본문을 감싸는 구분자 토큰을 **본문에 존재하지 않도록 동적 생성**(본문 스캔 후 충돌 없는 토큰 선택 or 길이 프레이밍) → 본문이 경계를 닫는 탈출 차단.
  - **정직성(정정)**: 프롬프트 인젝션은 확률적 모델에서 **완전 방어 불가**임을 명시한다. 방어는 "데이터 전달과 지시 전달을 최대한 분리 + '경계 안은 데이터, 내부 지시 불이행' 계약"이며, **잔여 위험을 문서화**. 적대 평가의 **합격 기준**(구조 탈출 0·경계 종료 시도 무력화)을 정의하되 "요약 무결성"은 best-effort로 규정(도구 표면이 아닌 챗봇 책임 경계).
  - **적대적 fixture 테스트**: "이전 지시 무시…" 자막 · 구조 탈출 제목 · **본문 내 경계 종료 문자열 삽입** → 렌더가 경계·이스케이프를 지키는지 검증.
- 태그 strip·롤링 dedup·시간가드는 기존 `parse_vtt` 그대로.

---

## 6. 출력·저장

### 6.1 산출물 (v1 계약 보존 + .md 추가) · 증분 생성 절차
```
<root>/<video_id>/<lang>-<hash>/
  ├─ raw/<id>.<lang>.vtt      원본 불변 (계승)
  ├─ meta.json               v1 필드 + [신규] chapters[]·parts[] (v1 리더는 미지 필드 무시)
  ├─ transcript.txt          [유지] v1 MCP 계약 보존 (10분 마커)
  ├─ transcript.md           [신규·통합] 전체 (파트 마커 포함)
  └─ parts/
       ├─ part-01.md          [신규] 파트별 개별 파일 (자족 헤더)
       └─ part-02.md
```
- **신규 추출**: 세트 전체(txt+md+parts+raw+meta)를 temp 완결 후 **디렉토리째 atomic rename**(v1 계약 그대로). `transcript.txt`는 **삭제·대체하지 않음**.
- **v1 캐시 증분(정정 — 절차 명세)**: `.md`/`parts/`가 없는 v1 캐시 히트 시 → **디렉토리째 rename이 아니라**, 같은 파일시스템에서 `transcript.txt`/vtt로부터 `.md`·`parts/*`를 **temp 생성 후 개별 파일 원자적 이동(rename)으로 기존 디렉터리에 추가**. `raw/`·`transcript.txt`·기존 `meta.json` 핵심 필드는 **불변**(meta는 chapters/parts 필드만 append-merge). 실패 시 temp만 정리(기존 디렉터리 무손상). 캐시키(`<lang>-<hash>` glob) **불변**.

### 6.2 저장 대상 — capability 모델 (정정: 네이티브 대화상자)
- **폴더 승인(capability)**: `POST /api/pick-folder`가 **백엔드 네이티브 폴더 대화상자**(예: `tkinter.filedialog.askdirectory`)를 띄워 사용자가 명시 선택한 OS 경로를 세션 capability(`capability_id`)로 등록. **브라우저 `showDirectoryPicker`·경로 텍스트 입력은 배제**(전자는 Python 경로 변환 불가, 후자는 승인 증명이 아니라 capability 모델 위배). 프론트에는 `capability_id`+표시명만(절대경로 미노출).
- **저장 범위**: capability 하위에만 원자적 쓰기. `is_within(capability_root, target)` + realpath + O_NOFOLLOW(= **v1 containment 수준 계승**). 임의 파일 쓰기 불가.
- **원자적 저장·충돌**: 목적지에도 temp 완결 후 원자적 커밋. 충돌(동명 영상 재저장) 시 **기본 비덮어쓰기**(버전 접미 or 사용자 확인).
- **TOCTOU/reparse(완화·이월)**: realpath 검사~쓰기 사이 심볼릭 교체·중간경로 링크·Windows reparse point 완전방어(openat/dirfd 성분검증)는 **단일 계정 로컬 위협모델 대비 과도** → v1 수준 유지, 심층은 **코드 층 교차감사로 이월**(§11).

---

## 7. 설치·배포·의존성

- **1순위: PyInstaller 단독 실행파일** — Python·yt-dlp·의존성 전부 번들 → **다운로드→더블클릭**. Phase 5 win-x64 자산·CI 계승, macOS 빌드 추가.
- **폴백: 소스 설치** — `install` 스크립트가 Python≥3.10·yt-dlp 순차 확인·설치.
- 배포 = **GitHub 릴리스**(심사·조직 불필요). OS별 아티팩트(mac/win).
- **macOS(정정)**: 미서명 실행파일은 Gatekeeper가 "다운로드→더블클릭"을 차단 → **서명·공증 앱 번들**이 이상적이나(Apple Developer 계정 필요), 그 전까지 macOS는 **소스 설치를 1순위 폴백**으로 명시(우클릭-열기 우회는 보조). win은 단독 실행파일 1순위 유지.
- **의존성 명시(정정)**: 웹서버는 `mcp>=1.28`의 전이 의존이지만 공개 계약이 아니므로, `pyproject.toml`에 `starlette`·`uvicorn`을 **직접 의존성으로 고정**(버전 범위 명시). frozen 빌드 import 가능성 + 버전 호환을 **CI 스모크로 검증**. 정직한 문서화: "런타임 의존 = yt-dlp + 웹서버(starlette/uvicorn); **추출 파이프라인 의존 = yt-dlp 하나**".

---

## 8. 보안 (위협모델 = 단일 사용자 로컬 · 정정)

- **위협모델 명시**: **같은 OS 계정의 로컬 프로세스는 신뢰**한다 — 그들은 이미 산출물 파일(`~/labyscribe`)에 직접 접근 가능하므로 nonce/handle로 막는 것은 무의미. 방어 대상은 **브라우저 컨텍스트발 공격**(타 웹사이트가 localhost API 호출)과 **네트워크 노출**이다.
- **바인딩**: `127.0.0.1`만(0.0.0.0 금지) → LAN·인터넷 노출 0.
- **nonce 목적(정정)**: nonce는 **브라우저발 CSRF/DNS-rebinding 방어**용 — `GET /`로 프론트에 주입, `/` 외 모든 API가 필수 요구(누락 403). **"타 로컬 프로세스 차단" 주장은 철회**(위협모델 밖). result_id도 nonce에 결속.
- **Origin/Host 검증**: 허용값 `127.0.0.1:PORT`/`localhost:PORT`, 그 외·변형 Host 거부. Origin 없는 비단순 요청 처리·실패 기본값(거부)을 구현 명세, 변형 Host·null Origin·누락 헤더 테스트.
- **SSRF(이월)**: `validate_url` allowlist 계승. **리다이렉트·추출기 추가요청 hop별 제한**은 v1 구현(Phase 4)으로 재확인 + 코드 층에서 테스트 고정.
- **프롬프트 인젝션·Markdown 탈출**: §5.3(완전방어 불가 명시·동적 경계·잔여위험).
- **프론트 XSS(신규·정정)**: 외부 제목·챕터명을 DOM에 넣을 때 **`textContent`만 사용**(`innerHTML` 금지) — Markdown 이스케이프는 HTML DOM 방어가 아니다. 로컬 UI XSS는 nonce·capability_id 탈취로 직결되므로 **엄격 CSP**(inline script 최소화·`default-src 'self'`)를 함께 건다.
- **파일시스템**: §6.2 capability containment(v1 수준, TOCTOU 이월).

---

## 9. 테스트 전략 (v1 2층 게이트 계승)

- **결정적 계약(CI 차단)**:
  - `chapters.split` 단위 — 챕터 있음/없음/과대/손상 메타/경계 겹침·역순·공백·챕터밖 cue. **모든 cue가 정확히 한 파트에 귀속**(무손실·무중복 불변조건). 반개구간 규칙. 고정 fixture.
  - `render_md` 단위 — 자족 헤더·본문 무손실·메타 이스케이프·**동적 경계**·**본문 내 경계 종료 문자열 무력화**·**렌더 후 바이트 상한**.
  - **적대적 인젝션 fixture** — "이전 지시 무시…" 자막·구조 탈출 제목·경계 종료 시도.
  - API 계약 — allowlist 투영(절대경로·`pick-folder` path 미노출)·**nonce 게이트(누락 403)**·**result_id 결속**·Origin 거부·에러 매핑.
  - **프론트 XSS fixture** — 제목·챕터명에 `<script>`·이벤트 핸들러 삽입 → `textContent` 렌더·CSP로 무력화 확인.
  - 저장 — capability containment(이탈 거부)·목적지 atomic·비덮어쓰기·**v1 transcript.txt 유지**·`.md` 증분 생성(기존 디렉터리 무손상).
- **온라인 스모크(비차단·env gated)**: 실영상 챕터 有/無 각 1.
- silent-failure 0: 빈/음향-only transcript는 `quality_ok`로 계속 차단.

---

## 10. 계승·재사용 요약

| 컴포넌트 | v2 처리 |
|---|---|
| `parse_vtt`·`select_track`·`quality_ok`(순수 정제) | **불변** |
| `run_ytdlp_json`(extract.py) | **확장**: chapters 수집 추가 |
| `storage.py` | **확장**: v2 저장 함수(.md·parts/)·증분 추가 · v1 transcript.txt·atomic·containment 보존 |
| `paging.py` | 챕터 폴백/과대 재분할에 재사용 |
| `prompts/`(무손실 요약) | 계승 + 파트 투입·데이터 경계 안내 보강 |
| `server.py`·`handles.py`·`.mcpb`(로컬 MCP) | **보존**(삭제 금지·v2 미개발) |
| **신규** `chapters.py`(순수) | cue+타임스탬프 → 챕터 분할 |
| **신규** `render_md.py`(순수) | 안전 Markdown 렌더(이스케이프·동적 경계) |
| **신규** `webapp.py`(IO) | 로컬 웹서버·라우트·nonce·result_id·브라우저 열기·폴더 대화상자 |
| **신규** 프론트(HTML/JS) | URL 입력·파트 목록·복사/저장 |

---

## 11. 미결·리스크 (구현 시 확정 / 코드 층 이월)

- **[코드층 이월] 파일시스템 TOCTOU/reparse** — openat/dirfd 성분 검증·Windows reparse 대응. v1 수준으로 시작, 코드 교차감사에서 판단.
- **[코드층 이월] SSRF redirect-hop** — validate_url이 리다이렉트·추출기 추가요청까지 제한하는지 v1 검증 + 테스트 고정.
- **[구현 확정] 파이프라인 원자성(3패스)** — 추출·분할·렌더를 **staging에서 완결 후 storage 단일 커밋**(저장 후 분할 순서 금지). §4.2 배선을 이 순서로 확정.
- **[구현 확정] 캐시 증분 완결성(3패스)** — 개별 rename 중간종료로 **불완전 `.md` 세트가 영구 캐시 히트** 되는 것 방지: **완료 마커/세대 manifest**로 완결 세대만 히트. v1 캐시(chapters 메타 부재)는 시간/바이트 폴백 or yt-dlp 메타 재조회 중 택일 확정·기록.
- **[구현 확정] 챕터 정규화·상한(3패스)** — 중첩·역순 챕터 **정규화 + 단일 귀속 우선순위(동률 규칙 고정)**. 헤더 자체 cap 초과·**단일 cue > cap** 처리(메타 길이 제한·안전 분할 또는 명시적 오류).
- **[구현 확정] 저장 충돌 규칙(3패스)** — 비덮어쓰기 **단일 규칙**(접미 번호 할당)·목적지 **세트 단위** 원자성·복구 정책 확정.
- **[구현 확정] tkinter·clipboard(3패스)** — 폴더 대화상자 **GUI 메인스레드 디스패치·단일 실행 잠금**(frozen 스모크). Clipboard API 권한거부 시 **선택가능 텍스트 수동 복사** 폴백.
- **동시성**: 중복 [추출] 제출 → 영상/캐시키별 단일 실행 잠금·중복 병합·종료 시 yt-dlp 자식 정리(경량).
- **yt-dlp `chapters` 신뢰도**: 챕터 없는 영상 비율 높으면 폴백이 주 경로 → 초기 실영상으로 분포 확인.
- **PyInstaller + 웹서버·tkinter 번들**: uvicorn/starlette/tkinter가 frozen에서 import·표시되는지 스모크(§7 CI).
- **macOS 빌드·서명**: 미서명 배포 Gatekeeper 마찰 → 우회 안내.
- **포트 충돌**: 고정 포트 점유 시 대체 포트 탐색.

## 12. 참조

- v1 정체성·2층 아키텍처: `CLAUDE.md`
- 로드맵(v2 = 범위 밖 항목·보안 관심사 line 89~91): `Labylinx/docs/.../2026-07-14-video-summary-mcp-share-plan.md`
- 브레인스토밍 조사 출처(MCP 원격·인증·ChatGPT·노트앱·터널): 본 세션 대화 로그

## 13. codex 교차감사 반영 이력 (2026-07-17 · engine=codex-cli 0.144.1)

**1패스**(blocking 5): 저장 계약 붕괴(CRITICAL)·"그대로" 모순·저장폴더 containment·인젝션 방어·nonce 소유권 → §6.1·§4.1·§6.2·§5.3·§8 반영.

**2패스**(신규 blocking 6 = 1패스 반영이 만든 새 표면의 심화):
- **CRITICAL** video_id 조회 충돌 → §4.3 `result_id` 발급(다중 결과 식별).
- **HIGH** showDirectoryPicker 경로 변환 불가·텍스트 폴백 모순 → §6.2 **백엔드 네이티브 대화상자**로 정정, 폴백 제거.
- **HIGH** nonce 획득 가능(타 프로세스) → §8 위협모델 정정(같은 계정 프로세스 신뢰), nonce 목적을 **브라우저 CSRF 방어**로 재정의, "타 프로세스 차단" 철회.
- **HIGH** 본문 경계 탈출 → §5.3 **동적 구분자**·경계 종료 적대 테스트.
- **HIGH** realpath TOCTOU/reparse → **완화·코드층 이월**(단일 계정 로컬 위협모델 대비 과도).
- **MEDIUM** 반영: pick-folder path 미노출(§4.3)·**렌더 후** 바이트 상한(§5.2)·챕터 귀속 반개구간 규칙(§5.1)·인젝션 방어 정직성/잔여위험(§5.3)·캐시 증분 절차(§6.1).

**3패스**(신규 HIGH 4 + MEDIUM): 대부분 **'어떻게(계획/구현)' 층 세부**(파이프라인 staging 순서·챕터 정규화·증분 완료 마커·macOS 서명·헤더/단일cue 상한·충돌 규칙·tkinter 스레드·clipboard 폴백). **스펙 수준 신규 2건만 반영** — 프론트 XSS(§8·§9)·macOS 배포 현실(§7). 나머지는 §11에 **[구현 확정]/[코드층 이월]**로 명시 기록.

**멈춤 선언**: plan 층이 코드 수준 세부로 하강 = 수확 체감. CLAUDE.md 원칙(plan 무한반복 금지 · 정확한 잠금 프리미티브·실측값은 구현+코드층 교차감사에서 확정)에 따라 **plan 층 감사 종료**. 남은 지적은 §11 이월 기록으로 보존(silent 누락 아님). 다음 = 구현 단계에서 **코드 층 교차감사**로 최종 검증.
