"""webapp.py API 계약 테스트 (실서버 기동·run_extract fake·네트워크 0).

CK-8 nonce 게이트(누락 403) · CK-9 allowlist 투영(절대경로·markdown 미노출) ·
CK-10 Host/Origin 거부 · CK-11 프론트 XSS(textContent·CSP nonce) · CK-17 127 바인딩.
"""
import http.client
import json
import os
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
