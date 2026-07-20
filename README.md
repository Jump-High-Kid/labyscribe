# labyscribe

유튜브 자막을 **결정론적으로 추출·페이징**하고 요약 프롬프트 계약을 제공하는 **로컬 MCP** 서버.
요약 자체는 당신의 호스트(Claude Desktop 등)가 수행한다 — labyscribe 는 추출·페이징·프롬프트만
담당하므로 요약 비용이 없다(yt-dlp 자막 직접 추출 · ffmpeg·STT 불필요 · 추출에 LLM 호출 0).

## 설치 (macOS · `.mcpb` 원클릭)

1. `labyscribe.mcpb` 를 연다 → Claude Desktop 이 확장으로 설치한다.
2. 설정에서 **저장 위치**(기본 `~/labyscribe`)를 확인/변경한다.

### 미서명 안내 (Gatekeeper 우회)

이 빌드는 **미서명·미공증**이다. 첫 실행 시 macOS 가
"개발자를 확인할 수 없어 열 수 없음" 으로 막을 수 있다. 우회:

- **시스템 설정 → 개인정보 보호 및 보안** → 하단의 차단 알림에서 **"확인 없이 열기"**, 또는
- 설치된 확장 폴더의 격리 속성 제거:
  ```sh
  xattr -dr com.apple.quarantine "<Claude Desktop 확장 설치 경로>/labyscribe"
  ```
  (정확한 경로는 Claude Desktop 버전에 따라 다르다.)

## 설치 (Windows · `.mcpb` 원클릭)

1. `labyscribe-win.mcpb` 를 연다 → Claude Desktop 이 확장으로 설치한다.
   (릴리스가 없으면 GitHub Actions `build` 워크플로의 artifact `labyscribe-win-mcpb` 에서 받는다.)
2. 설정에서 **저장 위치**(기본 `%USERPROFILE%\labyscribe`)를 확인/변경한다.

이 빌드도 **미서명**이다. Windows SmartScreen 이 "Windows의 PC 보호" 로 막으면
**추가 정보 → 실행** 을 눌러 진행한다.

## 웹 UI (v2 · Windows 단독 실행파일 · Claude Desktop 불요)

Claude Desktop 없이 **브라우저에서 바로** 쓰는 로컬 웹 UI다. 추출·챕터 분할·Markdown 파트를
로컬에서 만들고, 요약 프롬프트를 복사해 원하는 호스트(웹 Claude·ChatGPT 등)에 붙여넣는다.

1. GitHub 릴리스에서 **`labyscribe-webui-win.zip`** 을 받는다.
   (릴리스가 없으면 Actions `build` 워크플로의 artifact `labyscribe-webui-win` 에서 받는다.)
2. 압축을 풀고 **`labyscribe-web.exe`** 를 더블클릭한다 → 로컬 웹서버가 뜨고 기본 브라우저가
   `http://127.0.0.1:8760/` 을 연다(포트 점유 시 자동으로 다음 포트 탐색).
3. 브라우저가 **자동으로 열리지 않으면** 콘솔에 출력된 주소(`http://127.0.0.1:<포트>/`)를 직접 연다.

- **미서명 안내(SmartScreen)**: 이 빌드는 미서명이다. "Windows의 PC 보호" 가 뜨면
  **추가 정보 → 실행**. 출처(이 저장소 릴리스·아래 SHA-256)를 확인하고 실행한다.
- **네트워크 노출 0**: 서버는 `127.0.0.1` 에만 바인딩된다(LAN·인터넷 미노출).
- 저장 위치 기본 `%USERPROFILE%\labyscribe`(환경변수 `OUTPUT_DIR` 로 변경).

## 웹 UI (v2 · macOS 소스 실행)

macOS 는 **소스에서 바로** 실행한다. 미서명 실행파일은 Gatekeeper 가 더블클릭을 막으므로
(서명·공증은 Apple Developer 계정이 필요) 이번 릴리스는 **소스 실행을 1순위**로 지원한다.
런타임 의존성은 **yt-dlp 하나**뿐이라 별도 패키지 설치가 없다.

**요구**: Python ≥ 3.10 · yt-dlp on PATH.

1. 이 저장소를 받는다(`git clone` 또는 ZIP 다운로드 후 압축해제).
2. **`labyscribe-web.command`** 을 더블클릭한다 → Terminal 이 열리고 로컬 웹서버가 뜬 뒤
   기본 브라우저가 `http://127.0.0.1:8760/` 을 연다(포트 점유 시 자동으로 다음 포트 탐색).
   - 런처가 Python·yt-dlp 존재를 먼저 확인하고, **없으면 설치법을 안내하며 멈춘다**(자동 설치는 안 함).
   - 종료는 그 Terminal 창에서 **Ctrl+C**.
3. 터미널에서 직접 실행해도 된다: `python3 webapp.py`.

**의존성 설치**(런처가 안내하면):
- **Python 3.10+**: [python.org](https://www.python.org/downloads/) 또는 `brew install python`
- **yt-dlp**: `brew install yt-dlp` 또는 `python3 -m pip install -U yt-dlp`
  (소스 실행은 yt-dlp 를 사용자 환경에서 받으므로 **공식 배포처**로 설치한다.)
- (선택) **폴더 선택 대화상자**: Homebrew Python 은 `brew install python-tk` 가 필요하다.
  없어도 동작한다(대화상자만 비활성 · 저장 위치는 아래 참조).

- **네트워크 노출 0**: 서버는 `127.0.0.1` 에만 바인딩된다(LAN·인터넷 미노출).
- 저장 위치 기본 `~/labyscribe`(환경변수 `OUTPUT_DIR` 로 변경).

## 지원 호스트 / 플랜

labyscribe 는 **로컬 `.mcpb`(Claude Desktop 확장 번들)** 로 배포된다. 아래 표는 호스트·플랜별
설치/동작 지원을 정리한 것이다. **요약은 각 호스트/구독이 수행**하므로, 자율 도구호출·요약
품질은 호스트 모델에 달려 있다(labyscribe 는 추출·페이징만 담당).

| 호스트 | 플랜 | MCP(`.mcpb`) 지원 | 상태 | 비고 |
|--------|------|------------------|------|------|
| Claude Desktop | Pro / Max | 지원 | **사용자 검증 필요** | `.mcpb` 원클릭 설치. 자율 도구호출 실증은 [E2E 체크리스트](docs/E2E-CHECKLIST.md)로 사용자가 확인 |
| ChatGPT | Free | **미지원** | 확인됨 | 프리 플랜은 MCP/커넥터 미제공 |
| ChatGPT | Plus / Pro / Team 등 | **미지원** | 확인됨 | ChatGPT 의 MCP 는 **원격 커넥터(HTTP)** 방식이라 로컬 `.mcpb` 번들을 소비하지 않는다. ChatGPT 도달 = 원격 MCP = **v2 지연(범위 밖)** |
| Cursor / Claude Code | — | v2 지연 | 미검증 | 로컬 stdio MCP 연결 여지는 있으나 이번 릴리스 범위 밖(미검증) |

> **상태 컬럼의 뜻** — "지원"은 **설계상 지원**을 뜻하며, Claude Desktop 의 자율 도구호출·요약은
> 호스트 모델의 비결정적 동작이라 **실증은 사용자 검증**(E2E 체크리스트)에 맡긴다. 실증 없이 "동작
> 보장"으로 단정하지 않는다.

**확인일 · 출처 · 재검증** — 위 표의 외부 제품(ChatGPT 등) 상태는 **2026-07-16** 기준이며,
**각 호스트의 공식 고지**(Anthropic Claude Desktop 확장/MCP Bundle 문서, OpenAI ChatGPT
커넥터/MCP 문서)를 근거로 한다. 외부 제품 정책은 수시로 바뀌므로, 다음 절차로 재검증한다:

1. 각 호스트 공식 문서에서 **해당 플랜의 MCP/커넥터 지원 여부**를 확인한다.
2. **로컬 `.mcpb`(Desktop Extension) 설치 지원 여부**를 확인한다(원격 전용 커넥터는 `.mcpb` 미소비).
3. 변동이 있으면 이 표와 **확인일**을 갱신한다.

## 긴 영상 페이징 · 사용자 주도 폴백

긴 영상은 transcript 가 여러 **part** 로 나뉜다. `extract_transcript` 응답의 `total_parts`
가 1 보다 크면 **첫 part 만** 반환되고, 나머지는 순차 조회한다. 정상적으로는 호스트가
`summarize_video` 프롬프트 지시에 따라 **자동으로 2번째부터 마지막 part 까지** 이어받아 하나로
요약한다.

호스트가 긴 영상을 **자동으로 완주하지 못하면**(중간에 멈추거나 앞부분만 요약하면), 사용자가
직접 다음 part 를 이어받게 하면 된다:

1. `extract_transcript` 응답에서 **`transcript_handle`** 과 **`total_parts`** 값을 확인한다.
2. 호스트에게 **`get_transcript_part(transcript_handle, part)`** 를 `part = 2, 3, …, total_parts`
   순서로 호출해 이어달라고 지시한다(`part` 는 **1-based**, 첫 part 는 `extract_transcript` 가 이미 반환).
   - 예: "handle `<위 값>` 로 `get_transcript_part` 를 2번 part 부터 `total_parts` 까지 순서대로 불러서 이어서 요약해줘."
3. 각 응답의 `part_index` / `total_parts` 로 누락 없이 모았는지 확인한다.

> **핸들 수명** — `transcript_handle` 은 **서버 프로세스 생애 동안만** 유효한 인메모리 값이다.
> 호스트/서버를 재시작했다면 핸들이 무효가 되므로, `extract_transcript` 를 **다시 호출**한다(원본은
> 로컬에 보존돼 있어 **캐시 히트**로 즉시 복구되고 새 핸들이 발급된다).

**최후의 폴백** — 페이징 자체가 막히면, 로컬에 보존된 원본 transcript 를 직접 열어
호스트에 붙여넣으면 된다: `~/labyscribe/<video_id>/<lang>-<hash>/transcript.txt`
(자막은 [프라이버시](#프라이버시) 섹션대로 항상 로컬에 남는다).

## 프라이버시

- 자막 **추출은 전부 로컬**에서 일어난다(yt-dlp 가 유튜브에서 자막만 받아온다).
- **요약은 당신의 호스트/구독**([지원 호스트/플랜](#지원-호스트--플랜) 표 참조)이 수행한다 —
  labyscribe 는 요약을 하지 않고 외부로 보내지도 않는다(외부 전송 0).
- 원본 자막은 저장 위치(`~/labyscribe`)에 **불변 보존**된다.

## CLI (개발자용)

MCP 없이 추출만 쓰려면:

```sh
python extract.py <youtube-url> --out <저장-루트> [--lang ko,en]
```

산출물: `<루트>/<video_id>/<lang>-<hash>/` 아래 `transcript.txt`(10분 마커) ·
`raw/<id>.<lang>.vtt`(원본 불변) · `meta.json`.

## 빌드 (`.mcpb` 재생성)

macOS universal2(arm64+x64) `.mcpb` 를 직접 빌드하려면:

```sh
build/build_macos.sh
```

요구: **python.org universal2 Python 3.12**(Homebrew/uv 파이썬은 per-arch 라 불가 —
https://www.python.org/downloads/ 의 macOS universal2 인스톨러) · `node`/`npx` · 인터넷.
빌드는 yt-dlp 공식 바이너리를 SHA256+GPG 검증 후 번들한다([THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)).

Windows(win-x64) `.mcpb` 는 로컬 대신 **GitHub Actions**(`windows-latest`)에서 빌드한다
(macOS 에서는 win-x64 바이너리를 만들 수 없다). `.github/workflows/build.yml` 이
`build/build_windows.sh` 를 실행해 `labyscribe-win.mcpb` 를 artifact 로 올린다. 수동 실행은
Actions 탭의 `build` 워크플로 → **Run workflow**(`workflow_dispatch`).

v2 **웹 UI 단독 실행파일**(win-x64)도 같은 `build.yml` 의 `windows-webui` job 이
`build/build_webapp_windows.sh` 로 빌드해 artifact `labyscribe-webui-win` 으로 올린다
(webapp.py onedir + 번들 yt-dlp.exe · `.mcpb` 아님). 빌드 내부의 **frozen 스모크**
(`--selfcheck` 프롬프트·tkinter 번들 + 웹서버 기동 + yt-dlp 실행)가 hard gate 라, 번들 결함이면
job 이 RED 로 막힌다.

### 릴리스 (유지관리자)

Actions artifact 는 **보존기간**이 있으므로, 배포는 **GitHub 릴리스**에 에셋으로 고정한다:
`labyscribe-webui-win` artifact 를 내려받아 릴리스에 첨부하고, 무결성 확인용 **SHA-256** 을
릴리스 노트에 게시한다(`sha256sum labyscribe-webui-win.zip` · PowerShell `Get-FileHash`).
사용자는 다운로드 후 해시를 대조한다. (릴리스 최종 zip 파일명은 발행 시 확정.)

## 개발/테스트

```sh
pip install -e ".[dev]"
pytest
```
