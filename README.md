# patent-api MCP

KIPRIS Plus(한국)와 EPO OPS(유럽특허청)를 하나의 MCP 서버로 감싼 로컬 도구.
Claude 데스크톱 앱과 Claude Code에 **한 번만** 등록해 모든 프로젝트에서 쓴다.
API 키는 들어 있지 않다 — 쓰는 사람이 각자 무료로 발급받아 넣는다.

용도: 인용문헌·대응 출원의 서지, 패밀리, 법적 상태, 청구항 확인, 공개 문헌 검색(한국·해외), 공보 원본 PDF를 사건폴더에 저장. (선택) 한국 출원의 심사 서류·청구항 변동·등록사항 등 OA 대응 자료.

## 도구

| 도구 | 입력 | 하는 일 | 출처 |
|---|---|---|---|
| `kr_biblio` | 한국 출원번호 | 서지 상세(명칭·출원인·발명자·IPC·공개/등록번호·일자·초록·상태·우선권·심사관 인용문헌). 선택: 청구항, 행정처리 이력 | KIPRIS `getBibliographyDetailInfoSearch` |
| `kr_search` | 키워드·명칭·출원인·IPC·출원일 기간·쪽 | 한국 특허·실용신안 검색 목록 ⚠️ 출원 전 사건 내용으로 검색 금지 | KIPRIS `getAdvancedSearch` |
| `ep_biblio` | 공개·등록번호(국가 무관) | 서지·초록·인용문헌. 넣은 종류코드가 없으면 종류코드 없이 다시 찾고 실제 종류코드를 알려 줌 | OPS `published-data/.../biblio` |
| `ep_search` | 키워드(명칭·요약)·명칭·출원인·발명자·IPC·CPC·공개일 기간·국가, 또는 `cql` 직접·쪽 | 해외(EP·US·WO·CN·JP 등) 공개 문헌 검색 목록(공개번호·명칭·출원인·IPC·패밀리ID, 한 번에 최대 100건, 앞 2000건까지). 청구항·본문은 검색 안 됨 ⚠️ 출원 전 사건 내용으로 검색 금지 | OPS `published-data/search/biblio`(CQL) |
| `ep_text` | 번호, `section`=claims/description/abstract | 청구항·명세서·초록 원문(EP·WO 등 제공 범위 내, 기본 2만 자에서 자름) | OPS `published-data/.../claims` 등 |
| `family` | 번호(국가 무관) | INPADOC 패밀리 구성원(국가·번호·종류코드·일자) | OPS `family` |
| `legal_status` | 번호 | INPADOC 법적 상태 이벤트 | OPS `legal` |
| `ep_register` | EP 출원번호(`26123456.7`, `EP26123456`)·EP 공개번호·유럽 단계 WO 공개번호, 선택: `max_items` | EP Register: 진행 상태·갱신일, 공개번호·일자, 출원인·대리인, 모출원(분할), 검색보고서 종류·발송일, 인용문헌(X/Y/A·관련 청구항·단계), 심사청구일 등 일자, 지정국, 최근 절차 단계·이벤트. 응답 기한은 계산하지 않음. 서류 원문(PDF)은 OPS가 주지 않음 | OPS `register/.../biblio,events,procedural-steps` |
| `pdf_download` | 공개·등록번호(국가 무관), `part`=full/drawings, `save_dir`, `label` | 공보 **원본 PDF**(전문·도면)를 사건폴더에 저장(경로·쪽수·크기·텍스트층 여부·구간). 아래 「공보 원본 PDF」 참고 | 한국 전문: KIPRIS `getPubFullTextInfoSearch`·`getAnnFullTextInfoSearch` / 그 밖·도면: OPS `published-data/.../images` |
| `quota_status` | 선택: `recheck_products` | 이번 달 KIPRIS 호출 수, KIPRIS 상품별 켜짐·신청 상태, 최근 OPS 사용량 헤더와 서비스별 혼잡도(`opsThrottle`) | 내부 |

### KIPRIS 추가 상품 도구 (신청한 상품만 켜기)

KIPRIS Plus는 상품마다 따로(무료) 신청한다. 신청한 상품을 설정에서 켜면 아래 도구가 나타난다.
켜는 방법: 확장 설정의 **KIPRIS 상품** 체크칸, 또는 `.env`의 `PATENT_API_KIPRIS_PRODUCTS=쉼표목록`(`all` 가능)·
`KIPRIS_PRODUCT_<이름>=true/false`(목록보다 우선). `publication`(공개·등록공보)은 기본 상품이라 **항상 켜져 있다**(끌 수 없고, 확장 설정에도 체크칸이 없다).

| 설정 이름 | KIPRIS 상품 | 도구 | 하는 일 | KIPRIS 호출 |
|---|---|---|---|---|
| `publication` | 특허·실용 공개·등록공보 | `kr_biblio`, `kr_search`, `pdf_download`(한국 전문) | 위 표 | 1회(`pdf_download`는 1~2회) |
| `opinion` · `rejection` · `allowance` | 의견제출통지서 · 거절결정서 · 등록결정서 | `kr_exam_documents` | 서류별 발송일·제출기한, 심사 대상 청구항, 거절이유 표(부분·법조항), 구체적 거절이유 본문, 본문에서 뽑은 인용발명, 등록결정 내용. `kinds`·`send_number`로 좁힘 | 종류당 1~4회 |
| `claim_history` | 청구항 변동 이력 | `kr_claim_history` | 출원서·보정서마다 신규·수정·삭제 청구항 번호와 남은 청구항 수. `claim`을 주면 그 청구항의 판별 원문과 바뀐 부분(`[-삭제-]{+추가+}`) | 2회 |
| `deadline` | 특허·실용 통지서 마감기한 | `kr_deadlines` | 통지서별 마감일·남은 날. ⚠️ 참고용(공식 기한관리 대체 불가, 대응 완료·연장 미반영, 공개 전 출원은 없을 수 있음) | 1회 |
| `registration` | 등록사항 | `kr_registration` | 현재 권리자, 권리 이전, 존속기간 만료일, 소멸, 연차료 납부 기록(몇 년차까지). 출원번호로 넣으면 `kr_biblio` 서지에서 등록번호를 찾음 | 1회(+서지 1회) |
| `citation` · `citing` | 특허·실용 인용문헌 · 피인용문헌 | `kr_citations` | 이 출원이 인용한 문헌(번호·국가·종류·발행일·구분) / 이 출원을 인용한 한국 출원 | 방향당 1회 |
| `legal_status` | 법적 상태 이력(ST.27) | `kr_legal_history` | 출원~소멸 법적 상태 이벤트(WIPO ST.27 코드, 대분류, 국내 코드, 일자) | 1회 |
| `family` | 특허 패밀리 | `kr_family` | 한국 출원번호로 DOCDB 패밀리(해외 공보번호·출원번호·일자) | 1회 |

- 입력은 한국 출원번호(`kr_registration`·`kr_deadlines`는 등록번호도). 같은 조회는 캐시(심사 서류·마감기한·청구항 이력·등록사항·법적 상태 1일, 인용·패밀리 7일).
- **신청 상태 자동 반영:** KIPRIS에는 '신청한 상품 목록' API가 없다. 그래서 켠 상품을 처음 부를 때 응답으로 판단한다.
  미신청(`resultCode` 101·31·30·20)이면 `PRODUCT_NOT_SUBSCRIBED`와 함께 "KIPRIS에서 '<상품명>'을 무료 신청하면 바로 쓸 수 있다"고 안내하고,
  그 상태를 하루 기억해 그동안은 KIPRIS를 다시 부르지 않는다(한도 절약). 하루 뒤 다시 확인하므로 신청하면 자동으로 풀린다.
  방금 신청했다면 `quota_status(recheck_products=true)`로 바로 풀 수 있다. 인증키를 바꾸면 기록은 무시된다.
  서버를 켤 때 상품을 미리 호출하지 않는다.
- `quota_status`의 `kiprisProducts`: 상품별 켜짐(`enabled`)과 `신청됨`/`미신청`/`확인 안 함`.
- 월 1,000회 무료 한도는 **신청한 상품 전체 합산**이다. 한 번에 여러 번 부르는 도구(`kr_exam_documents` 등, 표의 호출 수 참고)가 있으니 남은 양은 `quota_status`로 확인한다.

### 공보 원본 PDF (`pdf_download`)

OA에 적힌 인용문헌 번호만으로 공보 원본 PDF를 사건폴더에 저장한다(도면 캡처, 심사관이 지적한 단락 대조용).

```
pdf_download(number="WO2022/119250 A1", save_dir="/…/사건폴더/인용문헌", label="D1")        → D1_WO2022119250A1.pdf
pdf_download(number="KR 10-3028032 B1", save_dir="/…/사건폴더/인용문헌", label="D2")        → D2_KR103028032B1.pdf (KIPRIS 공고전문)
pdf_download(number="EP1000000B1", part="drawings", save_dir="/…/사건폴더/인용문헌")        → EP1000000B1_drawings.pdf
```

- **입력:** `number`(공개·등록번호, 국가 무관 — 번호 정규화는 다른 도구와 같다), `part`=`full`(전문, 기본) | `drawings`(도면만),
  `save_dir`(저장할 폴더 절대경로, 비우면 허용 폴더), `label`(선택, 예: `D1` → 파일 이름 앞에 붙음), `max_pages`(선택, 기본 100)
- **출처:**
  - 한국 문헌 전문: **KIPRIS 공개전문·공고전문**(`getPubFullTextInfoSearch`·`getAnnFullTextInfoSearch`, '특허·실용 공개·등록공보' 상품에 포함 — 추가 신청 불필요)을 먼저 쓴다.
    이 오퍼레이션은 출원번호를 받으므로 공개·등록번호를 출원번호로 바꾼다: EPO OPS 서지(KIPRIS 한도와 무관)를 먼저 보고, OPS에 없는 최근 문헌은 KIPRIS 검색 1회.
    KIPRIS 호출은 1~2회이고 월 한도 집계에 들어간다. KIPRIS 키가 없거나, 상품 미신청·한도 초과·자료 없음이면 **EPO OPS로 넘어가고** `notes`에 적는다.
    KIPRIS가 준 파일 주소(`sourceUrl`)는 인증키 없이 열린다(2026-10 실측). 파일 내려받기 자체는 API 호출로 세지 않는다.
  - 그 밖의 문헌과 도면: **EPO OPS images**. `published-data/publication/docdb/{번호}/images`로 원본(FullDocument·Drawing)의 쪽 수·구간을 보고,
    `{link}.pdf?Range=n`을 **쪽마다 1회** 불러 하나의 PDF로 합친다([pypdf](https://pypi.org/project/pypdf/)). 한 번에 한 쪽만 온다(`Range=1-3`도 한 쪽, 실측).
  - 도면만: Drawing 원본이 있으면 그것을, 없으면 전문의 DRAWINGS 구간(다음 구간 앞까지)만 잘라 저장한다. 둘 다 없으면 '자료 없음'.
- **요청한 문헌만 받는다.** 다른 종류코드·패밀리 구성원으로 바꿔 받지 않는다. 없으면 `data: null`과 "자료 없음"(다른 공보는 `family`로 찾기).
  종류코드 없이 넣어 여러 문헌(A1·B1 등)이 걸리면 받지 않고 `AMBIGUOUS_NUMBER` 오류와 후보 목록(`error.candidates`)을 돌려준다.
- **파일 이름:** `{label}_{국가}{번호}{종류코드}.pdf`(ASCII만), 도면만이면 끝에 `_drawings`. 번호는 OPS(docdb) 표기를 따르되
  미국 공개번호만 흔히 쓰는 표기로 적는다(예: `US20220181889A1`, `KR20220079145A`, `KR103028032B1`, `JP2020123456A`). **같은 이름의 파일이 있으면 덮어쓰지 않고** 받지도 않으며 기존 경로를 알려 준다(`alreadyExists: true`).
- **허용 폴더:** `save_dir`은 허용 폴더(확장 설정의 **PDF 허용 폴더**, `.env`의 `PATENT_API_PDF_ALLOWED_DIR`, 기본 `~/Downloads`) 아래만 받는다.
  절대경로가 아니거나, `..`가 들어 있거나, 심볼릭 링크로 밖을 가리키면 오류다. 허용 폴더 안의 없는 하위 폴더는 만든다.
  경로는 NFC로 맞춰 비교한다(맥 한글 경로의 NFC/NFD 혼재). 사건폴더가 모여 있는 상위 폴더를 허용 폴더로 둔다.
- **결과:** `data`에 `path`, `source`(KIPRIS|OPS), `part`, `pages`, `bytes`, `hasTextLayer`, `sections`(예: `{"DRAWINGS": 6}`), `publicationNumber`.
  `hasTextLayer`는 앞쪽 몇 쪽에서 실제로 글자를 뽑아 판별한다. **OPS 원본은 대개 텍스트층 없는 스캔본**이라 글자 검색·복사가 안 된다(notes에 적음).
- **한도·안정성:** OPS 응답의 `X-Throttling-Control` images 상태가 yellow·red면 쪽 사이를 띄우고(분당 허용 수 기준), black이면 멈추고 알린다.
  쪽 수가 `max_pages`(기본 100, `PATENT_API_PDF_MAX_PAGES`)를 넘으면 받지 않고 쪽 수만 알려 준다.
  임시 파일에 받아 다 받은 뒤에만 이름을 붙이므로 중간에 실패해도 반쪽 파일이 남지 않는다. PDF 자체는 캐시에 넣지 않고 images 조회 결과만 캐시한다(30일).
- **코워크:** 확장은 내 컴퓨터에서 돌고 코워크는 격리된 작업 공간(VM)에서 일한다. 코워크에서 받은 PDF를 열어 보게 하려면 허용 폴더·저장 폴더가 코워크에 연결한 폴더 안이어야 한다.
- **원격 서버 모드에서는 이 도구를 등록하지 않는다**(서버 디스크에 파일을 남겨도 쓸 데가 없어서).
- **한계:** OPS에 원본이 없는 문헌(특히 최근 한국 공보, 일부 국가)은 받을 수 없다. 한국 문헌의 도면만(`drawings`)은 OPS에 있을 때만 된다 — 없으면 `part=full`로 KIPRIS 전문을 받는다.

### 응답 형식

```json
{"ok": true, "source": "KIPRIS", "query": {"applicationNumber": "1020260012345"},
 "cached": false, "data": {...}, "notes": []}
```

- 자료 없음: `"ok": true, "data": null, "notes": ["해당 번호의 자료 없음"]` (오류와 구분)
- 오류: `"ok": false, "error": {"code": "AUTH_FAILED", "message": "한국어 설명"}`
- 오류 코드: `CONFIG_MISSING_KEY`, `AUTH_FAILED`, `PERMISSION_DENIED`, `PRODUCT_NOT_SUBSCRIBED`, `QUOTA_EXCEEDED`, `RATE_LIMITED`,
  `INVALID_INPUT`, `AMBIGUOUS_NUMBER`, `UPSTREAM_BAD_REQUEST`, `UPSTREAM_UNAVAILABLE`, `TIMEOUT`,
  `NETWORK_ERROR`, `UPSTREAM_ERROR`, `PARSE_ERROR`, `FILE_ERROR`(허용 폴더가 없거나 PDF를 쓰지 못함)
- 날짜는 `YYYY-MM-DD`

### 번호 입력

| 입력 예 | 해석 |
|---|---|
| `10-2026-0012345`, `1020260012345`, `KR10-2026-0012345` | `kr_biblio`: 한국 출원번호 |
| `KR 10-2026-0012345 A` | OPS: 한국 공개번호 `KR.20260012345.A` |
| `KR 10-1234567 B1` | OPS: 한국 등록번호 `KR.101234567.B1` |
| `EP1234567A1`, `US 2026/0123456 A1`, `WO2026/012345` | OPS 공개번호 |
| `JPS60012345A`, `JP S60-012345 A`, `JPH10123456A` | 2000년 이전 일본 번호(연호 S 쇼와·H 헤이세이·T 다이쇼): OPS `JP.S60012345.A` |
| `26123456.7`, `EP26123456` | `ep_register`: EP 출원번호(다른 OPS 도구는 `number_type='application'`) |

한국 `10-YYYY-NNNNNNN`은 출원번호와 공개번호가 같은 모양이라, OPS 도구에서는 종류코드(`A`)를 붙이거나
`number_type`(`publication`/`application`)을 지정해야 한다. 추측하지 않고 `AMBIGUOUS_NUMBER`로 돌려준다.

OPS 번호 조회(`ep_biblio`·`ep_text`·`family`·`legal_status`·`pdf_download`)가 "자료 없음"이면 한 번씩만 다시 묻는다.

- 연호 없이 적은 2000년 이전 일본 번호(`JP60012345A`: 연도 2자리 + 6자리) → 연호를 붙인 후보(`JPS60012345A`, H01~H12면 `JPH…` 먼저).
  OPS는 연호 없는 `JP.60012345.A`로는 찾지 못한다(2026-10 실측).
- 넣은 종류코드가 없을 때(예: A2·A3만 있는데 `A1`) → 종류코드 없이(epodoc) 다시 찾고, `notes`에 실제 종류코드를 적는다.
  `pdf_download`는 문헌을 자동으로 고르지 않고 후보(`error.candidates`)를 돌려준다. 한국 번호는 종류코드 없이 찾지 못해 다시 묻지 않는다.
- 그래도 없는 EP·WO 번호는 공개 직후(1~2주)라 OPS에 아직 없을 수 있다고 알리고 `ep_register`를 권한다.

## 시작하기

1. **키 발급(무료):** KIPRIS Plus 인증키와 EPO OPS Consumer Key/Secret을 받는다. 그림 안내: [docs/API키_발급_안내.md](docs/API키_발급_안내.md)
   - KIPRIS Plus: 가입하면 키가 나오지만, **Open API '특허·실용 공개·등록공보'를 무료 플랜으로 신청**해야 동작한다
     (장바구니 기본값이 **유료**이니 꼭 무료로 바꾼다). 이용기간은 그해 12월 31일까지라 **해마다 다시 신청**한다.
     OA 대응용 추가 상품(의견제출통지서 등)도 같은 방법으로 신청하고 설정에서 켠다(위 표).
   - EPO OPS: 가입(Account Type: **Non-paying**) → EPO 승인 메일(1~2 영업일) → My Apps에서 앱을 만들면 키가 나온다.
2. **설치:** 아래 셋 중 하나.

### 방법 A. Claude 데스크톱 확장 파일 (가장 쉬움)

1. [Releases](https://github.com/describug/patent-api-mcp/releases/latest)에서 `patent-api.mcpb`를 받아 더블클릭(또는 Claude 데스크톱 앱 > 설정 > 확장 프로그램으로 끌어다 놓기) → **설치**
2. **설정 > 확장 프로그램 > Patent API**에서 키 3개 입력, 신청한 **KIPRIS 상품** 체크, (선택) **PDF 허용 폴더** 고르기(사건폴더가 모여 있는 상위 폴더) → **저장**
3. **Claude 데스크톱 앱 완전 종료(⌘Q) 후 다시 열기**

키는 운영체제 키체인에 저장된다. Python·uv를 따로 설치할 필요가 없다. **코워크에서 쓰려면 이 방법**으로 설치한다.

### 방법 B. 소스로 설치 (Claude Code·데스크톱 공용)

필요: Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
git clone https://github.com/describug/patent-api-mcp.git
cd patent-api-mcp
uv sync
cp .env.example .env
```

`.env`에 키를 넣는다(이 파일은 git에도 확장 파일에도 들어가지 않는다):

```
KIPRIS_SERVICE_KEY=...        # 공공데이터포털식 %2B 인코딩 키도 그대로 넣어도 된다
EPO_OPS_CONSUMER_KEY=...
EPO_OPS_CONSUMER_SECRET=...
KIPRIS_MONTHLY_LIMIT=1000
KIPRIS_WARN_AT=900
PATENT_API_KIPRIS_PRODUCTS=publication,opinion,rejection,allowance   # 신청한 상품(위 표의 설정 이름), 전부면 all
```

**Claude Code (모든 프로젝트에서 쓰도록 사용자 범위로):**

```bash
claude mcp add --scope user patent-api -- "$(which uv)" --directory "$PWD" run server.py
claude mcp list        # patent-api: ... ✔ Connected
```

**Claude 데스크톱 앱 (수동 설정):** `~/Library/Application Support/Claude/claude_desktop_config.json`
(Windows: `%APPDATA%\Claude\claude_desktop_config.json`)의 `mcpServers`에 추가한다.
`command`는 `which uv` 결과를 **절대경로**로 쓰고(데스크톱 앱은 셸 PATH를 못 읽을 수 있다),
폴더 경로는 공백이 있어도 `args`의 **한 원소**로 통째로 넣는다.

```json
{
  "mcpServers": {
    "patent-api": {
      "command": "/Users/<사용자>/.local/bin/uv",
      "args": ["--directory", "/절대/경로/patent-api-mcp", "run", "server.py"]
    }
  }
}
```

등록·키 변경 후에는 **데스크톱 앱을 완전히 종료(⌘Q)했다가 다시 연다.**
문제가 있으면 `~/Library/Logs/Claude/mcp-server-patent-api.log`를 본다.

## 원격 서버 모드 (선택)

같은 코드를 인터넷 주소가 있는 서버로 띄워 **Gemini Enterprise·ChatGPT·개인 Gemini 앱**에서 쓸 수 있다.
로컬 사용(위 방법 A·B)과 확장 파일은 그대로다 — 원격 모드는 `PATENT_API_TRANSPORT=http`(또는 `--transport http`)일 때만 켜진다.

- 전송: Streamable HTTP, 엔드포인트 `/mcp` (SSE 없음). 포트는 `PORT`(기본 8080, Cloud Run 호환)
- 인증: **OAuth 필수.** 로그인은 구글 계정, 허용은 `PATENT_API_ALLOWED_DOMAINS`(워크스페이스 도메인)·`PATENT_API_ALLOWED_EMAILS`.
  **둘 다 비어 있으면 시작하지 않는다**(인증 없는 공개 서버를 실수로 열지 않게).
  - 손으로 OAuth 값을 넣는 앱(Gemini Enterprise): `/authorize`·`/token`·고정 클라이언트 ID(`PATENT_API_OAUTH_CLIENT_ID`), PKCE
  - 주소만 넣는 앱(ChatGPT·개인 Gemini): `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, 동적 등록 `/register`
- 키: 환경변수(`KIPRIS_SERVICE_KEY` 등) — Cloud Run에서는 Secret Manager로 연결
- 캐시: 기본 `/tmp/patent-api-mcp/cache.sqlite3`. Cloud Run 디스크는 휘발성이라 재시작하면 캐시와 **KIPRIS 월 호출 수 집계가 초기화될 수 있다**
- 파일: `remote/`(원격 전용 — `config.py` 설정 검증, `auth.py` 얇은 OAuth 인가 서버, `app.py` 실행),
  `Dockerfile`(uv, 비루트), `.dockerignore`. `remote/`는 확장 파일(.mcpb)에 들어가지 않는다
- 테스트: `tests/test_remote.py` — 인증 거부/허용, 허용 목록 비면 기동 거부, 디스커버리, http 도구 목록(네트워크 없이)

운영 방식(회사 서버 1개·나만 쓰는 서버·지인 각자 서버), 단계별 명령, 인증 설계, '인증 없음' 등록 위험:
**[docs/원격_배포.md](docs/원격_배포.md)**

## 캐시와 한도

- SQLite 캐시: 사용자 폴더의 `patent-api-mcp/cache.sqlite3`
  (macOS `~/Library/Application Support/`, Windows `%LOCALAPPDATA%\`, Linux `~/.local/share/`). 키 = 도구명 + 정규화된 입력. 보존 기간 기본값: 서지·원문 30일, 패밀리 7일,
  법적 상태 1일, 검색 1일, KIPRIS 심사 서류 등 1일(`CACHE_TTL_DOCS_DAYS`), EP Register 6시간(`CACHE_TTL_REGISTER_DAYS=0.25`). `.env`의 `CACHE_TTL_*_DAYS`로 바꾼다. "자료 없음"은 최대 1일만 기억한다.
- KIPRIS: 실제로 보낸 요청(재시도 포함)을 월별로 센다. `KIPRIS_WARN_AT` 이상이면 `notes`에 경고,
  `KIPRIS_MONTHLY_LIMIT`에 닿으면 호출하지 않고 `QUOTA_EXCEEDED`(캐시는 계속 응답).
- OPS: 응답의 사용량 헤더(`X-Throttling-Control`, `X-IndividualQuotaPerHour-Used` 등)를 기록해 `quota_status`에 보여준다.
  원본 PDF(images) 요청의 헤더도 기록한다(`X-Throttling-Control`의 `images=…`).
  `ep_search`는 `search=…` 항목이 yellow·red면 `notes`로 알린다(검색은 분당 허용 수가 가장 적다).
  `quota_status`의 `opsThrottle`에 서비스별 혼잡도를 풀어 보여 준다. `ep_register`(Register)는 헤더에 따로 항목이 없다(2026-10 실측, 기타 `other`로 묶이는 것으로 보임).
  429·403(한도 초과)은 재시도하지 않는다.
- 일시 오류(타임아웃, 503)만 짧은 간격으로 최대 2회 재시도한다.
- KIPRIS 상품별 신청 상태(신청됨/미신청, 미신청은 하루만)도 같은 파일에 기록한다. 인증키 값은 저장하지 않는다(바뀌었는지만 짧은 해시로 비교).
- 캐시를 비우려면 위 `cache.sqlite3`를 지운다(이번 달 호출 수·상품 신청 상태 기록도 함께 지워진다).

## 테스트

```bash
uv run pytest
```

- 번호 정규화, XML→JSON 변환(`tests/fixtures/`의 응답 샘플), 캐시, 한도, 재시도·토큰 재발급·비밀값 비노출
- KIPRIS 추가 상품: 실제 응답(`real_kipris_*.xml`) 파싱, 상품 선택에 따른 도구 등록, 미신청 안내·하루 기억·재확인
- 해외 검색(`ep_search`): CQL 조립·따옴표/괄호 무력화, 실제 응답(`real_ops_search.xml`, 0건 `real_ops_search_nodata.xml`) 파싱, 쪽 범위(최대 100건·2000건), 스로틀 안내, 한도 오류 재시도 안 함(`tests/test_search.py`)
- 다시 묻기: JP 연호 후보, 틀린 종류코드 → 종류코드 없이, 공개 직후 안내(`tests/test_fallback.py`, 실제 응답 `real_ops_biblio_epodoc.xml`·`real_ops_biblio_jp_era.xml`·`real_ops_images_jp_era.xml`)
- EP Register(`ep_register`): 실제 응답(`real_ops_register.xml`, 없음 `real_ops_register_nodata.xml`) 파싱, 출원/공개번호 → 경로, 인용문헌이 빈 경우 안내, 캐시·`opsThrottle`(`tests/test_register.py`)
- 공보 원본 PDF: 실제 응답(`real_kipris_fulltext_*.xml`, `real_ops_images*.xml`, `real_ops_biblio_kr.xml`) 파싱, 파일 이름, 허용 폴더 밖·`..`·심볼릭 링크 거부, 덮어쓰기 거부, 쪽 합치기, 모호한 종류코드 후보, KIPRIS→OPS 넘어가기, 속도 조절(`tests/test_pdf.py`)
- MCP Inspector로 도구 목록 확인: 위 데스크톱 앱 설정과 같은 내용을 `mcp.json`으로 저장한 뒤

```bash
npx @modelcontextprotocol/inspector --cli --config mcp.json --server patent-api --method tools/list
```

  (`--cli` 뒤에 명령을 직접 적으면 Inspector가 `--directory`를 자기 옵션으로 가로채므로 설정 파일 방식을 쓴다)

### 확장 파일(.mcpb) 만들기

```bash
npx @anthropic-ai/mcpb validate manifest.json
npx @anthropic-ai/mcpb pack . dist/patent-api.mcpb
```

`.mcpbignore`가 `.env`·캐시·테스트를 뺀다. 만든 뒤 압축을 풀어 키 값이 없는지 확인한다.

## 구조

```
server.py        MCP 도구 정의만 (얇게)
core/            MCP를 모르는 계층 — B단계 원격 서버가 그대로 쓴다
  service.py     업무 단위 조회: 캐시·한도·응답 틀
  kipris.py      KIPRIS Plus 호출·파싱(공개·등록공보)
  kipris_docs.py KIPRIS 추가 상품 응답 파싱(심사 서류·청구항 이력·등록사항 등)
  products.py    KIPRIS 상품 목록·설정(어떤 상품의 도구를 켤지)
  ops.py         EPO OPS 인증·호출·파싱
  numbers.py     번호 정규화
  pdf.py         공보 원본 PDF: 허용 폴더 검사·파일 이름·쪽 합치기·안전한 저장
  cache.py       SQLite 캐시
  quota.py       호출 횟수·한도, KIPRIS 상품 신청 상태
  http.py        재시도
  errors.py      표준 오류 코드
  config.py      설정 읽기(환경변수 > .env)
manifest.json    Claude 데스크톱 확장(.mcpb) 정의 — 키 입력칸·PDF 허용 폴더·KIPRIS 상품 체크칸(user_config)
```

비밀값은 `.env`(또는 확장 설치 시 키체인)에만 두고 코드·로그·도구 응답 어디에도 출력하지 않는다(오류 메시지도 키를 가린다).

## 프로젝트 지침에 넣을 문장 (예시)

> 인용문헌·대응 출원의 서지, 패밀리, 법적 상태, 청구항은 patent-api 도구(kr_biblio, ep_biblio, family,
> legal_status, ep_text)로, 한국 출원의 심사 서류·청구항 변동은 kr_exam_documents·kr_claim_history로 확인한다.
> 공개된 기술 주제의 해외 문헌은 ep_search로, 한국 문헌은 kr_search로 찾는다(출원 전 사건 내용은 넣지 않는다).
> 유럽 출원의 진행 상태·검색보고서·인용문헌(X/Y/A)·분할 관계는 ep_register로 확인한다.
> 인용문헌 공보 원본 PDF가 필요하면 pdf_download로 사건폴더에 받는다(label에 D1 등).
> kr_deadlines의 기한은 참고만 하고 기한관리 시스템으로 확인한다. 도구 결과의 번호·날짜는 그대로 옮기고, 도구가 "자료 없음"을 돌려주면
> 추정해서 채우지 않고 그 사실을 적는다. 출원 전 사건의 발명 내용을 검색어로 보내지 않는다.
