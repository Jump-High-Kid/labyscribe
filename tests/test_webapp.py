"""webapp.py API 계약 테스트 (실서버 기동·run_extract fake·네트워크 0).

CK-8 nonce 게이트(누락 403) · CK-9 allowlist 투영(절대경로·markdown 미노출) ·
CK-10 Host/Origin 거부 · CK-11 프론트 XSS(textContent·CSP nonce) · CK-17 127 바인딩.
"""
import http.client
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import types

import pytest

import extract as E
import results
import webapp


def _fake_extract(url, lang, root, emit_markdown=False):
    parts = ({"part_no": 1, "chapter_no": 1, "title": "Intro",
              "markdown": "# 파트 본문\n내용", "bytes": 20},)
    meta = {"title": "<script>alert(1)</script>", "uploader": "U", "id": "vid"}
    return E.ExtractResult(E.EXIT_OK, "ok", meta, "transcript", parts)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp._extract, "run_extract", _fake_extract)
    httpd, nonce = webapp.build_server(str(tmp_path / "out"), 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd, nonce, port
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, nonce=None, origin=None, host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body) if body is not None else None
    conn.putrequest(method, path, skip_host=(host is not None))
    if host is not None:
        conn.putheader("Host", host)
    if nonce is not None:
        conn.putheader("X-Labyscribe-Nonce", nonce)
    if origin is not None:
        conn.putheader("Origin", origin)
    if data is not None:
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(data.encode("utf-8"))))
    conn.endheaders()
    if data is not None:
        conn.send(data.encode("utf-8"))
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    ct = resp.getheader("Content-Type", "")
    conn.close()
    parsed = json.loads(raw) if raw and "json" in ct else raw
    return resp.status, parsed, {k.lower(): v for k, v in resp.getheaders()}


def _origin(port):
    return "http://127.0.0.1:%d" % port


# ── CK-17 127.0.0.1 바인딩 ─────────────────────────────────

def test_binds_loopback_only(server):
    httpd, _nonce, _port = server
    assert httpd.server_address[0] == "127.0.0.1"


# ── GET / : nonce·CSP 주입 ─────────────────────────────────

def test_index_serves_html_with_nonce_and_csp(server):
    _httpd, nonce, port = server
    status, html, headers = _req(port, "GET", "/")
    assert status == 200
    assert nonce in html                                    # 프론트에 nonce 주입
    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'nonce-%s'" % nonce in csp           # nonce script(unsafe-inline 아님)
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


# ── CK-8 nonce 게이트 ──────────────────────────────────────

def test_api_extract_without_nonce_403(server):
    _httpd, _nonce, port = server
    status, _data, _h = _req(port, "POST", "/api/extract",
                             origin=_origin(port), body={"url": "x"})
    assert status == 403


def test_api_part_without_nonce_403(server):
    _httpd, _nonce, port = server
    status, _data, _h = _req(port, "GET", "/api/part/abc/1")
    assert status == 403


# ── CK-10 Host / Origin 거부 ───────────────────────────────

def test_bad_host_rejected(server):
    _httpd, nonce, port = server
    status, _data, _h = _req(port, "GET", "/", host="evil.com")
    assert status == 403


def test_post_without_origin_rejected(server):
    _httpd, nonce, port = server
    status, _data, _h = _req(port, "POST", "/api/extract", nonce=nonce, body={"url": "x"})
    assert status == 403                                    # Origin 누락 = 기본 거부


def test_post_bad_origin_rejected(server):
    _httpd, nonce, port = server
    status, _data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                             origin="http://evil.com", body={"url": "x"})
    assert status == 403


# ── CK-9 allowlist 투영 + 정상 추출 ────────────────────────

def test_extract_projection_no_markdown_no_abspath(server, tmp_path):
    _httpd, nonce, port = server
    status, data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                            origin=_origin(port), body={"url": "https://youtu.be/x"})
    assert status == 200
    assert data["result_id"] and data["parts"][0]["part_no"] == 1
    assert "markdown" not in data["parts"][0]               # 목록엔 markdown 미노출
    assert str(tmp_path) not in json.dumps(data)            # 절대경로 미노출


def test_part_returns_markdown(server):
    _httpd, nonce, port = server
    _s, data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                        origin=_origin(port), body={"url": "https://youtu.be/x"})
    rid = data["result_id"]
    status, part, _h = _req(port, "GET", "/api/part/%s/1" % rid, nonce=nonce)
    assert status == 200
    assert part["markdown"].startswith("# 파트 본문")


def test_json_array_body_returns_400(server):
    # 유효 JSON 이지만 dict 아님(배열) → 400(무응답 연결끊김 아님·codex #4 재작업)
    _httpd, nonce, port = server
    status, data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                            origin=_origin(port), body=["not", "a", "dict"])
    assert status == 400
    assert data["error"]["code"] == "BAD_INPUT"


def test_error_mapping(server, monkeypatch):
    def _fail(url, lang, root, emit_markdown=False):
        return E.ExtractResult(E.EXIT_NO_SUBTITLE, "자막 없음", {}, None)
    monkeypatch.setattr(webapp._extract, "run_extract", _fail)
    _httpd, nonce, port = server
    status, data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                            origin=_origin(port), body={"url": "https://youtu.be/x"})
    assert status == 400
    assert data["error"]["code"] == "NO_SUBTITLE"


# ── CK-11 저장: capability 검증·접미 비덮어쓰기 ────────────

def test_save_invalid_capability_404(server):
    _httpd, nonce, port = server
    _s, data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                        origin=_origin(port), body={"url": "https://youtu.be/x"})
    rid = data["result_id"]
    # pick-folder 없이 저장 → capability 미승인 404
    status, out, _h = _req(port, "POST", "/api/save", nonce=nonce, origin=_origin(port),
                           body={"result_id": rid, "capability_id": "nope"})
    assert status == 404
    assert out["error"]["code"] == "INVALID_CAPABILITY"


def test_save_to_capability_suffix(tmp_path):
    entry = results.ResultEntry("r", "MyVid", ({"markdown": "body"},), {})
    cap = str(tmp_path / "vault")
    os.makedirs(cap)
    n1 = webapp._save_to_capability(entry, cap)
    n2 = webapp._save_to_capability(entry, cap)             # 충돌 → 접미 번호
    assert os.path.isdir(os.path.join(cap, "MyVid"))
    assert os.path.isdir(os.path.join(cap, "MyVid-2"))      # 비덮어쓰기
    assert "transcript.md" in n1 and "parts" in n1
    # 절대경로 미노출 — 표시명(basename)만
    assert all(os.sep not in name for name in n2)


def test_save_preserves_picked_folder_permissions(tmp_path):
    """저장이 사용자가 고른 폴더의 Unix 권한을 강제 축소하지 않는다(CRITICAL)."""
    entry = results.ResultEntry("r", "MyVid", ({"markdown": "body"},), {})
    cap = str(tmp_path / "shared")
    os.makedirs(cap)
    os.chmod(cap, 0o755)
    webapp._save_to_capability(entry, cap)
    assert stat.S_IMODE(os.stat(cap).st_mode) == 0o755   # 앱이 임의로 chmod 하지 않음


def test_save_retries_past_same_name_plain_file(tmp_path):
    """저장폴더에 동명 '파일'이 있어도 접미로 재시도해 저장한다(HIGH·ENOTDIR)."""
    entry = results.ResultEntry("r", "MyVid", ({"markdown": "body"},), {})
    cap = str(tmp_path / "vault")
    os.makedirs(cap)
    open(os.path.join(cap, "MyVid"), "w").close()        # 동명 일반파일(디렉토리 아님)
    names = webapp._save_to_capability(entry, cap)
    assert os.path.isdir(os.path.join(cap, "MyVid-2"))   # 파일 건너뛰고 접미로 저장
    assert "transcript.md" in names


def test_api_extract_logs_traceback_on_unexpected(tmp_path, monkeypatch, capfd):
    """예상외 예외는 서버측 stderr 트레이스로 남긴다(HIGH·silent-failure 0·v1 server.py 정합)."""
    def boom(url, lang, root, emit_markdown=False):
        raise RuntimeError("boom-xyz-marker")
    monkeypatch.setattr(webapp._extract, "run_extract", boom)
    httpd, nonce = webapp.build_server(str(tmp_path / "out"), 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        st, body, _ = _req(port, "POST", "/api/extract", nonce=nonce,
                           origin=_origin(port), body={"url": "https://youtu.be/x"})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert st == 500 and body["error"]["code"] == "UNKNOWN_FAILURE"
    err = capfd.readouterr().err
    assert "boom-xyz-marker" in err and "Traceback" in err   # 진단 트레이스 보존


# ── 프로토콜 레벨 에러 응답도 보안헤더(reviewer P2 재작업) ─────

def test_unsupported_method_error_has_security_headers(server):
    # PUT(미지원) → send_error → end_headers 오버라이드로 CSP 부착
    _httpd, nonce, port = server
    status, _data, headers = _req(port, "PUT", "/api/extract",
                                  nonce=nonce, origin=_origin(port))
    assert status >= 400
    assert "content-security-policy" in headers
    assert "default-src 'self'" in headers["content-security-policy"]


# ── CK-11 프론트 XSS (정적 grep) ───────────────────────────

def test_frontend_uses_textcontent_not_innerhtml():
    html = webapp._INDEX_HTML
    assert "innerHTML" not in html                          # innerHTML 0
    assert "textContent" in html                            # 외부 데이터 = textContent
    assert 'nonce="{{NONCE}}"' in html                      # script nonce


# ── 진입점: `python webapp.py` 실기동 회귀 가드 ────────────

def test_python_webapp_py_launches_server(tmp_path):
    """`python webapp.py` 가 실제로 서버를 기동하는지(=__main__ 진입점 존재) 회귀 가드.

    build_server 를 직접 import 하는 위 계약 테스트는 __main__ 진입점 부재를 못 잡는다 —
    진입점이 없으면 스크립트는 정의만 하고 조용히 종료(출력·서버 0). 실 subprocess 로 재현.
    네트워크 0(기동만·run_extract 미호출) · BROWSER=true 로 실 브라우저 자동열기 억제.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "OUTPUT_DIR": str(tmp_path / "out"), "BROWSER": "true"}
    proc = subprocess.Popen(
        [sys.executable, os.path.join(root, "webapp.py")],
        cwd=root, env=env, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    line_q: "queue.Queue" = queue.Queue()
    threading.Thread(target=lambda: line_q.put(proc.stderr.readline()),
                     daemon=True).start()
    try:
        try:
            line = line_q.get(timeout=15)
        except queue.Empty:
            raise AssertionError("15s 내 기동 URL 미출력 — __main__ 진입점 부재")
        m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
        assert m, "기동 URL 미출력(진입점 부재 의심): %r" % line
        with socket.create_connection(("127.0.0.1", int(m.group(1))), timeout=5):
            pass                                               # 실제 리스닝 확인
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── 진입점: --selfcheck 번들 무결성 프로브 (AC-8·CK-6·CK-11) ────────

def test_selfcheck_short_circuits_before_bind(monkeypatch):
    """--selfcheck: 서버 bind 도달 전 조기 return(CK-11·기동 흐름 불변).
    tkinter 프로브는 별도 함수라 모킹 — 제어흐름은 데스크톱 tkinter 유무와 무관하게 검증."""
    reached = {"bind": False}
    monkeypatch.setattr(webapp, "_probe_tkinter", lambda: None)   # tkinter 독립
    monkeypatch.setattr(webapp.storage, "cleanup_stale_temp",
                        lambda *a, **k: None)                     # 회귀 시 실 홈 미접촉
    monkeypatch.setattr(webapp, "_bind_with_fallback",
                        lambda root: reached.__setitem__("bind", True) or (None, ""))
    webapp._summary_prompt.cache_clear()
    try:
        rc = webapp.main(["--selfcheck"])
        assert rc == 0
        assert not reached["bind"], "selfcheck 가 서버 bind 로 흘러감(조기 return 실패)"
    finally:
        webapp._summary_prompt.cache_clear()


def test_selfcheck_fails_on_missing_prompt(tmp_path, monkeypatch):
    """selfcheck 는 프롬프트 번들 누락(배포 결함)도 잡는다 — 오프라인 무결성 게이트."""
    monkeypatch.setattr(webapp, "_probe_tkinter", lambda: None)   # tkinter 독립
    monkeypatch.setattr(webapp, "_resource_dir", lambda: str(tmp_path))  # prompts/ 없음
    webapp._summary_prompt.cache_clear()
    try:
        with pytest.raises(OSError):                 # FileNotFoundError ⊂ OSError
            webapp.main(["--selfcheck"])
    finally:
        webapp._summary_prompt.cache_clear()


def test_selfcheck_real_tkinter_probe(monkeypatch):
    """실제 _tkinter 로드 + Tcl 인터프리터 생성으로 AC-8 충족(무디스플레이).
    frozen 스모크가 파일존재 관대매칭 대신 이 경로를 exe 로 실행. tkinter 부재 데스크톱은 skip."""
    pytest.importorskip("tkinter")
    webapp._summary_prompt.cache_clear()
    try:
        assert webapp.main(["--selfcheck"]) == 0     # 실 프로브 통과
    finally:
        webapp._summary_prompt.cache_clear()


# ── macOS 소스 런처: labyscribe-web.command preflight 회귀 (CK-1·CK-2·CK-6·CK-7) ──
#
# 런처는 mac용 `.command` 이지만 preflight 는 순수 bash 라 ubuntu CI 에서도 게이트가 된다.
# 실서버는 기동하지 않고(happy path 는 test_python_webapp_py_launches_server 가 커버) preflight
# 실패 분기만 검증 — 통제 PATH 로 python3/yt-dlp 부재를 결정론적으로 모의(test-entrypoint-launch-guard).

_LAUNCHER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labyscribe-web.command")


def _bindir(tmp_path, *, python3):
    """통제 PATH bin — 런처가 쓰는 유일 외부 coreutils(dirname)만 공통. yt-dlp 는 항상 부재.
    python3=True 면 실 인터프리터 심링크(버전 통과), False 면 부재(첫 게이트 실패)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dn = shutil.which("dirname")
    assert dn, "dirname 미존재 — 테스트 환경 이상"
    os.symlink(dn, bindir / "dirname")
    if python3:
        os.symlink(sys.executable, bindir / "python3")
    return bindir


def _run_launcher(bindir):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash 미존재 — 런처는 mac/linux 셸 전용")
    return subprocess.run(
        [bash, _LAUNCHER],
        env={"PATH": str(bindir), "HOME": os.environ.get("HOME", "/tmp")},
        capture_output=True, text=True, timeout=15)


def test_launcher_exists_and_executable():
    """AC-1: 런처가 repo 루트에 있고 실행권한(0755)."""
    assert os.path.isfile(_LAUNCHER), "labyscribe-web.command 부재"
    assert os.access(_LAUNCHER, os.X_OK), "labyscribe-web.command 실행권한 없음"


def test_launcher_fails_without_ytdlp(tmp_path):
    """CK-2: yt-dlp 부재 → 비0 종료 + 설치 안내(서버 미기동)."""
    r = _run_launcher(_bindir(tmp_path, python3=True))
    assert r.returncode != 0, "yt-dlp 부재인데 성공/기동(silent-failure)"
    out = r.stdout + r.stderr
    assert "yt-dlp" in out and "설치" in out, "yt-dlp 설치 안내 부재: %r" % out


def test_launcher_fails_without_python(tmp_path):
    """CK-1: python3 부재 → 비0 종료 + 설치 안내(서버 미기동)."""
    r = _run_launcher(_bindir(tmp_path, python3=False))
    assert r.returncode != 0, "python3 부재인데 성공/기동(silent-failure)"
    out = r.stdout + r.stderr
    assert "python3" in out and "설치" in out, "python3 설치 안내 부재: %r" % out


def test_launcher_rejects_old_python(tmp_path):
    """CK-6: Python <3.10 → 비0 + 버전 안내. 실인터프리터 질의(`-c` 종료코드)로 판정."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(shutil.which("dirname"), bindir / "dirname")
    stub = bindir / "python3"
    # `-c <식>` → exit 1(구버전 모의) · `-V` → 버전 문자열. 문자열 파싱이 아닌 종료코드 분기 검증.
    stub.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 1; fi\necho "Python 3.9.0"\n')
    stub.chmod(0o755)
    r = _run_launcher(bindir)
    assert r.returncode != 0, "구버전 Python 인데 통과(silent-failure)"
    out = r.stdout + r.stderr
    assert "3.10" in out, "버전 하한 안내 부재: %r" % out


def test_main_opens_browser_at_server_url(monkeypatch):
    """AC-10(codex H3): 기동 시 webbrowser.open 이 실제 서버 URL 로 호출된다.
    frozen 스모크는 no-op BROWSER 로 실 스폰만 억제하므로, 호출 계약은 이 유닛테스트가 검증."""
    opened = {}

    class _FakeHttpd:
        server_address = ("127.0.0.1", 8760)
        state = object()

        def serve_forever(self):
            pass

    monkeypatch.setattr(webapp, "_bind_with_fallback",
                        lambda root: (_FakeHttpd(), "nonce"))
    monkeypatch.setattr(webapp.storage, "cleanup_stale_temp",
                        lambda *a, **k: None)
    monkeypatch.setattr(webapp, "_gui_loop", lambda state: None)
    monkeypatch.setattr(webapp.webbrowser, "open",
                        lambda url: opened.__setitem__("url", url) or True)
    webapp._summary_prompt.cache_clear()
    try:
        assert webapp.main([]) == 0
        assert opened.get("url") == "http://127.0.0.1:8760/"   # 실 서버 URL·포트
    finally:
        webapp._summary_prompt.cache_clear()


# ── ① 전체 복사: GET /api/transcript/<rid> (통합 transcript.md) ─────

def test_transcript_returns_joined_markdown(server, monkeypatch):
    """전 파트를 \\n\\n 으로 결합한 통합 transcript 를 반환(과분할 UX 완화)."""
    def _multi(url, lang, root, emit_markdown=False):
        parts = ({"part_no": 1, "chapter_no": 1, "title": "A",
                  "markdown": "AAA", "bytes": 3},
                 {"part_no": 2, "chapter_no": 2, "title": "B",
                  "markdown": "BBB", "bytes": 3})
        return E.ExtractResult(E.EXIT_OK, "ok", {"title": "T", "id": "v"}, "t", parts)
    monkeypatch.setattr(webapp._extract, "run_extract", _multi)
    _httpd, nonce, port = server
    _s, data, _h = _req(port, "POST", "/api/extract", nonce=nonce,
                        origin=_origin(port), body={"url": "https://youtu.be/x"})
    rid = data["result_id"]
    status, full, _h = _req(port, "GET", "/api/transcript/%s" % rid, nonce=nonce)
    assert status == 200
    assert full["markdown"] == "AAA\n\nBBB"          # 전 파트 결합(파트 순서 보존)


def test_transcript_without_nonce_403(server):
    _httpd, _nonce, port = server
    status, _data, _h = _req(port, "GET", "/api/transcript/abc")
    assert status == 403                             # nonce 게이트(/api/part 와 동일)


def test_transcript_invalid_result_404(server):
    _httpd, nonce, port = server
    status, data, _h = _req(port, "GET", "/api/transcript/nope", nonce=nonce)
    assert status == 404
    assert data["error"]["code"] == "INVALID_RESULT"


# ── ② 웹 전용 프롬프트: MCP 도구 지시 없음 · v1 파일 불변 ───────────

def test_web_prompt_omits_mcp_tool_references():
    """웹 UI 는 순서대로 붙여넣기 흐름 — 존재않는 MCP 도구를 지시하면 안 됨."""
    webapp._summary_prompt.cache_clear()
    prompt = webapp._summary_prompt()
    assert prompt.strip()                            # 프롬프트 실재
    for tok in ("get_transcript_part", "extract_transcript", "transcript_handle"):
        assert tok not in prompt                     # 웹에 없는 도구 미지시


def test_length_levels_substitute_placeholder_and_budgets():
    """분량 프리셋: 자리표시자가 전 레벨에서 치환되고, 예산 자수가 실제로 들어간다.
    치환 누락은 프롬프트에 `{{...}}` 리터럴이 남는 침묵 실패라 챗봇이 무시한다."""
    for lv in webapp.LENGTH_LEVELS:
        webapp._summary_prompt.cache_clear()
        p = webapp._summary_prompt(lv)
        assert "{{" not in p, "%s 레벨에 미치환 자리표시자" % lv
    assert "5,000자" in webapp._summary_prompt("read10")
    assert "2,500자" in webapp._summary_prompt("read5")
    assert "자 내외" not in webapp._summary_prompt("full")   # 기본 = 제한 없음(현행 유지)
    webapp._summary_prompt.cache_clear()


def test_index_select_options_match_length_levels():
    """프론트 select 값과 서버 레벨 키의 SSOT 일치 — 어긋나면 '복사' 가 조용히 실패한다."""
    html = webapp._render_index("n")
    for lv in webapp.LENGTH_LEVELS:
        assert 'value="%s"' % lv in html
    assert html.count("<option") == len(webapp.LENGTH_LEVELS)


def test_index_length_select_is_outside_hidden_tools():
    """분량 select 는 추출 전(=tools hidden)에 골라야 자동 복사가 그 레벨로 나간다.
    tools 안으로 되돌아가면 사용자는 항상 '전체'로 자동 복사된 뒤에야 레벨을 본다."""
    html = webapp._render_index("n")
    assert html.index('id="len"') < html.index('id="tools"')
    # 템플릿이 일반 문자열이라 JS 안의 \n 은 반드시 이중 이스케이프 — 한 겹이면 실제 개행이
    # 박혀 <script> 전체가 SyntaxError 로 죽는다(추출은 되는데 버튼이 전부 먹통).
    assert r'"\n\n"' in html


def test_summary_prompt_missing_file_raises(tmp_path, monkeypatch):
    """프롬프트 파일 부재(번들 누락)를 "" 로 삼키면 프론트가 빈 프롬프트를
    '복사됨 ✓' 로 위장한다(M4). 부재는 배포 결함이므로 fail-fast(raise)."""
    monkeypatch.setattr(webapp, "_resource_dir", lambda: str(tmp_path))  # prompts/ 없음
    webapp._summary_prompt.cache_clear()
    try:
        with pytest.raises(OSError):                 # FileNotFoundError ⊂ OSError
            webapp._summary_prompt()
    finally:
        webapp._summary_prompt.cache_clear()         # 후속 테스트 오염 방지


def test_main_preflight_rejects_missing_prompt(tmp_path, monkeypatch):
    """main() 은 서버 bind 전에 프롬프트 존재를 preflight — 번들 누락이면
    기동조차 안 함(패키징 스모크가 서버 기동만으로 RED)."""
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(webapp, "_resource_dir", lambda: str(tmp_path))  # prompts/ 없음
    webapp._summary_prompt.cache_clear()
    reached = {"bind": False}
    def _fake_bind(root):
        reached["bind"] = True
        raise SystemExit(0)                          # 도달 시 즉시 탈출(hang 방지)
    monkeypatch.setattr(webapp, "_bind_with_fallback", _fake_bind)
    try:
        with pytest.raises(OSError):                 # preflight 가 bind 전에 잡아야
            webapp.main([])
        assert not reached["bind"], "preflight 없이 bind 도달 — 프롬프트 부재 미검출"
    finally:
        webapp._summary_prompt.cache_clear()


def test_v1_mcp_prompt_file_unchanged():
    """웹 분리는 v1 MCP 프롬프트(summarize_video.md)를 건드리지 않는다."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "prompts", "summarize_video.md"),
              encoding="utf-8") as f:
        assert "get_transcript_part" in f.read()     # v1 도구 지시 유지


# ── ③ tkinter 폴더 대화상자: 실패 ≠ 취소 (침묵실패 차단) ────────────

def test_ask_directory_forces_topmost_and_parent(monkeypatch):
    """Windows 폴더창이 브라우저 뒤로 숨는 문제 방지 — root topmost 설정 + askdirectory(parent).

    실제 tkinter 를 mock(실창 미표시)해 회귀를 잡는다: topmost 미설정으로 되돌아가면
    Windows 사용자에겐 '저장 폴더 선택 무반응'으로 재발한다.
    """
    calls = {"attributes": [], "askdir_kwargs": None, "destroyed": False}

    class _FakeRoot:
        def withdraw(self):
            pass

        def attributes(self, *a):
            calls["attributes"].append(a)

        def update(self):
            pass

        def destroy(self):
            calls["destroyed"] = True

    fake_root = _FakeRoot()

    def _fake_askdir(**kwargs):
        calls["askdir_kwargs"] = kwargs
        return "/picked/folder"

    fake_filedialog = types.SimpleNamespace(askdirectory=_fake_askdir)
    fake_tkinter = types.SimpleNamespace(Tk=lambda: fake_root, filedialog=fake_filedialog)
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_filedialog)

    result = webapp._ask_directory()

    assert result == "/picked/folder"
    assert ("-topmost", True) in calls["attributes"]              # 전면화 설정
    assert calls["askdir_kwargs"].get("parent") is fake_root      # parent 전달(topmost 상속)
    assert calls["destroyed"]                                     # finally 정리 보장


def _run_gui(state):
    threading.Thread(target=webapp._gui_loop, args=(state,), daemon=True).start()


def test_pick_folder_dialog_failure_is_error_not_cancelled(server, monkeypatch):
    """대화상자 열기 실패(tkinter 부재·TclError)를 '취소'로 위장하지 않는다."""
    def _boom():
        raise RuntimeError("no display")
    monkeypatch.setattr(webapp, "_ask_directory", _boom)
    httpd, nonce, port = server
    _run_gui(httpd.state)
    status, data, _h = _req(port, "POST", "/api/pick-folder", nonce=nonce,
                            origin=_origin(port), body={})
    assert status == 500
    assert data["error"]["code"] == "FOLDER_DIALOG_FAILED"


def test_pick_folder_user_cancel_still_cancelled(server, monkeypatch):
    """사용자 취소(빈 경로)는 여전히 cancelled — 정상 흐름 보존."""
    monkeypatch.setattr(webapp, "_ask_directory", lambda: None)
    httpd, nonce, port = server
    _run_gui(httpd.state)
    status, data, _h = _req(port, "POST", "/api/pick-folder", nonce=nonce,
                            origin=_origin(port), body={})
    assert status == 200
    assert data["status"] == "cancelled"


def test_pick_folder_empty_error_message_still_error(server, monkeypatch):
    """예외 메시지가 비어도(str(e)=='') 실패를 취소로 위장하지 않는다(codex MEDIUM).

    '실패 여부'는 error 값의 truthiness 가 아니라 error 키 존재로 판정해야 한다.
    """
    def _boom():
        raise RuntimeError("")           # 메시지 없는 예외 → str(e) == ""
    monkeypatch.setattr(webapp, "_ask_directory", _boom)
    httpd, nonce, port = server
    _run_gui(httpd.state)
    status, data, _h = _req(port, "POST", "/api/pick-folder", nonce=nonce,
                            origin=_origin(port), body={})
    assert status == 500
    assert data["error"]["code"] == "FOLDER_DIALOG_FAILED"


def test_log_hint_absent_when_logging_failed():
    """log_error 실패(None) → 안내 접미 없이 generic 메시지 유지(dangling 'error.log' 0)."""
    assert webapp._log_hint(None) == ""
    assert "error.log" in webapp._log_hint("error.log")     # 성공 시에만 파일명 안내
