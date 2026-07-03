"""Claude 기반 판단 계층.

도메인이 아니라 "텍스트·문맥"만 보고 판단하는 부분을 전부 모아둔다.
모든 호출은 structured outputs(Pydantic)로 받아서 파싱 실패를 없앤다.

호출 수를 아끼려고 thinking은 켜지 않는다(짧은 분류성 판단이라 불필요).
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

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


class LLM:
    """Anthropic 클라이언트를 감싸고, 호출 수를 세어준다."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self.client = client or anthropic.Anthropic()
        self.calls = 0

    # ------------------------------------------------------------------
    def _parse(self, *, system: str, user: str, schema: type[BaseModel], max_tokens: int):
        self.calls += 1
        resp = self.client.messages.parse(
            model=config.MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if resp.parsed_output is None:
            # refusal 등으로 스키마에 못 맞춘 경우.
            raise RuntimeError(f"LLM returned no parsed output (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    # ------------------------------------------------------------------
    def identify_page(self, professor_name: str, url: str, page_text: str) -> PageIdentification:
        """이 페이지가 정말 그 교수의 연구실/개인 페이지인지 텍스트로 판단."""
        return self._parse(
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
        return self._parse(
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
        return self._parse(
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
        return self._parse(
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
        return self._parse(
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
