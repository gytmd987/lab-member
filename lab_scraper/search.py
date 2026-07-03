"""웹 검색 제공자.

연구실/개인 페이지 링크가 아예 없는 교수를 위해 이름으로 재검색한다.
별도 검색 API 키 없이 Claude의 web_search 서버 툴을 사용한다.

반환값은 "제목 / URL / 요약" 텍스트 블록이며, 이후 llm.judge_search_results가
그중 교수 본인의 페이지가 맞는 것을 고른다.
"""

from __future__ import annotations

import logging

import anthropic

from . import config

log = logging.getLogger(__name__)


def search_web(client: anthropic.Anthropic, query: str, max_uses: int = 3) -> str:
    """query로 웹 검색을 돌리고, 검색 결과를 사람이 읽을 수 있는 텍스트로 반환한다."""
    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=2048,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
        messages=[
            {
                "role": "user",
                "content": (
                    "Search the web for the following and just gather candidate result "
                    "links; do not answer in prose:\n" + query
                ),
            }
        ],
    )

    lines: list[str] = []
    for block in resp.content:
        # web_search 결과는 web_search_tool_result 블록으로 온다.
        if getattr(block, "type", None) == "web_search_tool_result":
            content = getattr(block, "content", None)
            # 에러면 content가 리스트가 아니라 단일 에러 객체다.
            if isinstance(content, list):
                for r in content:
                    title = getattr(r, "title", "") or ""
                    url = getattr(r, "url", "") or ""
                    snippet = (getattr(r, "encrypted_content", "") or "")[:0]  # snippet 미노출 대비
                    lines.append(f"- {title}\n  {url}\n  {snippet}".rstrip())
            else:
                err = getattr(content, "error_code", content)
                log.warning("web_search error: %s", err)
    return "\n".join(lines)
