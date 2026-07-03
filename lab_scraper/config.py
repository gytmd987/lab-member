"""전역 설정값.

환경변수로 덮어쓸 수 있게 해서 테스트/운영 전환을 쉽게 한다.
"""

from __future__ import annotations

import json
import os

# --- LLM (사내 OpenAI 호환 API) ---------------------------------------
# openai SDK에 base_url을 사내 것으로 바꾸고, 인증/식별은 헤더로 붙인다.
# (사내 게이트웨이는 OPENAI_API_KEY가 아니라 x-dep-ticket 헤더로 인증하므로
#  api_key는 더미여도 된다. 단, openai SDK가 빈 값이면 에러라 더미를 준다.)
LLM_BASE_URL = os.environ.get("LAB_SCRAPER_LLM_BASE_URL")  # 예: http://llm.internal/...
LLM_API_KEY = os.environ.get("LAB_SCRAPER_LLM_API_KEY", "dummy")  # 대개 더미
MODEL = os.environ.get("LAB_SCRAPER_MODEL", "gemma4")  # 사내 모델 이름으로 지정

# --- 사내 게이트웨이 헤더 (고정, 앱 공통) ------------------------------
# 사내 API 예제의 default_headers에 대응. 값은 환경변수로 지정한다.
#   x-dep-ticket     : 인증 크리덴셜 (예: "credential:TICKET-....")
#   Send-System-Name : 호출 시스템 이름
#   User-Id / User-Type : 호출 사용자 식별
LLM_CREDENTIAL_KEY = os.environ.get("LAB_SCRAPER_LLM_CREDENTIAL_KEY", "")
LLM_SYSTEM_NAME = os.environ.get("LAB_SCRAPER_LLM_SYSTEM_NAME", "")
LLM_USER_ID = os.environ.get("LAB_SCRAPER_LLM_USER_ID", "")
LLM_USER_TYPE = os.environ.get("LAB_SCRAPER_LLM_USER_TYPE", "")


def _load_json_env(name: str) -> dict:
    """환경변수를 JSON dict로 파싱한다. 없거나 잘못됐으면 빈 dict."""
    raw = os.environ.get(name)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return val if isinstance(val, dict) else {}


# 위 4개 외에 게이트웨이가 요구하는 추가 고정 헤더가 있으면 여기에.
# 예: export LAB_SCRAPER_LLM_HEADERS='{"X-Extra":"value"}'
LLM_EXTRA_HEADERS: dict = _load_json_env("LAB_SCRAPER_LLM_HEADERS")


def fixed_headers() -> dict:
    """OpenAI 클라이언트 default_headers로 넘길 고정 헤더 dict를 만든다.

    값이 빈 항목은 제외한다. Prompt-Msg-Id / Completion-Msg-Id 처럼 요청마다
    달라져야 하는 헤더는 여기 넣지 않고 호출 시점에 생성한다(llm.py 참고).
    """
    headers = {
        "x-dep-ticket": LLM_CREDENTIAL_KEY,
        "Send-System-Name": LLM_SYSTEM_NAME,
        "User-Id": LLM_USER_ID,
        "User-Type": LLM_USER_TYPE,
    }
    headers = {k: v for k, v in headers.items() if v}
    headers.update(LLM_EXTRA_HEADERS)
    return headers

# 사내 API가 JSON 스키마 강제(structured outputs)까지는 아니고 JSON 모드만
# 지원하므로, response_format={"type":"json_object"}로 요청하고 프롬프트로
# 형식을 지정한 뒤 Pydantic으로 검증한다. 게이트웨이가 이 파라미터를 거부하면
# 아래를 false로 두면 프롬프트만으로 JSON을 유도한다.
USE_JSON_MODE = os.environ.get("LAB_SCRAPER_JSON_MODE", "1") not in ("0", "false", "")

# JSON 파싱 실패 시 재시도 횟수.
JSON_RETRIES = int(os.environ.get("LAB_SCRAPER_JSON_RETRIES", "1"))

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

# --- 검색 (Selenium으로 검색엔진 결과 페이지를 긁는다) ------------------
# 연구실 링크를 못 찾았을 때 교수 이름으로 재검색하는 최대 시도 횟수.
MAX_SEARCH_ATTEMPTS = int(os.environ.get("LAB_SCRAPER_MAX_SEARCH", "2"))

# 검색 URL 템플릿. {query}에 URL 인코딩된 검색어가 들어간다.
# 봇 차단이 덜한 엔진으로 바꿔가며 쓸 수 있게 환경변수로 뺀다.
SEARCH_URL_TEMPLATE = os.environ.get(
    "LAB_SCRAPER_SEARCH_URL",
    "https://www.bing.com/search?q={query}",
)

# 검색 결과에서 제외할 호스트(검색엔진 자기 도메인, SNS 등 노이즈).
SEARCH_EXCLUDE_HOSTS = tuple(
    h.strip()
    for h in os.environ.get(
        "LAB_SCRAPER_SEARCH_EXCLUDE",
        "bing.com,google.com,naver.com,microsoft.com,go.microsoft.com,"
        "youtube.com,facebook.com,twitter.com,x.com,instagram.com",
    ).split(",")
    if h.strip()
)

# LLM에 넘길 검색 결과(링크) 최대 개수.
MAX_SEARCH_RESULTS = int(os.environ.get("LAB_SCRAPER_MAX_SEARCH_RESULTS", "25"))
