"""업무 단위 조회. 캐시·한도·응답 틀을 여기서 처리한다.

MCP를 모르는 계층이다. server.py(로컬 MCP)와 B단계 원격 서버가 같은 PatentService를 쓴다.
모든 메서드는 같은 바깥 틀의 dict를 돌려준다:
  성공: {"ok": True, "source", "query", "cached", "data", "notes"}
  실패: {"ok": False, "source", "query", "error": {"code", "message"}, "notes"}
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import httpx

from . import errors as E
from .cache import Cache
from .config import Settings
from .errors import PatentApiError, scrub
from .kipris import KiprisClient
from .kipris import SOURCE as KIPRIS
from .numbers import (
    OpsReference,
    format_kr_application,
    normalize_kr_application_number,
    normalize_ops_number,
)
from .ops import OpsClient
from .ops import SOURCE as OPS
from .quota import Quota

log = logging.getLogger("patent_api")

NO_DATA_NOTE = "해당 번호의 자료 없음"
TEXT_SECTIONS = ("claims", "description", "abstract")
SEARCH_MAX_ROWS = 50
_CACHE_MISS = object()


def envelope_ok(source: str, query: dict, data: Any, *, cached: bool = False, notes: list[str] | None = None) -> dict:
    return {"ok": True, "source": source, "query": query, "cached": cached, "data": data, "notes": notes or []}


def envelope_error(source: str, query: dict, code: str, message: str, notes: list[str] | None = None) -> dict:
    return {"ok": False, "source": source, "query": query, "error": {"code": code, "message": message}, "notes": notes or []}


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    if cut < limit * 0.8:
        cut = limit
    return text[:cut].rstrip(), True


def _yyyymmdd(value: str | None, label: str) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) != 8:
        raise PatentApiError(E.INVALID_INPUT, f"{label}은 YYYY-MM-DD 형식이어야 합니다(입력: {value}).")
    return digits


class PatentService:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        cache: Cache | None = None,
        quota: Quota | None = None,
    ):
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=settings.http_timeout, follow_redirects=True)
        self.cache = cache or Cache(settings.cache_path)
        self.quota = quota or Quota(
            settings.cache_path,
            kipris_monthly_limit=settings.kipris_monthly_limit,
            kipris_warn_at=settings.kipris_warn_at,
        )
        key_hint = settings.key_hint()
        self.kipris = KiprisClient(
            settings.kipris_service_key,
            base_url=settings.kipris_base_url,
            http=self.http,
            on_call=self.quota.record_kipris_call,
            key_hint=key_hint,
        )
        self.ops = OpsClient(
            settings.epo_consumer_key,
            settings.epo_consumer_secret,
            base_url=settings.ops_base_url,
            http=self.http,
            on_headers=self.quota.record_ops_headers,
            key_hint=key_hint,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- 공통 실행 틀 ---------------------------------------------------

    async def _run(
        self,
        *,
        source: str,
        tool: str,
        query: dict,
        cache_params: dict | None,
        ttl: int,
        fetch: Callable[[], Awaitable[Any]],
        kipris: bool = False,
    ) -> tuple[dict, Any]:
        """캐시 확인 → 한도 확인 → 호출 → 캐시 저장. (envelope, data) 를 돌려준다.

        data는 후처리(잘라내기 등)를 위해 따로 돌려준다. envelope["data"]는 호출한 쪽이 채운다.
        """
        notes: list[str] = []
        if cache_params is not None:
            hit = self.cache.get(tool, cache_params, _CACHE_MISS)
            if hit is not _CACHE_MISS:
                if hit is None:
                    notes.append(NO_DATA_NOTE)
                return envelope_ok(source, query, None, cached=True, notes=notes), hit
        if kipris:
            self.quota.check_kipris()
        data = await fetch()
        if kipris:
            warn = self.quota.kipris_warning()
            if warn:
                notes.append(warn)
        if cache_params is not None:
            # '자료 없음'은 곧 바뀔 수 있으므로 하루만 기억한다.
            self.cache.set(tool, cache_params, data, ttl if data is not None else min(ttl, 86400))
        if data is None:
            notes.append(NO_DATA_NOTE)
        return envelope_ok(source, query, None, notes=notes), data

    async def _guard(self, source: str, query: dict, body: Callable[[], Awaitable[dict]]) -> dict:
        try:
            return await body()
        except PatentApiError as e:
            return envelope_error(source, query, e.code, scrub(e.message, self.settings.secrets()))
        except Exception as e:  # 예상하지 못한 오류도 틀을 지켜 돌려준다
            log.exception("unexpected error")
            msg = scrub(f"예상하지 못한 내부 오류: {type(e).__name__}: {e}", self.settings.secrets())
            return envelope_error(source, query, E.UPSTREAM_ERROR, msg)

    def _ops_ref(self, number: str, number_type: str | None) -> OpsReference:
        return normalize_ops_number(number, number_type or None)  # type: ignore[arg-type]

    @staticmethod
    def _kind_note(env: dict, ref: OpsReference) -> None:
        if ref.kind_assumed:
            env["notes"].append(f"종류코드가 없어 {ref.kind}로 보고 조회했습니다({ref.display}). 다른 종류면 종류코드를 붙여 주세요.")

    # --- 도구 -----------------------------------------------------------

    async def kr_biblio(self, application_number: str, include_claims: bool = False, include_history: bool = False) -> dict:
        query: dict[str, Any] = {"input": application_number}

        async def body() -> dict:
            digits = normalize_kr_application_number(application_number)
            query.clear()
            query.update({"applicationNumber": digits})
            env, data = await self._run(
                source=KIPRIS,
                tool="kr_biblio",
                query=query,
                cache_params={"applicationNumber": digits},
                ttl=self.settings.ttl_biblio,
                fetch=lambda: self.kipris.biblio_detail(digits),
                kipris=True,
            )
            if data is None:
                env["notes"].append(
                    f"{format_kr_application(digits)}을(를) 출원번호로 조회했습니다. "
                    "공개번호를 넣은 것이라면 ep_biblio에 'KR 공개번호 A' 형식으로 조회하세요."
                )
                return env
            data = dict(data)
            if not include_claims:
                if data.get("claims"):
                    env["notes"].append(f"청구항 {len(data['claims'])}개 생략(include_claims=true로 볼 수 있음)")
                data.pop("claims", None)
            else:
                claims_text = "\n".join(data.get("claims") or [])
                claims_text, cut = truncate_text(claims_text, self.settings.max_text_chars)
                if cut:
                    data["claims"] = claims_text.split("\n")
                    env["notes"].append(f"청구항이 길어 {self.settings.max_text_chars}자에서 잘랐습니다.")
            if not include_history:
                data.pop("history", None)
            env["data"] = data
            return env

        return await self._guard(KIPRIS, query, body)

    async def kr_search(
        self,
        keyword: str | None = None,
        title: str | None = None,
        applicant: str | None = None,
        ipc: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_utility: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        query: dict[str, Any] = _drop_empty(
            {
                "keyword": keyword,
                "title": title,
                "applicant": applicant,
                "ipc": ipc,
                "dateFrom": date_from,
                "dateTo": date_to,
                "includeUtility": include_utility,
                "page": page,
                "pageSize": page_size,
            }
        )

        async def body() -> dict:
            if not any(v and str(v).strip() for v in (keyword, title, applicant, ipc)):
                raise PatentApiError(E.INVALID_INPUT, "keyword, title, applicant, ipc 중 하나는 넣어야 합니다.")
            p = max(int(page or 1), 1)
            rows = min(max(int(page_size or 20), 1), SEARCH_MAX_ROWS)
            d_from, d_to = _yyyymmdd(date_from, "date_from"), _yyyymmdd(date_to, "date_to")
            date_range = None
            if d_from or d_to:
                date_range = f"{d_from or '19480101'}~{d_to or '20991231'}"
            params = _drop_empty(
                {
                    "word": (keyword or "").strip() or None,
                    "inventionTitle": (title or "").strip() or None,
                    "applicant": (applicant or "").strip() or None,
                    "ipcNumber": (ipc or "").strip() or None,
                    "applicationDate": date_range,
                    "patent": "true",
                    "utility": "true" if include_utility else "false",
                    "pageNo": p,
                    "numOfRows": rows,
                    "sortSpec": "AD",
                    "descSort": "true",
                }
            )
            query["page"], query["pageSize"] = p, rows
            env, data = await self._run(
                source=KIPRIS,
                tool="kr_search",
                query=query,
                cache_params=params,
                ttl=self.settings.ttl_search,
                fetch=lambda: self.kipris.advanced_search(params),
                kipris=True,
            )
            if data is None:
                env["notes"] = [n if n != NO_DATA_NOTE else "검색 결과 없음" for n in env["notes"]]
            else:
                total = data.get("totalCount", 0)
                if total > p * rows:
                    env["notes"].append(f"전체 {total}건 중 {p}쪽({rows}건씩)입니다. page를 올려 다음 쪽을 볼 수 있습니다.")
            env["data"] = data
            return env

        return await self._guard(KIPRIS, query, body)

    async def ep_biblio(self, number: str, number_type: str | None = None) -> dict:
        query: dict[str, Any] = {"input": number}

        async def body() -> dict:
            ref = self._ops_ref(number, number_type)
            query.update(ref.to_query())
            env, data = await self._run(
                source=OPS,
                tool="ep_biblio",
                query=query,
                cache_params={"ref": ref.path()},
                ttl=self.settings.ttl_biblio,
                fetch=lambda: self.ops.biblio(ref),
            )
            self._kind_note(env, ref)
            if data is None and not ref.kind:
                env["notes"].append("종류코드(A1, B1 등)를 붙여 다시 조회하면 찾을 수도 있습니다.")
            env["data"] = data
            return env

        return await self._guard(OPS, query, body)

    async def ep_text(
        self,
        number: str,
        section: str = "claims",
        lang: str | None = None,
        max_chars: int | None = None,
    ) -> dict:
        query: dict[str, Any] = {"input": number, "section": section}

        async def body() -> dict:
            if section not in TEXT_SECTIONS:
                raise PatentApiError(E.INVALID_INPUT, "section은 claims, description, abstract 중 하나여야 합니다.")
            ref = self._ops_ref(number, None)
            if ref.ref_type != "publication":
                raise PatentApiError(E.INVALID_INPUT, "원문은 공개·등록번호로만 조회할 수 있습니다.")
            query.update(ref.to_query())
            if lang:
                query["lang"] = lang
            lang_norm = lang.strip().lower() if lang else None
            env, data = await self._run(
                source=OPS,
                tool="ep_text",
                query=query,
                cache_params={"ref": ref.path(), "section": section, "lang": lang_norm},
                ttl=self.settings.ttl_text,
                fetch=lambda: self.ops.fulltext(ref, section, lang_norm),
            )
            self._kind_note(env, ref)
            if data is None:
                env["notes"].append(
                    "OPS 원문(청구항·명세서)은 EP·WO와 일부 국가만 제공합니다. "
                    + ("종류코드(A1, B1 등)를 붙여 다시 조회해 보세요. " if not ref.kind else "")
                    + "다른 패밀리 구성원(EP·WO)을 family로 찾아 조회해 보세요."
                )
                env["data"] = None
                return env
            data = dict(data)
            paragraphs = data.pop("paragraphs", [])
            text = "\n".join(paragraphs)
            limit = self.settings.max_text_chars if not max_chars else min(max(int(max_chars), 1000), 100000)
            text, cut = truncate_text(text, limit)
            data["section"] = section
            data["length"] = len("\n".join(paragraphs))
            data["text"] = text
            data["truncated"] = cut
            if cut:
                env["notes"].append(f"원문이 길어 {limit}자에서 잘랐습니다(전체 {data['length']}자). max_chars로 늘릴 수 있습니다(최대 100000).")
            if lang_norm and (data.get("lang") or "").lower() != lang_norm:
                env["notes"].append(f"요청한 언어({lang})가 없어 {data.get('lang')}로 돌려줍니다.")
            env["data"] = data
            return env

        return await self._guard(OPS, query, body)

    async def family(self, number: str, number_type: str | None = None) -> dict:
        query: dict[str, Any] = {"input": number}

        async def body() -> dict:
            ref = self._ops_ref(number, number_type)
            query.update(ref.to_query())
            env, data = await self._run(
                source=OPS,
                tool="family",
                query=query,
                cache_params={"ref": ref.path()},
                ttl=self.settings.ttl_family,
                fetch=lambda: self.ops.family(ref),
            )
            self._kind_note(env, ref)
            env["data"] = data
            return env

        return await self._guard(OPS, query, body)

    async def legal_status(self, number: str, number_type: str | None = None) -> dict:
        query: dict[str, Any] = {"input": number}

        async def body() -> dict:
            ref = self._ops_ref(number, number_type)
            query.update(ref.to_query())
            env, data = await self._run(
                source=OPS,
                tool="legal_status",
                query=query,
                cache_params={"ref": ref.path()},
                ttl=self.settings.ttl_legal,
                fetch=lambda: self.ops.legal(ref),
            )
            self._kind_note(env, ref)
            if data is None:
                env["notes"].append("OPS(INPADOC)에 법적 상태 이벤트가 없습니다. 한국 출원은 kr_biblio(include_history=true)로도 확인할 수 있습니다.")
            env["data"] = data
            return env

        return await self._guard(OPS, query, body)

    async def quota_status(self) -> dict:
        query: dict[str, Any] = {}

        async def body() -> dict:
            data = self.quota.status()
            data["keys"] = {
                "kipris": bool(self.settings.kipris_service_key),
                "epoOps": bool(self.settings.epo_consumer_key and self.settings.epo_consumer_secret),
            }
            notes = []
            if data["ops"] is None:
                notes.append("OPS 사용량 헤더가 아직 기록되지 않았습니다(OPS를 한 번 호출하면 기록됨).")
            warn = self.quota.kipris_warning()
            if warn:
                notes.append(warn)
            return envelope_ok("internal", query, data, notes=notes)

        return await self._guard("internal", query, body)


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}
