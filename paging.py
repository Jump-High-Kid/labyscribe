"""labyscribe transcript 페이징 — 순수 함수 (stdlib only · mcp/network import 0).

바이트 상한으로 transcript 를 파트로 분할. 10분 마커(`[HH:MM:SS]`) 경계를 우선
단위로 그리디 누적하고, 단일 마커 구간이 상한을 넘으면 라인→문자 경계로 재분할한다.

강제 불변식(테스트):
- 각 파트 `.encode("utf-8")` ≤ limit_bytes (바이트 측정·한글 3B)
- UTF-8 코드포인트 무절단(문자 단위 재분할)
- `"".join(parts) == transcript` (무손실 재구성) — 개행 포함 atom 을 순서 보존 분할
- 전량이 상한 이하면 `[transcript]`(파트 1개). part_index 는 server 가 1-based.
"""
from __future__ import annotations

import re

# 파트 바이트 상한 — D-G 잠정값. 호스트별 실측·매트릭스는 Phase 6. 설정 가능성=YAGNI(고정).
PART_LIMIT_BYTES = 48 * 1024

# 10분 마커 = 대괄호 안 HH:MM:SS 만인 줄. `[Music]`·`[Applause]` 등 음향이벤트는 마커 아님.
_MARKER_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]$")


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _is_marker(atom: str) -> bool:
    return _MARKER_RE.match(atom.rstrip("\n")) is not None


def _sections(atoms):
    """atom 리스트 → 섹션 리스트. 각 섹션 = 마커 줄부터 다음 마커 직전까지.

    선행 블록(첫 마커 이전)도 하나의 섹션. atom 은 개행 포함이라 순서만 지키면
    concat 무손실. 섹션은 파트 경계 후보(마커 = 10분 구간 경계).
    """
    sections, cur = [], []
    for atom in atoms:
        if _is_marker(atom) and cur:
            sections.append(cur)
            cur = []
        cur.append(atom)
    if cur:
        sections.append(cur)
    return sections


def _split_oversized_atom(atom: str, limit_bytes: int):
    """단일 줄(+개행)이 상한 초과 → 문자 경계로 재분할(UTF-8 코드포인트 무절단)."""
    chunks, cur, cur_b = [], "", 0
    for ch in atom:
        cb = _nbytes(ch)
        if cur and cur_b + cb > limit_bytes:
            chunks.append(cur)
            cur, cur_b = "", 0
        cur += ch                 # 단일 문자(≤4B)는 항상 통째 — 코드포인트 무절단
        cur_b += cb
    if cur:
        chunks.append(cur)
    return chunks


def _split_big_section(atoms, limit_bytes: int):
    """상한 초과 섹션을 줄 경계로 그리디 분할. 단일 줄 초과 시 문자 재분할."""
    parts, cur, cur_b = [], [], 0
    for atom in atoms:
        pieces = ([atom] if _nbytes(atom) <= limit_bytes
                  else _split_oversized_atom(atom, limit_bytes))
        for piece in pieces:
            pb = _nbytes(piece)
            if cur and cur_b + pb > limit_bytes:
                parts.append("".join(cur))
                cur, cur_b = [], 0
            cur.append(piece)
            cur_b += pb
    if cur:
        parts.append("".join(cur))
    return parts


def split_transcript(transcript: str, limit_bytes: int = PART_LIMIT_BYTES):
    """transcript → 파트 리스트. 무손실(`"".join(parts) == transcript`)·각 파트 ≤ 상한."""
    if _nbytes(transcript) <= limit_bytes:      # 패스트패스
        return [transcript]

    atoms = transcript.splitlines(keepends=True)   # 개행 포함 → concat 무손실
    parts, cur, cur_b = [], [], 0
    for sec in _sections(atoms):
        sec_str = "".join(sec)
        sec_b = _nbytes(sec_str)
        if sec_b > limit_bytes:                  # 단일 섹션 초과 → 줄/문자 재분할
            if cur:
                parts.append("".join(cur))
                cur, cur_b = [], 0
            parts.extend(_split_big_section(sec, limit_bytes))
            continue
        if cur and cur_b + sec_b > limit_bytes:  # 다음 섹션이 안 맞으면 마커 경계에서 끊음
            parts.append("".join(cur))
            cur, cur_b = [], 0
        cur.extend(sec)
        cur_b += sec_b
    if cur:
        parts.append("".join(cur))
    return parts
