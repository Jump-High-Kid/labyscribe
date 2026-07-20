"""labyscribe MCP stdio 서버 (FastMCP) — 추출·페이징·프롬프트 계약 표면.

호스트(Claude Desktop 등 사용자 구독)가 도구를 자율 호출 → 스스로 요약한다.
labyscribe 는 요약을 하지 않는다("무료 메커니즘"). 이 서버는 결정론적 추출 코어
(`extract.run_extract`)를 감싸 구조화 스키마·불투명 핸들·페이징·에러 계약만 제공한다.

원칙:
- 툴 본체 = plain 함수(`_do_extract`·`_do_get_part`)의 얇은 데코레이터 래퍼 →
  계약 테스트가 SDK 우회로 직접 호출(D-J).
- 응답은 `_assemble` allowlist 8필드 투영 → 절대경로·내부 경로키 미노출(CK-27).
- env 결합(`OUTPUT_DIR`)은 `_resolve_output_dir` 경계에만(D-E).
- 순수코어(paging·handles) vs I/O셸(server·extract) 경계.

자원 한도 상수(backstop·확정 · 단일사용자 로컬 위협모델 기준 · 근거표 = design_intend-phase6 §W3):
- MAX_URL_LEN: 입력 URL 길이 상한(비정상 입력 차단).
- MAX_SUBTITLE_BYTES: server 가 페이징·반환할 transcript 최대 바이트(메모리/폭주 방지).
- MAX_PARTS: 페이징 파트 수 상한(위 바이트 상한의 백스톱).
- 다운로드 subprocess 타임아웃은 extract.DOWNLOAD_TIMEOUT_SEC(무한 블로킹 차단).
"""
from __future__ import annotations

import os
import sys
import traceback
from functools import lru_cache
from typing import Optional
from urllib.parse import parse_qs, urlparse

from mcp.server.fastmcp import FastMCP

import storage
from extract import (
    DOWNLOAD_TIMEOUT_SEC,
    EXIT_BAD_INPUT,
    EXIT_DOWNLOAD_FAILED,
    EXIT_EMPTY_TRANSCRIPT,
    EXIT_NO_SUBTITLE,
    EXIT_OK,
    EXIT_STORAGE_LIMIT,
    EXIT_SUBTITLE_TOO_LARGE,
    EXIT_UNAVAILABLE,
    _ytdlp_bin,
    run_extract,
)
from handles import HandleRegistry, content_hash
from paging import PART_LIMIT_BYTES, split_transcript

# ── 자원 한도(backstop·확정·근거표 W3) ────────────────────────────
MAX_URL_LEN = 2048                       # 표준 URL 길이 sanity 상한
MAX_SUBTITLE_BYTES = 4 * 1024 * 1024     # 서버가 다루는 transcript 최대 4MB(backstop·근거표 W3)
MAX_PARTS = 256                          # 페이징 파트 수 상한(backstop·근거표 W3)

# _assemble 응답 오버헤드(handle·title·channel·JSON 구조) 여유 차감 — transcript 를
# PART_LIMIT_BYTES 그대로 담으면 필드·직렬화가 얹혀 실제 반환이 상한을 넘는다(CK-28·D-G).
# transcript 예산 = 파트 상한 − 오버헤드(backstop·근거표 W3).
_RESPONSE_OVERHEAD_BYTES = 4 * 1024
_TRANSCRIPT_PART_BUDGET = PART_LIMIT_BYTES - _RESPONSE_OVERHEAD_BYTES

_DEFAULT_OUTPUT_DIR = "~/labyscribe"

# 서버 시작 시 stale temp 정리 임계 — 최대 추출시간(타임아웃 × (재시도+1)) 여유. 이보다
# 오래된 <root>/.tmp/* 만 삭제(라이브 temp 보존·codex HIGH). backstop·확정(근거표 W3).
_STALE_TEMP_MAX_AGE_SEC = DOWNLOAD_TIMEOUT_SEC * 8

# exit code → 구조화 에러 code (D-H). 미분류는 UNKNOWN_DOWNLOAD_FAILURE 폴백.
_EXIT_TO_CODE = {
    EXIT_NO_SUBTITLE: "NO_SUBTITLE",
    EXIT_DOWNLOAD_FAILED: "DOWNLOAD_FAILED",
    EXIT_UNAVAILABLE: "VIDEO_UNAVAILABLE",
    EXIT_BAD_INPUT: "BAD_INPUT",
    EXIT_EMPTY_TRANSCRIPT: "EMPTY_TRANSCRIPT",
    EXIT_STORAGE_LIMIT: "STORAGE_LIMIT_EXCEEDED",
    EXIT_SUBTITLE_TOO_LARGE: "SUBTITLE_TOO_LARGE",
}

# 채널/재생목록 경로 조각(단일 영상만 허용·자원 고갈 차단·D-K).
_PLAYLIST_PATH_HINTS = ("/playlist", "/channel/", "/@", "/c/", "/user/", "/results")

_registry = HandleRegistry()

mcp = FastMCP("labyscribe")


# ── 경계 헬퍼 ──────────────────────────────────────────────────

def _resolve_output_dir() -> str:
    """저장 루트 — env OUTPUT_DIR 우선, 미설정 시 ~/labyscribe(D-E). env 결합은 여기만."""
    return os.environ.get("OUTPUT_DIR") or os.path.expanduser(_DEFAULT_OUTPUT_DIR)


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _validate_input(url: str) -> Optional[dict]:
    """최소 안전 입력 검증(SSRF 는 run_extract.validate_url). 위반 시 error dict."""
    if not isinstance(url, str) or not url:
        return _err("BAD_INPUT", "URL 이 비어 있습니다.")
    if len(url) > MAX_URL_LEN:
        return _err("BAD_INPUT", "URL 길이 상한(%d)을 초과했습니다." % MAX_URL_LEN)
    try:                                   # malformed URL(예: https://[::1)도 계약 안으로(CK-31)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except ValueError:
        return _err("BAD_INPUT", "URL 형식이 올바르지 않습니다.")
    if "list" in query:
        return _err("PLAYLIST_UNSUPPORTED",
                    "재생목록 URL 은 지원하지 않습니다(단일 영상만).")
    path = parsed.path.lower()
    if any(seg in path for seg in _PLAYLIST_PATH_HINTS):
        return _err("PLAYLIST_UNSUPPORTED",
                    "채널/재생목록 URL 은 지원하지 않습니다(단일 영상만).")
    return None


def _map_error(exit_code: int, message: str) -> dict:
    """exit code → 구조화 에러. message = run_extract 의 정제 메시지(절대경로 없음·CK-31)."""
    return _err(_EXIT_TO_CODE.get(exit_code, "UNKNOWN_DOWNLOAD_FAILURE"), message)


def _assemble(handle: str, meta: dict, parts, part_index: int) -> dict:
    """allowlist 8필드 투영 — AC-1 절대경로 차단점. 경로키(raw_vtt 등) 미노출(CK-27)."""
    return {
        "transcript_handle": handle,
        "title": meta.get("title"),
        "channel": meta.get("uploader"),      # 명시 매핑 uploader → channel(CK-26)
        "duration": meta.get("duration"),
        "orig_lang": meta.get("orig_lang"),
        "total_parts": len(parts),
        "part_index": part_index,
        "transcript": parts[part_index - 1],  # 1-based
    }


# ── 툴 본체(plain) — SDK 우회 계약 테스트 표적 ──────────────────

def _do_extract(url: str, lang: Optional[str] = None) -> dict:
    # 1) yt-dlp preflight(run_extract 밖·AC-5·테스트 결정성) — 해석-인지(번들 회귀차단·CK-2)
    if not _ytdlp_ready():
        return _err("TOOLING_MISSING",
                    "yt-dlp 실행 파일을 찾을 수 없습니다. 설치 후 재시도.")
    # 2) 입력 검증(단일 영상·길이)
    input_err = _validate_input(url)
    if input_err is not None:
        return input_err
    # 3) 추출 — 저장 루트 전달(Phase 3·D3-D). temp/final 격리·원자 발행·총량상한·캐시조회는
    #    run_extract 가 storage 로 스스로 관리(video_id 사전미지라 dir 사전결정 불가).
    try:
        result = run_extract(url, lang, _resolve_output_dir())
    except OSError:
        traceback.print_exc(file=sys.stderr)
        return _err("OUTPUT_WRITE_FAILED", "출력 저장 중 오류가 발생했습니다.")
    except Exception:                              # 미분류 예외만 UNKNOWN(CK-31)
        traceback.print_exc(file=sys.stderr)
        return _err("UNKNOWN_DOWNLOAD_FAILURE", "예기치 못한 추출 실패.")
    if result.exit_code != EXIT_OK:
        return _map_error(result.exit_code, result.message)
    # 5) 자원 한도: transcript 바이트·파트 수 상한 → 구조화 에러(CK-37)
    transcript = result.transcript or ""
    if len(transcript.encode("utf-8")) > MAX_SUBTITLE_BYTES:
        return _err("TRANSCRIPT_TOO_LARGE",
                    "자막이 최대 처리 크기(%d bytes)를 초과했습니다." % MAX_SUBTITLE_BYTES)
    parts = split_transcript(transcript, _TRANSCRIPT_PART_BUDGET)   # 오버헤드 차감(CK-28)
    if len(parts) > MAX_PARTS:
        return _err("TRANSCRIPT_TOO_LARGE",
                    "자막 파트 수가 상한(%d)을 초과했습니다." % MAX_PARTS)
    # 6) 핸들 발급 → part 1 반환
    handle = _registry.issue(result.meta.get("id"), result.meta.get("lang"),
                             content_hash(transcript), parts, result.meta)
    return _assemble(handle, result.meta, parts, 1)


def _do_get_part(transcript_handle: str, part: int) -> dict:
    entry = _registry.get(transcript_handle)
    if entry is None:
        return _err("INVALID_HANDLE", "유효하지 않거나 만료된 핸들입니다.")
    if not isinstance(part, int) or part < 1 or part > len(entry.parts):
        return _err("PART_OUT_OF_RANGE",
                    "part 는 1..%d 범위여야 합니다." % len(entry.parts))
    return _assemble(transcript_handle, entry.meta, entry.parts, part)


def _shutil_which(cmd: str):
    # 얇은 래퍼 — 테스트에서 monkeypatch 하기 쉽게 분리(preflight 결정성).
    import shutil
    return shutil.which(cmd)


def _ytdlp_ready() -> bool:
    """yt-dlp 실행 가능 여부 — 해석된 경로(_ytdlp_bin) 기준. raise 금지.

    절대경로(번들 env/frozen 인접) → 존재확인 / 비절대("yt-dlp" 소스 폴백) → PATH.
    번들 실행 시 yt-dlp 는 PATH 에 없으므로 `_shutil_which("yt-dlp")` 만으론 오탐 거부(CK-2).
    """
    p = _ytdlp_bin()
    if os.path.isabs(p):
        return os.path.exists(p)
    return _shutil_which(p) is not None


def _resource_dir() -> str:
    """번들 리소스 base — frozen(PyInstaller)이면 _MEIPASS, 아니면 소스 dir. raise 금지."""
    src = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", src)
    return src


@lru_cache(maxsize=1)
def _load_summary_prompt() -> str:
    # 부재 = FileNotFoundError 전파(삼키지 않음). 빈/공백-only = 배포 결함(번들 손상)이므로
    # fail-fast raise — "" 반환은 summarize_video() 가 조용히 빈 프롬프트를 호스트에 넘겨
    # silent-failure(webapp M4 대칭·이 v1 MCP 경로엔 그동안 가드가 없었다).
    path = os.path.join(_resource_dir(), "prompts", "summarize_video.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        raise RuntimeError("요약 프롬프트가 비어 있습니다: %s" % path)
    return text


# ── MCP 프리미티브 등록(얇은 래퍼) ──────────────────────────────

@mcp.tool()
def extract_transcript(url: str, lang: Optional[str] = None) -> dict:
    """유튜브 URL 에서 자막 transcript 를 추출한다(원어 우선·요약은 호스트가 수행).

    긴 영상은 페이징된다: total_parts>1 이면 첫 part 를 반환하고
    나머지는 get_transcript_part(transcript_handle, k) 로 순차 조회한다.
    """
    return _do_extract(url, lang)


@mcp.tool()
def get_transcript_part(transcript_handle: str, part: int) -> dict:
    """페이징된 transcript 의 k 번째(1-based) part 를 조회한다."""
    return _do_get_part(transcript_handle, part)


@mcp.prompt()
def summarize_video() -> str:
    """추출 transcript 를 요약하기 위한 지시(무손실 편집자·인젝션 펜스·페이징 대응)."""
    return _load_summary_prompt()


def main() -> None:
    """MCP stdio 서버 기동 — 프롬프트 번들 preflight(M4 대칭) 후 서빙.

    __main__ 에서 분리 = 배선 순서(preflight → cleanup → mcp.run)를 테스트로 고정 가능
    (webapp.main 대칭). preflight 없이 mcp.run 에 도달하면 빈 프롬프트가 silent 로 서빙됨.
    """
    # preflight — 프롬프트 번들 누락/빈파일이면 기동 실패(부재/빈파일=raise·M4 대칭).
    # 서버가 도구를 서빙하기 전에 배포 결함을 부트에서 드러낸다(silent-failure 0).
    _load_summary_prompt()
    # 시작 시 오래된 stale temp 정리(라이브 temp 는 age-based 로 보존·D3-F).
    storage.cleanup_stale_temp(_resolve_output_dir(), _STALE_TEMP_MAX_AGE_SEC)
    mcp.run()   # stdio 기본


if __name__ == "__main__":
    main()
