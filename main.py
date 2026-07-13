"""연구실 구성원 스크래퍼 실행 진입점.

사용법:
    python main.py
        → 학과 교수진 페이지 URL을 입력받아, 교수 목록을 자동 수집하고
          각 교수의 연구실 구성원을 찾는다.

    python main.py https://me.snu.ac.kr/faculty
        → URL을 인자로 바로 지정(입력 프롬프트 생략).

    python main.py professors.json
        → 교수 목록 JSON 파일로 실행(자동 수집 대신 직접 지정).

    두 경우 모두 두 번째 인자로 결과 파일명을 줄 수 있다:
        python main.py <url|json> out.xlsx

교수 목록 JSON 형식 (배열):
    [
      {"name": "홍길동", "department": "서울대학교 기계공학부", "lab_url": "https://..."},
      {"name": "김철수", "department": "서울대학교 기계공학부"}
    ]

교수 처리는 LAB_SCRAPER_CONCURRENCY(기본 4)명씩 병렬로 진행되며,
결과는 xlwings로 엑셀 파일에 저장된다("요약" 시트 + "구성원" 시트).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from lab_scraper import config, excel_out, llm
from lab_scraper.browser import Browser
from lab_scraper.models import ProfessorInput, ProfessorResult
from lab_scraper.scraper import collect_professors, process_professor

DEFAULT_OUTPUT = "results.xlsx"

log = logging.getLogger(__name__)


def load_professors(path: str) -> list[ProfessorInput]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        ProfessorInput(
            name=item["name"],
            department=item.get("department"),
            lab_url=item.get("lab_url"),
        )
        for item in raw
    ]


def _looks_like_json(arg: str) -> bool:
    return arg.lower().endswith(".json")


class _BrowserPerThread:
    """스레드마다 브라우저 1개를 lazy 생성하고, 종료 시 전부 정리한다.

    Selenium 드라이버는 스레드 안전하지 않아 워커 스레드끼리 공유하면 안 된다.
    """

    def __init__(self) -> None:
        self._tls = threading.local()
        self._all: list[Browser] = []
        self._lock = threading.Lock()

    def get(self) -> Browser:
        b: Browser | None = getattr(self._tls, "browser", None)
        if b is None:
            b = Browser().__enter__()
            self._tls.browser = b
            with self._lock:
                self._all.append(b)
        return b

    def close_all(self) -> None:
        for b in self._all:
            try:
                b.__exit__(None, None, None)
            except Exception:
                pass
        self._all.clear()


def _run_parallel(
    professors: list[ProfessorInput], shared_client
) -> list[ProfessorResult]:
    """교수들을 CONCURRENCY명씩 병렬 처리한다. 입력 순서를 유지해 반환."""
    pool = _BrowserPerThread()

    def work(prof: ProfessorInput) -> ProfessorResult:
        # OpenAI 클라이언트는 공유(스레드 안전), LLM 인스턴스는 교수별로 만들어
        # llm_calls 카운터가 교수별로 정확히 잡히게 한다.
        judge = llm.LLM(client=shared_client)
        log.info("[%s] 처리 시작", prof.name)
        res = process_professor(prof, pool.get(), judge)
        log.info(
            "[%s] → %s (members=%d, llm_calls=%d)",
            prof.name, res.status.value, len(res.members), res.llm_calls,
        )
        return res

    try:
        with ThreadPoolExecutor(max_workers=config.CONCURRENCY) as ex:
            return list(ex.map(work, professors))
    finally:
        pool.close_all()


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(threadName)s %(name)s: %(message)s",
    )

    arg = argv[1] if len(argv) > 1 else None
    out_path = argv[2] if len(argv) > 2 else None

    judge = llm.LLM()

    # 1) 교수 목록 확보 (순차, 단일 브라우저) --------------------------------
    if arg and _looks_like_json(arg):
        professors = load_professors(arg)
        log.info("JSON에서 교수 %d명 로드", len(professors))
    else:
        dept_url = arg or input("학과 교수진 페이지 URL을 입력하세요: ").strip()
        if not dept_url:
            log.error("URL이 필요합니다.")
            return 2
        department = input("학과 이름(선택, 엔터로 건너뛰기): ").strip() or None
        log.info("학과 페이지에서 교수 목록 수집: %s", dept_url)
        with Browser() as browser:
            professors = collect_professors(dept_url, browser, judge, department)
        log.info("교수 %d명 발견", len(professors))
        if not professors:
            log.error("교수 목록을 찾지 못했습니다. URL을 확인하세요.")
            return 1

    # 2) 결과 파일명 결정 ---------------------------------------------------
    if not out_path:
        entered = input(f"결과 파일 이름(엔터={DEFAULT_OUTPUT}): ").strip()
        out_path = entered or DEFAULT_OUTPUT

    # 3) 교수별 병렬 처리 -----------------------------------------------------
    log.info("교수 %d명을 동시 %d명씩 병렬 처리 시작", len(professors), config.CONCURRENCY)
    results = _run_parallel(professors, judge.client)

    saved = excel_out.write_results(results, out_path)
    log.info("완료: %s (교수 %d명)", saved, len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
