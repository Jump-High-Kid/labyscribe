#!/usr/bin/env bash
# build_webapp_windows.sh — labyscribe v2 웹UI Windows(win-x64) 단독 실행파일 빌드
#
# 산출: dist/labyscribe-web/ (PyInstaller onedir · webapp.py 진입점 + 번들 yt-dlp.exe)
#   → 다운로드→압축해제→labyscribe-web.exe 더블클릭→로컬 웹서버+브라우저.
#   CI(upload-artifact)가 디렉토리를 zip 으로 포장(labyscribe-webui-win.zip).
# 요구: ① Python 3.12 x64  ② gpg  ③ 인터넷  ④ Windows(git bash). node/npx 불요(.mcpb 아님).
#
# v1 build_windows.sh(server.py→.mcpb) 파생 — 차이:
#   - 진입점 server.py → webapp.py · exe명 labyscribe → labyscribe-web (v1 자산과 완전 분리)
#   - 제거: mcp/pydantic collect(webapp 미import) · pip install .(런타임 의존 0) · manifest 패치 · mcpb pack
#   - 스모크: MCP stdin 핸드셰이크 → HTTP 서버 기동 + --selfcheck(프롬프트·tkinter 번들)
#   - yt-dlp.exe sibling 배치를 스모크 '이전'에 완료(스모크가 실행 확인 — 순서 필수)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYBUILD="${PYBUILD:-python}"                 # actions/setup-python 이 PATH 에 3.12 x64 배치
BUILD_VENV="$ROOT/.venv-build"
VPY="$BUILD_VENV/Scripts/python"             # win venv = Scripts(≠ bin) — 핵심 함정
DIST="$ROOT/dist/labyscribe-web"             # PyInstaller onedir 출력(v1 dist/labyscribe 와 분리)
EXE="$DIST/labyscribe-web.exe"

echo "── [0/5] preflight ──"
command -v "$PYBUILD" >/dev/null || { echo "ERROR: python 없음: $PYBUILD"; exit 1; }
command -v gpg >/dev/null || { echo "ERROR: gpg 없음(yt-dlp provenance 검증 필수)"; exit 1; }

echo "── [1/5] 빌드 venv 생성 + PyInstaller 설치 ──"
# 런타임 의존성(pip install .) 미설치 — webapp 는 stdlib + 로컬 모듈만 import(AC-7).
rm -rf "$BUILD_VENV"
"$PYBUILD" -m venv "$BUILD_VENV"
"$VPY" -m pip install -U pip wheel pyinstaller pyinstaller-hooks-contrib

echo "── [2/5] yt-dlp.exe provenance 검증 다운로드(SHA256+GPG vendor) ──"
TARGET_OS=windows bash "$ROOT/build/fetch_ytdlp.sh" "$ROOT/build/vendor"

echo "── [3/5] PyInstaller onedir (win-x64 · webapp.py) ──"
rm -rf "$ROOT/build/pyi-web" "$DIST"
# 경로 인자는 cygpath -w 로 절대 Windows 경로로 변환(네이티브 python 의 MSYS 경로 오해 방지).
PROMPTS_WIN="$(cygpath -w "$ROOT/prompts")"
WEBAPP_WIN="$(cygpath -w "$ROOT/webapp.py")"
DIST_WIN="$(cygpath -w "$ROOT/dist")"
WORK_WIN="$(cygpath -w "$ROOT/build/pyi-web")"   # v1 build/pyi 와 분리(동시 빌드 격리)
"$VPY" -m PyInstaller --noconfirm --clean \
  --name labyscribe-web --onedir \
  --distpath "$DIST_WIN" --workpath "$WORK_WIN" --specpath "$WORK_WIN" \
  --add-data "$PROMPTS_WIN;prompts" \
  "$WEBAPP_WIN"
# --add-data 는 win os.pathsep 세미콜론(';'). tkinter·로컬 모듈(extract/results/storage/…)은
# 정적 import 라 자동 수집 — mcp/pydantic collect 불요(webapp 그래프에 없음).

echo "── [4/5] 번들 yt-dlp.exe sibling 배치(스모크 이전 — 실행 확인 대상) ──"
install -m 755 "$ROOT/build/vendor/yt-dlp.exe" "$DIST/yt-dlp.exe" 2>/dev/null \
  || cp "$ROOT/build/vendor/yt-dlp.exe" "$DIST/yt-dlp.exe"

echo "── [5/5] frozen 스모크 (hard gate) ──"
SMOKE_OUT="${RUNNER_TEMP:-$(mktemp -d)}"

# (a) --selfcheck: 프롬프트 번들(M4) + tkinter/Tcl 번들(AC-8) 오프라인 프로브
echo "  [5a] --selfcheck (프롬프트·tkinter/Tcl 번들)"
"$EXE" --selfcheck \
  || { echo "ERROR: --selfcheck 실패(프롬프트 or _tkinter/Tcl 데이터 번들 누락)"; exit 1; }

# (b) yt-dlp.exe sibling 실행 확인(무네트워크)
echo "  [5b] yt-dlp.exe --version"
[ -f "$DIST/yt-dlp.exe" ] || { echo "ERROR: yt-dlp.exe sibling 누락"; exit 1; }
"$DIST/yt-dlp.exe" --version >/dev/null 2>&1 \
  || { echo "ERROR: yt-dlp.exe 실행 불가(provenance 손상 의심)"; exit 1; }

# (c) 서버 기동 스모크 — 백그라운드 기동 · no-op BROWSER · stderr URL 파싱
echo "  [5c] 웹서버 기동 + GET / 200"
ERRLOG="$ROOT/dist/webui-smoke.err"; OUTLOG="$ROOT/dist/webui-smoke.out"
OUTPUT_DIR="$SMOKE_OUT" BROWSER="cmd /c rem" \
  "$EXE" >"$OUTLOG" 2>"$ERRLOG" &
APP_PID=$!
# 성공·실패 무관 프로세스·자식(브라우저) 정리 보장(파일락→artifact 업로드 실패 방지)
trap 'taskkill //F //T //PID '"$APP_PID"' >/dev/null 2>&1 || kill '"$APP_PID"' 2>/dev/null || true' EXIT

PORT=""
for _ in $(seq 1 60); do                     # 최대 ~30s(무네트워크라 실제 1~3s)
  # sed 캡처그룹으로 포트만 추출(‘:PORT/’) — grep -oE '[0-9]+' 는 127·0·0·1 까지 뽑아 오염.
  # `|| true`: set -e 에서 URL 부재 시 대입이 스크립트를 즉사시키지 않게(codex H1)
  PORT="$(sed -nE 's#.*http://127\.0\.0\.1:([0-9]+)/.*#\1#p' "$ERRLOG" 2>/dev/null | head -1 || true)"
  [ -n "$PORT" ] && break
  kill -0 "$APP_PID" 2>/dev/null \
    || { echo "ERROR: 앱 조기종료 — preflight 실패 의심(프롬프트 번들 누락=M4)"; sed -n '1,40p' "$ERRLOG"; exit 1; }
  sleep 0.5
done
[ -n "$PORT" ] || { echo "ERROR: 30s 내 기동 URL 미출력(hang 의심)"; sed -n '1,40p' "$ERRLOG"; exit 1; }

BODY="$(curl -fsS --noproxy '*' "http://127.0.0.1:$PORT/")" \
  || { echo "ERROR: GET / 실패"; sed -n '1,40p' "$ERRLOG"; exit 1; }
printf '%s' "$BODY" | grep -q 'nonce="' && printf '%s' "$BODY" | grep -q '<title>labyscribe' \
  || { echo "ERROR: / 응답에 nonce/타이틀 마커 없음(프론트 미주입 의심)"; exit 1; }

echo "완료: $DIST (labyscribe-web.exe + _internal/ + yt-dlp.exe) · 스모크 통과(selfcheck·yt-dlp·HTTP 200)"
