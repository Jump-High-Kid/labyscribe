"""챕터 분할 — 순수 함수 (stdlib only · mcp/network/tkinter import 0).

정제된 cue `(start, text)` 2-튜플 + yt-dlp `chapters` 메타 → 파트 튜플.
cue 는 **start 시각이 속한 챕터**에 귀속(반개구간 `[start, end)`). 중첩·역순·중복
챕터는 정렬-유일 start 경계로 환원해 by-construction 정규화한다.

강제 불변조건(테스트):
- 모든 cue 가 정확히 한 파트에 귀속(무손실·무중복) — `_assign_cues` 순서 보존
- 과대 챕터는 `paging` 재분할(UTF-8 무절단). `part_no` 연속·`chapter_no` 동일
- 순수·raise 금지(손상 메타는 폴백 귀속·부분 복구)
"""
from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from typing import NamedTuple, Optional

import paging

# 렌더 헤더+경계 예약 — render_md 가 헤더를 붙여도 총합 ≤ byte_cap 보장(고정 차감).
HEADER_RESERVE_BYTES = 2 * 1024
# 외부 제목·챕터명 길이 상한(헤더 bounded 보장 — render_md 이스케이프와 함께).
TITLE_MAX_CHARS = 100
# 10분 마커 간격(extract.parse_vtt 와 동일값 — 의도적 복제·각각 독립).
_MARKER_INTERVAL = 600


@dataclass(frozen=True)
class Part:
    part_no: int               # 1..N 연속(전송 순서·파일명·헤더 분모)
    chapter_no: int            # 논리 챕터 서수(과대 챕터 하위파트는 동일값·폴백=0)
    title: Optional[str]       # 챕터명(폴백·챕터無=None)
    start: float               # 파트 시작초(헤더 구간 표시)
    end: float                 # 파트 끝초
    body: str                  # 렌더된 transcript 본문(10분 마커+cue 텍스트·헤더 이전)


class _Seg(NamedTuple):
    chapter_no: int
    title: Optional[str]
    start: float
    end: float
    cues: list                 # [(start, text)]


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _finite(x) -> bool:
    """유한 수치 여부 — inf/NaN/비수치 배제(마커 while 무한루프·bisect 오염 차단)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _finite_span(cues, default: float = 0.0):
    """cue 의 유한 start 최소·최대 → (lo, hi). Part.start/end 유한 보장(render _hms 예외 차단)."""
    finite = [s for s, _ in cues if _finite(s)]
    if not finite:
        return default, default
    return min(finite), max(finite)


def _fmt_ts(sec: float) -> str:
    """초 → HH:MM:SS (extract._fmt_ts 복제 — 순환 import 회피·각각 독립)."""
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


_TS_PREFIX = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*")
_LEAD_SEP = "-–—·|:"                     # 타임스탬프 뒤에 남는 구분자


def clean_chapter_title(title) -> Optional[str]:
    """챕터명 선두의 타임스탬프·이모지·구분자 제거 — 설명란 파싱 산물이라 헤더를 더럽힌다.

    예: `12:56 👁️ 인간이 아닌 웹` → `인간이 아닌 웹`. 「」( 같은 의미 있는 문장부호는
    보존(기호 So/Sk·이모지 변이자 Cf/Mn 만 제거). 순수함수 · raise 금지 · 빈 결과는
    None(챕터無 폴백).
    """
    if not isinstance(title, str):
        return None
    t = _TS_PREFIX.sub("", title)
    i = 0
    while i < len(t) and (t[i].isspace() or t[i] in _LEAD_SEP
                          or unicodedata.category(t[i]) in ("So", "Sk", "Cf", "Mn")):
        i += 1
    return t[i:].strip() or None


def _valid_chapters(chapters_meta) -> list:
    """손상 항목 skip → [(start_time, title), ...] (원 순서 보존·raise 금지)."""
    if not isinstance(chapters_meta, (list, tuple)):
        return []
    out = []
    for c in chapters_meta:
        if not isinstance(c, dict):
            continue
        st = c.get("start_time")
        if not _finite(st):                     # inf/NaN/비수치/bool 배제(int() 예외·정렬 오염)
            continue
        # _finite(st) 가 위에서 유한 수치 보장 → float() 안전(mypy 는 _finite 내로잉 못 봄).
        out.append((float(st), clean_chapter_title(c.get("title"))))  # type: ignore[arg-type]
    return out


def _boundaries(valid):
    """유일 start 경계(오름차순) + 동률 start = 최초 등장 title (고정 tie 규칙)."""
    title_by_start = {}
    for st, title in valid:                 # valid = 원 순서 → 첫 등장이 최초 챕터
        if st not in title_by_start:
            title_by_start[st] = title
    return sorted(title_by_start), title_by_start


def _assign_cues(cues, chapters_meta) -> list:
    """cue 를 챕터 세그먼트에 귀속. 모든 cue 정확히 한 seg(무손실·무중복).

    입력 cue 는 정제 파이프라인(`_dedup_rolling`)이 보장하는 **시간순**을 가정한다.
    챕터 메타 없음/전부 손상 → 전체를 폴백 seg 1개(chapter_no=0·title=None)로.
    seg start/end 는 유한 cue span 으로(비유한 cue 가 render _hms 예외 유발 차단).
    """
    cue_list = list(cues)
    starts, title_by_start = _boundaries(_valid_chapters(chapters_meta))
    if not starts:                          # 폴백 — 전체 한 세그먼트
        if not cue_list:
            return []
        lo, hi = _finite_span(cue_list)
        return [_Seg(0, None, lo, hi, cue_list)]

    groups: dict = {}                       # seg_idx → [(start, text)]
    for s, t in cue_list:
        # 반개구간 [start, end) 귀속. 비유한 start 는 bisect 오염 회피 위해 seg 0 흡수(텍스트 보존).
        idx = (bisect_right(starts, s) - 1) if _finite(s) else 0
        if idx < 0:                         # 첫 경계 이전 → 최근접(seg 0) 흡수
            idx = 0
        groups.setdefault(idx, []).append((s, t))

    segs = []
    for idx in sorted(groups):              # 비어있지 않은 seg 만, start 순
        grp = groups[idx]
        lo, hi = _finite_span(grp, default=starts[idx])
        seg_start = min(starts[idx], lo)    # 흡수된 첫챕터前 cue 반영(헤더 범위 정확)
        seg_end = starts[idx + 1] if idx + 1 < len(starts) else hi
        # chapter_no = 정규화 경계 서수(idx+1) — 빈 챕터 건너뛰어도 논리 번호 유지
        segs.append(_Seg(idx + 1, title_by_start[starts[idx]], seg_start, seg_end, grp))
    return segs


def _render_body(cues) -> str:
    """cue → 10분 마커(`[HH:MM:SS]`) 렌더 본문.

    첫 마커 = **세그먼트 첫 cue 이후의 다음 10분 경계**(이전 구간 마커 반복 방지 —
    파트 헤더가 이미 구간을 표시). start 가 비유한(inf/NaN)이면 마커 skip(무한루프 차단)·
    텍스트만 보존(무손실).
    """
    first = next((s for s, _ in cues if _finite(s)), 0.0)
    next_marker = (int(first) // _MARKER_INTERVAL + 1) * _MARKER_INTERVAL
    lines = []
    for start, text in cues:
        if _finite(start):
            while start >= next_marker:
                lines.append("[%s]" % _fmt_ts(next_marker))
                next_marker += _MARKER_INTERVAL
        lines.append(text)
    return "\n".join(lines)


def split(cues, chapters_meta, byte_cap: int = paging.PART_LIMIT_BYTES) -> tuple:
    """정제 cue + chapters 메타 → `Part` 튜플. 순수·raise 금지.

    body 는 `byte_cap - HEADER_RESERVE_BYTES` 이하(render_md 헤더 여유). 과대 세그먼트는
    `paging.split_transcript` 재분할 — part_no 연속·chapter_no 동일.
    """
    segs = _assign_cues(cues, chapters_meta)
    if not segs:
        return ()

    body_budget = max(1, byte_cap - HEADER_RESERVE_BYTES)   # 음수 예산 방어(paging 무한루프 차단)
    parts = []
    part_no = 0
    for seg in segs:
        body = _render_body(seg.cues)
        chunks = ([body] if _nbytes(body) <= body_budget
                  else paging.split_transcript(body, body_budget))
        for chunk in chunks:
            part_no += 1
            parts.append(Part(part_no, seg.chapter_no, seg.title,
                              seg.start, seg.end, chunk))
    return tuple(parts)
