"""labyscribe v2 결과·capability 레지스트리 — 순수 인메모리 (stdlib only · mcp/network 0).

webapp 이 추출 결과(파트 레코드)와 승인 폴더(capability)를 **불투명 id** 로 보관·재조회.
handles.py 패턴 계승(OrderedDict LRU + threading.Lock + secrets 난수) — v1 handles 미접촉.

- id = `secrets.token_urlsafe(32)` 암호학적 난수 → 위조·열거 구조적 불가.
- capability.root(절대경로)는 **내부 전용** — 외부엔 display_name 만(경로 미노출).
- 프로세스 생애 인메모리(재시작=무효·재추출로 복구).
"""
from __future__ import annotations

import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

MAX_RESULTS = 32
MAX_CAPABILITIES = 16

# video_meta 에서 응답에 투영할 allowlist(신뢰경계 밖 필드 과대 노출 차단).
_VIDEO_META_ALLOWLIST = ("title", "uploader", "duration", "duration_string",
                         "orig_lang", "url")


def _project_video_meta(meta: dict) -> dict:
    return {k: meta.get(k) for k in _VIDEO_META_ALLOWLIST}


@dataclass(frozen=True)
class ResultEntry:
    result_id: str
    title: Optional[str]
    parts: tuple            # tuple[dict] — {part_no,chapter_no,title,markdown,bytes}
    video_meta: dict        # allowlist 투영(가변 참조 유출 0)


@dataclass(frozen=True)
class CapabilityEntry:
    capability_id: str
    root: str               # 절대경로 — 내부 전용(외부 미노출)
    display_name: str       # basename — 프론트 표시용


class ResultRegistry:
    """result_id → ResultEntry LRU. issue/get 만 노출(video_id 역조회 API 없음)."""

    def __init__(self, max_results: int = MAX_RESULTS):
        self._max = max_results
        self._d: "OrderedDict[str, ResultEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def issue(self, title, parts, video_meta) -> str:
        rid = secrets.token_urlsafe(32)
        # 파트 dict 얕은 복사 → 호출자 원본 변조 격리(값은 str/int/None 불변).
        entry = ResultEntry(rid, title, tuple(dict(p) for p in parts),
                            _project_video_meta(video_meta or {}))
        with self._lock:
            self._d[rid] = entry
            self._d.move_to_end(rid)
            while len(self._d) > self._max:
                self._d.popitem(last=False)
        return rid

    def get(self, rid) -> Optional[ResultEntry]:
        with self._lock:
            entry = self._d.get(rid)
            if entry is None:
                return None
            self._d.move_to_end(rid)
            return entry


class CapabilityRegistry:
    """capability_id → CapabilityEntry LRU. register/get. root 는 내부 전용."""

    def __init__(self, max_caps: int = MAX_CAPABILITIES):
        self._max = max_caps
        self._d: "OrderedDict[str, CapabilityEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def register(self, abs_path: str) -> CapabilityEntry:
        cid = secrets.token_urlsafe(32)
        display = os.path.basename(abs_path.rstrip(os.sep)) or abs_path
        entry = CapabilityEntry(cid, abs_path, display)
        with self._lock:
            self._d[cid] = entry
            self._d.move_to_end(cid)
            while len(self._d) > self._max:
                self._d.popitem(last=False)
        return entry

    def get(self, cid) -> Optional[CapabilityEntry]:
        with self._lock:
            entry = self._d.get(cid)
            if entry is None:
                return None
            self._d.move_to_end(cid)
            return entry
