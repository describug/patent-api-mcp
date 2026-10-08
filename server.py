"""patent-api MCP 서버. 도구 정의만 둔다 — 실제 일은 core/가 한다.

기본은 로컬 stdio(Claude Code·데스크톱 확장). PATENT_API_TRANSPORT=http 또는 `--transport http`면
원격 서버 모드(streamable-http, /mcp, OAuth 필수)로 뜬다 — remote/ 참고.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from core.config import Settings
from core.service import PatentService

# stdio 서버는 stdout을 프로토콜에 쓰므로 로그는 stderr로만 보낸다.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")



def select_transport(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> str:
    """'stdio'(기본) 또는 'http'. 실행 인자(--transport http, --http)가 환경변수 PATENT_API_TRANSPORT보다 우선한다."""
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    value = environ.get("PATENT_API_TRANSPORT") or "stdio"
    for i, arg in enumerate(argv):
        if arg == "--http":
            value = "http"
        elif arg == "--stdio":
            value = "stdio"
        elif arg == "--transport" and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--transport="):
            value = arg.split("=", 1)[1]
    value = value.strip().lower()
    if value in ("http", "streamable-http", "streamable_http"):
        return "http"
    if value == "stdio":
        return "stdio"
    raise SystemExit(f"[patent-api] 알 수 없는 전송 방식: {value} (stdio 또는 http)")


TRANSPORT = select_transport()
settings = Settings.load(remote=TRANSPORT == "http")
service = PatentService(settings)

mcp = MCPServer(
    "patent-api",
    instructions=(
        "KIPRIS Plus(한국)와 EPO OPS(유럽특허청)로 특허 서지·패밀리·법적 상태·청구항을 조회한다. "
        "모든 도구는 {ok, source, query, cached, data, notes} JSON을 돌려준다. "
        "data가 null이고 notes에 '자료 없음'이 있으면 그 사실을 그대로 전하고 추정해서 채우지 않는다. "
        "결과의 번호·날짜는 그대로 옮긴다. 출원 전 사건의 발명 내용을 검색어로 보내지 않는다."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

NumberType = Literal["publication", "application"]


def kipris_tool(**meta: Any):
    """KIPRIS 상품 도구. 설정에서 그 상품이 꺼져 있으면 등록하지 않는다(core/products.py)."""
    if settings.tool_enabled(meta["name"]):
        return mcp.tool(**meta)
    return lambda fn: fn


@kipris_tool(
    name="kr_biblio",
    title="한국 출원 서지 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "한국 특허·실용신안의 출원번호를 알 때 서지 상세를 확인하는 도구.\n"
        "명칭, 출원인, 발명자, 대리인, IPC, 공개·공고·등록번호와 일자, 초록, 등록상태·최종처분, 우선권, "
        "심사관 인용문헌을 돌려준다. include_claims=true면 청구항, include_history=true면 행정처리 이력도 붙인다.\n"
        "입력은 출원번호만 받는다(10-2026-0012345, 1020260012345, KR10-2026-0012345 모두 가능). "
        "공개번호·등록번호(종류코드가 붙었거나 10-1234567 모양)는 오류로 돌려준다 — 그때는 ep_biblio를 쓴다."
    ),
)
async def kr_biblio(
    application_number: Annotated[str, Field(description="한국 출원번호. 예: 10-2026-0012345")],
    include_claims: Annotated[bool, Field(description="청구항 전문 포함 여부")] = False,
    include_history: Annotated[bool, Field(description="행정처리 이력(접수·발송 서류) 포함 여부")] = False,
) -> dict[str, Any]:
    return await service.kr_biblio(application_number, include_claims, include_history)


@kipris_tool(
    name="kr_search",
    title="한국 특허 검색 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "공개된 한국 특허·실용신안을 키워드·출원인·IPC·출원일 기간으로 찾을 때 쓰는 도구.\n"
        "⚠️ 검색어는 KIPRIS 서버로 전송된다. 출원 전 사건(미공개 발명)의 내용·청구항·기술 요지를 검색어로 "
        "넣지 말 것. 이미 공개된 문헌의 명칭·출원인·IPC 확인 용도로만 쓴다.\n"
        "결과는 출원번호·명칭·출원인·IPC·상태·공개/등록번호와 일자·초록 목록이며 출원일 최신순이다. "
        "상세는 출원번호로 kr_biblio를 다시 부른다."
    ),
)
async def kr_search(
    keyword: Annotated[str | None, Field(description="자유검색어(공개된 문헌 확인용). 출원 전 사건 내용 금지")] = None,
    title: Annotated[str | None, Field(description="발명의 명칭에 들어간 말")] = None,
    applicant: Annotated[str | None, Field(description="출원인 이름. 예: 삼성전자")] = None,
    ipc: Annotated[str | None, Field(description="IPC. 예: H04W 72/04 또는 H04W")] = None,
    date_from: Annotated[str | None, Field(description="출원일 시작 YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Field(description="출원일 끝 YYYY-MM-DD")] = None,
    include_utility: Annotated[bool, Field(description="실용신안 포함 여부")] = True,
    page: Annotated[int, Field(ge=1, description="쪽 번호(1부터)")] = 1,
    page_size: Annotated[int, Field(ge=1, le=50, description="한 쪽 건수(최대 50)")] = 20,
) -> dict[str, Any]:
    return await service.kr_search(keyword, title, applicant, ipc, date_from, date_to, include_utility, page, page_size)


@mcp.tool(
    name="ep_biblio",
    title="공개·등록 문헌 서지 (EPO OPS)",
    annotations=READ_ONLY,
    description=(
        "공개·등록번호로 특허 문헌의 서지를 확인하는 도구(국가 무관: EP, US, WO, JP, CN, KR 등).\n"
        "명칭, 출원인, 발명자, IPC·CPC, 출원번호·일자, 우선권, 초록, 인용문헌(카테고리 포함)을 돌려준다.\n"
        "번호 예: EP1234567A1, US 2026/0123456 A1, WO2026/012345, KR 10-2026-0012345 A, KR 10-1234567 B1. "
        "종류코드를 붙이면 그 문헌만, 빼면 같은 번호의 문헌을 모두 돌려준다. "
        "한국 10-YYYY-NNNNNNN은 출원번호와 공개번호가 같은 모양이라 종류코드(A)를 붙이거나 "
        "number_type을 지정해야 한다."
    ),
)
async def ep_biblio(
    number: Annotated[str, Field(description="공개·등록번호. 예: EP1234567A1")],
    number_type: Annotated[NumberType | None, Field(description="번호 종류. 기본은 공개번호(publication)")] = None,
) -> dict[str, Any]:
    return await service.ep_biblio(number, number_type)


@mcp.tool(
    name="ep_text",
    title="청구항·명세서 원문 (EPO OPS)",
    annotations=READ_ONLY,
    description=(
        "공개·등록 문헌의 청구항·명세서·초록 원문을 읽을 때 쓰는 도구.\n"
        "section=claims(청구항) | description(명세서) | abstract(초록). OPS 원문은 EP·WO와 일부 국가만 제공한다 "
        "(US·KR·JP 원문은 대부분 없음 → family로 EP·WO 구성원을 찾아 조회). "
        "길면 max_chars(기본 20000자)에서 자르고 notes에 적는다. 여러 언어가 있으면 lang(예: en, de, fr)으로 고른다."
    ),
)
async def ep_text(
    number: Annotated[str, Field(description="공개·등록번호. 종류코드를 붙이는 게 정확하다. 예: EP1234567B1")],
    section: Annotated[Literal["claims", "description", "abstract"], Field(description="원문 구역")] = "claims",
    lang: Annotated[str | None, Field(description="언어 코드(en, de, fr 등). 없으면 영어 우선")] = None,
    max_chars: Annotated[int | None, Field(ge=1000, le=100000, description="최대 글자 수(기본 20000)")] = None,
) -> dict[str, Any]:
    return await service.ep_text(number, section, lang, max_chars)


@mcp.tool(
    name="family",
    title="INPADOC 패밀리 (EPO OPS)",
    annotations=READ_ONLY,
    description=(
        "어떤 문헌의 대응 출원(패밀리 구성원)을 찾을 때 쓰는 도구.\n"
        "INPADOC 확장 패밀리의 구성원별 공개번호(국가·번호·종류코드·일자), 출원번호·일자, 우선권과 "
        "구성원 국가 목록을 돌려준다. 번호는 국가 무관(EP1234567A1, KR 10-2026-0012345 A, US 2026/0123456 A1 등)."
    ),
)
async def family(
    number: Annotated[str, Field(description="공개·등록번호(출원번호면 number_type='application')")],
    number_type: Annotated[NumberType | None, Field(description="번호 종류. 기본은 공개번호(publication)")] = None,
) -> dict[str, Any]:
    return await service.family(number, number_type)


@mcp.tool(
    name="legal_status",
    title="법적 상태 (EPO OPS INPADOC)",
    annotations=READ_ONLY,
    description=(
        "문헌의 법적 상태(등록, 거절, 취하, 연차료 미납 소멸, 지정국 진입 등) 이력을 확인할 때 쓰는 도구.\n"
        "INPADOC 법적 상태 이벤트를 일자·코드·설명·영향(+/-)과 함께 구성원별로 돌려준다. "
        "이벤트는 각국 특허청이 EPO에 보낸 자료라 늦거나 빠질 수 있다. 한국 출원의 최신 상태는 kr_biblio도 함께 본다."
    ),
)
async def legal_status(
    number: Annotated[str, Field(description="공개·등록번호. 예: EP1234567B1")],
    number_type: Annotated[NumberType | None, Field(description="번호 종류. 기본은 공개번호(publication)")] = None,
) -> dict[str, Any]:
    return await service.legal_status(number, number_type)


ExamKind = Literal["opinion", "rejection", "allowance"]
KrApplicationNumber = Annotated[str, Field(description="한국 출원번호. 예: 10-2020-0168607")]


@kipris_tool(
    name="kr_exam_documents",
    title="심사 서류: 의견제출통지서·거절결정서·등록결정서 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "OA 대응·검토 때 한국 출원의 심사 서류(의견제출통지서, 거절결정서, 등록결정서) 내용을 확인하는 도구.\n"
        "서류별로 발송번호·발송일·제출기한, 심사 대상 청구항, 거절이유 표(거절이유가 있는 부분·관련 법조항), "
        "구체적 거절이유 본문, 본문에서 뽑은 인용발명 목록(citedReferences), 등록결정 내용·직권보정을 돌려준다. "
        "kinds로 서류 종류를 고르고(opinion=의견제출통지서, rejection=거절결정서, allowance=등록결정서, 기본은 켜진 것 전부), "
        "send_number로 한 통만 고를 수 있다. 서류 종류마다 KIPRIS를 1~4회 부르므로(캐시 1일) 필요한 종류만 고르면 한도를 아낀다. "
        "citedReferences는 본문에서 자동 추출한 것이라 원문으로 확인한다."
    ),
)
async def kr_exam_documents(
    application_number: KrApplicationNumber,
    kinds: Annotated[list[ExamKind] | None, Field(description="서류 종류. 비우면 켜진 상품 전부")] = None,
    send_number: Annotated[str | None, Field(description="발송번호(15자리)로 한 통만 보기")] = None,
    include_text: Annotated[bool, Field(description="거절이유 본문·결정 내용 포함 여부(false면 서지·대상 청구항·법조항 표만)")] = True,
) -> dict[str, Any]:
    return await service.kr_exam_documents(application_number, kinds, send_number, include_text)


@kipris_tool(
    name="kr_claim_history",
    title="청구항 변동 이력 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "보정으로 청구항이 어떻게 바뀌어 왔는지 확인할 때 쓰는 도구.\n"
        "청구항이 바뀐 서류(출원서, 보정서 등)마다 일자·서류명과 신규·수정·삭제된 청구항 번호를 돌려준다. "
        "claim을 주면 그 청구항의 판별 원문과 바뀐 부분(diff: [-삭제-] {+추가+})을, include_text=true면 모든 청구항 원문을 붙인다. "
        "KIPRIS를 2회 부른다(캐시 1일)."
    ),
)
async def kr_claim_history(
    application_number: KrApplicationNumber,
    claim: Annotated[int | None, Field(ge=1, description="이 청구항만 원문·변경 내용 보기")] = None,
    include_text: Annotated[bool, Field(description="모든 청구항 원문 포함(길면 잘림)")] = False,
) -> dict[str, Any]:
    return await service.kr_claim_history(application_number, claim, include_text)


@kipris_tool(
    name="kr_deadlines",
    title="통지서 마감기한 (KIPRIS) — 참고용",
    annotations=READ_ONLY,
    description=(
        "한국 출원·등록에 걸린 통지서의 제출 마감기한을 참고로 확인할 때 쓰는 도구.\n"
        "⚠️ 공식 기한관리를 대체할 수 없다. 이미 대응한 통지서와 지난 기한도 그대로 나오고, 지정기간 연장이 반영되지 않을 수 있으며, "
        "공개 전 출원은 자료가 없을 수 있다. 기한은 반드시 특허로·사내 기한관리로 확인한다.\n"
        "application_number(출원 통지서) 또는 registration_number(등록 통지서) 중 하나를 넣는다. "
        "통지서별 문서명·발송번호·마감일과 오늘 기준 남은 날(daysLeft)을 돌려준다."
    ),
)
async def kr_deadlines(
    application_number: Annotated[str | None, Field(description="한국 출원번호. 예: 10-2020-0168607")] = None,
    registration_number: Annotated[str | None, Field(description="한국 등록번호. 예: 10-3028032")] = None,
) -> dict[str, Any]:
    return await service.kr_deadlines(application_number, registration_number)


@kipris_tool(
    name="kr_registration",
    title="등록사항: 권리자·존속기간·연차료 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "한국 등록특허·실용의 등록원부 사항(현재 권리자, 권리 이전, 존속기간 만료일, 소멸, 연차료 납부)을 확인할 때 쓰는 도구.\n"
        "registration_number(예: 10-3028032) 또는 application_number 중 하나를 넣는다. "
        "출원번호를 넣으면 kr_biblio 서지에서 등록번호를 찾아 조회한다(KIPRIS 1회 더, 캐시되어 있으면 0회). "
        "연차료는 납부 기록(몇 년차까지)만 주며 다음 납부기한은 계산하지 않는다."
    ),
)
async def kr_registration(
    registration_number: Annotated[str | None, Field(description="한국 등록번호. 예: 10-3028032")] = None,
    application_number: Annotated[str | None, Field(description="한국 출원번호(등록번호를 모를 때)")] = None,
) -> dict[str, Any]:
    return await service.kr_registration(registration_number, application_number)


@kipris_tool(
    name="kr_citations",
    title="인용·피인용 문헌 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "한국 출원의 인용문헌(이 출원의 심사·조사에서 인용된 선행문헌)과 피인용(이 출원을 인용한 뒤의 한국 출원)을 확인하는 도구.\n"
        "direction=cited(인용문헌) | citing(피인용) | both(기본). 인용문헌은 번호·국가·종류코드·발행일·구분(선행기술조사문헌, 심사관 인용 등)을, "
        "피인용은 인용한 출원번호를 돌려준다. 방향마다 KIPRIS를 1회 부른다(캐시 7일)."
    ),
)
async def kr_citations(
    application_number: KrApplicationNumber,
    direction: Annotated[Literal["cited", "citing", "both"], Field(description="cited=인용문헌, citing=피인용, both=둘 다")] = "both",
) -> dict[str, Any]:
    return await service.kr_citations(application_number, direction)


@kipris_tool(
    name="kr_legal_history",
    title="법적 상태 이력 ST.27 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "한국 출원의 법적 상태 변화(출원, 공개, 심사청구, 거절이유통지, 등록결정, 등록, 소멸 등)를 시간순으로 확인할 때 쓰는 도구.\n"
        "WIPO ST.27 표준 이벤트(일자, 대분류 category, 주요·상세 이벤트 코드, 국내 법적상태코드, 단계 변화)를 돌려준다. "
        "서류 이름이 붙은 행정처리 이력은 kr_biblio(include_history=true), 해외 구성원의 법적 상태는 legal_status를 쓴다."
    ),
)
async def kr_legal_history(application_number: KrApplicationNumber) -> dict[str, Any]:
    return await service.kr_legal_history(application_number)


@kipris_tool(
    name="kr_family",
    title="한국 출원의 해외 패밀리 (KIPRIS)",
    annotations=READ_ONLY,
    description=(
        "한국 출원번호로 해외 대응 출원(DOCDB 패밀리)을 찾을 때 쓰는 도구.\n"
        "구성원별 공보번호(국가·번호·종류코드)·공보일, 출원번호·출원일과 국가 목록을 돌려준다. "
        "한국 출원번호 하나로 바로 찾을 때 편하고, 공개번호 기준·INPADOC 확장 패밀리는 family(EPO OPS)를 쓴다."
    ),
)
async def kr_family(application_number: KrApplicationNumber) -> dict[str, Any]:
    return await service.kr_family(application_number)


@mcp.tool(
    name="quota_status",
    title="호출 한도 현황",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "API 호출 한도가 걱정되거나 어떤 KIPRIS 상품을 쓸 수 있는지 볼 때 쓰는 도구.\n"
        "이번 달 KIPRIS 호출 수·한도·경고선(신청 상품 전체 합산), 최근 OPS 응답의 사용량 헤더(X-Throttling-Control 등), "
        "키 설정 여부(값은 보여주지 않음), KIPRIS 상품별 켜짐 여부와 신청 상태(신청됨/미신청/확인 안 함)를 돌려준다. "
        "외부 호출은 하지 않는다. KIPRIS에서 상품을 방금 신청했다면 recheck_products=true로 '미신청' 기록을 지운다."
    ),
)
async def quota_status(
    recheck_products: Annotated[bool, Field(description="'미신청' 기록을 지워 다음 조회 때 다시 확인")] = False,
) -> dict[str, Any]:
    return await service.quota_status(recheck_products)


def main() -> None:
    if TRANSPORT == "http":
        # 원격 모드: 위에서 등록한 도구를 그대로 옮겨 OAuth를 붙인 서버로 띄운다.
        from remote.app import run_http

        run_http(mcp)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
