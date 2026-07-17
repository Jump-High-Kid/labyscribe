"""안전 Markdown 렌더 — 순수 함수 (stdlib only · mcp/network/tkinter import 0).

파트를 **자족 헤더 + 동적 경계로 감싼 본문**으로 렌더한다. 외부 메타(영상 제목·
챕터명)는 헤더(경계 밖)에 이스케이프해 넣고, transcript 본문은 동적 경계 토큰으로
감싼다 — 본문이 경계를 닫는 탈출을 차단(완전 방어 불가·§5.3 정직성, 구조 탈출 0).

정직성: 프롬프트 인젝션은 확률적 모델에서 완전 방어 불가. 여기 계약은 "데이터/지시
전달 분리 + 경계 안은 데이터"까지 — 요약 무결성은 챗봇 책임 경계(best-effort).
"""
from __future__ import annotations

import hashlib
import math

from chapters import Part, TITLE_MAX_CHARS

# 헤더(경계 밖)에서 이스케이프할 Markdown 구조 문자.
_MD_SPECIAL = set("\\`*_{}[]()#+-.!|>~")


def _hms(sec) -> str:
    if not (isinstance(sec, (int, float)) and math.isfinite(sec)):
        return "00:00:00"                    # 비유한(inf/NaN) 방어 — 순수층 raise 금지 belt
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _escape(text, max_chars: int = TITLE_MAX_CHARS) -> str:
    """외부 메타 → 헤더 안전 문자열. 개행 제거·길이 절단·Markdown 구조문자 이스케이프.

    비문자열(None·숫자 등)은 빈 문자열(순수·raise 금지). 코드펜스(```)는 백틱 개별
    이스케이프로 함께 무력화된다.
    """
    if not isinstance(text, str):
        return ""
    text = text.replace("\r", " ").replace("\n", " ")[:max_chars]   # 개행→space·절단 먼저
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in text)


def _boundary_token(body: str) -> str:
    """본문에 존재하지 않는 경계 토큰 — hash 유래(결정적) + 충돌 시 확장.

    hash preimage 저항으로 본문이 자기 토큰을 선점 불가. 그럼에도 명시 충돌검사로
    적대적 삽입(본문 내 경계 종료 문자열)까지 회피 → 경계 종료 탈출 0.
    """
    token = "LABYSCRIBE-DATA-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    while token in body:
        token += hashlib.sha256((token + body).encode("utf-8")).hexdigest()[:8]
    return token


def part(p: Part, total_parts: int, video_meta) -> str:
    """자족 헤더 + 동적 경계로 감싼 본문.

    크기는 by construction ≤ byte_cap — 헤더는 `TITLE_MAX_CHARS` bounded, body 는
    chapters 가 `HEADER_RESERVE_BYTES` 차감해 넘긴다. 순수층이라 상한 초과에도 raise 금지
    (belt assert 제거 — 판정은 상위 게이트; 초과는 chapters body budget 버그로 상류에서 잡힘).
    """
    vtitle = _escape((video_meta or {}).get("title") if isinstance(video_meta, dict) else None)
    ctitle = _escape(p.title)
    rng = "%s–%s" % (_hms(p.start), _hms(p.end))
    label = "[%d/%d]" % (p.part_no, total_parts)
    if vtitle:
        label = "「%s」 · %s" % (vtitle, label)

    head = []
    if ctitle:
        head.append("> " + label)
        head.append("> 챕터: 「%s」 · %s" % (ctitle, rng))
    else:
        head.append("> %s · %s" % (label, rng))
    head.append("> (직전 챕터까지의 흐름을 이어서 요약해 주세요)")

    token = _boundary_token(p.body)
    return "%s\n\n<<<%s\n%s\n%s>>>" % ("\n".join(head), token, p.body, token)


def combined(parts, video_meta) -> str:
    """transcript.md 통합본 — 각 파트 렌더를 이어붙임(파트 헤더가 마커 역할·§6.1)."""
    total = len(parts)
    return "\n\n".join(part(p, total, video_meta) for p in parts)
