"""연구실 구성원 스크래핑 패키지.

학과 페이지 → 교수별 연구실/개인 페이지 → 구성원 명단을 수집한다.
도메인 종류(.ac.kr / Wix / GitHub Pages / Notion 등)로 거르지 않고,
어떤 형태의 사이트든 실제로 방문해서 LLM이 텍스트·문맥으로 판단한다.
실패는 "접근 실패"로만 정의하며, 그 외에는 정상 처리한다.
"""

from .models import Member, ProfessorInput, ProfessorResult, ProcessStatus

__all__ = [
    "Member",
    "ProfessorInput",
    "ProfessorResult",
    "ProcessStatus",
]
