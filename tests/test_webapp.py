"""webapp.py API 계약 테스트 (실서버 기동·run_extract fake·네트워크 0).

CK-8 nonce 게이트(누락 403) · CK-9 allowlist 투영(절대경로·markdown 미노출) ·
CK-10 Host/Origin 거부 · CK-11 프론트 XSS(textContent·CSP nonce) · CK-17 127 바인딩.
"""
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading

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
    entry = results.ResultEntry("r", "MyVid", ({"markdown": "body"},), "P", {})
    cap = str(tmp_path / "vault")
    os.makedirs(cap)
    n1 = webapp._save_to_capability(entry, cap)
    n2 = webapp._save_to_capability(entry, cap)             # 충돌 → 접미 번호
    assert os.path.isdir(os.path.join(cap, "MyVid"))
    assert os.path.isdir(os.path.join(cap, "MyVid-2"))      # 비덮어쓰기
    assert "transcript.md" in n1 and "parts" in n1
    # 절대경로 미노출 — 표시명(basename)만
    assert all(os.sep not in name for name in n2)


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


def test_v1_mcp_prompt_file_unchanged():
    """웹 분리는 v1 MCP 프롬프트(summarize_video.md)를 건드리지 않는다."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "prompts", "summarize_video.md"),
              encoding="utf-8") as f:
        assert "get_transcript_part" in f.read()     # v1 도구 지시 유지


# ── ③ tkinter 폴더 대화상자: 실패 ≠ 취소 (침묵실패 차단) ────────────

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
