"""server.py 계약 테스트 + AC-7 SDK 스모크 (run_extract monkeypatch·네트워크 무의존).

계약: CK-26(스키마 8필드)·CK-27(절대경로 미반환)·CK-28(페이징)·CK-29(핸들 결속)·
CK-30(핸들 거부)·CK-31(에러 매핑·미노출)·CK-36(입력검증)·CK-37(자원한도·격리).
통합: CK-33(핸드셰이크 + SDK 경유 성공1·에러1·페이징·JSON 직렬화).
"""
import asyncio
import inspect
import json

import pytest

import paging
import server as S
from extract import (
    EXIT_BAD_INPUT,
    EXIT_DOWNLOAD_FAILED,
    EXIT_EMPTY_TRANSCRIPT,
    EXIT_NO_SUBTITLE,
    EXIT_OK,
    EXIT_STORAGE_LIMIT,
    EXIT_SUBTITLE_TOO_LARGE,
    EXIT_UNAVAILABLE,
    ExtractResult,
)
from handles import HandleRegistry

VALID_URL = "https://youtu.be/vidOK"
_SCHEMA_KEYS = {"transcript_handle", "title", "channel", "duration",
                "orig_lang", "total_parts", "part_index", "transcript"}


def _ok_meta(vid="vidOK", title="제목", uploader="채널", duration=5,
             orig_lang="en", lang="en", **extra):
    m = {"id": vid, "title": title, "uploader": uploader, "duration": duration,
         "orig_lang": orig_lang, "lang": lang}
    m.update(extra)
    return m


def _ok_result(transcript="hello real transcript world", **mkw):
    return ExtractResult(EXIT_OK, "OK", _ok_meta(**mkw), transcript)


def _prep(monkeypatch, tmp_path, extract_fn, which="/usr/bin/yt-dlp"):
    """which·OUTPUT_DIR·run_extract·fresh registry 를 격리 설정."""
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(S, "_shutil_which", lambda c: which)
    monkeypatch.setattr(S, "run_extract", extract_fn)
    monkeypatch.setattr(S, "_registry", HandleRegistry())


# ── CK-26 스키마 8필드 + CK-27 절대경로 미반환 ──────────────────

def test_extract_returns_exact_8_field_schema(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result())
    r = S._do_extract(VALID_URL)
    assert set(r.keys()) == _SCHEMA_KEYS         # 정확히 8필드
    assert r["channel"] == "채널"                # uploader → channel 명시 매핑
    assert r["title"] == "제목" and r["orig_lang"] == "en"
    assert r["part_index"] == 1 and r["total_parts"] == 1


def test_no_absolute_path_in_response(monkeypatch, tmp_path):
    # meta 에 경로키가 있어도 응답엔 새지 않음(allowlist 투영)
    result = ExtractResult(EXIT_OK, "OK",
                           _ok_meta(raw_vtt="/Users/secret/out/raw/x.vtt",
                                    transcript="transcript.txt",
                                    url="https://youtu.be/vidOK"),
                           "본문")
    _prep(monkeypatch, tmp_path, lambda u, l, o: result)
    r = S._do_extract(VALID_URL)
    blob = json.dumps(r, ensure_ascii=False)
    assert "/Users" not in blob
    assert "raw_vtt" not in blob and "transcript.txt" not in blob


# ── CK-28 페이징: total_parts>1·순차조회·각 파트 상한·무손실 ────

def _big_transcript():
    return "\n".join("줄 %04d 자막 내용 텍스트 line content" % i for i in range(6000))


def test_paging_splits_and_sequential_get(monkeypatch, tmp_path):
    big = _big_transcript()
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result(transcript=big))
    first = S._do_extract(VALID_URL)
    assert first["total_parts"] > 1
    assert first["part_index"] == 1
    handle = first["transcript_handle"]

    collected = [first["transcript"]]
    for k in range(2, first["total_parts"] + 1):
        part = S._do_get_part(handle, k)
        assert part["part_index"] == k
        assert part["total_parts"] == first["total_parts"]
        collected.append(part["transcript"])

    assert "".join(collected) == big                        # 무손실
    for p in collected:
        assert len(p.encode("utf-8")) <= paging.PART_LIMIT_BYTES


# ── CK-29 핸들 결속: 자기 엔트리만 ──────────────────────────────

def test_handles_bound_to_own_entry(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, lambda u, l, o: None)   # 덮어씀 아래
    def fake(url, lang, outdir):
        return _ok_result(transcript="AAA content here", vid="vidA", title="A")
    monkeypatch.setattr(S, "run_extract", fake)
    a = S._do_extract("https://youtu.be/a")["transcript_handle"]

    def fake_b(url, lang, outdir):
        return _ok_result(transcript="BBB content here", vid="vidB", title="B")
    monkeypatch.setattr(S, "run_extract", fake_b)
    b = S._do_extract("https://youtu.be/b")["transcript_handle"]

    assert a != b
    assert S._do_get_part(a, 1)["transcript"] == "AAA content here"
    assert S._do_get_part(a, 1)["title"] == "A"
    assert S._do_get_part(b, 1)["transcript"] == "BBB content here"


# ── CK-30 핸들 거부: 미발급·조작·범위밖 ─────────────────────────

def test_invalid_and_out_of_range_handles(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result())
    handle = S._do_extract(VALID_URL)["transcript_handle"]

    assert S._do_get_part("never-issued", 1)["error"]["code"] == "INVALID_HANDLE"
    assert S._do_get_part(handle + "x", 1)["error"]["code"] == "INVALID_HANDLE"
    assert S._do_get_part("../../etc/passwd", 1)["error"]["code"] == "INVALID_HANDLE"
    assert S._do_get_part(handle, 0)["error"]["code"] == "PART_OUT_OF_RANGE"
    assert S._do_get_part(handle, 99)["error"]["code"] == "PART_OUT_OF_RANGE"


# ── CK-31 에러 매핑·미노출 ──────────────────────────────────────

@pytest.mark.parametrize("exit_code,expected", [
    (EXIT_NO_SUBTITLE, "NO_SUBTITLE"),
    (EXIT_DOWNLOAD_FAILED, "DOWNLOAD_FAILED"),
    (EXIT_UNAVAILABLE, "VIDEO_UNAVAILABLE"),
    (EXIT_BAD_INPUT, "BAD_INPUT"),
    (EXIT_EMPTY_TRANSCRIPT, "EMPTY_TRANSCRIPT"),
    (EXIT_STORAGE_LIMIT, "STORAGE_LIMIT_EXCEEDED"),       # Phase 3 신규(CK-16)
    (EXIT_SUBTITLE_TOO_LARGE, "SUBTITLE_TOO_LARGE"),      # Phase 3 신규(CK-16)
    (99, "UNKNOWN_DOWNLOAD_FAILURE"),      # 미분류 exit → UNKNOWN 폴백
])
def test_exit_code_error_mapping(monkeypatch, tmp_path, exit_code, expected):
    res = ExtractResult(exit_code, "MSG id=vidOK", {}, None)
    _prep(monkeypatch, tmp_path, lambda u, l, o: res)
    r = S._do_extract(VALID_URL)
    assert r["error"]["code"] == expected
    assert r["error"]["message"] == "MSG id=vidOK"


def test_tooling_missing_preflight(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result(), which=None)
    r = S._do_extract(VALID_URL)
    assert r["error"]["code"] == "TOOLING_MISSING"


def test_unknown_exception_is_masked(monkeypatch, tmp_path):
    def boom(url, lang, outdir):
        raise ValueError("internal detail /Users/secret leak")
    _prep(monkeypatch, tmp_path, boom)
    r = S._do_extract(VALID_URL)
    assert r["error"]["code"] == "UNKNOWN_DOWNLOAD_FAILURE"
    assert "/Users" not in json.dumps(r)          # traceback 미노출


def test_output_write_failure_maps_structured(monkeypatch, tmp_path):
    def raise_os(url, lang, outdir):
        raise OSError("disk full")
    _prep(monkeypatch, tmp_path, raise_os)
    r = S._do_extract(VALID_URL)
    assert r["error"]["code"] == "OUTPUT_WRITE_FAILED"


# (Phase 3: server 는 makedirs 를 직접 하지 않음 — 격리 dir 생성은 run_extract 의
#  storage.make_temp 로 이동. run_extract 가 OSError 를 던지는 경로는
#  test_output_write_failure_maps_structured 가 이미 커버.)


# ── CK-36 입력 검증 ─────────────────────────────────────────────

@pytest.mark.parametrize("url,code", [
    ("https://www.youtube.com/watch?v=abc&list=PL123", "PLAYLIST_UNSUPPORTED"),
    ("https://www.youtube.com/playlist?list=PL123", "PLAYLIST_UNSUPPORTED"),
    ("https://www.youtube.com/channel/UC123", "PLAYLIST_UNSUPPORTED"),
    ("https://www.youtube.com/@somechannel", "PLAYLIST_UNSUPPORTED"),
    ("https://youtu.be/" + "a" * 3000, "BAD_INPUT"),
])
def test_input_validation_rejects(monkeypatch, tmp_path, url, code):
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result())
    assert S._do_extract(url)["error"]["code"] == code


def test_single_video_url_passes_validation(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result())
    r = S._do_extract("https://www.youtube.com/watch?v=abc123")
    assert "error" not in r


# ── CK-14 signature·자원 한도 ───────────────────────────────────

def test_extract_passes_output_root(monkeypatch, tmp_path):
    # Phase 3(D3-D): server 는 저장 루트(OUTPUT_DIR)를 run_extract 에 전달 — 격리는
    # run_extract 내부 storage.make_temp(.tmp/<token>)로 이동(server 난수 dir 폐기).
    seen = []
    def rec(url, lang, output_root):
        seen.append(output_root)
        return _ok_result()
    _prep(monkeypatch, tmp_path, rec)
    S._do_extract(VALID_URL)
    S._do_extract(VALID_URL)
    assert len(seen) == 2
    assert seen[0] == seen[1] == str(tmp_path)           # 동일 root 전달


def test_transcript_too_large_byte_cap(monkeypatch, tmp_path):
    huge = "a" * (S.MAX_SUBTITLE_BYTES + 10)
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result(transcript=huge))
    r = S._do_extract(VALID_URL)
    assert r["error"]["code"] == "TRANSCRIPT_TOO_LARGE"


def test_too_many_parts_cap(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result(transcript="x" * 500))
    # split_transcript 를 파트 폭증하도록 대체(MAX_PARTS 백스톱 검증)
    monkeypatch.setattr(S, "split_transcript",
                        lambda t, *a: ["p"] * (S.MAX_PARTS + 1))
    r = S._do_extract(VALID_URL)
    assert r["error"]["code"] == "TRANSCRIPT_TOO_LARGE"


# ── CK-32 프롬프트 4요소 ────────────────────────────────────────

def test_prompt_has_four_required_elements():
    text = S._load_summary_prompt()
    assert "데이터" in text and "따르지 마" in text           # ① 인젝션 펜스
    assert "get_transcript_part" in text and "total_parts" in text  # ② 페이징
    assert "자가 점검" in text or "자가점검" in text          # ③ 구간 자가점검
    assert "보존" in text and "밀도" in text                  # ④ 저작권 완화


# ── CK-33 AC-7 SDK 경유 스모크(핸드셰이크·성공1·에러1·페이징) ───

def _call_json(result):
    """CallToolResult → 반환 dict(JSON 직렬화 왕복 검증)."""
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


def test_sdk_smoke_handshake_and_tool_calls(monkeypatch, tmp_path):
    big = _big_transcript()
    def fake(url, lang, outdir):
        return _ok_result(transcript=big)
    _prep(monkeypatch, tmp_path, fake)

    async def scenario():
        from mcp.shared.memory import (
            create_connected_server_and_client_session as sess,
        )
        async with sess(S.mcp) as client:            # initialize 자동(핸드셰이크)
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert {"extract_transcript", "get_transcript_part"} <= names
            prompts = await client.list_prompts()
            assert "summarize_video" in {p.name for p in prompts.prompts}

            # 성공 1 + 페이징
            ok = _call_json(await client.call_tool(
                "extract_transcript", {"url": VALID_URL}))
            assert ok["total_parts"] > 1 and ok["part_index"] == 1
            handle = ok["transcript_handle"]
            p2 = _call_json(await client.call_tool(
                "get_transcript_part",
                {"transcript_handle": handle, "part": 2}))
            assert p2["part_index"] == 2

            # 에러 1 (플레이리스트 — 네트워크 무의존 거부)
            err = _call_json(await client.call_tool(
                "extract_transcript",
                {"url": "https://www.youtube.com/playlist?list=PL1"}))
            assert err["error"]["code"] == "PLAYLIST_UNSUPPORTED"

    asyncio.run(scenario())


# ── Phase 4 AC-6/CK-10: 도구표면 blast-radius 0 계약 ────────────

def test_exposed_tools_blast_radius_zero(monkeypatch, tmp_path):
    # 노출 도구 2개 = 파일경로 인자 미수신·핸들경유·읽기전용(8필드 투영).
    # ① 시그니처: 예상 인자만·파일경로/디렉토리 계열 이름 부재
    assert set(inspect.signature(S.extract_transcript).parameters) == {"url", "lang"}
    assert (set(inspect.signature(S.get_transcript_part).parameters)
            == {"transcript_handle", "part"})
    pathish = ("path", "file", "dir", "output", "outdir", "root")
    for fn in (S.extract_transcript, S.get_transcript_part):
        for name in inspect.signature(fn).parameters:
            assert not any(tok in name.lower() for tok in pathish), name
    # ② get_transcript_part 는 핸들 경유만 — 임의 경로 문자열은 파일 접근 없이 INVALID_HANDLE
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result())
    for evil in ("/etc/passwd", "../../secret.txt", str(tmp_path / "x")):
        assert S._do_get_part(evil, 1)["error"]["code"] == "INVALID_HANDLE"
    # ③ 성공 응답은 8필드 allowlist 투영만(경로·내부키 유출 0·읽기전용)
    assert set(S._do_extract(VALID_URL).keys()) == _SCHEMA_KEYS


# ── CK-31 malformed URL(urlparse 예외)도 구조화 계약 안으로 ──────

def test_malformed_url_returns_structured_error(monkeypatch, tmp_path):
    # https://[::1 → urlparse ValueError. {code,message} 계약 우회·원문 예외 노출 0(CK-31)
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result())
    r = S._do_extract("https://[::1")
    assert r["error"]["code"] == "BAD_INPUT"
    assert "IPv6" not in r["error"]["message"]        # 파이썬 원문 예외 미노출


# ── CK-28 페이징: 실제 반환 JSON 이 파트 상한 이하(오버헤드 차감) ──

def test_each_part_serialized_within_limit(monkeypatch, tmp_path):
    # transcript 예산이 응답 오버헤드를 차감 → 각 파트의 직렬화 반환이 PART_LIMIT_BYTES 이하
    big = ("긴 자막 문장 라인 " * 24 + "\n") * 3000     # 48KB 훨씬 초과 → 다중 파트
    _prep(monkeypatch, tmp_path, lambda u, l, o: _ok_result(transcript=big))
    r = S._do_extract(VALID_URL)
    assert r["total_parts"] > 1
    for k in range(1, r["total_parts"] + 1):
        part = S._do_get_part(r["transcript_handle"], k)
        blob = json.dumps(part, ensure_ascii=False).encode("utf-8")
        assert len(blob) <= paging.PART_LIMIT_BYTES   # 오버헤드 포함해도 상한 이하


# ── Phase 5 W1-b: frozen 리소스 경로(_resource_dir → _MEIPASS·CK-3) ──

def test_frozen_prompt_loads_from_meipass(monkeypatch, tmp_path):
    # frozen 이면 _MEIPASS/prompts 에서 로드(소스 경로 아님) — sentinel 로 브랜치 증명
    S._load_summary_prompt.cache_clear()
    prompts = tmp_path / "mei" / "prompts"
    prompts.mkdir(parents=True)
    sentinel = "SENTINEL_FROZEN_PROMPT_본문\n"
    (prompts / "summarize_video.md").write_text(sentinel, encoding="utf-8")
    monkeypatch.setattr(S.sys, "frozen", True, raising=False)
    monkeypatch.setattr(S.sys, "_MEIPASS", str(tmp_path / "mei"), raising=False)
    assert S._load_summary_prompt() == sentinel
    S._load_summary_prompt.cache_clear()   # 후속 테스트 오염 방지


def test_source_prompt_loads_real_file(monkeypatch):
    # 비frozen(소스) → 모듈 dir/prompts 로드·무회귀(실제 프롬프트 비어있지 않음)
    S._load_summary_prompt.cache_clear()
    monkeypatch.setattr(S.sys, "frozen", False, raising=False)
    text = S._load_summary_prompt()
    assert isinstance(text, str) and len(text) > 0
    S._load_summary_prompt.cache_clear()


def test_load_summary_prompt_empty_raises(tmp_path, monkeypatch):
    """빈/공백-only 프롬프트 파일(번들 손상)을 "" 로 삼키면 summarize_video() 가
    조용히 빈 프롬프트를 호스트에 반환(silent-failure). 빈 파일은 배포 결함이므로
    raise — webapp M4 대칭(server.py 경로엔 그동안 이 가드가 없었다)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "summarize_video.md").write_text("   \n", encoding="utf-8")  # 공백-only
    monkeypatch.setattr(S, "_resource_dir", lambda: str(tmp_path))
    S._load_summary_prompt.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            S._load_summary_prompt()
    finally:
        S._load_summary_prompt.cache_clear()   # 후속 테스트 오염 방지


def test_main_preflight_rejects_empty_prompt(tmp_path, monkeypatch):
    """main() 은 mcp.run() 전에 프롬프트를 preflight — 빈/누락이면 서빙 전 기동 실패.
    __main__ 배선 순서(preflight → mcp.run)를 테스트로 고정(webapp M4 대칭·회귀 락).
    빈파일 raise 만 있고 배선이 없으면, preflight 호출이 빠져도 아무도 못 잡는다."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "summarize_video.md").write_text("  \n", encoding="utf-8")  # 공백-only
    monkeypatch.setattr(S, "_resource_dir", lambda: str(tmp_path))
    S._load_summary_prompt.cache_clear()
    reached = {"run": False}
    monkeypatch.setattr(S.mcp, "run", lambda *a, **k: reached.__setitem__("run", True))
    monkeypatch.setattr(S.storage, "cleanup_stale_temp", lambda *a, **k: None)
    try:
        with pytest.raises(RuntimeError):        # preflight 가 mcp.run 전에 잡아야
            S.main()
        assert not reached["run"], "preflight 없이 mcp.run 도달 — 빈 프롬프트 미검출"
    finally:
        S._load_summary_prompt.cache_clear()


# ── Phase 5 CK-2: preflight 해석-인지 화해(_ytdlp_ready·번들 회귀차단) ──

def test_preflight_accepts_bundled_ytdlp_via_env(monkeypatch, tmp_path):
    # CK-2/AC-1: 번들 시 yt-dlp 는 PATH 에 없고 YTDLP_PATH(절대경로)로만 존재 →
    # preflight 가 _shutil_which("yt-dlp")==None 으로 오탐 거부하면 안 됨(해석-인지)
    ytdlp = tmp_path / "yt-dlp"
    ytdlp.write_text("#!bundled")
    monkeypatch.setenv("YTDLP_PATH", str(ytdlp))
    monkeypatch.setattr(S, "_shutil_which", lambda c: None)   # PATH 에 yt-dlp 없음(번들 모사)
    assert S._ytdlp_ready() is True                           # env 절대경로 존재 → ready


def test_preflight_rejects_missing_bundled_ytdlp(monkeypatch, tmp_path):
    # CK-2: YTDLP_PATH 가 실존 안 하면 ready=False(정직 실패·silent 통과 0)
    monkeypatch.setenv("YTDLP_PATH", str(tmp_path / "nonexistent-yt-dlp"))
    monkeypatch.setattr(S, "_shutil_which", lambda c: None)
    assert S._ytdlp_ready() is False


def test_preflight_source_uses_path_which(monkeypatch):
    # CK-2/AC-6 무회귀: 무env(소스) → _ytdlp_bin()="yt-dlp"(비절대) → 기존 _shutil_which 경로
    monkeypatch.delenv("YTDLP_PATH", raising=False)
    monkeypatch.setattr(S.sys, "frozen", False, raising=False)
    monkeypatch.setattr(S, "_shutil_which",
                        lambda c: "/usr/bin/yt-dlp" if c == "yt-dlp" else None)
    assert S._ytdlp_ready() is True
    monkeypatch.setattr(S, "_shutil_which", lambda c: None)
    assert S._ytdlp_ready() is False
