"""웹 검색 (Selenium으로 검색엔진 결과 페이지를 긁는다).

연구실/개인 페이지 링크가 아예 없는 교수를 위해 이름으로 재검색한다.
LLM 서버 툴이나 별도 검색 API 키 없이, 이미 쓰고 있는 브라우저로
검색엔진 결과 페이지에 접속해 링크를 수집한다.

반환값은 "제목 / URL" 텍스트 블록이며, 이후 llm.judge_search_results가
그중 교수 본인의 페이지가 맞는 것을 고른다.
"""

from __future__ import annotations

import logging
import urllib.parse

from . import config
from .browser import Browser

log = logging.getLogger(__name__)


def search_web(browser: Browser, query: str) -> str:
    """query로 검색엔진 결과 페이지를 열고, 후보 링크를 텍스트로 반환한다."""
    url = config.SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))
    fetch = browser.fetch(url)
    if not fetch.ok:
        log.warning("검색 결과 페이지 접근 실패(%s): %s", fetch.failure_reason, url)
        return ""

    lines: list[str] = []
    for link in fetch.links:
        host = urllib.parse.urlparse(link.href).netloc.lower()
        # 검색엔진 자기 도메인/SNS 등 노이즈는 제외.
        if any(bad in host for bad in config.SEARCH_EXCLUDE_HOSTS):
            continue
        text = link.text.strip() or "(no text)"
        lines.append(f"- {text}\n  {link.href}")
        if len(lines) >= config.MAX_SEARCH_RESULTS:
            break
    return "\n".join(lines)
