"""교수 1명 처리 오케스트레이션.

흐름 (설계 논의 그대로):

  1. 연구실/개인 페이지 URL 확보
       - 학과 페이지에서 받은 lab_url이 있으면 사용
       - 없으면 이름으로 재검색 → 검색결과에서 LLM이 본인 페이지 판정
       - 그래도 못 찾으면 → SITE_NOT_FOUND
  2. 사이트 방문
       - 접속 불가(404/타임아웃/로그인/추출불가) → ACCESS_FAILED, 스킵
       - 도메인 종류는 안 봄. 어떤 사이트든 그대로 방문.
  3. 구성원 페이지 탐색 (LLM이 메뉴/링크 판단, 1차)
       - 찾으면 이동해서 텍스트 확보
  4. 명단 추출
       - 성공 → SUCCESS
       - 실패 & 구성원 페이지로 이동했었다면 → 메인 페이지 전체 텍스트로 재시도
  5. 최종 판정
       - 명단이 애초에 없는 사이트 → NO_MEMBER_INFO (정상)
       - 어딘가 있는데 못 찾은 듯 → ACCESS_FAILED (검토 필요로 기록)
"""

from __future__ import annotations

import logging

from . import llm, search
from .browser import Browser, FetchResult
from .config import MAX_SEARCH_ATTEMPTS
from .models import ProcessStatus, ProfessorInput, ProfessorResult

log = logging.getLogger(__name__)


def process_professor(
    prof: ProfessorInput, browser: Browser, judge: llm.LLM
) -> ProfessorResult:
    result = ProfessorResult(professor=prof, status=ProcessStatus.SITE_NOT_FOUND)

    # 1. 사이트 URL 확보 -------------------------------------------------
    site_url = prof.lab_url or _discover_site(prof, browser, judge)
    if not site_url:
        result.detail = "연구실/개인 페이지를 찾지 못함"
        result.llm_calls = judge.calls
        return result
    result.site_url = site_url

    # 2. 사이트 방문 -----------------------------------------------------
    fetch = browser.fetch(site_url)
    if not fetch.ok:
        result.status = ProcessStatus.ACCESS_FAILED
        result.detail = fetch.failure_reason
        result.llm_calls = judge.calls
        return result

    # 3. 구성원 페이지 탐색 (1차) ----------------------------------------
    member_fetch: FetchResult | None = None
    if fetch.links:
        decision = judge.find_member_page(fetch.url, fetch.links)
        if decision.found and decision.url and decision.url != fetch.url:
            sub = browser.fetch(decision.url)
            if sub.ok:
                member_fetch = sub
            else:
                log.info("구성원 페이지 접근 실패(%s) → 메인 텍스트로 진행", sub.failure_reason)

    # 4. 명단 추출 (구성원 페이지 우선, 없으면 메인) ----------------------
    primary = member_fetch or fetch
    extraction = judge.extract_members(primary.text)

    # 4-b. 재시도: 구성원 페이지에서 못 찾았으면 메인 페이지 전체 텍스트 재검토
    if not extraction.has_member_info and member_fetch is not None:
        log.info("구성원 페이지에서 명단 미발견 → 메인 페이지 전체 텍스트 재시도")
        extraction = judge.extract_members(fetch.text)

    if extraction.has_member_info and extraction.members:
        result.status = ProcessStatus.SUCCESS
        result.members = llm.to_members(extraction.members)
        result.detail = extraction.reasoning
        result.llm_calls = judge.calls
        return result

    # 5. 최종 판정: 정보 없음(정상) vs 못 찾음(검토 필요) -----------------
    verdict = judge.judge_absence(fetch.text)
    if verdict.genuinely_absent:
        result.status = ProcessStatus.NO_MEMBER_INFO
    else:
        result.status = ProcessStatus.ACCESS_FAILED
        result.detail = "명단이 있을 가능성이 있으나 크롤러가 도달하지 못함(검토 필요): " + verdict.reasoning
    result.detail = result.detail or verdict.reasoning
    result.llm_calls = judge.calls
    return result


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
