# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 정체성 (WHY)

labyscribe = 개인 "영상요약" 스킬의 **로컬 MCP 공유 에디션**. 이미 검증된 개인 스킬을 남이 쓸 수 있게 오픈소스화한다.
- 요약은 사용자 각자의 호스트(Claude Desktop 등 구독)가 수행 → labyscribe(MCP)는 **추출·페이징·프롬프트 계약만** 진다. 요약 로직·비용 0 = "무료 메커니즘".
- Labylinx에서 파생된 **독립 git repo** — 자체 커밋 필요(Labylinx `/저장-L`은 이 repo git을 건드리지 않음).

## 계승한 장점 (원본 스킬에서 이미 구현·검증 성공)

원본 `~/.claude/skills/영상요약`이 목표했던 특성이 스킬 단계에서 구현·검증됐고, labyscribe는 그 결정론적 추출 코어를 **그대로 계승**한다. 아래는 마케팅 문구가 아니라 코드로 강제되는 성질이다:

- **정확성(충실도 우선)** — 결정론적 파이프라인이 낳는다:
  - 원어 자막 우선 선택(`select_track`) → 자동번역 자막의 **이중 손실 회피**(원어를 받아 호스트가 번역)
  - 롤링 자동자막 dedup + **시간가드**(`_ROLLING_GAP_SEC`) → 중복은 접되 **원거리 정상 반복은 과삭제하지 않음**
  - 원본 자막을 `raw/`에 **불변 보존** → 만일의 과삭제도 복구 가능
  - 태그 완전 strip · 10분 마커 → transcript 충실도 유지
  - 동봉 요약 프롬프트 = "무손실 편집자"(모든 논지·예시·수치·구간 보존 + 뒤 구간 자가점검으로 후반부 부실화 차단)
- **신속성** — 자막 직접 추출(yt-dlp)이라 **STT·ffmpeg 불필요**. 추출은 순수 결정론(**LLM 호출 0**)이라 빠르고 재현 가능.
- **silent-failure 0** — 자막 없음/정제 무효를 가짜 성공으로 넘기지 않음(종료코드 계약 + `quality_ok` 게이트).

## 설계 SSOT (참조 우선순위)

1. 계획·헌장: `~/Projects/Labylinx/docs/superpowers/specs/2026-07-14-video-summary-mcp-share-plan.md` (7단계 로드맵)
2. 원본 스킬 설계: `Labylinx/docs/superpowers/specs/2026-07-13-video-summary-skill-design.md` (v3 · "무손실" 정의 = 원본 불변 보존)
3. Phase별 설계의도·구현노트: 이 repo `.harness/design_intend*.md`, `.harness/impl_notes.md`

**로드맵 현황**: Phase 0(추출 코어)·Phase 1(`parse_vtt`)·Phase 2(MCP stdio 서버)·Phase 3(저장 안전) **완료** · Phase 4(보안 경화)~Phase 6(E2E) 대기.

## 명령어

- 추출 CLI: `python extract.py <youtube-url> --out <root>` (`--out` = 저장 **루트** · `--lang ko,en` 선택 · 미지정 시 원어 자동감지·우선)
- MCP 서버: `python server.py`(stdio · 호스트가 `extract_transcript`/`get_transcript_part` 도구·`summarize_video` 프롬프트 사용 · 저장 루트 env `OUTPUT_DIR`, 기본 `~/labyscribe`)
- 요구: **yt-dlp on PATH** · Python ≥ 3.10(`mcp` SDK) · **ffmpeg 불필요**
- 테스트: `.venv/bin/python -m pytest` (또는 `pip install -e ".[dev]" && pytest`)
- 단일 테스트: `.venv/bin/python -m pytest tests/test_parser.py -k <name>`
- 산출물(Phase 3 불변 버전 디렉토리): `<root>/<video_id>/<lang>-<hash>/` 아래 `transcript.txt`(10분 마커) · `raw/<id>.<lang>.vtt`(원본 불변) · `meta.json`(`orig_lang`·`translated`·`lang`·`status`). 세트를 temp 에 완결 후 디렉토리째 atomic rename(부분손상 0) · 재실행=재조회(캐시 히트).

## 아키텍처 (extract.py — 큰 그림)

**2층 구조 — 이 분리가 테스트 전략과 오류 계약의 뼈대다:**
- **순수함수층**(단위테스트 대상 · **절대 raise 안 함** · 빈 결과 반환): `validate_url`(SSRF allowlist) → `select_track`(원어 우선) → `parse_vtt`(코어) → `quality_ok`
- **오케스트레이션층**(subprocess): `run_ytdlp_json` → `download_sub`(지수 백오프 재시도) → **`run_extract`**(오케스트레이션 SSOT·`ExtractResult` 반환) → `main`/`server`(얇은 배선)

**종료코드가 계약** — `run_extract`의 `ExtractResult.exit_code`를 `main`·`server`가 성공/에러를 가르는 유일 판별자:
`0` OK · `2` NO_SUBTITLE · `3` DOWNLOAD_FAILED(재시도성) · `4` UNAVAILABLE · `5` BAD_INPUT · `6` EMPTY_TRANSCRIPT · `7` STORAGE_LIMIT(디스크 총량·Phase 3) · `8` SUBTITLE_TOO_LARGE(자막 파일 하드캡·Phase 3)
- silent-failure 차단이 설계 축: `quality_ok(_speech_text(...))`가 빈/음향-only(`[Music]`) transcript를 exit 6으로 잡는다.

**`parse_vtt` = load-bearing 코어** (`_parse_vtt_cues` → `_dedup_rolling` → 마커 렌더):
- 롤링(인라인 타이밍 태그 `<00:..>`) vs 정적 자막을 자동 분기 · 시간가드로 과삭제 방지 · `start ≤ end` 검증 · 손상입력 부분복구(크래시 0)
- `clean_srt`/`parse_srt`는 **파이프라인 미사용** 라이브러리 표면(현 파이프라인은 vtt 전용) — 삭제하지 말고 유지. 공통 헬퍼로 추출하면 `clean_srt` 거동이 조용히 바뀌므로 각각 독립.

## MCP 서버층 (`server.py` FastMCP · Phase 2) — 순수/IO 경계

- 툴 본체 = plain `_do_extract`/`_do_get_part` + `@mcp.tool` **얇은 래퍼**(계약 테스트가 SDK 우회로 직접 호출) · 프롬프트 `@mcp.prompt summarize_video`.
- `_assemble` **allowlist 8필드 투영** = 절대경로·내부 경로키 미노출. env `OUTPUT_DIR` 결합은 `_resolve_output_dir` 경계에만.
- **순수 모듈**(mcp/네트워크 import 0 · 결정적 단위테스트): `handles.py`(불투명 `secrets` 난수 핸들·`OrderedDict` LRU·`threading.Lock`) · `paging.py`(바이트 상한 페이징·10분 경계·UTF-8 무절단·무손실 재구성).
- 핸들 = 프로세스 생애 인메모리(재시작=무효). 재조회는 `extract_transcript` 재호출 시 저장본 캐시 히트로 복구(Phase 3).

## 저장 안전층 (`storage.py` · Phase 3) — 순수/IO 경계

- **불변 버전 디렉토리** `<root>/<video_id>/<lang>-<hash>/` — 세트(meta+raw/*+transcript)를 temp 완결 후 **디렉토리째 atomic rename**(부분손상 0). `run_extract(url,lang,root)` 가 temp/final 관리.
- **동시성 = flock 미도입 · rename idempotency** — 경쟁 패자는 EEXIST→재조회(단일사용자 로컬·상시구동 MCP). stale temp = 시작 시 **age-based** 정리(라이브 보존).
- **재실행=재조회** = `find_cached` glob `<video_id>/<lang>-*/` 히트 시 자막 다운로드 스킵(info 1회).
- **파일시스템 안전** = 0700 + O_NOFOLLOW + **realpath containment**(자기 경로 이탈 차단·Phase 4 allowlist 와 층 분리) + fsync(파일+parent+root).
- **자원 상한** = 디스크 총량 SOFT 경고·HARD 거부(자동삭제 0·원본 불변) · 자막 다운로드 하드캡(`--max-filesize`+stat) · `run_capped`(subprocess 출력 메모리 캡).
- **순수층**(fs 무의존·raise 금지): `is_safe_component`·`is_within`·`version_dir_name`. **잔여**(사용자 확정 비례): 실행중 peak-disk·disk TOCTOU = Phase 6 실측.

## 개발 규율

- `.harness/` **하네스 워크플로우**로 개발(토의→기획→실행→검증→마무리) · **codex 교차감사** 반영
- **Phase 경계 엄수**: 각 Phase 설계의도에 '의도적 제외' 목록이 있다 — 저장 원자성 = Phase 3(완료) · 보안경화(URL IDNA/redirect·일반 id/tag allowlist 심층) = Phase 4 · 패키징(PyInstaller/`.mcpb`) = Phase 5 · 실측 상한값·E2E = Phase 6. 다음 Phase 항목을 범위로 당겨오지 말 것.
- 순수함수는 불변·raise 금지(판정은 `main`의 게이트가) · 신규 종료코드는 계약 레지스트리와 충돌 확인 · 새 런타임 의존성 추가 전 재고("의존성 = yt-dlp 하나" 원칙).
