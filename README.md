# lab-member

연구실 구성원 스크래퍼. 학과 페이지의 교수 목록에서 시작해 각 교수의
연구실/개인 페이지를 방문하고, 사내 LLM(OpenAI 호환 API)이 텍스트·문맥으로
판단해 구성원(학생/연구원) 명단을 수집한다.

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

**사이트 안 탐색은 LLM이 결정한다(제한된 탐색 루프).** 뼈대(스텝 상한
`LAB_SCRAPER_MAX_NAV_STEPS`=4, 방문 URL 중복 차단, 실패 폴백)는 코드가
강제하고, 매 스텝 "다음 행동"만 LLM이 고른다: 프로필/안내 페이지면 실제
연구실 홈페이지 링크를 따라가고(`follow_lab_link`), 연구실 사이트면 구성원
페이지를 열고(`open_members_page`), 명단이 보이면 그 자리에서 추출한다
(`extract_here`). 학과 페이지의 교수 링크가 연구실 본체가 아니라 "연구실
홈페이지 안내" 프로필이어도 따라간다.

**재시도 로직.** 이동한 페이지에서 명단을 못 찾으면 첫 페이지 전체 텍스트를
한 번 더 재검토한다. 그래도 없으면 "원래 정보를 공개 안 하는 사이트"인지
"어딘가 있는데 못 찾은 것"인지 LLM이 구분해 전자는 `NO_MEMBER_INFO`(정상)로
확정한다. 억지로 찾으려다 Google Scholar·ResearchGate 등에서 엉뚱한 정보를
긁어오는 걸 막는다.

**사이트 자체를 못 찾는 교수.** 학과 페이지에 연구실 링크가 없으면 이름으로
재검색(1~2회) → 검색 결과에서 "본인 페이지가 맞는지" LLM이 방문 후 판정한다.

**수집 대상과 필드.** 현재 소속 **포닥·박사과정·석사과정(석박통합 포함)**만
수집한다(alumni·교수·학부연구생·행정직원 제외). 필드: 한글명/영문명(페이지에
없는 쪽은 LLM이 추정하고 `(추정)` 표시), 직위, 이메일, 전화번호, 개인
홈페이지, 연구분야. **개인 홈페이지가 있고 정보가 부족한 멤버는 그 페이지를
방문해 빈 필드를 보강**한다(이미 충분하면 방문 생략;
`LAB_SCRAPER_VISIT_MEMBER_PAGES=0`으로 끌 수 있음).

**병렬 처리.** 교수를 `LAB_SCRAPER_CONCURRENCY`(기본 4)명씩 동시에 처리한다.
브라우저는 스레드당 1개씩 뜬다(Chrome 4개 동시 실행 시 메모리 ~1.5GB 참고).
병렬이 높을수록 검색엔진(재검색 경로) 봇 차단 확률도 올라간다.

교수 1명당 LLM 호출 수는 사이트 구조에 따라 약 4~10회(탐색·재시도·보강 포함).

## 설치

LLM은 **사내 OpenAI 호환 API**를 사용한다(`openai` SDK에 `base_url`을 사내
것으로 지정하고, 인증·식별은 헤더로 붙인다).

```bash
pip install -r requirements.txt

# --- 엔드포인트 / 모델 ---
export LAB_SCRAPER_LLM_BASE_URL=http://llm.internal/...   # 사내 엔드포인트
export LAB_SCRAPER_MODEL=gemma4                           # 사내 모델 이름
# LAB_SCRAPER_LLM_API_KEY 는 대개 불필요(더미). 게이트웨이 인증은 아래 헤더로 한다.

# --- 사내 게이트웨이 헤더 (고정, 앱 공통) ---
export LAB_SCRAPER_LLM_CREDENTIAL_KEY='credential:TICKET-...'  # x-dep-ticket
export LAB_SCRAPER_LLM_SYSTEM_NAME='lab-scraper'              # Send-System-Name
export LAB_SCRAPER_LLM_USER_ID='your-id'                      # User-Id
export LAB_SCRAPER_LLM_USER_TYPE='id'                         # User-Type

# Chromium + chromedriver 필요 (경로는 환경변수로 지정 가능, config.py 참고)
```

위 4개 헤더는 클라이언트 `default_headers`로 한 번만 지정된다. `Prompt-Msg-Id`,
`Completion-Msg-Id`는 요청마다 새 UUID로 자동 생성해 붙이므로 설정할 필요 없다.
그 외 게이트웨이가 요구하는 추가 고정 헤더가 있으면
`LAB_SCRAPER_LLM_HEADERS='{"X-Extra":"value"}'`(JSON)로 넣으면 된다.

**인증이 아예 없는 API로 바꿀 때**: 위 4개 헤더 변수를 지우고(빈 값이면 자동
미전송) 아래 두 개를 추가한다.

```bash
export LAB_SCRAPER_LLM_SEND_AUTH=0      # openai SDK가 무조건 붙이는 Authorization: Bearer 제거
export LAB_SCRAPER_LLM_SEND_MSG_IDS=0   # Prompt-Msg-Id/Completion-Msg-Id(이전 API 전용) 미전송
```

> **curl로는 되는데 SDK로는 403이 날 때**: 십중팔구 `Authorization: Bearer dummy`
> 때문이다(openai SDK는 키가 없어도 이 헤더를 붙임; curl 테스트에는 없던 헤더).
> `LAB_SCRAPER_LLM_SEND_AUTH=0`으로 끄면 curl과 동일한 요청이 된다. 그래도
> 403이면 (a) base_url 경로가 예제와 정확히 같은지(`/v1` 포함 여부), (b) 사내
> 프록시 경유 여부(`HTTPS_PROXY`가 설정돼 있으면 `NO_PROXY=<API 호스트>` 추가)를
> 확인한다.

사내 API가 JSON 스키마 강제(structured outputs)까지는 아니고 **JSON 모드만**
지원하므로, `response_format={"type":"json_object"}`로 요청하고 프롬프트에
스키마를 명시한 뒤 Pydantic으로 검증한다(파싱 실패 시 오류를 붙여 재시도).
게이트웨이가 `response_format`을 거부하면 `LAB_SCRAPER_JSON_MODE=0`으로 끄면
프롬프트만으로 JSON을 유도한다.

> **"Invalid JSON: EOF" 파싱 경고가 뜰 때**: 응답이 `max_tokens` 한도에서 잘린
> 것이다. 잘림(`finish_reason=length`)이 감지되면 한도를 2배로 늘려 자동
> 재시도하므로("잘림 → 늘려 재시도" 로그) 대부분 자체 복구된다. 재시도 횟수는
> `LAB_SCRAPER_JSON_RETRIES`(기본 2)로 조절.

## 실행

가장 간단한 방법 — 학과 교수진 페이지 URL만 넣으면 교수 목록을 자동으로
수집해서 전부 처리한다:

```bash
python main.py
# → "학과 교수진 페이지 URL을 입력하세요: " 프롬프트가 뜸
#   예: https://me.snu.ac.kr/faculty
# → "학과 이름(선택, 엔터로 건너뛰기): " (검색 정확도용, 안 넣어도 됨)
# → "결과 파일 이름(엔터=results.xlsx): "
```

URL/파일명을 인자로 바로 줄 수도 있다(프롬프트 생략):

```bash
python main.py https://me.snu.ac.kr/faculty              # 결과는 results.xlsx
python main.py https://me.snu.ac.kr/faculty snu_me.xlsx  # 파일명 지정
```

교수 목록을 직접 만든 JSON으로 돌리려면(자동 수집 대신):

```bash
python main.py professors.json           # 결과는 results.xlsx
python main.py professors.json out.xlsx
```

결과 파일명은 안 주면 `results.xlsx`로 저장되며 **같은 이름이면 이전 결과를
덮어쓴다**. 실행할 때 프롬프트에서 다른 이름을 넣거나, 인자로 지정하면 된다.

결과는 **xlwings로 엑셀 파일**에 저장된다. `요약` 시트(교수별 상태·사이트·
구성원 수·비고)와 `구성원` 시트(교수별 명단)로 나뉜다. xlwings는 Excel
애플리케이션을 구동하므로 **실행 PC에 Excel이 설치돼 있어야 한다**(Windows/
macOS). 헤드리스/리눅스 환경에서는 동작하지 않는다.

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
| `lab_scraper/llm.py` | LLM 판단 계층(페이지 식별·구성원 페이지 탐색·명단 추출·정보없음 판정·검색결과 판정). 사내 OpenAI 호환 API + JSON 모드 + Pydantic 검증 |
| `lab_scraper/search.py` | 이름 재검색(Selenium으로 검색엔진 결과 페이지를 긁음) |
| `lab_scraper/scraper.py` | 오케스트레이션 + 재시도 로직 |
| `lab_scraper/excel_out.py` | 결과를 xlwings로 엑셀(.xlsx)에 저장(요약/구성원 시트) |
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
