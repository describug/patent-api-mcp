"""업무 단위 조회. 캐시·한도·응답 틀을 여기서 처리한다.

MCP를 모르는 계층이다. server.py(로컬 MCP)와 B단계 원격 서버가 같은 PatentService를 쓴다.
모든 메서드는 같은 바깥 틀의 dict를 돌려준다:
  성공: {"ok": True, "source", "query", "cached", "data", "notes"}
  실패: {"ok": False, "source", "query", "error": {"code", "message"}, "notes"}
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx

from . import errors as E
from .cache import Cache
from .config import Settings
from .errors import PatentApiError, scrub
from . import kipris_docs as KD
from .kipris import NOT_SUBSCRIBED_CODES, KiprisClient
from .kipris import SOURCE as KIPRIS
from .numbers import (
    OpsReference,
    format_kr_application,
    normalize_kr_application_number,
    normalize_kr_registration_number,
    normalize_ops_number,
)
from .ops import OpsClient
from .ops import SOURCE as OPS
from .products import BY_KEY as PRODUCTS
from .products import PRODUCTS as PRODUCT_LIST
from .quota import NOT_SUBSCRIBED, SUBSCRIBED, Quota

log = logging.getLogger("patent_api")

NO_DATA_NOTE = "해당 번호의 자료 없음"
EXAM_KINDS = ("opinion", "rejection", "allowance")
EXAM_SERVICES = {
    "opinion": "IntermediateDocumentOPService",
    "rejection": "IntermediateDocumentREService",
    "allowance": "IntermediateDocumentRGService",
}
DEADLINE_WARNING = (
    "참고용입니다 — 공식 기한관리를 대체할 수 없습니다. 지정기간 연장·대응 완료 여부는 반영되지 않을 수 있으니 "
    "특허로·사내 기한관리로 반드시 확인하세요."
)
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
            rest_base_url=settings.kipris_rest_base_url,
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
        product: str | None = None,
    ) -> tuple[dict, Any]:
        """캐시 확인 → 한도 확인 → 호출 → 캐시 저장. (envelope, data) 를 돌려준다.

        data는 후처리(잘라내기 등)를 위해 따로 돌려준다. envelope["data"]는 호출한 쪽이 채운다.
        """
        data, cached, notes = await self._fetch(
            tool=tool, cache_params=cache_params, ttl=ttl, fetch=fetch,
            product=(product or "publication") if kipris else None,
        )
        if data is None:
            notes.append(NO_DATA_NOTE)
        return envelope_ok(source, query, None, cached=cached, notes=notes), data

    async def _fetch(
        self,
        *,
        tool: str,
        cache_params: dict | None,
        ttl: int,
        fetch: Callable[[], Awaitable[Any]],
        product: str | None = None,
    ) -> tuple[Any, bool, list[str]]:
        """(data, cached, notes). product가 있으면 KIPRIS 호출: 한도·상품 신청 상태를 함께 다룬다."""
        notes: list[str] = []
        if cache_params is not None:
            hit = self.cache.get(tool, cache_params, _CACHE_MISS)
            if hit is not _CACHE_MISS:
                return hit, True, notes
        if product:
            self.kipris.require_key()
            remembered = self.quota.product_status(product, self.settings.kipris_service_key)
            if remembered and remembered["active"]:
                raise self._not_subscribed(product, remembered.get("detail"), remembered=remembered)
            self.quota.check_kipris()
            try:
                data = await fetch()
            except PatentApiError as e:
                if e.upstream_code in NOT_SUBSCRIBED_CODES:
                    self.quota.set_product_status(product, NOT_SUBSCRIBED, self.settings.kipris_service_key, e.upstream_code)
                    raise self._not_subscribed(product, e.upstream_code, original=e) from None
                raise
            self.quota.set_product_status(product, SUBSCRIBED, self.settings.kipris_service_key)
            warn = self.quota.kipris_warning()
            if warn:
                notes.append(warn)
        else:
            data = await fetch()
        if cache_params is not None:
            # '자료 없음'은 곧 바뀔 수 있으므로 하루만 기억한다.
            self.cache.set(tool, cache_params, data, ttl if data is not None else min(ttl, 86400))
        return data, False, notes

    def _not_subscribed(
        self, product: str, upstream_code: str | None, *, original: PatentApiError | None = None, remembered: dict | None = None
    ) -> PatentApiError:
        """상품 미신청(이용기간 없음·접근 거부) 안내 오류를 만든다."""
        name = PRODUCTS[product].name
        tail = ""
        if remembered:
            tail = (
                f" ({remembered['checkedAt'][:16].replace('T', ' ')}에 확인한 결과를 하루 동안 기억해 KIPRIS를 다시 부르지 않았습니다. "
                "방금 신청했다면 quota_status를 recheck_products=true로 불러 기록을 지운 뒤 다시 조회하세요.)"
            )
        if product == "publication" and original is not None:
            # 기본 상품은 키 오류와 겹치므로 기존 안내(AUTH_FAILED 등)를 그대로 쓴다.
            return PatentApiError(original.code, original.message + tail, upstream_code=upstream_code)
        code_txt = f"KIPRIS resultCode {upstream_code}" if upstream_code else "KIPRIS 응답"
        msg = (
            f"KIPRIS Plus에서 Open API '{name}' 상품을 신청하지 않았거나 이용기간이 끝난 것으로 보입니다({code_txt}). "
            f"plus.kipris.or.kr > 데이터 신청 > Open API에서 '{name}'을 무료 플랜으로 신청하면 바로 쓸 수 있습니다"
            "(이용기간은 그해 12월 31일까지, 월 호출 한도는 신청한 상품 전체 합산)."
        )
        if upstream_code in ("30", "101") and not self.quota.any_product_subscribed(self.settings.kipris_service_key):
            msg += " 다른 KIPRIS 도구도 같은 오류라면 인증키(KIPRIS_SERVICE_KEY)가 맞는지도 확인하세요."
        if product == "publication":
            return PatentApiError(E.AUTH_FAILED, msg + tail, upstream_code=upstream_code)
        return PatentApiError(E.PRODUCT_NOT_SUBSCRIBED, msg + tail, upstream_code=upstream_code)

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

    # --- KIPRIS 추가 상품 -----------------------------------------------

    async def _kp(self, product: str, tool: str, op_key: str, params: dict, ttl: int, call) -> tuple[Any, bool, list[str]]:
        """KIPRIS 상품 한 오퍼레이션을 캐시·한도·신청 상태와 함께 부른다. call: () -> XML 파싱 결과."""
        return await self._fetch(
            tool=f"{tool}:{op_key}", cache_params=params, ttl=ttl, fetch=call, product=product
        )

    def _product_off_note(self, product: str) -> str:
        p = PRODUCTS[product]
        return (
            f"'{p.name}' 상품이 설정에서 꺼져 있어 조회하지 않았습니다"
            f"(확장 설정의 KIPRIS 상품 선택 또는 .env의 PATENT_API_KIPRIS_PRODUCTS에 {p.key} 추가)."
        )

    async def kr_exam_documents(
        self,
        application_number: str,
        kinds: list[str] | None = None,
        send_number: str | None = None,
        include_text: bool = True,
    ) -> dict:
        query: dict[str, Any] = {"input": application_number}

        async def body() -> dict:
            digits = normalize_kr_application_number(application_number)
            query.clear()
            query["applicationNumber"] = digits
            wanted = list(dict.fromkeys(kinds or [k for k in EXAM_KINDS if self.settings.product_enabled(k)] or list(EXAM_KINDS)))
            bad = [k for k in wanted if k not in EXAM_KINDS]
            if bad:
                raise PatentApiError(E.INVALID_INPUT, f"kinds는 {', '.join(EXAM_KINDS)} 중에서 고릅니다(입력: {', '.join(bad)}).")
            query["kinds"] = wanted
            if send_number:
                query["sendNumber"] = send_number.strip()
            notes: list[str] = []
            errors: list[PatentApiError] = []
            documents: list[dict] = []
            all_cached = True
            checked: list[str] = []
            for kind in wanted:
                if not self.settings.product_enabled(kind):
                    notes.append(self._product_off_note(kind))
                    continue
                try:
                    docs, cached, n = await self._exam_kind(kind, digits, include_text)
                except PatentApiError as e:
                    if e.code in (E.CONFIG_MISSING_KEY, E.QUOTA_EXCEEDED):
                        raise
                    errors.append(e)
                    notes.append(f"{PRODUCTS[kind].name}: {e.message}")
                    continue
                checked.append(PRODUCTS[kind].name)
                all_cached = all_cached and cached
                notes.extend(x for x in n if x not in notes)
                documents.extend(docs or [])
            if not checked:
                if len(errors) == 1:
                    raise errors[0]
                raise PatentApiError(errors[0].code if errors else E.INVALID_INPUT, " / ".join(notes))
            if send_number:
                documents = [d for d in documents if d.get("sendNumber") == send_number.strip()]
            documents.sort(key=lambda d: (d.get("sendDate") or "", d.get("sendNumber") or ""))
            if checked:
                notes.insert(0, f"확인한 서류: {', '.join(checked)}")
            if not documents:
                notes.append(NO_DATA_NOTE + ("(해당 발송번호)" if send_number else "") + " — 이 출원에 해당 서류가 없거나 아직 KIPRIS에 반영되지 않았습니다.")
                return envelope_ok(KIPRIS, query, None, cached=all_cached and bool(checked), notes=notes)
            self._cap_exam_text(documents, notes)
            data = {"applicationNumber": format_kr_application(digits), "documents": documents}
            return envelope_ok(KIPRIS, query, data, cached=all_cached, notes=notes)

        return await self._guard(KIPRIS, query, body)

    async def _exam_kind(self, kind: str, digits: str, include_text: bool) -> tuple[list[dict] | None, bool, list[str]]:
        service = EXAM_SERVICES[kind]
        ttl = self.settings.ttl_docs
        params = {"applicationNumber": digits}
        tool = "kr_exam_documents"

        def op(name, parser):
            async def call():
                return parser(await self.kipris.rest(service, name, params))
            return self._kp(kind, tool, f"{kind}:{name}", params, ttl, call)

        notes: list[str] = []
        docs, cached, n = await op("bibliographicInfo", KD.parse_doc_biblio)
        notes += n
        if not docs:
            return None, cached, notes
        by_send = {d["sendNumber"]: {"kind": PRODUCTS[kind].name, **d} for d in docs}
        extra: list[tuple[str, Any]] = []
        if kind in ("opinion", "rejection"):
            extra.append(("examineResultInfo", KD.parse_exam_result))
            if include_text:
                extra.append(("additionRejectInfo", KD.parse_addition_reject))
                extra.append(("rejectDecisionInfo", KD.parse_reject_decision))
        elif include_text:
            extra.append(("contentInfo", KD.parse_allowance_content))
        for name, parser in extra:
            part, c, n = await op(name, parser)
            cached = cached and c
            notes += n
            for send, value in (part or {}).items():
                doc = by_send.get(send)
                if doc is None:
                    continue
                if name == "examineResultInfo":
                    doc.update(value)
                elif name == "additionRejectInfo":
                    doc["reasons"] = value + doc.get("reasons", [])
                elif name == "rejectDecisionInfo":
                    if value.get("reasons"):
                        doc.setdefault("reasons", []).append(value["reasons"])
                    if kind == "rejection" and value.get("notice"):
                        doc["decision"] = value["notice"]
                    if value.get("attachment"):
                        doc["attachment"] = value["attachment"]
                elif name == "contentInfo":
                    doc["contents"] = value
        for doc in by_send.values():
            refs = KD.extract_cited_references("\n".join(doc.get("reasons", [])))
            if refs:
                doc["citedReferences"] = refs
        return list(by_send.values()), cached, notes

    def _cap_exam_text(self, documents: list[dict], notes: list[str]) -> None:
        budget = self.settings.max_text_chars
        cut_any = False
        for doc in documents:
            for field in ("reasons",):
                paras = doc.get(field)
                if not paras:
                    continue
                kept = []
                for p in paras:
                    if budget <= 0:
                        cut_any = True
                        break
                    if len(p) > budget:
                        p, _ = truncate_text(p, budget)
                        cut_any = True
                    kept.append(p)
                    budget -= len(p)
                doc[field] = kept
        if cut_any:
            notes.append(
                f"거절이유 본문이 길어 모두 합쳐 {self.settings.max_text_chars}자에서 잘랐습니다. "
                "send_number로 서류 하나만 고르면 더 볼 수 있습니다."
            )

    async def kr_deadlines(self, application_number: str | None = None, registration_number: str | None = None) -> dict:
        query: dict[str, Any] = _drop_empty({"applicationNumber": application_number, "registrationNumber": registration_number})

        async def body() -> dict:
            if bool(application_number) == bool(registration_number):
                raise PatentApiError(E.INVALID_INPUT, "application_number와 registration_number 중 하나만 넣어 주세요.")
            if application_number:
                digits = normalize_kr_application_number(application_number)
                query.clear()
                query["applicationNumber"] = digits
                op, params = "dueDateApplicationNoticeApplnoInfo", {"applicationNumber": digits}
            else:
                digits = normalize_kr_registration_number(registration_number or "")
                query.clear()
                query["registrationNumber"] = digits
                op, params = "dueDateRegistratioNoticeRgstnoInfo", {"registrationNumber": digits}

            async def call():
                return KD.parse_due_dates(await self.kipris.rest("DueDateService", op, params))

            data, cached, notes = await self._kp("deadline", "kr_deadlines", op, params, self.settings.ttl_docs, call)
            notes = notes + [DEADLINE_WARNING]
            if not data:
                notes.append(NO_DATA_NOTE + " — 마감기한이 걸린 통지서가 없거나, 공개 전 출원이라 제공되지 않을 수 있습니다.")
                return envelope_ok(KIPRIS, query, None, cached=cached, notes=notes)
            today = self.quota._now().date()
            items = []
            for d in data:
                d = dict(d)
                if d.get("dueDate"):
                    try:
                        due = datetime.strptime(d["dueDate"], "%Y-%m-%d").date()
                        d["daysLeft"] = (due - today).days
                        d["passed"] = due < today
                    except ValueError:
                        pass
                items.append(d)
            items.sort(key=lambda d: d.get("dueDate") or "")
            if any(d.get("passed") for d in items):
                notes.append("지난 기한도 그대로 나옵니다(이미 대응했거나 연장된 기한일 수 있음).")
            return envelope_ok(KIPRIS, query, {"asOf": today.isoformat(), "deadlines": items}, cached=cached, notes=notes)

        return await self._guard(KIPRIS, query, body)

    async def kr_claim_history(self, application_number: str, claim: int | None = None, include_text: bool = False) -> dict:
        query: dict[str, Any] = {"input": application_number}

        async def body() -> dict:
            digits = normalize_kr_application_number(application_number)
            query.clear()
            query.update(_drop_empty({"applicationNumber": digits, "claim": claim, "includeText": include_text or None}))
            params = {"applicationNumber": digits}
            ttl = self.settings.ttl_docs

            def op(name, parser):
                async def call():
                    return parser(await self.kipris.rest("ClaimsChangeHistoryService", name, params))
                return self._kp("claim_history", "kr_claim_history", name, params, ttl, call)

            order, c1, n1 = await op("amendmentHistoryInfo", KD.parse_claim_history_order)
            if not order:
                return envelope_ok(KIPRIS, query, None, cached=c1, notes=n1 + [NO_DATA_NOTE])
            detail, c2, n2 = await op("amendmentHistoryDetailInfo", KD.parse_claim_history_detail)
            notes = n1 + [x for x in n2 if x not in n1]
            detail = detail or []
            versions = []
            budget = self.settings.max_text_chars
            cut = False
            live: set[int] = set()  # 그 서류 뒤에 살아 있는 청구항 번호
            for v in order:
                rows = [r for r in detail if r.get("version") == v["version"]]
                changes: dict[str, list[int]] = {}
                for r in rows:
                    changes.setdefault(r.get("change") or r.get("changeCode") or "?", []).append(r["claim"])
                    if r.get("changeCode") == "D" or r.get("change") == "삭제":
                        live.discard(r["claim"])
                    else:
                        live.add(r["claim"])
                entry = dict(v)
                entry["claimCount"] = len(live)
                entry["changes"] = {k: _ranges(x) for k, x in changes.items()}
                if claim is not None or include_text:
                    texts = []
                    for r in rows:
                        if claim is not None and r["claim"] != claim:
                            continue
                        t = {"claim": r["claim"], "change": r.get("change")}
                        if r.get("text"):
                            t["text"] = r["text"]
                        if r.get("change") == "수정" and r.get("diff"):
                            t["diff"] = r["diff"]
                        size = len(t.get("text", "")) + len(t.get("diff", ""))
                        if budget - size < 0:
                            cut = True
                            break
                        budget -= size
                        texts.append(t)
                    entry["claims"] = texts
                versions.append(entry)
            notes.append(
                "changes는 그 서류에서 신규·수정·삭제된 청구항 번호, claimCount는 그 서류 뒤 남은 청구항 수입니다. "
                "diff의 [-…-]는 삭제, {+…+}는 추가된 부분입니다."
            )
            if cut:
                notes.append(f"청구항 원문이 길어 {self.settings.max_text_chars}자까지만 담았습니다. claim으로 청구항 하나만 고르세요.")
            data = {"applicationNumber": format_kr_application(digits), "versions": versions}
            return envelope_ok(KIPRIS, query, data, cached=c1 and c2, notes=notes)

        return await self._guard(KIPRIS, query, body)

    async def kr_registration(self, registration_number: str | None = None, application_number: str | None = None) -> dict:
        query: dict[str, Any] = _drop_empty({"registrationNumber": registration_number, "applicationNumber": application_number})

        async def body() -> dict:
            if bool(application_number) == bool(registration_number):
                raise PatentApiError(E.INVALID_INPUT, "registration_number와 application_number 중 하나만 넣어 주세요.")
            notes: list[str] = []
            cached_all = True
            if registration_number:
                reg = normalize_kr_registration_number(registration_number)
            else:
                app = normalize_kr_application_number(application_number or "")
                if not self.settings.product_enabled("publication"):
                    raise PatentApiError(
                        E.INVALID_INPUT,
                        "출원번호로 조회하려면 '특허·실용 공개·등록공보' 상품이 켜져 있어야 합니다(등록번호를 찾는 데 씀). 등록번호로 넣어 주세요.",
                    )
                biblio, c0, n0 = await self._fetch(
                    tool="kr_biblio", cache_params={"applicationNumber": app}, ttl=self.settings.ttl_biblio,
                    fetch=lambda: self.kipris.biblio_detail(app), product="publication",
                )
                cached_all = c0
                notes += n0
                reg_disp = (biblio or {}).get("registerNumber")
                if not reg_disp:
                    status = (biblio or {}).get("status")
                    notes.append(
                        NO_DATA_NOTE + f" — {format_kr_application(app)}은(는) 등록번호가 없습니다"
                        + (f"(현재 상태: {status})." if status else "(서지 자료 없음).")
                    )
                    query.clear()
                    query["applicationNumber"] = app
                    return envelope_ok(KIPRIS, query, None, cached=cached_all, notes=notes)
                reg = normalize_kr_registration_number(reg_disp)
                notes.append(f"출원번호 {format_kr_application(app)}의 등록번호 {reg_disp}로 조회했습니다.")
            query.clear()
            query["registrationNumber"] = reg
            params = {"registrationNumber": reg}

            async def call():
                return KD.parse_registration(await self.kipris.rest("RegistrationService", "registrationInfo", params))

            data, cached, n = await self._kp("registration", "kr_registration", "registrationInfo", params, self.settings.ttl_docs, call)
            notes += [x for x in n if x not in notes]
            if not data:
                notes.append(NO_DATA_NOTE)
                return envelope_ok(KIPRIS, query, None, cached=cached and cached_all, notes=notes)
            if data.get("paidThroughAnnual"):
                notes.append(
                    f"등록료는 {data['paidThroughAnnual']}년차분까지 납부 기록이 있습니다. 다음 연차료 납부기한은 특허로에서 확인하세요."
                )
            return envelope_ok(KIPRIS, query, data, cached=cached and cached_all, notes=notes)

        return await self._guard(KIPRIS, query, body)

    async def kr_citations(self, application_number: str, direction: str = "both") -> dict:
        query: dict[str, Any] = {"input": application_number, "direction": direction}

        async def body() -> dict:
            if direction not in ("cited", "citing", "both"):
                raise PatentApiError(E.INVALID_INPUT, "direction은 cited(이 출원이 인용한 문헌), citing(이 출원을 인용한 출원), both 중 하나입니다.")
            digits = normalize_kr_application_number(application_number)
            query.clear()
            query.update({"applicationNumber": digits, "direction": direction})
            plan = []
            if direction in ("cited", "both"):
                plan.append(("citation", "CitationService", "citationInfoV3", {"applicationNumber": digits}, KD.parse_citations, "cites"))
            if direction in ("citing", "both"):
                plan.append(("citing", "CitingService", "citingInfo", {"standardCitationApplicationNumber": digits}, KD.parse_citing, "citedBy"))
            notes: list[str] = []
            errors: list[PatentApiError] = []
            data: dict[str, Any] = {"applicationNumber": format_kr_application(digits)}
            cached_all, done = True, 0
            for product, service, op, params, parser, field in plan:
                if not self.settings.product_enabled(product):
                    notes.append(self._product_off_note(product))
                    continue

                async def call(service=service, op=op, params=params, parser=parser):
                    return parser(await self.kipris.rest(service, op, params))

                try:
                    part, cached, n = await self._kp(product, "kr_citations", op, params, self.settings.ttl_family, call)
                except PatentApiError as e:
                    if e.code in (E.CONFIG_MISSING_KEY, E.QUOTA_EXCEEDED):
                        raise
                    errors.append(e)
                    notes.append(f"{PRODUCTS[product].name}: {e.message}")
                    continue
                done += 1
                cached_all = cached_all and cached
                notes += [x for x in n if x not in notes]
                data[field] = part or []
                if not part:
                    notes.append(("인용문헌" if field == "cites" else "피인용 출원") + " 자료 없음")
            if errors and not done:
                raise errors[0] if len(errors) == 1 else PatentApiError(errors[0].code, " / ".join(notes))
            if not done:
                raise PatentApiError(E.INVALID_INPUT, " ".join(notes))
            if not data.get("cites") and not data.get("citedBy"):
                notes.append(NO_DATA_NOTE)
                return envelope_ok(KIPRIS, query, None, cached=cached_all, notes=notes)
            if data.get("citedBy"):
                notes.append("citedBy는 이 출원을 인용한 한국 출원번호입니다. 서지는 kr_biblio로 확인합니다.")
            return envelope_ok(KIPRIS, query, data, cached=cached_all, notes=notes)

        return await self._guard(KIPRIS, query, body)

    async def kr_legal_history(self, application_number: str) -> dict:
        query: dict[str, Any] = {"input": application_number}

        async def body() -> dict:
            digits = normalize_kr_application_number(application_number)
            query.clear()
            query["applicationNumber"] = digits
            params = {"applicationNumber": digits}

            async def call():
                return KD.parse_st27(await self.kipris.rest("legStatusST27InfoSearchService", "BasicInfo", params))

            data, cached, notes = await self._kp("legal_status", "kr_legal_history", "BasicInfo", params, self.settings.ttl_docs, call)
            if not data:
                return envelope_ok(KIPRIS, query, None, cached=cached, notes=notes + [NO_DATA_NOTE])
            notes.append(
                "WIPO ST.27 표준 법적 상태 이벤트입니다. category는 주요 이벤트 코드 첫 글자의 대분류이고, "
                "서류 이름이 있는 행정처리 이력은 kr_biblio(include_history=true)로 봅니다."
            )
            return envelope_ok(KIPRIS, query, data, cached=cached, notes=notes)

        return await self._guard(KIPRIS, query, body)

    async def kr_family(self, application_number: str) -> dict:
        query: dict[str, Any] = {"input": application_number}

        async def body() -> dict:
            digits = normalize_kr_application_number(application_number)
            query.clear()
            query["applicationNumber"] = digits
            params = {"applicationNumber": digits}

            async def call():
                return KD.parse_family(await self.kipris.kipi("patFamInfoSearchService", "getAppNoPatFamInfoSearch", params))

            data, cached, notes = await self._kp("family", "kr_family", "getAppNoPatFamInfoSearch", params, self.settings.ttl_family, call)
            if not data:
                return envelope_ok(KIPRIS, query, None, cached=cached, notes=notes + [NO_DATA_NOTE + " — 해외 대응 출원이 없거나 아직 반영되지 않았습니다."])
            notes.append("KIPRIS의 DOCDB 패밀리입니다(조회한 한국 출원 자신은 빠질 수 있음). 더 넓은 INPADOC 패밀리는 family 도구로 봅니다.")
            return envelope_ok(KIPRIS, query, data, cached=cached, notes=notes)

        return await self._guard(KIPRIS, query, body)

    def product_statuses(self) -> list[dict]:
        key = self.settings.kipris_service_key
        out = []
        for p in PRODUCT_LIST:
            st = self.quota.product_status(p.key, key)
            label = "확인 안 함"
            if st:
                label = "신청됨" if st["status"] == SUBSCRIBED else "미신청"
            out.append(
                _drop_empty(
                    {
                        "key": p.key,
                        "name": p.name,
                        "enabled": self.settings.product_enabled(p.key),
                        "status": label,
                        "checkedAt": st["checkedAt"] if st else None,
                        "tools": ", ".join(p.tools),
                    }
                )
            )
        return out

    async def quota_status(self, recheck_products: bool = False) -> dict:
        query: dict[str, Any] = {"recheckProducts": True} if recheck_products else {}

        async def body() -> dict:
            notes = []
            if recheck_products:
                n = self.quota.clear_product_status(NOT_SUBSCRIBED)
                notes.append(f"'미신청' 기록 {n}건을 지웠습니다. 다음 조회 때 KIPRIS에 다시 확인합니다.")
            data = self.quota.status()
            data["kiprisProducts"] = self.product_statuses()
            data["keys"] = {
                "kipris": bool(self.settings.kipris_service_key),
                "epoOps": bool(self.settings.epo_consumer_key and self.settings.epo_consumer_secret),
            }
            if data["ops"] is None:
                notes.append("OPS 사용량 헤더가 아직 기록되지 않았습니다(OPS를 한 번 호출하면 기록됨).")
            warn = self.quota.kipris_warning()
            if warn:
                notes.append(warn)
            notes.append(
                "kiprisProducts의 status: 신청됨/미신청은 실제 호출 결과로 안 것이고, '확인 안 함'은 아직 그 상품을 부르지 않은 것입니다. "
                "미신청 기록은 하루 뒤 다시 확인합니다. 월 호출 한도는 신청한 상품 전체 합산입니다."
            )
            notes.extend(self.settings.config_warnings)
            return envelope_ok("internal", query, data, notes=notes)

        return await self._guard("internal", query, body)


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _ranges(nums: list[int]) -> str:
    """[1,2,3,5,7,8] → '1-3, 5, 7-8'"""
    nums = sorted(set(nums))
    out: list[str] = []
    start = prev = None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(out)
