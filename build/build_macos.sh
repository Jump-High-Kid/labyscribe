#!/usr/bin/env bash
# build_macos.sh — labyscribe macOS universal2 .mcpb 빌드 (Phase 5)
#
# 산출: labyscribe.mcpb (Claude Desktop 원클릭 설치용 · PyInstaller onedir binary + 번들 yt-dlp)
# 요구: ① python.org universal2 Python 3.12  ② node/npx  ③ 인터넷  ④ macOS(arm64 또는 x64)
#   - Homebrew/uv 파이썬은 per-arch(단일 아키텍처)라 universal2 빌드 불가 → python.org 인스톨러 필요:
#     https://www.python.org/downloads/  (macOS 64-bit universal2 installer)
#
# 빌드타임 전용 도구(런타임 의존성 아님): PyInstaller · delocate · @anthropic-ai/mcpb(npx).
# universal2 = pydantic-core·rpds-py 의 arm64/x86_64 thin wheel 을 delocate-fuse 로 융합(네이티브
#   universal2 wheel 부재 — R2 실측). 융합 실패 시 arch-split(2 .mcpb) 폴백은 하단 주석 참조.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYBUILD="${PYBUILD:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"
BUILD_VENV="$ROOT/.venv-build"
DIST="$ROOT/dist/labyscribe"          # PyInstaller onedir 출력
PACK="$ROOT/dist/pack"                # mcpb pack 스테이징(manifest + 바이너리)
MCPB_OUT="$ROOT/labyscribe.mcpb"

echo "── [0/7] 빌드 파이썬 preflight (universal2 필수) ──"
command -v "$PYBUILD" >/dev/null || {
  echo "ERROR: universal2 Python 3.12 없음: $PYBUILD"
  echo "       https://www.python.org/downloads/ 의 macOS universal2 인스톨러 설치 후 재시도"
  echo "       (다른 경로면 PYBUILD=/path/to/python3 build/build_macos.sh)"; exit 1; }
file "$(readlink -f "$PYBUILD" 2>/dev/null || echo "$PYBUILD")" | grep -q "2 architectures" || {
  echo "ERROR: $PYBUILD 가 universal2(fat) 가 아님 → --target-arch universal2 불가"; exit 1; }
command -v npx >/dev/null || { echo "ERROR: node/npx 없음"; exit 1; }

echo "── [1/7] 빌드 venv 생성 + 도구 설치 ──"
rm -rf "$BUILD_VENV"
"$PYBUILD" -m venv "$BUILD_VENV"
VPY="$BUILD_VENV/bin/python"
"$VPY" -m pip install -U pip wheel delocate pyinstaller pyinstaller-hooks-contrib
"$VPY" -m pip install .                       # 런타임 의존성(mcp 등) — 이 시점 thin(arm64) wheel

echo "── [2/7] Rust 확장(pydantic-core·rpds-py) thin→universal2 융합 ──"
PC_VER="$("$VPY" -m pip show pydantic-core | awk '/^Version/{print $2}')"
RP_VER="$("$VPY" -m pip show rpds-py       | awk '/^Version/{print $2}')"
WHEELS="$ROOT/dist/wheels"; rm -rf "$WHEELS"; mkdir -p "$WHEELS/fused"
for spec in "pydantic-core==$PC_VER" "rpds-py==$RP_VER"; do
  for plat in macosx_11_0_arm64 macosx_10_12_x86_64; do
    "$VPY" -m pip download --only-binary=:all: --no-deps \
      --platform "$plat" --python-version 3.12 --implementation cp --abi cp312 \
      -d "$WHEELS/$plat" "$spec"
  done
done
# delocate-fuse: arm64 + x86_64 thin → universal2 wheel
for pkg in pydantic_core rpds_py; do
  A="$(ls "$WHEELS"/macosx_11_0_arm64/${pkg}-*.whl)"
  X="$(ls "$WHEELS"/macosx_10_12_x86_64/${pkg}-*.whl)"
  "$VPY" -m delocate.cmd.delocate_fuse "$A" "$X" -w "$WHEELS/fused"
done
"$VPY" -m pip install --force-reinstall --no-deps "$WHEELS"/fused/*.whl

echo "── [3/7] yt-dlp provenance 검증 다운로드(SHA256+GPG) ──"
bash "$ROOT/build/fetch_ytdlp.sh" "$ROOT/build/vendor"

echo "── [4/7] PyInstaller onedir universal2 ──"
rm -rf "$ROOT/build/pyi" "$ROOT/dist"
"$VPY" -m PyInstaller --noconfirm --clean \
  --name labyscribe --onedir --target-arch universal2 \
  --distpath "$ROOT/dist" --workpath "$ROOT/build/pyi" --specpath "$ROOT/build/pyi" \
  --add-data "$ROOT/prompts:prompts" \
  --collect-all mcp --collect-all pydantic --collect-all pydantic_core \
  --copy-metadata mcp --copy-metadata pydantic \
  "$ROOT/server.py"
# 재현 시 hidden-import 누락 발견: --debug=imports 로 스모크 실행 후 --hidden-import 보강.

echo "── [5/7] 번들 yt-dlp sibling 배치(중첩 PyInstaller 회피 — add-binary 아님) ──"
install -m 755 "$ROOT/build/vendor/yt-dlp" "$DIST/yt-dlp"

echo "── [6/7] universal2(fat) 검증 ──"
BAD=0
for f in "$DIST/labyscribe" "$DIST/yt-dlp" $(find "$DIST" -name "*.so"); do
  if ! lipo -archs "$f" 2>/dev/null | grep -q "x86_64 arm64\|arm64 x86_64"; then
    echo "  ✗ 단일 arch: $f ($(lipo -archs "$f" 2>/dev/null))"; BAD=1
  fi
done
[ "$BAD" = 0 ] && echo "  ✓ 전 바이너리 universal2" || {
  echo "ERROR: 일부 바이너리가 universal2 아님 → arch-split(2 .mcpb) 폴백 필요(하단 주석)"; exit 1; }

echo "── [7/7] .mcpb 패키징 ──"
rm -rf "$PACK"; mkdir -p "$PACK"
cp "$ROOT/manifest.json" "$PACK/"
cp -R "$DIST"/. "$PACK/"
npx -y @anthropic-ai/mcpb@2.1.2 validate "$PACK/manifest.json"
npx -y @anthropic-ai/mcpb@2.1.2 pack "$PACK" "$MCPB_OUT"
echo "완료: $MCPB_OUT"
lipo -archs "$DIST/labyscribe"

# ── arch-split 폴백(universal2 융합 실패 시) ────────────────────────────
# delocate-fuse/PyInstaller universal2 가 불가하면 arch별 2 아티팩트로 배포:
#   arm64:  네이티브 python.org arm64 로 --target-arch arm64  → labyscribe-arm64.mcpb
#   x86_64: arch -x86_64 python.org x86_64 로 --target-arch x86_64 → labyscribe-x86_64.mcpb
# (사용자가 Apple Silicon/Intel 에 맞는 .mcpb 선택. platform_overrides 는 OS 단위라 arch 분기 불가.)
