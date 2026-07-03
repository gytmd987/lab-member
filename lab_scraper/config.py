"""전역 설정값.

환경변수로 덮어쓸 수 있게 해서 테스트/운영 전환을 쉽게 한다.
"""

from __future__ import annotations

import os

# --- LLM ---------------------------------------------------------------
# 기본은 Opus 4.8. 구성원 판단 같은 짧은 분류 호출이 대부분이라
# thinking 없이 lean 하게 돌린다(교수 1명당 호출 수를 아끼려는 목적).
MODEL = os.environ.get("LAB_SCRAPER_MODEL", "claude-opus-4-8")

# LLM에 넘기는 페이지 텍스트 최대 길이(문자). 이 이상은 잘라내되,
# 잘렸다는 사실은 호출부에서 로깅한다(무단 절삭 금지).
MAX_PAGE_CHARS = int(os.environ.get("LAB_SCRAPER_MAX_PAGE_CHARS", "40000"))

# 탐색 단계에서 LLM에 넘길 링크 최대 개수.
MAX_LINKS = int(os.environ.get("LAB_SCRAPER_MAX_LINKS", "150"))

# --- Selenium ----------------------------------------------------------
# 페이지 로드 타임아웃(초). 초과하면 "접근 실패"로 기록.
PAGE_LOAD_TIMEOUT = int(os.environ.get("LAB_SCRAPER_PAGE_TIMEOUT", "30"))

# JS 렌더링을 기다리는 시간(초).
RENDER_WAIT = float(os.environ.get("LAB_SCRAPER_RENDER_WAIT", "2.5"))

# 본문 텍스트가 이 길이 미만이면서 이미지/캔버스 위주면
# "콘텐츠 추출 불가"(접근 실패)로 본다.
MIN_EXTRACTABLE_CHARS = int(os.environ.get("LAB_SCRAPER_MIN_TEXT", "40"))

# Chromium 실행 파일 경로(선택). 환경에 이미 설치된 바이너리를 쓰고 싶을 때.
CHROME_BINARY = os.environ.get("LAB_SCRAPER_CHROME_BINARY") or None
CHROMEDRIVER_PATH = os.environ.get("LAB_SCRAPER_CHROMEDRIVER") or None

# --- 검색 --------------------------------------------------------------
# 연구실 링크를 못 찾았을 때 교수 이름으로 재검색하는 최대 시도 횟수.
MAX_SEARCH_ATTEMPTS = int(os.environ.get("LAB_SCRAPER_MAX_SEARCH", "2"))
