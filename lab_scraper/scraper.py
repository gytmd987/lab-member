"""교수 1명 처리 오케스트레이션.

흐름:

  1. 연구실/개인 페이지 URL 확보
       - 학과 페이지에서 받은 lab_url이 있으면 사용
       - 없으면 이름으로 재검색 → 검색결과에서 LLM이 본인 페이지 판정
       - 그래도 못 찾으면 → SITE_NOT_FOUND
  2. LLM 탐색 루프 (최대 MAX_NAV_STEPS 스텝, 방문 URL 중복 차단)
       - 매 스텝 LLM이 다음 행동을 고른다:
           follow_lab_link   : 프로필/안내 페이지 → 실제 연구실 홈페이지로 이동
           open_members_page : 사이트 안의 구성원 페이지로 이동
           extract_here      : 현재 페이지에서 명단 추출
           give_up           : 더 볼 곳 없음
       - 뼈대(스텝 상한, 중복 방지, 실패 폴백)는 코드가 강제한다.
  3. 명단 추출 (실패 시 첫 페이지 텍스트로 1회 재시도)
  4. 개인페이지 보강: homepage가 있고 정보가 부족한 멤버만 방문해서 빈 필드 채움
  5. 최종 판정
       - 명단이 애초에 없는 사이트 → NO_MEMBER_INFO (정상)
       - 어딘가 있는데 못 찾은 듯 → ACCESS_FAILED (검토 필요로 기록)

  접근 실패(404/타임아웃/로그인/추출불가)만 예외로 취급하고,
  도메인 종류(개인 도메인/Wix/GitHub Pages/Notion 등)는 보지 않는다.
"""

from __future__ import annotations

import logging

from . import config, llm, search
from .browser import Browser, FetchResult
from .config import MAX_SEARCH_ATTEMPTS
from .models import ProcessStatus, ProfessorInput, ProfessorResult

log = logging.getLogger(__name__)


def collect_professors(
    department_url: str, browser: Browser, judge: llm.LLM, department: str | None = None
) -> list[ProfessorInput]:
    """학과 교수진 페이지 URL에서 교수 목록을 뽑아 ProfessorInput 리스트로 만든다.

    페이지에 각 교수의 개인/연구실 링크가 있으면 lab_url로 채워, 이후 이름
    재검색 단계를 건너뛰게 한다.
    """
    fetch = browser.fetch(department_url)
    if not fetch.ok:
        log.warning("학과 페이지 접근 실패(%s): %s", fetch.failure_reason, department_url)
        return []

    faculty = judge.extract_faculty(fetch.url, fetch.text, fetch.links)
    return [
        ProfessorInput(name=f.name, department=department, lab_url=f.lab_url)
        for f in faculty.professors
        if f.name.strip()
    ]


def needs_enrichment(m: llm.ExtractedMember) -> bool:
    """개인페이지를 방문할 가치가 있는가 — 이미 정보가 충분하면 False.

    기준: 이름이 미확정(누락 또는 '(추정)')이거나, 이메일/연구분야가 비어 있으면
    방문한다. phone은 기준에서 제외 — 대부분 페이지에 없어서 기준에 넣으면
    사실상 전원 방문이 된다.
    """
    def unconfirmed(name: str | None) -> bool:
        return not name or "추정" in name

    return (
        unconfirmed(m.name_kr)
        or unconfirmed(m.name_en)
        or not m.email
        or not m.research_area
    )


def process_professor(
    prof: ProfessorInput, browser: Browser, judge: llm.LLM
) -> ProfessorResult:
    result = ProfessorResult(professor=prof, status=ProcessStatus.SITE_NOT_FOUND)
    notes: list[str] = []

    # 1. 사이트 URL 확보 -------------------------------------------------
    site_url = prof.lab_url or _discover_site(prof, browser, judge)
    if not site_url:
        result.detail = "연구실/개인 페이지를 찾지 못함"
        result.llm_calls = judge.calls
        return result
    result.site_url = site_url

    first_fetch = browser.fetch(site_url)
    if not first_fetch.ok:
        result.status = ProcessStatus.ACCESS_FAILED
        result.detail = first_fetch.failure_reason
        result.llm_calls = judge.calls
        return result

    # 2. LLM 탐색 루프 ----------------------------------------------------
    current = first_fetch
    visited: list[str] = [current.url]
    gave_up = False
    for step in range(config.MAX_NAV_STEPS):
        decision = judge.decide_next_action(
            prof.name, current.url, visited, current.text, current.links
        )
        log.info("[%s] step %d: %s → %s", prof.name, step + 1, decision.action, decision.url or "-")

        if decision.action == "extract_here":
            break
        if decision.action == "give_up":
            gave_up = True
            break

        # follow_lab_link / open_members_page — 이동
        target = decision.url
        if not target or target in visited:
            notes.append(f"step{step+1}: 이동 URL 없음/중복({target}) → 현재 페이지에서 추출")
            break
        nxt = browser.fetch(target)
        if not nxt.ok:
            notes.append(f"step{step+1}: {target} 접근 실패({nxt.failure_reason}) → 현재 페이지에서 추출")
            break
        if decision.action == "follow_lab_link":
            # 실제 연구실 홈페이지로 갈아탐 — 결과에 기록
            result.site_url = nxt.url
            notes.append(f"연구실 홈페이지로 이동: {nxt.url}")
        current = nxt
        visited.append(current.url)
    # (상한 도달 시에도 마지막 페이지에서 추출을 시도한다)

    # 3. 명단 추출 ---------------------------------------------------------
    if not gave_up:
        extraction = judge.extract_members(current.text, current.links)
        # 재시도: 이동한 페이지에서 못 찾았으면 첫 페이지 텍스트 재검토
        if not extraction.has_member_info and current.url != first_fetch.url:
            log.info("[%s] 명단 미발견 → 첫 페이지 전체 텍스트 재시도", prof.name)
            extraction = judge.extract_members(first_fetch.text, first_fetch.links)

        if extraction.has_member_info and extraction.members:
            # 4. 개인페이지 보강 (정보 부족 + homepage 있는 멤버만) ---------
            members = list(extraction.members)
            if config.VISIT_MEMBER_PAGES:
                members = [
                    _maybe_enrich(m, prof.name, browser, judge) for m in members
                ]
            result.status = ProcessStatus.SUCCESS
            result.members = llm.to_members(members)
            result.detail = "; ".join(notes) or extraction.reasoning
            result.llm_calls = judge.calls
            return result

    # 5. 최종 판정: 정보 없음(정상) vs 못 찾음(검토 필요) -------------------
    verdict = judge.judge_absence(current.text)
    if verdict.genuinely_absent:
        result.status = ProcessStatus.NO_MEMBER_INFO
        result.detail = "; ".join(notes) or verdict.reasoning
    else:
        result.status = ProcessStatus.ACCESS_FAILED
        result.detail = (
            "명단이 있을 가능성이 있으나 크롤러가 도달하지 못함(검토 필요): "
            + verdict.reasoning
            + ("; " + "; ".join(notes) if notes else "")
        )
    result.llm_calls = judge.calls
    return result


def _maybe_enrich(
    member: llm.ExtractedMember, prof_name: str, browser: Browser, judge: llm.LLM
) -> llm.ExtractedMember:
    """homepage가 있고 정보가 부족한 멤버만 개인페이지를 방문해 보강한다."""
    if not member.homepage or not needs_enrichment(member):
        return member
    fetch = browser.fetch(member.homepage)
    if not fetch.ok:
        log.info("[%s] 개인페이지 접근 실패(%s): %s",
                 prof_name, fetch.failure_reason, member.homepage)
        return member
    try:
        return judge.enrich_member(member, fetch.url, fetch.text)
    except Exception as exc:  # 보강 실패는 치명적이지 않다 — 원본 유지
        log.warning("[%s] 개인페이지 보강 실패: %s", prof_name, exc)
        return member


def _discover_site(prof: ProfessorInput, browser: Browser, judge: llm.LLM) -> str | None:
    """연구실 링크가 없을 때 이름으로 재검색 → LLM이 본인 페이지 판정."""
    for attempt in range(1, MAX_SEARCH_ATTEMPTS + 1):
        query = _search_query(prof, attempt)
        try:
            results_text = search.search_web(browser, query)
        except Exception as exc:  # 검색 자체 실패는 치명적이지 않게 넘어간다
            log.warning("검색 실패(%s): %s", prof.name, exc)
            continue
        if not results_text.strip():
            continue

        judgement = judge.judge_search_results(prof.name, prof.department, results_text)
        if judgement.best_url:
            # 검색으로 찾은 URL은 실제 방문 후 본인 페이지가 맞는지 한 번 더 확인.
            fetch = browser.fetch(judgement.best_url)
            if not fetch.ok:
                log.info("검색 후보 접근 실패(%s)", fetch.failure_reason)
                continue
            ident = judge.identify_page(prof.name, fetch.url, fetch.text)
            if ident.is_target and ident.confidence >= 0.5:
                return fetch.url
            log.info("검색 후보가 본인 페이지 아님(conf=%.2f): %s", ident.confidence, ident.reasoning)
    return None


def _search_query(prof: ProfessorInput, attempt: int) -> str:
    parts = [prof.name]
    if prof.department:
        parts.append(prof.department)
    # 2차 시도에서는 연구실/구성원 키워드를 더 얹는다.
    parts.append("연구실" if attempt == 1 else "lab members people 연구실 구성원")
    return " ".join(parts)
