#!/usr/bin/env bash
# fetch_ytdlp.sh — yt-dlp 바이너리 provenance 검증 다운로드 (macOS universal2 / Windows x64)
# Phase 5·5.x · ADR provenance · 공식 릴리스 SHA256 + GPG 서명(고정 키)을 검증한 통과분만 배치.
#                검증 실패 = 즉시 종료(빌드 중단 · 미검증 바이너리 출하 0).
# 사용: [TARGET_OS=macos|windows] build/fetch_ytdlp.sh [출력디렉토리]   (기본 macos · build/vendor)
#   - macos:   yt-dlp_macos(universal2) → <out>/yt-dlp
#   - windows: yt-dlp.exe(win-x64)      → <out>/yt-dlp.exe
# GPG 공개키 = repo vendor(build/yt-dlp-signing-key.asc) import(keyserver 의존 제거 — G2·win git bash
#             hkps 불안정 회피). VALIDSIG 고정 지문 검증은 불변(임의 good-sig 우회 차단).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# ── 고정값 (보안 재릴리스 시에만 수동 갱신 — 자동 업데이트 금지) ──
TARGET_OS="${TARGET_OS:-macos}"
YTDLP_VERSION="2026.07.04"
YTDLP_KEY_FPR="AC0CBBE6848D6A873464AF4E57CF65933B5A7581"   # Simon Sawicki (yt-dlp signing key)

# OS별 자산·SHA256·출력파일명 (SHA256 = 공식 SHA2-256SUMS 확인·GPG 서명 목록)
case "$TARGET_OS" in
  macos)
    YTDLP_ASSET="yt-dlp_macos"
    YTDLP_OUTBIN="yt-dlp"
    YTDLP_SHA256="498bd0dae17855c599d371d68ec5bafc439a9d8640e838be25c765a9792f261b"
    CHECK_UNIVERSAL2=1 ;;
  windows)
    YTDLP_ASSET="yt-dlp.exe"
    YTDLP_OUTBIN="yt-dlp.exe"
    YTDLP_SHA256="52fe3c26dcf71fbdc85b528589020bb0b8e383155cfa81b64dd447bbe35e24b8"
    CHECK_UNIVERSAL2=0 ;;
  *)
    echo "ERROR: 미지원 TARGET_OS=$TARGET_OS (macos|windows)"; exit 1 ;;
esac

OUT_DIR="${1:-build/vendor}"
KEY_ASC="$HERE/yt-dlp-signing-key.asc"
BASE="https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── preflight (win git bash 존재 계약 명시 — CK-14) ──
command -v gpg    >/dev/null || { echo "ERROR: gpg 없음(PATH 확인)"; exit 1; }
command -v shasum >/dev/null || { echo "ERROR: shasum 없음(PATH 확인)"; exit 1; }
command -v curl   >/dev/null || { echo "ERROR: curl 없음(PATH 확인)"; exit 1; }
[ -f "$KEY_ASC" ] || { echo "ERROR: 서명 공개키 없음: $KEY_ASC"; exit 1; }

echo "[1/4] 다운로드 (${YTDLP_ASSET} ${YTDLP_VERSION} · TARGET_OS=${TARGET_OS})"
curl -fsSL -o "$WORK/$YTDLP_ASSET"     "$BASE/$YTDLP_ASSET"
curl -fsSL -o "$WORK/SHA2-256SUMS"     "$BASE/SHA2-256SUMS"
curl -fsSL -o "$WORK/SHA2-256SUMS.sig" "$BASE/SHA2-256SUMS.sig"

echo "[2/4] GPG 서명 검증 (vendor 공개키 · 고정 지문 ${YTDLP_KEY_FPR})"
# vendor 키 지문이 핀과 일치하는지(위조 .asc 차단) — import 전 show-only 로 확인
gpg --batch --with-colons --import-options show-only --import "$KEY_ASC" \
  | awk -F: '/^fpr:/{print $10}' | grep -qx "$YTDLP_KEY_FPR" \
  || { echo "ERROR: vendor 서명키 지문이 핀과 불일치 — 중단"; exit 1; }
gpg --batch --import "$KEY_ASC"
# --status-fd 로 VALIDSIG 가 '고정 키' 인지까지 확인(임의 good-sig 우회 차단)
gpg --batch --status-fd 1 --verify "$WORK/SHA2-256SUMS.sig" "$WORK/SHA2-256SUMS" \
  | grep -q "VALIDSIG ${YTDLP_KEY_FPR}" \
  || { echo "ERROR: SHA2-256SUMS 서명이 고정 키와 불일치 — 중단"; exit 1; }

echo "[3/4] SHA256 체크섬 검증 (고정값 + 릴리스 목록 이중)"
ACTUAL="$(shasum -a 256 "$WORK/$YTDLP_ASSET" | awk '{print $1}')"
[ "$ACTUAL" = "$YTDLP_SHA256" ] \
  || { echo "ERROR: $YTDLP_ASSET SHA256 불일치 (기대 $YTDLP_SHA256 / 실제 $ACTUAL) — 중단"; exit 1; }
# 자산명 필드 완전일치(awk $2==) — grep 정규식의 '.' 메타문자 오탐 회피(yt-dlp.exe)
( cd "$WORK" && awk -v a="$YTDLP_ASSET" '$2==a' SHA2-256SUMS | shasum -a 256 -c - )

echo "[4/4] 검증 통과분 배치 → $OUT_DIR/$YTDLP_OUTBIN"
mkdir -p "$OUT_DIR"
install -m 755 "$WORK/$YTDLP_ASSET" "$OUT_DIR/$YTDLP_OUTBIN"
if [ "$CHECK_UNIVERSAL2" = 1 ]; then
  # universal2(fat) 확인 — 단일 arch 면 arm64+x64 목표 미달(경고)
  lipo -archs "$OUT_DIR/$YTDLP_OUTBIN" 2>/dev/null | grep -q "x86_64 arm64\|arm64 x86_64" \
    || echo "경고: yt-dlp 가 universal2 가 아님 — arch 확인 필요"
fi
echo "OK: ${YTDLP_ASSET} ${YTDLP_VERSION} provenance(SHA256+GPG vendor) 검증 통과 · $OUT_DIR/$YTDLP_OUTBIN"
