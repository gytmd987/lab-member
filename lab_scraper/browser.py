"""Selenium 래퍼.

핵심 원칙:
  - 도메인 화이트리스트 없음. 주어진 URL로 그냥 이동한다.
  - "예외"는 도메인이 아니라 접근 실패로만 정의한다:
      * 페이지가 안 뜸 (404 / 타임아웃 / 접속 거부)
      * 로그인이 필요함
      * 콘텐츠가 텍스트로 추출 불가 (전체가 이미지/캔버스인 옛날 사이트 등)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

from . import config


@dataclass
class Link:
    text: str
    href: str


@dataclass
class FetchResult:
    ok: bool
    url: str
    text: str = ""
    links: list[Link] = field(default_factory=list)
    title: str = ""
    failure_reason: str | None = None      # ok=False일 때만 채워짐
    truncated: bool = False                # LLM 전달용으로 텍스트가 잘렸는지


# 접근 실패 판정에 쓰는 힌트 문자열들 --------------------------------------
_NOT_FOUND_HINTS = (
    "404", "not found", "page not found", "페이지를 찾을 수 없",
    "존재하지 않", "요청하신 페이지",
)
_LOGIN_HINTS = (
    "sign in", "log in", "login required", "please log in",
    "로그인이 필요", "로그인 후 이용", "인증이 필요",
)


class Browser:
    """headless Chromium 드라이버 한 개를 감싸서 여러 페이지를 순회한다."""

    def __init__(self) -> None:
        self._driver: webdriver.Chrome | None = None

    def __enter__(self) -> "Browser":
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1400,1800")
        # 봇 차단을 조금이라도 덜 당하도록 평범한 UA를 준다.
        opts.add_argument(
            "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        if config.CHROME_BINARY:
            opts.binary_location = config.CHROME_BINARY

        service = Service(config.CHROMEDRIVER_PATH) if config.CHROMEDRIVER_PATH else Service()
        self._driver = webdriver.Chrome(options=opts, service=service)
        self._driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        return self

    def __exit__(self, *exc) -> None:
        if self._driver is not None:
            self._driver.quit()
            self._driver = None

    # ------------------------------------------------------------------
    def fetch(self, url: str) -> FetchResult:
        """URL로 이동해서 텍스트/링크를 뽑는다. 실패는 FetchResult.ok=False로."""
        assert self._driver is not None, "Browser must be used as a context manager"
        driver = self._driver

        try:
            driver.get(url)
        except TimeoutException:
            return FetchResult(ok=False, url=url, failure_reason="timeout")
        except WebDriverException as exc:
            # DNS 실패, 접속 거부, net::ERR_* 등
            return FetchResult(ok=False, url=url, failure_reason=f"navigation_error: {exc.msg}")

        # JS 렌더링 대기(간단 버전). 정교하게 하려면 WebDriverWait로 특정 요소를 기다린다.
        import time

        time.sleep(config.RENDER_WAIT)

        title = (driver.title or "").strip()
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text or ""
        except WebDriverException:
            body_text = ""
        body_text = body_text.strip()

        # 1) 404 / not found
        low_title = title.lower()
        low_head = body_text[:400].lower()
        if any(h in low_title or h in low_head for h in _NOT_FOUND_HINTS):
            return FetchResult(ok=False, url=url, title=title, failure_reason="not_found")

        # 2) 로그인 필요 (본문이 빈약하면서 로그인 힌트가 있을 때)
        if len(body_text) < 600 and any(h in body_text.lower() for h in _LOGIN_HINTS):
            return FetchResult(ok=False, url=url, title=title, failure_reason="login_required")

        # 3) 콘텐츠 추출 불가 (텍스트가 거의 없음 → 전체 이미지/캔버스로 추정)
        if len(body_text) < config.MIN_EXTRACTABLE_CHARS:
            if self._looks_image_only():
                return FetchResult(ok=False, url=url, title=title, failure_reason="non_extractable")

        links = self._collect_links()
        text, truncated = self._cap_text(body_text)
        return FetchResult(
            ok=True, url=driver.current_url, text=text, links=links,
            title=title, truncated=truncated,
        )

    # ------------------------------------------------------------------
    def _looks_image_only(self) -> bool:
        driver = self._driver
        assert driver is not None
        try:
            imgs = len(driver.find_elements(By.TAG_NAME, "img"))
            canvas = len(driver.find_elements(By.TAG_NAME, "canvas"))
            embeds = len(driver.find_elements(By.TAG_NAME, "embed"))  # 옛 Flash 등
        except WebDriverException:
            return False
        return (imgs + canvas + embeds) >= 3

    def _collect_links(self) -> list[Link]:
        driver = self._driver
        assert driver is not None
        out: list[Link] = []
        seen: set[str] = set()
        try:
            anchors = driver.find_elements(By.TAG_NAME, "a")
        except WebDriverException:
            return out
        for a in anchors:
            try:
                href = a.get_attribute("href") or ""
                text = (a.text or "").strip()
            except WebDriverException:
                continue
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append(Link(text=text, href=href))
            if len(out) >= config.MAX_LINKS:
                break
        return out

    @staticmethod
    def _cap_text(text: str) -> tuple[str, bool]:
        if len(text) <= config.MAX_PAGE_CHARS:
            return text, False
        return text[: config.MAX_PAGE_CHARS], True
