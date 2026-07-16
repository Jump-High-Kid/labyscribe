"""Phase 6 W2 — 온라인 스모크(gated·비차단).

기본 실행에서는 `conftest.py` 게이트로 자동 deselect.
활성: `LABYSCRIBE_ONLINE=1 pytest -m online`. 실 네트워크·PATH yt-dlp 필요.

skip 범위(codex HIGH·code-reviewer p1·회귀 은폐 금지): **사전 가용성 실패만** skip —
yt-dlp 미존재 · youtube 미도달(TCP). **`run_extract` 호출(실행 시작) 후의 exit≠0
은 DOWNLOAD_FAILED 포함 전부 진짜 회귀로 fail**(진단=message 보존 · download_sub
argv·재시도 로직 회귀 은폐 방지). 순수 네트워크 변동은 사전 connectivity 체크가
흡수하고, 그 이후는 흡수하지 않는다(1·2회차 정책 일관). tmp_path 격리 — `~/labyscribe` 미접촉.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket

import pytest

import extract as E

# Me at the zoo(2005 최초 업로드·삭제위험 최저·영어 수동자막).
SMOKE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SMOKE_VID = "jNQXAC9IVRw"


def _ytdlp_available() -> bool:
    return shutil.which("yt-dlp") is not None


def _connectivity_ok(host: str = "www.youtube.com", port: int = 443,
                     timeout: float = 5.0) -> bool:
    """가벼운 사전 가용성 체크 — TCP 연결만(순수 네트워크 변동 흡수용)."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _skip_if_unavailable() -> None:
    if not _ytdlp_available():
        pytest.skip("yt-dlp 미존재(PATH) — 사전 가용성 실패")
    if not _connectivity_ok():
        pytest.skip("youtube 미도달 — 사전 가용성 실패")


@pytest.mark.online
@pytest.mark.smoke
def test_smoke_real_video_extract_and_cache_hit(tmp_path, monkeypatch):
    _skip_if_unavailable()

    # download_sub seam spy — 실함수 호출은 유지하고 호출 횟수만 계수(캐시 히트 검증).
    real_dl = E.download_sub
    calls = {"n": 0}

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real_dl(*args, **kwargs)
    monkeypatch.setattr(E, "download_sub", spy)

    # 1회차 추출 — 실행 시작 후 exit≠0 은 (DOWNLOAD_FAILED 포함) 전부 fail(진단 보존).
    # 순수 네트워크 변동은 위 _skip_if_unavailable() 사전 체크가 이미 흡수했다.
    r1 = E.run_extract(SMOKE_URL, None, str(tmp_path))
    assert r1.exit_code == E.EXIT_OK, r1.message
    assert r1.transcript and r1.transcript.strip()

    # 버전 디렉토리 <vid>/en-*/ 존재 · meta.json orig_lang
    leaves = glob.glob(os.path.join(str(tmp_path), SMOKE_VID, "en-*"))
    assert len(leaves) == 1, "en-* 버전 디렉토리는 tmp_path 격리상 정확히 1개: %r" % leaves
    with open(os.path.join(leaves[0], "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta.get("orig_lang"), "meta.orig_lang 부재"

    first = calls["n"]
    assert first >= 1                                   # 1회차는 실제 다운로드

    # 2회차 = 캐시 히트 → download_sub 추가 호출 0(자막 재다운로드 스킵)
    r2 = E.run_extract(SMOKE_URL, None, str(tmp_path))
    assert r2.exit_code == E.EXIT_OK, r2.message
    assert calls["n"] == first                          # seam spy: 추가 호출 0
    assert r2.transcript == r1.transcript
