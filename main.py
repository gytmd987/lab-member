"""연구실 구성원 스크래퍼 실행 진입점.

사용법:
    python main.py professors.json            # 결과를 stdout(JSON)으로
    python main.py professors.json out.json   # 파일로 저장

입력 JSON 형식 (교수 배열):
    [
      {"name": "홍길동", "department": "컴퓨터공학과", "lab_url": "https://..."},
      {"name": "김철수", "department": "전자공학과"}
    ]
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict

from lab_scraper import llm
from lab_scraper.browser import Browser
from lab_scraper.models import ProfessorInput
from lab_scraper.scraper import process_professor


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


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) < 2:
        print(__doc__)
        return 2

    professors = load_professors(argv[1])
    out_path = argv[2] if len(argv) > 2 else None

    judge = llm.LLM()
    results = []
    with Browser() as browser:
        for prof in professors:
            logging.info("처리 시작: %s", prof.name)
            res = process_professor(prof, browser, judge)
            logging.info(
                "  → %s (members=%d, llm_calls=%d)",
                res.status.value, len(res.members), res.llm_calls,
            )
            results.append(_result_to_dict(res))

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload)
        logging.info("결과 저장: %s", out_path)
    else:
        print(payload)
    return 0


def _result_to_dict(res) -> dict:
    return {
        "professor": asdict(res.professor),
        "status": res.status.value,
        "site_url": res.site_url,
        "members": [asdict(m) for m in res.members],
        "detail": res.detail,
        "llm_calls": res.llm_calls,
    }


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
