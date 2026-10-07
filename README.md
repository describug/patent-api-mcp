# patent-api MCP

KIPRIS Plus(한국)와 EPO OPS(유럽특허청)를 하나의 MCP 서버로 감싼 로컬 도구.
Claude 데스크톱 앱과 Claude Code에 **한 번만** 등록해 모든 프로젝트에서 쓴다.
API 키는 들어 있지 않다 — 쓰는 사람이 각자 무료로 발급받아 넣는다.

용도: 인용문헌·대응 출원의 서지, 패밀리, 법적 상태, 청구항 확인.

## 도구

| 도구 | 입력 | 하는 일 | 출처 |
|---|---|---|---|
| `kr_biblio` | 한국 출원번호 | 서지 상세(명칭·출원인·발명자·IPC·공개/등록번호·일자·초록·상태·우선권·심사관 인용문헌). 선택: 청구항, 행정처리 이력 | KIPRIS `getBibliographyDetailInfoSearch` |
| `kr_search` | 키워드·명칭·출원인·IPC·출원일 기간·쪽 | 한국 특허·실용신안 검색 목록 ⚠️ 출원 전 사건 내용으로 검색 금지 | KIPRIS `getAdvancedSearch` |
| `ep_biblio` | 공개·등록번호(국가 무관) | 서지·초록·인용문헌 | OPS `published-data/.../biblio` |
| `ep_text` | 번호, `section`=claims/description/abstract | 청구항·명세서·초록 원문(EP·WO 등 제공 범위 내, 기본 2만 자에서 자름) | OPS `published-data/.../claims` 등 |
| `family` | 번호(국가 무관) | INPADOC 패밀리 구성원(국가·번호·종류코드·일자) | OPS `family` |
| `legal_status` | 번호 | INPADOC 법적 상태 이벤트 | OPS `legal` |
| `quota_status` | 없음 | 이번 달 KIPRIS 호출 수, 최근 OPS 사용량 헤더 | 내부 |

### 응답 형식

```json
{"ok": true, "source": "KIPRIS", "query": {"applicationNumber": "1020260012345"},
 "cached": false, "data": {...}, "notes": []}
```

- 자료 없음: `"ok": true, "data": null, "notes": ["해당 번호의 자료 없음"]` (오류와 구분)
- 오류: `"ok": false, "error": {"code": "AUTH_FAILED", "message": "한국어 설명"}`
- 오류 코드: `CONFIG_MISSING_KEY`, `AUTH_FAILED`, `PERMISSION_DENIED`, `QUOTA_EXCEEDED`, `RATE_LIMITED`,
  `INVALID_INPUT`, `AMBIGUOUS_NUMBER`, `UPSTREAM_BAD_REQUEST`, `UPSTREAM_UNAVAILABLE`, `TIMEOUT`,
  `NETWORK_ERROR`, `UPSTREAM_ERROR`, `PARSE_ERROR`
- 날짜는 `YYYY-MM-DD`

### 번호 입력

| 입력 예 | 해석 |
|---|---|
| `10-2026-0012345`, `1020260012345`, `KR10-2026-0012345` | `kr_biblio`: 한국 출원번호 |
| `KR 10-2026-0012345 A` | OPS: 한국 공개번호 `KR.20260012345.A` |
| `KR 10-1234567 B1` | OPS: 한국 등록번호 `KR.101234567.B1` |
| `EP1234567A1`, `US 2026/0123456 A1`, `WO2026/012345` | OPS 공개번호 |

한국 `10-YYYY-NNNNNNN`은 출원번호와 공개번호가 같은 모양이라, OPS 도구에서는 종류코드(`A`)를 붙이거나
`number_type`(`publication`/`application`)을 지정해야 한다. 추측하지 않고 `AMBIGUOUS_NUMBER`로 돌려준다.

## 시작하기

1. **키 발급(무료):** KIPRIS Plus 인증키와 EPO OPS Consumer Key/Secret을 받는다. 그림 안내: [docs/API키_발급_안내.md](docs/API키_발급_안내.md)
   - KIPRIS Plus: 가입하면 키가 나오지만, **Open API '특허·실용 공개·등록공보'를 무료 플랜으로 신청**해야 동작한다
     (장바구니 기본값이 **유료**이니 꼭 무료로 바꾼다). 이용기간은 그해 12월 31일까지라 **해마다 다시 신청**한다.
   - EPO OPS: 가입(Account Type: **Non-paying**) → EPO 승인 메일(1~2 영업일) → My Apps에서 앱을 만들면 키가 나온다.
2. **설치:** 아래 셋 중 하나.

### 방법 A. Claude 데스크톱 확장 파일 (가장 쉬움)

1. [Releases](https://github.com/describug/patent-api-mcp/releases/latest)에서 `patent-api.mcpb`를 받아 더블클릭(또는 Claude 데스크톱 앱 > 설정 > 확장 프로그램으로 끌어다 놓기) → **설치**
2. **설정 > 확장 프로그램 > Patent API**에서 키 3개 입력 → **저장**
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

## 캐시와 한도

- SQLite 캐시: 사용자 폴더의 `patent-api-mcp/cache.sqlite3`
  (macOS `~/Library/Application Support/`, Windows `%LOCALAPPDATA%\`, Linux `~/.local/share/`). 키 = 도구명 + 정규화된 입력. 보존 기간 기본값: 서지·원문 30일, 패밀리 7일,
  법적 상태 1일, 검색 1일. `.env`의 `CACHE_TTL_*_DAYS`로 바꾼다. "자료 없음"은 최대 1일만 기억한다.
- KIPRIS: 실제로 보낸 요청(재시도 포함)을 월별로 센다. `KIPRIS_WARN_AT` 이상이면 `notes`에 경고,
  `KIPRIS_MONTHLY_LIMIT`에 닿으면 호출하지 않고 `QUOTA_EXCEEDED`(캐시는 계속 응답).
- OPS: 응답의 사용량 헤더(`X-Throttling-Control`, `X-IndividualQuotaPerHour-Used` 등)를 기록해 `quota_status`에 보여준다.
  429·403(한도 초과)은 재시도하지 않는다.
- 일시 오류(타임아웃, 503)만 짧은 간격으로 최대 2회 재시도한다.
- 캐시를 비우려면 위 `cache.sqlite3`를 지운다(이번 달 호출 수 기록도 함께 지워진다).

## 테스트

```bash
uv run pytest
```

- 번호 정규화, XML→JSON 변환(`tests/fixtures/`의 응답 샘플), 캐시, 한도, 재시도·토큰 재발급·비밀값 비노출
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
  kipris.py      KIPRIS Plus 호출·파싱
  ops.py         EPO OPS 인증·호출·파싱
  numbers.py     번호 정규화
  cache.py       SQLite 캐시
  quota.py       호출 횟수·한도
  http.py        재시도
  errors.py      표준 오류 코드
  config.py      설정 읽기(환경변수 > .env)
manifest.json    Claude 데스크톱 확장(.mcpb) 정의 — 키 입력칸(user_config)
```

비밀값은 `.env`(또는 확장 설치 시 키체인)에만 두고 코드·로그·도구 응답 어디에도 출력하지 않는다(오류 메시지도 키를 가린다).

## 프로젝트 지침에 넣을 문장 (예시)

> 인용문헌·대응 출원의 서지, 패밀리, 법적 상태, 청구항은 patent-api 도구(kr_biblio, ep_biblio, family,
> legal_status, ep_text)로 확인한다. 도구 결과의 번호·날짜는 그대로 옮기고, 도구가 "자료 없음"을 돌려주면
> 추정해서 채우지 않고 그 사실을 적는다. 출원 전 사건의 발명 내용을 검색어로 보내지 않는다.
