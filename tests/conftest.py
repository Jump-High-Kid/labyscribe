"""Phase 6 W0 — 온라인 테스트 게이트(env-only).

`online` 마커가 붙은 테스트는 기본 실행에서 자동 deselect 된다.
env `LABYSCRIBE_ONLINE=1` 일 때만 수집에 포함(실 네트워크·yt-dlp 필요).
`--run-online` addoption 은 미도입 — testpaths+conftest 조합에서 안 먹는 footgun.
기존 결정적 테스트는 online 마크 0 → deselect 대상 0(무영향).
"""
from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    if os.environ.get("LABYSCRIBE_ONLINE") == "1":
        return
    kept, deselected = [], []
    for item in items:
        (deselected if "online" in item.keywords else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
