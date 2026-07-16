# Third-Party Notices

labyscribe 배포물(`.mcpb`)에는 아래 서드파티 소프트웨어가 포함됩니다.

## yt-dlp

- 프로젝트: https://github.com/yt-dlp/yt-dlp
- 라이선스: The Unlicense (public domain)
- 포함 형태: 공식 릴리스 바이너리(macOS `yt-dlp_macos` universal2 · Windows `yt-dlp.exe` x64)를
  **SHA256 체크섬 + GPG 서명 검증**(고정 서명 지문 `AC0CBBE6848D6A873464AF4E57CF65933B5A7581`
  — Simon Sawicki, yt-dlp signing key) 통과분만 번들. 서명 공개키는 `build/yt-dlp-signing-key.asc`
  로 저장소에 동봉(keyserver 의존 제거). 버전·SHA256 은 `build/fetch_ytdlp.sh` 참조.
- 보안 업데이트: 자동 자체 업데이트는 하지 않는다. 취약점 발생 시 고정 버전을 갱신해 재릴리스한다.

## Python 런타임 및 의존성 (PyInstaller 동결)

PyInstaller 로 동결된 실행파일에는 Python 인터프리터와 런타임 의존성이 포함됩니다:

- **mcp** (Python MCP SDK) — MIT — https://github.com/modelcontextprotocol/python-sdk
- **pydantic** / **pydantic-core** — MIT
- 그 외 전이 의존성 — 각 상위 라이선스를 따른다.

labyscribe 자체 소스는 MIT (루트 `LICENSE` 참조).
