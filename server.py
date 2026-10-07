"""patent-api MCP 서버 (stdio). 도구 정의만 둔다 — 실제 일은 core/가 한다."""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from core.config import Settings
from core.service import PatentService

# stdio 서버는 stdout을 프로토콜에 쓰므로 로그는 stderr로만 보낸다.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")

settings = Settings.load()
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


@mcp.tool(
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


@mcp.tool(
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


@mcp.tool(
    name="quota_status",
    title="호출 한도 현황",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    description=(
        "API 호출 한도가 걱정될 때 남은 양을 확인하는 도구.\n"
        "이번 달 KIPRIS 호출 수·한도·경고선, 최근 OPS 응답의 사용량 헤더(X-Throttling-Control 등), "
        "키 설정 여부(값은 보여주지 않음)를 돌려준다. 외부 호출은 하지 않는다."
    ),
)
async def quota_status() -> dict[str, Any]:
    return await service.quota_status()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
