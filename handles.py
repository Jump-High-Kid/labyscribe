"""labyscribe 핸들 레지스트리 — 순수 인메모리 LRU (stdlib only · mcp/network import 0).

server 가 transcript 파트를 발급하고 불투명 토큰으로 재조회한다. 순수 모듈이므로
SDK·네트워크 없이 결정적 계약 테스트가 돈다(D-J·CK-38).

- 토큰 = `secrets.token_urlsafe(32)` 암호학적 난수 → 위조·경로성분 주입 구조적 불가(CK-30).
- `OrderedDict` LRU + `threading.Lock`(FastMCP 워커스레드 레이스 방어·CK-40).
- Phase 2 = LRU 전용·TTL 없음·프로세스 생애. 재시작=무효(파일 재조회·TTL 은 Phase 3).
- 파일 재오픈 0 = parts 는 인메모리 튜플 → traversal 불가(CK-30).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

MAX_HANDLES = 32  # LRU 상한(YAGNI·고정·D-F)

# HandleEntry.meta 에 불변 투영할 allowlist 필드(발급 후 변경 불가 → 응답 계약 안정·CK-38).
_META_ALLOWLIST = ("title", "uploader", "duration", "orig_lang")


@dataclass(frozen=True)
class HandleEntry:
    """발급된 핸들의 결속 대상. frozen = 불변. meta 는 allowlist 불변 투영."""
    video_id: Optional[str]
    lang: Optional[str]
    content_hash: str
    parts: tuple           # tuple[str, ...] — 인메모리·파일 재오픈 없음
    meta: dict             # allowlist 필드만 복사(가변 참조 유출 0)


def content_hash(text: str) -> str:
    """transcript 내용 sha256 — Phase 3 staleness 감지용 저장(Phase 2 결속은 매핑 자동)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project_meta(meta: dict) -> dict:
    """allowlist 필드만 새 dict 로 복사 — 원본 meta 변경이 엔트리에 새지 않게(CK-38)."""
    return {k: meta.get(k) for k in _META_ALLOWLIST}


class HandleRegistry:
    """불투명 토큰 → HandleEntry LRU. issue/get 만 노출(video_id 조회 API 없음·CK-29)."""

    def __init__(self, max_handles: int = MAX_HANDLES):
        self._max = max_handles
        self._d: "OrderedDict[str, HandleEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def issue(self, video_id, lang, content_hash, parts, meta) -> str:
        """엔트리 발급 → 불투명 토큰 반환. LRU 상한 초과 시 최오래 항목 축출."""
        token = secrets.token_urlsafe(32)
        entry = HandleEntry(video_id, lang, content_hash,
                            tuple(parts), _project_meta(meta))
        with self._lock:
            self._d[token] = entry
            self._d.move_to_end(token)
            while len(self._d) > self._max:
                self._d.popitem(last=False)   # 최오래(LRU) 축출
        return token

    def get(self, token) -> Optional[HandleEntry]:
        """미존재·축출·조작 토큰 → None(CK-30). 조회 성공 시 LRU 갱신."""
        with self._lock:
            entry = self._d.get(token)
            if entry is None:
                return None
            self._d.move_to_end(token)
            return entry
