"""labyscribe v2 로컬 웹 UI — stdlib http.server (127.0.0.1 전용 · 런타임 의존 추가 0).

더블클릭/`labyscribe-web` → 로컬 웹서버 기동 → 브라우저 자동 열기. 유튜브 URL 추출 →
챕터별 Markdown 파트 → 클립보드 순환/저장. 요약은 사용자 챗봇이 수행(정체성 불변).

위협모델 = 단일 사용자 로컬(같은 계정 프로세스 신뢰). 방어 대상 = 브라우저 컨텍스트발
공격(CSRF·DNS-rebinding)·네트워크 노출:
- 127.0.0.1 만 바인딩(0.0.0.0 금지·LAN/인터넷 노출 0).
- 서버 nonce: `GET /` 로 프론트에 주입, `/` 외 전 API 필수(누락 403) — CSRF/rebinding 방어.
- Host/Origin 검증(허용값 외 거부·기본 거부). 엄격 CSP(nonce script·`default-src 'self'`).
- 응답 = allowlist 투영(절대경로·capability 실경로 미노출). 프론트는 `textContent` 만.
- tkinter 폴더 대화상자는 **메인스레드 큐 디스패치**(Tk 비스레드안전).
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import secrets
import sys
import threading
import traceback
import webbrowser
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

import extract as _extract
import results
import storage
from extract import (
    EXIT_BAD_INPUT,
    EXIT_DOWNLOAD_FAILED,
    EXIT_EMPTY_TRANSCRIPT,
    EXIT_NO_SUBTITLE,
    EXIT_OK,
    EXIT_STORAGE_LIMIT,
    EXIT_SUBTITLE_TOO_LARGE,
    EXIT_UNAVAILABLE,
)

DEFAULT_PORT = 8760
_PORT_SCAN = 12                          # 기본 포트 점유 시 대체 탐색 범위
_DEFAULT_OUTPUT_DIR = "~/labyscribe"
_MAX_BODY_BYTES = 8 * 1024               # POST 본문 상한(URL·id 만 받음·과대 차단)
_MAX_URL_LEN = 2048
_DIALOG_TIMEOUT_SEC = 180
# 시작 시 stale temp 정리 임계(server.py 일관 — 최대 추출시간 여유).
_STALE_TEMP_MAX_AGE_SEC = _extract.DOWNLOAD_TIMEOUT_SEC * 8

# exit code → 에러 코드 (server._EXIT_TO_CODE 복제 · webapp 은 mcp 미import).
_EXIT_TO_CODE = {
    EXIT_NO_SUBTITLE: "NO_SUBTITLE",
    EXIT_DOWNLOAD_FAILED: "DOWNLOAD_FAILED",
    EXIT_UNAVAILABLE: "VIDEO_UNAVAILABLE",
    EXIT_BAD_INPUT: "BAD_INPUT",
    EXIT_EMPTY_TRANSCRIPT: "EMPTY_TRANSCRIPT",
    EXIT_STORAGE_LIMIT: "STORAGE_LIMIT_EXCEEDED",
    EXIT_SUBTITLE_TOO_LARGE: "SUBTITLE_TOO_LARGE",
}

_dialog_lock = threading.Lock()          # 폴더 대화상자 단일 실행 잠금(AC-17)


def _resolve_output_dir() -> str:
    return os.environ.get("OUTPUT_DIR") or os.path.expanduser(_DEFAULT_OUTPUT_DIR)


def _resource_dir() -> str:
    src = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", src)
    return src


@lru_cache(maxsize=1)
def _summary_prompt() -> str:
    # 웹 전용 프롬프트 — v1 MCP 프롬프트(summarize_video.md)와 분리. 웹은 도구 없이
    # 파트를 순서대로 붙여넣는 흐름이라 get_transcript_part 등 MCP 도구를 지시하면 안 됨.
    # 부재/빈 파일 = 배포 결함(번들 누락). "" 반환은 프론트가 빈 프롬프트를 "복사됨 ✓"로
    # 위장하므로 fail-fast — main() preflight 가 기동 시 검출해 배포 전에 드러낸다(M4).
    path = os.path.join(_resource_dir(), "prompts", "web_summarize.md")
    with open(path, encoding="utf-8") as f:   # 부재 = FileNotFoundError 전파(삼키지 않음)
        text = f.read()
    if not text.strip():
        raise RuntimeError("웹 요약 프롬프트가 비어 있습니다: %s" % path)
    return text


class AppState:
    """서버 생애 인메모리 상태 — nonce·레지스트리·GUI 큐."""

    def __init__(self, output_root: str, nonce: str):
        self.output_root = output_root
        self.nonce = nonce
        self.results = results.ResultRegistry()
        self.capabilities = results.CapabilityRegistry()
        self.gui_q: "queue.Queue" = queue.Queue()
        self.extract_lock = threading.Lock()   # 동시 추출 직렬화(storage 경합·yt-dlp 폭증 방지)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler_cls, state: AppState):
        super().__init__(addr, handler_cls)
        self.state = state


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "labyscribe"
    sys_version = ""                     # Python 버전 노출 억제

    def log_message(self, *args):        # 기본 stderr 로깅 억제(정보 누출·소음)
        pass

    def end_headers(self):
        # 모든 응답에 보안헤더 — send_error(프로토콜 레벨 501·400 등)까지 포함. 이미 CSP 를
        # 보낸 응답(_send_json/_send_html)은 중복 방지 위해 skip.
        buffered = b"".join(getattr(self, "_headers_buffer", []))
        if b"Content-Security-Policy" not in buffered:
            self._secure_headers()           # nonce 없는 엄격 CSP(에러 응답엔 script 없음)
        super().end_headers()

    @property
    def _state(self) -> AppState:
        return self.server.state

    # ── 가드 (브라우저발 공격 방어) ────────────────────────

    def _port(self) -> int:
        return self.server.server_address[1]

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host in ("127.0.0.1:%d" % self._port(), "localhost:%d" % self._port())

    def _origin_ok(self) -> bool:
        """POST(state 변경) CSRF 방어 — Origin 존재+허용값만. null/불일치/누락 거부."""
        origin = self.headers.get("Origin")
        if not origin:
            return False                 # 기본 거부
        return origin in ("http://127.0.0.1:%d" % self._port(),
                          "http://localhost:%d" % self._port())

    def _nonce_ok(self) -> bool:
        got = self.headers.get("X-Labyscribe-Nonce", "")
        return secrets.compare_digest(got, self._state.nonce)   # 상수시간 비교(타이밍 방어)

    # ── 응답 헬퍼 ──────────────────────────────────────────

    def _secure_headers(self, nonce_for_csp: Optional[str] = None):
        script_src = "'nonce-%s'" % nonce_for_csp if nonce_for_csp else "'none'"
        csp = ("default-src 'self'; script-src %s; style-src 'self' 'unsafe-inline'; "
               "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
               "base-uri 'none'; frame-ancestors 'none'; form-action 'none'" % script_src)
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def _send_json(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._secure_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, nonce: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._secure_headers(nonce_for_csp=nonce)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Optional[dict]:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > _MAX_BODY_BYTES:
            return None
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None   # 배열/문자열 등 → None(무응답 차단)

    # ── 라우팅 ─────────────────────────────────────────────

    def do_GET(self):
        if not self._host_ok():
            self._send_json(403, _err("FORBIDDEN_HOST", "허용되지 않은 Host."))
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(_render_index(self._state.nonce), self._state.nonce)
            return
        if path.startswith("/api/part/"):
            if not self._nonce_ok():
                self._send_json(403, _err("FORBIDDEN_NONCE", "nonce 누락/불일치."))
                return
            self._api_part(path)
            return
        if path.startswith("/api/transcript/"):
            if not self._nonce_ok():
                self._send_json(403, _err("FORBIDDEN_NONCE", "nonce 누락/불일치."))
                return
            self._api_transcript(path)
            return
        self._send_json(404, _err("NOT_FOUND", "경로 없음."))

    def do_POST(self):
        if not self._host_ok():
            self._send_json(403, _err("FORBIDDEN_HOST", "허용되지 않은 Host."))
            return
        if not self._nonce_ok():
            self._send_json(403, _err("FORBIDDEN_NONCE", "nonce 누락/불일치."))
            return
        if not self._origin_ok():
            self._send_json(403, _err("FORBIDDEN_ORIGIN", "허용되지 않은 Origin."))
            return
        path = urlparse(self.path).path
        body = self._read_json()
        if body is None:
            self._send_json(400, _err("BAD_INPUT", "요청 본문이 유효하지 않습니다."))
            return
        if path == "/api/extract":
            self._api_extract(body)
        elif path == "/api/save":
            self._api_save(body)
        elif path == "/api/pick-folder":
            self._api_pick_folder()
        else:
            self._send_json(404, _err("NOT_FOUND", "경로 없음."))

    # ── API 핸들러 ─────────────────────────────────────────

    def _api_extract(self, body: dict):
        url = body.get("url")
        lang = body.get("lang")
        if not isinstance(url, str) or not url or len(url) > _MAX_URL_LEN:
            self._send_json(400, _err("BAD_INPUT", "URL 이 비어있거나 너무 깁니다."))
            return
        if lang is not None and not isinstance(lang, str):
            lang = None
        try:
            with self._state.extract_lock:   # 동시 추출 직렬화(같은 영상 중복 제출·경합 방지)
                result = _extract.run_extract(url, lang, self._state.output_root,
                                              emit_markdown=True)
        except OSError:
            traceback.print_exc(file=sys.stderr)   # 진단 트레이스 보존(client 응답은 generic)
            self._send_json(500, _err("OUTPUT_WRITE_FAILED", "출력 저장 중 오류."))
            return
        except Exception:                # 미분류만 UNKNOWN(정보 누출 없이)
            traceback.print_exc(file=sys.stderr)   # v2 신규코드 버그를 stderr 로 노출(silent 0)
            self._send_json(500, _err("UNKNOWN_FAILURE", "예기치 못한 추출 실패."))
            return
        if result.exit_code != EXIT_OK:
            self._send_json(400, _err(_EXIT_TO_CODE.get(result.exit_code,
                                      "UNKNOWN_FAILURE"), result.message))
            return
        parts = result.parts or ()
        rid = self._state.results.issue(result.meta.get("title"), parts,
                                        _summary_prompt(), result.meta)
        self._send_json(200, {
            "result_id": rid,
            "title": result.meta.get("title"),
            "parts": [{"part_no": p["part_no"], "chapter_no": p["chapter_no"],
                       "title": p["title"], "bytes": p["bytes"]} for p in parts],
            "summary_prompt": _summary_prompt(),
            "status": "ok",
        })

    def _api_part(self, path: str):
        segs = path.split("/")           # ['', 'api', 'part', rid, part_no]
        if len(segs) != 5:
            self._send_json(404, _err("NOT_FOUND", "경로 형식 오류."))
            return
        rid, raw_no = segs[3], segs[4]
        entry = self._state.results.get(rid)
        if entry is None:
            self._send_json(404, _err("INVALID_RESULT", "유효하지 않거나 만료된 결과."))
            return
        try:
            part_no = int(raw_no)
        except ValueError:
            self._send_json(400, _err("BAD_INPUT", "part_no 정수 아님."))
            return
        for p in entry.parts:
            if p["part_no"] == part_no:
                self._send_json(200, {"part_no": p["part_no"],
                                      "chapter_no": p["chapter_no"],
                                      "title": p["title"], "markdown": p["markdown"]})
                return
        self._send_json(404, _err("PART_OUT_OF_RANGE", "해당 파트 없음."))

    def _api_transcript(self, path: str):
        segs = path.split("/")           # ['', 'api', 'transcript', rid]
        if len(segs) != 4:
            self._send_json(404, _err("NOT_FOUND", "경로 형식 오류."))
            return
        entry = self._state.results.get(segs[3])
        if entry is None:
            self._send_json(404, _err("INVALID_RESULT", "유효하지 않거나 만료된 결과."))
            return
        self._send_json(200, {"markdown": _full_transcript(entry)})

    def _api_save(self, body: dict):
        entry = self._state.results.get(body.get("result_id"))
        cap = self._state.capabilities.get(body.get("capability_id"))
        if entry is None:
            self._send_json(404, _err("INVALID_RESULT", "유효하지 않은 결과."))
            return
        if cap is None:
            self._send_json(404, _err("INVALID_CAPABILITY", "승인된 저장 폴더가 아닙니다."))
            return
        try:
            saved = _save_to_capability(entry, cap.root)
        except OSError:
            traceback.print_exc(file=sys.stderr)   # 저장 실패 원인(디스크풀·권한 등) 진단 보존
            self._send_json(500, _err("OUTPUT_WRITE_FAILED", "저장 폴더 쓰기 오류."))
            return
        self._send_json(200, {"saved_names": saved, "status": "ok"})

    def _api_pick_folder(self):
        ev = threading.Event()
        holder: dict = {}
        self._state.gui_q.put((ev, holder))
        if not ev.wait(timeout=_DIALOG_TIMEOUT_SEC):
            self._send_json(503, _err("DIALOG_BUSY", "폴더 선택 대화상자를 열 수 없습니다."))
            return
        if "error" in holder:            # 실패 여부는 error '키 존재'로 — 빈 메시지 예외도 포착
            self._send_json(500, _err("FOLDER_DIALOG_FAILED",
                            "폴더 대화상자를 열 수 없습니다. 파트를 복사해 직접 저장하세요."))
            return
        path = holder.get("path")
        if not path:
            self._send_json(200, {"status": "cancelled"})
            return
        cap = self._state.capabilities.register(path)
        self._send_json(200, {"capability_id": cap.capability_id,
                              "display_name": cap.display_name, "status": "ok"})


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _full_transcript(entry) -> str:
    """entry 전 파트 markdown 을 순서대로 \\n\\n 결합 — 통합 transcript(저장·전체복사 공용)."""
    return "\n\n".join(p["markdown"] for p in entry.parts)


def _save_to_capability(entry, cap_root: str) -> list:
    """entry 파트 세트를 승인 폴더(cap_root) 하위에 원자 저장. 충돌=접미 번호 비덮어쓰기.

    make_temp→stage→atomic_publish(containment·O_NOFOLLOW=v1 계승). raw/ 는 save 불필요.
    """
    safe = _extract.safe_filename(entry.title or "labyscribe") or "labyscribe"
    part_mds = tuple(p["markdown"] for p in entry.parts)
    transcript_md = _full_transcript(entry)

    # 사용자가 tkinter 로 고른 외부 폴더 = 앱 비소유 → chmod 금지(권한 강제 축소 방지·CRITICAL).
    temp = storage.make_temp(cap_root, chmod_root=False)
    try:
        raw_dir = os.path.join(temp, "raw")
        if os.path.isdir(raw_dir):
            os.rmdir(raw_dir)            # save 엔 raw 불필요(비어있음)
        storage.stage_v2_files(temp, part_mds, transcript_md)
        for n in range(1, 100):          # 접미 번호(단일 규칙·비덮어쓰기)
            name = safe if n == 1 else "%s-%d" % (safe, n)
            final = os.path.join(cap_root, name)
            if storage.atomic_publish(temp, final, cap_root, chmod_parent=False):
                return sorted(os.listdir(final))   # 표시명만(절대경로 미노출)
        raise OSError("저장 대상 이름 충돌 100회 — 정리 후 재시도")
    finally:
        import shutil
        shutil.rmtree(temp, ignore_errors=True)


# ── 서버 기동 (127.0.0.1·포트 폴백) ─────────────────────────────

def build_server(output_root: str, port: int = 0):
    """(_Server, nonce) 반환 — 테스트·main 공용. 127.0.0.1 만 바인딩."""
    nonce = secrets.token_urlsafe(32)
    state = AppState(output_root, nonce)
    httpd = _Server(("127.0.0.1", port), Handler, state)
    return httpd, nonce


def _bind_with_fallback(output_root: str):
    """기본 포트→소범위 스캔→ephemeral(0) 폴백. (_Server, nonce) 반환."""
    for p in [DEFAULT_PORT] + list(range(DEFAULT_PORT + 1, DEFAULT_PORT + _PORT_SCAN)):
        try:
            return build_server(output_root, p)
        except OSError:
            continue
    return build_server(output_root, 0)


def _ask_directory() -> Optional[str]:
    """tkinter 폴더 대화상자 — **메인스레드에서만** 호출(Tk 비스레드안전).

    tkinter 부재/대화상자 실패는 **raise**(호출측 `_gui_loop` 이 holder['error']로 포착) —
    '사용자 취소'(빈 경로→None)와 혼동해 침묵실패로 위장하지 않기 위함. 취소만 None.
    """
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as e:
        raise RuntimeError("tkinter 를 사용할 수 없어 폴더 대화상자를 열 수 없습니다") from e
    root = tkinter.Tk()
    try:                                 # withdraw 포함 전체를 감싸 예외 시에도 destroy 보장
        root.withdraw()
        path = filedialog.askdirectory()
    finally:
        root.destroy()
    return path or None


def _gui_loop(state: AppState):
    """메인스레드 GUI 루프 — 워커스레드가 큐로 요청한 대화상자를 단일 실행.

    `_ask_directory` 예외(TclError 등)에도 **`ev.set()` 보장**(finally) — 아니면 워커가
    타임아웃까지 hang 하고 이 루프 스레드가 죽어 이후 모든 pick-folder 가 막힌다.
    """
    while True:
        try:
            ev, holder = state.gui_q.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            with _dialog_lock:
                holder["path"] = _ask_directory()
        except Exception as e:               # noqa: BLE001 — GUI 실패도 워커에 전달·복구
            holder["error"] = str(e)
        finally:
            ev.set()


def _probe_tkinter() -> None:
    """무디스플레이 tkinter/Tcl 번들 프로브 — `_tkinter` C확장 로드 + Tcl 인터프리터 생성.
    창(Tk)이 아니라 Tcl 만 만들어 디스플레이 불요 → headless CI 에서도 실행. 번들 누락 시 raise."""
    import tkinter                       # frozen 에 _tkinter 미번들이면 ImportError
    tkinter.Tcl()                        # Tcl 데이터(init.tcl 등) 미번들이면 TclError


def _selfcheck() -> int:
    """`--selfcheck`: frozen 번들 무결성 오프라인 프로브(AC-8) — 프롬프트 번들 + tkinter/Tcl.
    서버를 기동하지 않고 성공 0 반환. 실패는 raise(패키징 스모크 hard gate)."""
    _summary_prompt()                    # 프롬프트 번들 확인(부재/빈파일=raise·M4)
    _probe_tkinter()                     # _tkinter + Tcl 데이터 번들 확인
    print("selfcheck OK", file=sys.stderr)
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--selfcheck" in args:            # 패키징 검증 프로브 — 서버 기동 전 조기 return
        return _selfcheck()
    _summary_prompt()                    # preflight — 프롬프트 번들 누락/빈파일이면 기동 실패(M4)
    output_root = _resolve_output_dir()
    storage.cleanup_stale_temp(output_root, _STALE_TEMP_MAX_AGE_SEC)   # 라이브 temp age-based 보존
    httpd, _nonce = _bind_with_fallback(output_root)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("labyscribe 웹 UI: http://127.0.0.1:%d/" % port, file=sys.stderr)
    try:
        webbrowser.open("http://127.0.0.1:%d/" % port)
    except Exception:
        pass                             # 브라우저 자동열기 실패는 비치명(URL 출력됨)
    try:
        _gui_loop(httpd.state)           # 메인스레드 점유(tkinter)
    except KeyboardInterrupt:
        pass
    return 0


# ── 인라인 프론트 (nonce 주입·textContent 만·CSP 준수) ──────────────

def _render_index(nonce: str) -> str:
    return _INDEX_HTML.replace("{{NONCE}}", nonce)


_INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>labyscribe</title>
<style>
:root { --bg:#0f1115; --card:#181b22; --line:#262b36; --fg:#e6e9ef; --muted:#98a0b3;
        --accent:#6ea8fe; --ok:#3fb950; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:760px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }
h1 { font-size:1.5rem; letter-spacing:-.02em; margin:0 0 .25rem; }
.sub { color:var(--muted); margin:0 0 1.75rem; font-size:.9rem; }
.row { display:flex; gap:.5rem; }
input[type=text] { flex:1; padding:.7rem .85rem; background:var(--card);
       border:1px solid var(--line); border-radius:9px; color:var(--fg); font-size:.95rem; }
input[type=text]:focus { outline:none; border-color:var(--accent); }
button { padding:.7rem 1rem; background:var(--accent); color:#0b1020; border:0;
       border-radius:9px; font-weight:600; cursor:pointer; font-size:.9rem; }
button.ghost { background:var(--card); color:var(--fg); border:1px solid var(--line); }
button:disabled { opacity:.5; cursor:default; }
.tools { display:flex; gap:.5rem; flex-wrap:wrap; margin:1.25rem 0; }
#status { color:var(--muted); font-size:.88rem; min-height:1.2em; margin:.75rem 0; }
#title { font-weight:600; margin:1.5rem 0 .5rem; }
ul { list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:.5rem; }
li { display:flex; align-items:center; gap:.75rem; padding:.7rem .85rem;
     background:var(--card); border:1px solid var(--line); border-radius:9px; }
li .meta { flex:1; min-width:0; }
li .pt { font-weight:600; }
li .ch { color:var(--muted); font-size:.85rem; overflow:hidden; text-overflow:ellipsis;
         white-space:nowrap; }
li .bytes { color:var(--muted); font-size:.78rem; }
.copied { color:var(--ok); }
</style>
</head>
<body>
<main>
  <h1>labyscribe</h1>
  <p class="sub">유튜브 자막을 챕터별 Markdown 으로 추출 → 챗봇에 붙여넣어 요약</p>
  <div class="row">
    <input type="text" id="url" placeholder="https://youtu.be/… 붙여넣기"
           autocomplete="off" spellcheck="false">
    <button id="go">추출</button>
  </div>
  <div id="status"></div>
  <div class="tools" id="tools" hidden>
    <button class="ghost" id="copyPrompt">요약 프롬프트 복사</button>
    <button class="ghost" id="copyAll">전체 복사</button>
    <button class="ghost" id="pick">저장 폴더 선택</button>
    <button class="ghost" id="save" disabled>이 폴더에 저장</button>
  </div>
  <div id="title"></div>
  <ul id="parts"></ul>
</main>
<script nonce="{{NONCE}}">
const NONCE = "{{NONCE}}";
const $ = (id) => document.getElementById(id);
let RESULT = null, CAP = null;

async function api(method, path, body) {
  const opt = { method, headers: { "X-Labyscribe-Nonce": NONCE } };
  if (body) { opt.headers["Content-Type"] = "application/json";
              opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data.error && data.error.message) || ("HTTP " + res.status));
  return data;
}

function setStatus(msg) { $("status").textContent = msg; }   // textContent = XSS 안전

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {                                                // 권한거부 → 수동 복사 폴백
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    setStatus("클립보드 권한이 없어 텍스트를 선택했습니다. 직접 복사(Cmd/Ctrl+C)하세요.");
    return;
  }
  if (btn) { const t = btn.textContent; btn.textContent = "복사됨 ✓";
             btn.classList.add("copied");
             setTimeout(() => { btn.textContent = t; btn.classList.remove("copied"); }, 1200); }
}

function renderParts(parts) {
  const ul = $("parts"); ul.textContent = "";
  parts.forEach((p) => {
    const li = document.createElement("li");
    const meta = document.createElement("div"); meta.className = "meta";
    const pt = document.createElement("div"); pt.className = "pt";
    pt.textContent = "파트 " + p.part_no + " / " + parts.length;
    const ch = document.createElement("div"); ch.className = "ch";
    ch.textContent = p.title ? p.title : "(챕터 없음)";      // 외부 데이터 = textContent
    meta.appendChild(pt); meta.appendChild(ch);
    const by = document.createElement("span"); by.className = "bytes";
    by.textContent = Math.round(p.bytes / 1024) + " KB";
    const btn = document.createElement("button");
    btn.textContent = "복사";
    btn.addEventListener("click", async () => {
      const d = await api("GET", "/api/part/" + RESULT.result_id + "/" + p.part_no);
      copyText(d.markdown, btn);
    });
    li.appendChild(meta); li.appendChild(by); li.appendChild(btn);
    ul.appendChild(li);
  });
}

$("go").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!url) return;
  $("go").disabled = true; setStatus("추출 중… (자막 다운로드·챕터 분할)");
  try {
    RESULT = await api("POST", "/api/extract", { url });
    $("title").textContent = RESULT.title || "";
    renderParts(RESULT.parts);
    $("tools").hidden = false;
    setStatus(RESULT.parts.length + "개 파트. 요약 프롬프트 복사 → 전체 복사(한 번에) 또는 파트별로 챗봇에 붙여넣으세요.");
  } catch (e) { setStatus("실패: " + e.message); }
  finally { $("go").disabled = false; }
});

$("copyPrompt").addEventListener("click", (e) => {
  if (RESULT) copyText(RESULT.summary_prompt, e.currentTarget);
});

$("copyAll").addEventListener("click", async (e) => {
  if (!RESULT) return;
  try {                                                       // 통합 transcript 한 번에 복사
    const d = await api("GET", "/api/transcript/" + RESULT.result_id);
    copyText(d.markdown, e.currentTarget);
  } catch (err) { setStatus("전체 복사 실패: " + err.message); }
});

$("pick").addEventListener("click", async () => {
  setStatus("폴더 선택 대화상자를 확인하세요…");
  try {
    const d = await api("POST", "/api/pick-folder", {});
    if (d.status === "cancelled") { setStatus("폴더 선택이 취소되었습니다."); return; }
    CAP = d.capability_id;
    $("save").disabled = false;
    setStatus("저장 폴더: " + d.display_name);                // display_name = textContent
  } catch (e) { setStatus("폴더 선택 실패: " + e.message); }
});

$("save").addEventListener("click", async () => {
  if (!RESULT || !CAP) return;
  setStatus("저장 중…");
  try {
    const d = await api("POST", "/api/save",
                        { result_id: RESULT.result_id, capability_id: CAP });
    setStatus("저장됨: " + d.saved_names.join(", "));
  } catch (e) { setStatus("저장 실패: " + e.message); }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
