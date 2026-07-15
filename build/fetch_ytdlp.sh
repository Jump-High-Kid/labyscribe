#!/usr/bin/env bash
# fetch_ytdlp.sh — yt-dlp macOS(universal2) 바이너리 provenance 검증 다운로드
# Phase 5 · ADR-C · CK-7: 공식 릴리스 SHA256 + GPG 서명(고정 키)을 검증한 통과분만 배치.
#                        검증 실패 = 즉시 종료(빌드 중단 · 미검증 바이너리 출하 0).
# 사용: build/fetch_ytdlp.sh [출력디렉토리]   (기본 build/vendor)
set -euo pipefail

# ── 고정값 (보안 재릴리스 시에만 수동 갱신 — 자동 업데이트 금지) ──
YTDLP_VERSION="2026.03.17"
YTDLP_KEY_FPR="AC0CBBE6848D6A873464AF4E57CF65933B5A7581"   # Simon Sawicki (yt-dlp signing key)
YTDLP_SHA256="e80c47b3ce712acee51d5e3d4eace2d181b44d38f1942c3a32e3c7ff53cd9ed5"  # yt-dlp_macos

OUT_DIR="${1:-build/vendor}"
BASE="https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[1/4] 다운로드 (yt-dlp_macos ${YTDLP_VERSION})"
curl -fsSL -o "$WORK/yt-dlp_macos"     "$BASE/yt-dlp_macos"
curl -fsSL -o "$WORK/SHA2-256SUMS"     "$BASE/SHA2-256SUMS"
curl -fsSL -o "$WORK/SHA2-256SUMS.sig" "$BASE/SHA2-256SUMS.sig"

echo "[2/4] GPG 서명 검증 (고정 지문 ${YTDLP_KEY_FPR})"
gpg --batch --keyserver hkps://keys.openpgp.org --recv-keys "$YTDLP_KEY_FPR"
# --status-fd 로 VALIDSIG 가 '고정 키' 인지까지 확인(임의 good-sig 우회 차단)
gpg --batch --status-fd 1 --verify "$WORK/SHA2-256SUMS.sig" "$WORK/SHA2-256SUMS" \
  | grep -q "VALIDSIG ${YTDLP_KEY_FPR}" \
  || { echo "ERROR: SHA2-256SUMS 서명이 고정 키와 불일치 — 중단"; exit 1; }

echo "[3/4] SHA256 체크섬 검증 (고정값 + 릴리스 목록 이중)"
ACTUAL="$(shasum -a 256 "$WORK/yt-dlp_macos" | awk '{print $1}')"
[ "$ACTUAL" = "$YTDLP_SHA256" ] \
  || { echo "ERROR: yt-dlp_macos SHA256 불일치 (기대 $YTDLP_SHA256 / 실제 $ACTUAL) — 중단"; exit 1; }
( cd "$WORK" && grep ' yt-dlp_macos$' SHA2-256SUMS | shasum -a 256 -c - )

echo "[4/4] 검증 통과분 배치 → $OUT_DIR/yt-dlp"
mkdir -p "$OUT_DIR"
install -m 755 "$WORK/yt-dlp_macos" "$OUT_DIR/yt-dlp"
# universal2(fat) 확인 — 단일 arch 면 arm64+x64 목표 미달(경고)
lipo -archs "$OUT_DIR/yt-dlp" 2>/dev/null | grep -q "x86_64 arm64\|arm64 x86_64" \
  || echo "경고: yt-dlp 가 universal2 가 아님 — arch 확인 필요"
echo "OK: yt-dlp ${YTDLP_VERSION} provenance(SHA256+GPG) 검증 통과 · $OUT_DIR/yt-dlp"
