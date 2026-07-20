#!/usr/bin/env bash
# labyscribe-web.command — macOS 소스 실행 런처 (Finder 더블클릭 → Terminal 자동 기동)
#
# v2 웹UI(webapp.py)를 소스에서 바로 띄운다 — 바이너리·설치 불요.
#   더블클릭 → Terminal 이 열리고 → preflight 통과 시 로컬 웹서버 기동 + 브라우저 자동열기.
# 요구(둘 다 preflight 하드체크 · 부재 시 안내 후 중단):
#   ① Python ≥ 3.10   ② yt-dlp on PATH (유일 런타임 의존).
# tkinter(폴더피커)는 옵션 — 부재해도 OUTPUT_DIR/기본 ~/labyscribe 로 동작(README 참조).
set -euo pipefail

# ── 스크립트 위치 자동해석 ──
# Finder 더블클릭 시 작업 디렉토리는 홈(스크립트 폴더 아님) → dirname "$0" 로 repo 루트 확정.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# command -v 검사는 if 로 감싼다 — set -e 하에서 조기 즉사시키지 않고 안내 메시지에 도달(silent-failure 0).

# ── preflight ①: Python ≥ 3.10 ──
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 가 없습니다." >&2
  echo "  설치: https://www.python.org/downloads/  또는  brew install python" >&2
  exit 1
fi
# 버전 판정은 실인터프리터 질의(문자열 파싱 회피).
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "✗ Python 3.10 이상이 필요합니다 (현재: $(python3 -V 2>&1))." >&2
  echo "  설치: https://www.python.org/downloads/  또는  brew install python" >&2
  exit 1
fi

# ── preflight ②: yt-dlp on PATH (자막 추출에 필요) ──
if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "✗ yt-dlp 가 PATH 에 없습니다 (자막 추출에 필요)." >&2
  echo "  설치: brew install yt-dlp   또는   python3 -m pip install -U yt-dlp" >&2
  exit 1
fi

# ── 기동 (foreground · exec 로 셸 대체 → 백그라운드 아님) ──
echo "labyscribe 웹 UI 를 시작합니다 — 기본 브라우저가 자동으로 열립니다."
echo "종료하려면 이 창에서 Ctrl+C 를 누르세요."
exec python3 webapp.py
