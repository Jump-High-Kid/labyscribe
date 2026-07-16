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

## 프라이버시

- 자막 **추출은 전부 로컬**에서 일어난다(yt-dlp 가 유튜브에서 자막만 받아온다).
- **요약은 당신의 호스트/구독**이 수행한다 — labyscribe 는 요약을 하지 않고 외부로 보내지도 않는다.
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

## 개발/테스트

```sh
pip install -e ".[dev]"
pytest
```
