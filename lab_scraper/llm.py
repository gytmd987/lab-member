"""LLM 판단 계층 (사내 OpenAI 호환 API).

도메인이 아니라 "텍스트·문맥"만 보고 판단하는 부분을 전부 모아둔다.

사내 API는 JSON 스키마 강제(structured outputs)까지는 아니고 JSON 모드만
지원하므로:
  1. 응답 스키마를 Pydantic 모델로 정의하고,
  2. 그 모델의 JSON 스키마를 프롬프트에 넣어 형식을 지정하고,
  3. response_format={"type":"json_object"}로 JSON을 유도한 뒤,
  4. Pydantic으로 검증하고, 실패하면 오류를 붙여 재시도한다.

사이트 안에서 "다음에 뭘 할지"(연구실 링크 따라가기 / 구성원 페이지 열기 /
여기서 추출 / 포기)는 decide_next_action이 매 스텝 결정한다 — 뼈대(스텝 상한,
방문 중복 방지)는 코드가 강제하고, 선택만 LLM이 한다.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Literal

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


class NextAction(BaseModel):
    """사이트 탐색 루프에서 다음 행동. 코드가 아니라 LLM이 고른다."""

    action: Literal[
        "follow_lab_link",    # 이 페이지는 프로필/안내 페이지 → 실제 연구실 홈페이지로 이동
        "open_members_page",  # 이 사이트 안의 구성원(Members/People) 페이지로 이동
        "extract_here",       # 현재 페이지에 구성원 명단이 있음 → 여기서 추출
        "give_up",            # 이 사이트에서는 구성원 정보를 더 찾을 곳이 없음
    ]
    url: str | None = Field(
        default=None,
        description="follow_lab_link/open_members_page일 때 이동할 절대 URL(링크 목록에서 그대로), 그 외 null",
    )
    reasoning: str


class ExtractedMember(BaseModel):
    name_kr: str | None = Field(
        default=None, description="한글 이름. 페이지에 없으면 영문명에서 추정하고 '(추정)'을 붙임"
    )
    name_en: str | None = Field(
        default=None, description="영문 이름. 페이지에 없으면 한글명에서 추정하고 '(추정)'을 붙임"
    )
    position: str | None = Field(
        default=None, description="포닥 | 박사과정 | 석사과정 | 석박통합 중 하나"
    )
    email: str | None = None
    phone: str | None = None
    homepage: str | None = Field(
        default=None, description="이 구성원의 개인페이지 절대 URL(링크 목록에서 매칭), 없으면 null"
    )
    research_area: str | None = None


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


class FacultyMember(BaseModel):
    name: str
    lab_url: str | None = Field(
        default=None,
        description="이 교수의 개인/연구실/프로필 페이지 링크(주어진 링크 목록 중), 없으면 null",
    )


class FacultyList(BaseModel):
    professors: list[FacultyMember] = Field(default_factory=list)
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


def _link_lines(links: list[Link]) -> str:
    return "\n".join(f"- [{l.text or '(no text)'}]({l.href})" for l in links)


# 구성원 추출 공통 규칙 (extract_members / enrich_member가 공유)
_MEMBER_RULES = (
    "Include ONLY current members who are: postdocs (포닥), PhD students (박사과정), "
    "MS students (석사과정), or integrated MS-PhD students (석박통합). "
    "EXCLUDE: alumni/graduates (졸업생), the professor(s) themselves, undergraduate "
    "students/interns (학부연구생), administrative staff, and visiting researchers whose "
    "status is unclear. "
    "For each member fill BOTH name_kr and name_en: if one is not written on the page, "
    "infer it from the other (transliterate) and append ' (추정)' to the inferred value. "
    "position must be one of: 포닥, 박사과정, 석사과정, 석박통합. "
    "Do NOT invent emails, phones, URLs, or research areas — only use what the text/links show."
)


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

    def decide_next_action(
        self,
        professor_name: str,
        current_url: str,
        visited: list[str],
        page_text: str,
        links: list[Link],
    ) -> NextAction:
        """탐색 루프의 다음 행동을 고른다.

        - 이 페이지가 교수 프로필/디렉토리/안내 페이지이고 실제 연구실 홈페이지
          링크가 따로 있으면 follow_lab_link.
        - 연구실 사이트 본체인데 구성원 페이지가 따로 있으면 open_members_page.
        - 현재 페이지에 이미 구성원 명단이 보이면 extract_here.
        - 더 볼 곳이 없으면 give_up.
        """
        visited_lines = "\n".join(f"- {u}" for u in visited) or "(none)"
        return self._complete(
            system=(
                "You are navigating web pages to find the member roster (students/postdocs) "
                f"of professor '{professor_name}''s research lab. Decide the single next action:\n"
                "- follow_lab_link: the current page is a professor PROFILE/DIRECTORY/announcement "
                "page (not the lab site itself) and it links to the actual lab homepage — go there. "
                "Lab-homepage links may be labeled 연구실 홈페이지, 홈페이지, Lab, Website, etc.\n"
                "- open_members_page: the current page IS the lab/personal site and one of its links "
                "leads to a members page (Members, People, Team, Group, Students, 구성원, 연구원...).\n"
                "- extract_here: the member roster is already visible in the current page text.\n"
                "- give_up: none of the above applies and no link plausibly leads to members.\n"
                "Rules: for follow_lab_link/open_members_page return the absolute URL exactly as it "
                "appears in the LINKS list; NEVER pick a URL in the ALREADY VISITED list; a Google "
                "Scholar/ResearchGate profile is not a lab homepage."
            ),
            user=(
                f"Current page: {current_url}\n"
                f"--- ALREADY VISITED ---\n{visited_lines}\n\n"
                f"--- PAGE TEXT ---\n{page_text}\n\n"
                f"--- LINKS ---\n{_link_lines(links)}"
            ),
            schema=NextAction,
            max_tokens=512,
        )

    def extract_faculty(self, url: str, page_text: str, links: list[Link]) -> FacultyList:
        """학과 교수진 페이지에서 교수 목록(이름 + 개인/연구실 링크)을 뽑는다."""
        return self._complete(
            system=(
                "This is a university department's faculty/people page. Extract the list of "
                "PROFESSORS (faculty members) — not staff, students, or administrators. For "
                "each professor, if the page links to their personal/lab/profile page, set "
                "lab_url to that absolute URL chosen from the link list (match by the "
                "professor's name in the link text or href); otherwise null. Do not invent "
                "links. Emeritus/adjunct faculty may be included if clearly professors."
            ),
            user=(
                f"Faculty page: {url}\n\n--- PAGE TEXT ---\n{page_text}\n\n"
                f"--- LINKS ---\n{_link_lines(links)}"
            ),
            schema=FacultyList,
            max_tokens=8192,
        )

    def extract_members(self, page_text: str, links: list[Link]) -> MemberExtraction:
        """텍스트+링크에서 구성원 명단을 뽑는다. 명단이 없으면 has_member_info=false."""
        return self._complete(
            system=(
                "Extract the lab's member roster from the page text. "
                + _MEMBER_RULES
                + " For homepage, match each member to their personal-page link in the LINKS "
                "list by name (link text or href); use the absolute URL as given; null if no "
                "such link. If the text contains NO member roster at all, set "
                "has_member_info=false and return an empty list — do NOT invent members and "
                "do NOT pull names from unrelated sections (news, publications)."
            ),
            user=f"--- PAGE TEXT ---\n{page_text}\n\n--- LINKS ---\n{_link_lines(links)}",
            schema=MemberExtraction,
            max_tokens=8192,
        )

    def enrich_member(self, member: ExtractedMember, page_url: str, page_text: str) -> ExtractedMember:
        """구성원 개인페이지 텍스트로 빈 필드만 보강한다."""
        current = member.model_dump_json(exclude_none=False)
        return self._complete(
            system=(
                "You are given a lab member's known info (JSON) and the text of what should be "
                "their personal homepage. Fill in ONLY the missing (null) fields — and you may "
                "replace a name marked ' (추정)' with the confirmed spelling from the page. "
                "Keep every other existing value unchanged. "
                + _MEMBER_RULES
                + " If the page does not appear to belong to this person, return the JSON "
                "unchanged."
            ),
            user=(
                f"--- KNOWN MEMBER INFO ---\n{current}\n\n"
                f"--- PERSONAL PAGE ({page_url}) TEXT ---\n{page_text}"
            ),
            schema=ExtractedMember,
            max_tokens=1024,
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


def to_member(e: ExtractedMember) -> Member:
    return Member(
        name_kr=e.name_kr,
        name_en=e.name_en,
        position=e.position,
        email=e.email,
        phone=e.phone,
        homepage=e.homepage,
        research_area=e.research_area,
    )


def to_members(extracted: list[ExtractedMember]) -> list[Member]:
    return [to_member(e) for e in extracted]
