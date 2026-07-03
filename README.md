# lab-member

연구실 구성원 스크래퍼. 학과 페이지의 교수 목록에서 시작해 각 교수의
연구실/개인 페이지를 방문하고, LLM(Claude)이 텍스트·문맥으로 판단해
구성원(학생/연구원) 명단을 수집한다.

## 설계 원칙

**도메인으로 거르지 않는다.** 연구실 사이트가 개인 도메인, Wix, GitHub
Pages, Notion 등으로 존재하는 건 정상 케이스다. Selenium은 어떤 URL이든
그냥 방문하고, "이게 연구실/교수 페이지인지"는 LLM이 판단한다.

**"예외"는 도메인이 아니라 접근 실패로만 정의한다.**

| 상황 | 처리 상태 |
|---|---|
| 사이트 접속 불가 (404 / 타임아웃 / 접속 거부) | `ACCESS_FAILED` |
| 로그인 필요 / 텍스트 추출 불가(전체 이미지·캔버스) | `ACCESS_FAILED` |
| 도메인이 학교 밖 | 정상 케이스, 그대로 방문 |
| 구성원 페이지 못 찾음 (1차 탐색) | 재시도: 메인 텍스트 전체 재검토 |
| 사이트는 있는데 명단 자체가 없음 | `NO_MEMBER_INFO` (정상) |
| 명단 수집 성공 | `SUCCESS` |
| 연구실 사이트 자체를 못 찾음 | `SITE_NOT_FOUND` |

**재시도 로직.** 구성원 페이지를 1차 탐색으로 못 찾으면 메인 페이지 전체
텍스트를 한 번 더 LLM에 넘겨 재검토한다. 그래도 명단이 없으면 "원래 정보를
공개 안 하는 사이트"인지 "어딘가 있는데 못 찾은 것"인지 LLM이 구분해
전자는 `NO_MEMBER_INFO`(정상)로 확정한다. 억지로 찾으려다 Google
Scholar·ResearchGate 등에서 엉뚱한 정보를 긁어오는 걸 막는다.

**사이트 자체를 못 찾는 교수.** 학과 페이지에 연구실 링크가 없으면 이름으로
재검색(1~2회) → 검색 결과에서 "본인 페이지가 맞는지" LLM이 방문 후 판정한다.

교수 1명당 LLM 호출 수는 사이트 구조에 따라 약 4~8회(재시도·재검색 포함).

## 설치

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...   # 또는 `ant auth login`
# Chromium + chromedriver 필요 (경로는 환경변수로 지정 가능, config.py 참고)
```

## 실행

```bash
python main.py professors.json          # 결과를 stdout(JSON)으로
python main.py professors.json out.json # 파일로 저장
```

입력 `professors.json`:

```json
[
  {"name": "홍길동", "department": "컴퓨터공학과", "lab_url": "https://example.com/lab"},
  {"name": "김철수", "department": "전자공학과"}
]
```

## 구조

| 파일 | 역할 |
|---|---|
| `lab_scraper/models.py` | 결과 데이터 모델 · 처리 상태 enum |
| `lab_scraper/browser.py` | Selenium 래퍼, 접근 실패 판정(404/타임아웃/로그인/추출불가) |
| `lab_scraper/llm.py` | Claude 판단 계층(페이지 식별·구성원 페이지 탐색·명단 추출·정보없음 판정·검색결과 판정), structured outputs |
| `lab_scraper/search.py` | 이름 재검색(Claude web_search 서버 툴) |
| `lab_scraper/scraper.py` | 오케스트레이션 + 재시도 로직 |
| `lab_scraper/config.py` | 환경변수로 덮어쓸 수 있는 설정값 |
| `main.py` | CLI 진입점 |

## 주의

이건 골격이다. 실제 운영 전에 확인/보강할 부분:

- **JS 렌더링 대기**: 지금은 고정 `RENDER_WAIT` 초. SPA가 많으면
  `WebDriverWait`로 특정 요소를 기다리도록 바꾸는 게 안전하다.
- **접근 실패 판정 휴리스틱**: 404/로그인 힌트 문자열은 예시 수준. 대상
  사이트들을 보고 조정한다.
- **명단 페이지가 여러 depth**인 경우 현재는 1-hop만 따라간다. 필요하면
  탐색을 재귀/큐로 확장.
- **비용/속도**: 교수 수가 많으면 LLM 호출·페이지 로드가 병목. 결과 캐싱과
  동시성(드라이버 풀)을 고려한다.
