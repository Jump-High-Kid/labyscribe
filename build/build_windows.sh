#!/usr/bin/env bash
# build_windows.sh — labyscribe Windows(win-x64) .mcpb 빌드 (Phase 5.x)
#
# 산출: labyscribe-win.mcpb (Claude Desktop 원클릭 설치용 · PyInstaller onedir + 번들 yt-dlp.exe)
# 요구: ① Python 3.12 x64  ② node/npx  ③ gpg  ④ 인터넷  ⑤ Windows(git bash)
# 실행: GitHub Actions windows-latest 러너의 `shell: bash`(로컬 Windows git bash 도 가능).
#
# 빌드타임 전용 도구(런타임 의존성 아님): PyInstaller · @anthropic-ai/mcpb(npx).
# macOS(build_macos.sh) 대비 제거: universal2 융합(delocate-merge)·lipo/file·--target-arch
#   — win 은 단일 arch(x64)라 융합 불요. pydantic-core·rpds-py 는 win_amd64 wheel 직접 조달.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYBUILD="${PYBUILD:-python}"            # actions/setup-python 이 PATH 에 3.12 x64 배치
BUILD_VENV="$ROOT/.venv-build"
VPY="$BUILD_VENV/Scripts/python"        # win venv = Scripts(≠ bin) — 핵심 함정
DIST="$ROOT/dist/labyscribe"           # PyInstaller onedir 출력
PACK="$ROOT/dist/pack"                 # mcpb pack 스테이징(manifest + 바이너리)
MCPB_OUT="$ROOT/labyscribe-win.mcpb"

echo "── [0/6] preflight ──"
command -v "$PYBUILD" >/dev/null || { echo "ERROR: python 없음: $PYBUILD"; exit 1; }
command -v npx >/dev/null || { echo "ERROR: node/npx 없음"; exit 1; }
command -v gpg >/dev/null || { echo "ERROR: gpg 없음(yt-dlp provenance 검증 필수)"; exit 1; }

echo "── [1/6] 빌드 venv 생성 + 도구 설치 ──"
rm -rf "$BUILD_VENV"
"$PYBUILD" -m venv "$BUILD_VENV"
"$VPY" -m pip install -U pip wheel pyinstaller pyinstaller-hooks-contrib
"$VPY" -m pip install .                 # 런타임 의존성(mcp 등) — win_amd64 wheel 직접 조달

echo "── [2/6] yt-dlp.exe provenance 검증 다운로드(SHA256+GPG vendor) ──"
TARGET_OS=windows bash "$ROOT/build/fetch_ytdlp.sh" "$ROOT/build/vendor"

echo "── [3/6] PyInstaller onedir (win-x64) ──"
rm -rf "$ROOT/build/pyi" "$ROOT/dist"
# 경로 인자는 cygpath -w 로 절대 Windows 경로(D:\a\…)로 변환한다. 네이티브 Windows python 은
# MSYS 경로(/d/a/…)를 \d\a\… 로 오해하고, 상대경로는 --specpath(build/pyi) 기준으로 해석해
# 둘 다 소스를 못 찾는다 — 절대 Windows 경로가 확실하다.
PROMPTS_WIN="$(cygpath -w "$ROOT/prompts")"
SERVER_WIN="$(cygpath -w "$ROOT/server.py")"
DIST_WIN="$(cygpath -w "$ROOT/dist")"
WORK_WIN="$(cygpath -w "$ROOT/build/pyi")"
"$VPY" -m PyInstaller --noconfirm --clean \
  --name labyscribe --onedir \
  --distpath "$DIST_WIN" --workpath "$WORK_WIN" --specpath "$WORK_WIN" \
  --add-data "$PROMPTS_WIN;prompts" \
  --collect-submodules mcp.server --collect-data mcp --copy-metadata mcp \
  --collect-all pydantic --collect-submodules pydantic_core --copy-metadata pydantic \
  "$SERVER_WIN"
# mcp 는 collect-all 금지 — mcp.cli 가 선택적 typer 를 import 해 수집 중 실패(stdio 서버만 사용).
# --add-data 는 win os.pathsep 세미콜론(';') — macOS 콜론(':')은 win 에서 깨진다.

echo "── [3b] frozen 스모크(MCP 핸드셰이크 + 도구 2개 + 프롬프트 실로드) ──"
SMOKE_IN=$'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"build-smoke","version":"0"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n{"jsonrpc":"2.0","id":3,"method":"prompts/get","params":{"name":"summarize_video"}}'
SMOKE_OUT="$( ( printf '%s\n' "$SMOKE_IN"; sleep 4 ) | OUTPUT_DIR="${RUNNER_TEMP:-$(mktemp -d)}" "$DIST/labyscribe.exe" 2>/dev/null )"
echo "$SMOKE_OUT" | grep -q '"name":"extract_transcript"' && echo "$SMOKE_OUT" | grep -q '"name":"get_transcript_part"' \
  || { echo "ERROR: 스모크 실패(도구 미노출=hidden-import 누락 의심). '$VPY -m PyInstaller … --debug=imports' 로 진단"; exit 1; }
# prompts/get 이 실제 프롬프트 파일을 로드했는지 = --add-data prompts 번들 확인(codex M4).
# _load_summary_prompt 가 open() 을 lazy 실행하므로 도구 노출만으론 데이터 누락을 못 잡는다.
echo "$SMOKE_OUT" | grep -q '"messages"' \
  || { echo "ERROR: 스모크 프롬프트 로드 실패(prompts/summarize_video.md 번들 누락 의심 = --add-data)"; exit 1; }
echo "  ✓ MCP 핸드셰이크·도구 2개·프롬프트 실로드"

echo "── [4/6] 번들 yt-dlp.exe sibling 배치(중첩 PyInstaller 회피 — add-binary 아님) ──"
install -m 755 "$ROOT/build/vendor/yt-dlp.exe" "$DIST/yt-dlp.exe" 2>/dev/null \
  || cp "$ROOT/build/vendor/yt-dlp.exe" "$DIST/yt-dlp.exe"

echo "── [5/6] win manifest 패치(python 치환 — 단일 소스·build_macos.sh 미접촉) ──"
rm -rf "$PACK"; mkdir -p "$PACK"
"$VPY" - "$ROOT/manifest.json" "$PACK/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
m["compatibility"]["platforms"] = ["win32"]
m["server"]["entry_point"] = "labyscribe.exe"
m["server"]["mcp_config"]["command"] = "${__dirname}/labyscribe.exe"
m["server"]["mcp_config"]["env"]["YTDLP_PATH"] = "${__dirname}/yt-dlp.exe"
json.dump(m, open(sys.argv[2], "w", encoding="utf-8"), indent=2, ensure_ascii=False)
PY

echo "── [6/6] .mcpb 패키징 ──"
cp -R "$DIST"/. "$PACK/"                 # exe + _internal/ + yt-dlp.exe (manifest 는 [5/6] 것 유지)
npx -y @anthropic-ai/mcpb@2.1.2 validate "$PACK/manifest.json"
npx -y @anthropic-ai/mcpb@2.1.2 pack "$PACK" "$MCPB_OUT"
echo "완료: $MCPB_OUT"
