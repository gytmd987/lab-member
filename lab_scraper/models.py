"""결과 데이터 모델과 처리 상태 정의.

처리 결과는 표(설계 논의)의 4가지 상태 중 하나로 귀결된다:

  | 상황                          | 상태                |
  |-------------------------------|---------------------|
  | 사이트 접속 불가(404/타임아웃)| ACCESS_FAILED       |
  | 도메인이 학교 밖              | (분류 아님, 정상 방문) |
  | 구성원 페이지 못 찾음(1차)    | (재시도로 흡수)      |
  | 사이트는 있는데 명단 자체 없음 | NO_MEMBER_INFO      |
  | 명단 수집 성공                | SUCCESS             |
  | 연구실 사이트 자체를 못 찾음  | SITE_NOT_FOUND      |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProcessStatus(str, Enum):
    SUCCESS = "success"                # 구성원 명단 수집 성공
    NO_MEMBER_INFO = "no_member_info"  # 사이트 정상, 그러나 명단 미공개(정상)
    SITE_NOT_FOUND = "site_not_found"  # 연구실/개인 페이지 자체를 못 찾음
    ACCESS_FAILED = "access_failed"    # 404/타임아웃/로그인필요/추출불가


@dataclass
class Member:
    """구성원 1명. 현재 소속 포닥/박사과정/석사과정(석박통합 포함)만 수집한다.

    페이지에 없는 한글명/영문명은 LLM이 추정하고 값 뒤에 "(추정)"을 붙인다.
    """

    name_kr: str | None = None       # 한글명
    name_en: str | None = None       # 영문명
    position: str | None = None      # 포닥 | 박사과정 | 석사과정 | 석박통합
    email: str | None = None
    phone: str | None = None
    homepage: str | None = None      # 개인페이지 절대 URL
    research_area: str | None = None


@dataclass
class ProfessorInput:
    """스크래핑 대상 교수 1명의 입력 정보."""

    name: str
    department: str | None = None
    lab_url: str | None = None       # 학과 페이지에서 확보한 연구실 링크(없을 수 있음)


@dataclass
class ProfessorResult:
    professor: ProfessorInput
    status: ProcessStatus
    site_url: str | None = None          # 실제로 방문/판정한 사이트 URL
    members: list[Member] = field(default_factory=list)
    detail: str | None = None            # 실패 사유 / LLM 판단 근거 등
    llm_calls: int = 0                   # 이 교수 처리에 쓴 LLM 호출 수(비용 추적)

    @property
    def ok(self) -> bool:
        return self.status in (ProcessStatus.SUCCESS, ProcessStatus.NO_MEMBER_INFO)
