"""LLM 판단 계층 (사내 OpenAI 호환 API).

도메인이 아니라 "텍스트·문맥"만 보고 판단하는 부분을 전부 모아둔다.

사내 API는 JSON 스키마 강제(structured outputs)까지는 아니고 JSON 모드만
지원하므로:
  1. 응답 스키마를 Pydantic 모델로 정의하고,
  2. 그 모델의 JSON 스키마를 프롬프트에 넣어 형식을 지정하고,
  3. response_format={"type":"json_object"}로 JSON을 유도한 뒤,
  4. Pydantic으로 검증하고, 실패하면 오류를 붙여 재시도한다.
"""

from __future__ import annotations

import json
import logging
import uuid

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from . import config
from .browser import Link
from .models import Member

log = logging.getLogger(__name__)


# --- 구조화 응답 스키마 -------------------------------------------------
class PageIdentification(BaseModel):
    is_target: bool = Field(description="이 페이지가 해당 교수의 연구실/개인 페이지가 맞는가")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class MemberPageDecision(BaseModel):
    found: bool = Field(description="구성원(Members/People/Lab/Team) 페이지 링크를 찾았는가")
    url: str | None = Field(default=None, description="찾았다면 이동할 절대 URL, 없으면 null")
    reasoning: str


class ExtractedMember(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None
    extra: str | None = None


class MemberExtraction(BaseModel):
    has_member_info: bool = Field(
        description="이 텍스트 안에 실제 구성원/학생 명단이 존재하는가"
    )
    members: list[ExtractedMember] = Field(default_factory=list)
    reasoning: str


class AbsenceVerdict(BaseModel):
    genuinely_absent: bool = Field(
        description="이 사이트가 애초에 구성원 정보를 공개하지 않는 것으로 보이는가"
        " (True=정상적으로 정보 없음, False=어딘가 있는데 못 찾았을 가능성)"
    )
    reasoning: str


class SearchJudgement(BaseModel):
    best_url: str | None = Field(
        default=None, description="교수 개인/연구실 페이지로 판단되는 URL, 없으면 null"
    )
    reasoning: str


def _strip_code_fence(text: str) -> str:
    """```json ... ``` 같은 코드펜스를 벗겨서 순수 JSON만 남긴다."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    # 앞뒤에 잡담이 섞여도 첫 { ~ 마지막 } 만 취한다.
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1]
    return t.strip()


class LLM:
    """사내 OpenAI 호환 클라이언트를 감싸고, 호출 수를 세어준다."""

    def __init__(self, client: OpenAI | None = None) -> None:
        # 인증/식별 헤더는 고정이라 클라이언트 default_headers로 한 번만 지정한다.
        self.client = client or OpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            default_headers=config.fixed_headers() or None,
        )
        self.calls = 0

    # ------------------------------------------------------------------
    def _complete(self, *, system: str, user: str, schema: type[BaseModel], max_tokens: int):
        """JSON 모드로 호출하고 schema로 검증한다. 실패 시 재시도."""
        # 프롬프트에 원하는 JSON 형식을 명시한다(스키마 강제가 안 되므로).
        schema_hint = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        sys_prompt = (
            f"{system}\n\n"
            "Respond with a single JSON object and nothing else. "
            "It must validate against this JSON Schema:\n"
            f"{schema_hint}"
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ]
        kwargs: dict = {
            "model": config.MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if config.USE_JSON_MODE:
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(config.JSON_RETRIES + 1):
            self.calls += 1
            # Prompt-Msg-Id / Completion-Msg-Id는 요청마다 새 uuid여야 한다.
            per_request_headers = {
                "Prompt-Msg-Id": str(uuid.uuid4()),
                "Completion-Msg-Id": str(uuid.uuid4()),
            }
            resp = self.client.chat.completions.create(
                extra_headers=per_request_headers, **kwargs
            )
            content = resp.choices[0].message.content or ""
            try:
                return schema.model_validate_json(_strip_code_fence(content))
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_err = exc
                log.warning("JSON 파싱 실패(attempt %d): %s", attempt + 1, exc)
                # 다음 시도에 오류를 알려주고 다시 요청한다.
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That was not valid according to the schema: "
                            f"{exc}. Return ONLY a corrected JSON object."
                        ),
                    }
                )
        raise RuntimeError(f"LLM JSON 검증 실패: {last_err}")

    # ------------------------------------------------------------------
    def identify_page(self, professor_name: str, url: str, page_text: str) -> PageIdentification:
        """이 페이지가 정말 그 교수의 연구실/개인 페이지인지 텍스트로 판단."""
        return self._complete(
            system=(
                "You decide whether a web page is the personal or research-lab page "
                "of a specific professor. Judge only from the visible text and context — "
                "the domain (personal site, Wix, GitHub Pages, Notion, university subdomain) "
                "is irrelevant. A Google Scholar / ResearchGate profile is NOT their lab page."
            ),
            user=(
                f"Professor: {professor_name}\nURL: {url}\n\n"
                f"--- PAGE TEXT ---\n{page_text}"
            ),
            schema=PageIdentification,
            max_tokens=512,
        )

    def find_member_page(self, current_url: str, links: list[Link]) -> MemberPageDecision:
        """메뉴/링크를 보고 구성원 페이지로 갈 링크를 고른다(탐색 1차)."""
        link_lines = "\n".join(f"- [{l.text or '(no text)'}]({l.href})" for l in links)
        return self._complete(
            system=(
                "You navigate a professor's website to find the page listing lab members "
                "(students, postdocs, researchers). Members pages are variously labeled: "
                "Members, People, Lab, Team, Group, Students, 구성원, 연구원, 사람들, etc. — "
                "and sometimes members are on the main page itself. Choose the single best "
                "link to follow, or report found=false if none of the links looks like a "
                "members page. Return an absolute URL exactly as given in the list."
            ),
            user=f"Current page: {current_url}\n\n--- LINKS ---\n{link_lines}",
            schema=MemberPageDecision,
            max_tokens=512,
        )

    def extract_members(self, page_text: str) -> MemberExtraction:
        """텍스트에서 구성원 명단을 뽑는다. 명단이 없으면 has_member_info=false."""
        return self._complete(
            system=(
                "Extract the lab's members (students, postdocs, researchers, staff) from the "
                "page text. Include the professor only if listed among members. If the text "
                "contains NO member/student roster at all, set has_member_info=false and return "
                "an empty list — do NOT invent members and do NOT pull names from unrelated "
                "sections (news, publications, alumni-only if ambiguous)."
            ),
            user=f"--- PAGE TEXT ---\n{page_text}",
            schema=MemberExtraction,
            max_tokens=4096,
        )

    def judge_absence(self, page_text: str) -> AbsenceVerdict:
        """명단을 못 찾았을 때: 원래 없는 사이트인가 vs 어딘가 있는데 못 찾았나."""
        return self._complete(
            system=(
                "A members roster could not be found on this professor's site. Decide whether "
                "the site simply does not publish member information (genuinely_absent=true, a "
                "normal case — e.g. only research/publication descriptions), or whether it "
                "likely exists somewhere the crawler didn't reach (genuinely_absent=false). "
                "Do not guess names."
            ),
            user=f"--- PAGE TEXT ---\n{page_text}",
            schema=AbsenceVerdict,
            max_tokens=512,
        )

    def judge_search_results(
        self, professor_name: str, department: str | None, results_text: str
    ) -> SearchJudgement:
        """검색 결과 중 교수의 개인/연구실 페이지가 맞는 것을 고른다."""
        dept = f" ({department})" if department else ""
        return self._complete(
            system=(
                "From web search results, pick the single URL that is the personal or "
                "research-lab homepage of the given professor. Ignore directory listings, "
                "news articles, Google Scholar/ResearchGate profiles, and other people's "
                "pages. If none is clearly the professor's own site, return best_url=null."
            ),
            user=(
                f"Professor: {professor_name}{dept}\n\n--- SEARCH RESULTS ---\n{results_text}"
            ),
            schema=SearchJudgement,
            max_tokens=512,
        )


def to_members(extracted: list[ExtractedMember]) -> list[Member]:
    return [
        Member(name=m.name, role=m.role, email=m.email, extra=m.extra) for m in extracted
    ]
