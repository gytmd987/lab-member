"""결과를 xlwings로 엑셀(.xlsx) 파일에 쓴다.

두 개의 시트를 만든다:
  - "요약": 교수 1명당 1행 (상태 / 사이트 / 구성원 수 / LLM 호출수 / 비고)
  - "구성원": 수집한 구성원을 교수별로 펼친 명단

주의: xlwings는 Excel 애플리케이션을 구동해 파일을 만든다(사내 PC에 Excel 설치
필요). 헤드리스/리눅스 CI에서는 동작하지 않으니, 그런 환경에선 openpyxl 등으로
바꿔야 한다.
"""

from __future__ import annotations

import logging
import os

import xlwings as xw

from .models import ProcessStatus, ProfessorResult

log = logging.getLogger(__name__)

# 상태 코드 → 한글 라벨.
_STATUS_LABEL = {
    ProcessStatus.SUCCESS: "성공",
    ProcessStatus.NO_MEMBER_INFO: "정보없음(정상)",
    ProcessStatus.SITE_NOT_FOUND: "사이트 못찾음",
    ProcessStatus.ACCESS_FAILED: "접근 실패",
}

_SUMMARY_HEADERS = ["교수명", "학과", "상태", "사이트 URL", "구성원 수", "LLM 호출수", "비고"]
_MEMBER_HEADERS = ["교수명", "학과", "이름", "역할", "이메일", "부가정보"]


def write_results(results: list[ProfessorResult], path: str) -> str:
    """결과를 엑셀 파일로 저장하고, 저장된 절대경로를 반환한다."""
    abspath = os.path.abspath(path)

    summary_rows = [
        [
            r.professor.name,
            r.professor.department or "",
            _STATUS_LABEL.get(r.status, r.status.value),
            r.site_url or "",
            len(r.members),
            r.llm_calls,
            r.detail or "",
        ]
        for r in results
    ]

    member_rows = [
        [
            r.professor.name,
            r.professor.department or "",
            m.name,
            m.role or "",
            m.email or "",
            m.extra or "",
        ]
        for r in results
        for m in r.members
    ]

    app = xw.App(visible=False, add_book=False)
    try:
        wb = app.books.add()

        summary = wb.sheets[0]
        summary.name = "요약"
        _fill_sheet(summary, _SUMMARY_HEADERS, summary_rows)

        members = wb.sheets.add("구성원", after=summary)
        _fill_sheet(members, _MEMBER_HEADERS, member_rows)

        summary.activate()
        wb.save(abspath)
        wb.close()
    finally:
        app.quit()

    log.info("엑셀 저장: %s", abspath)
    return abspath


def _fill_sheet(sht, headers: list[str], rows: list[list]) -> None:
    """헤더 + 데이터를 채우고, 헤더 굵게 + 열 너비 자동조정."""
    sht.range("A1").value = [headers]
    if rows:
        sht.range("A2").value = rows

    last_col = _col_letter(len(headers))
    sht.range(f"A1:{last_col}1").font.bold = True
    try:
        sht.autofit("columns")
    except Exception:  # 환경에 따라 autofit이 없을 수 있으니 실패해도 무시
        pass


def _col_letter(n: int) -> str:
    """1 → 'A', 2 → 'B' ... 열 번호를 엑셀 열 문자로."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters
