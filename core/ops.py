"""EPO OPS v3.2 인증·호출·파싱.

- 인증: POST {base}/auth/accesstoken (OAuth2 client credentials). 토큰 수명 약 20분.
  만료 1분 전이거나 401을 받으면 다시 발급한다.
- 조회: {base}/rest-services/...
    published-data/{ref}/biblio | claims | description | abstract
    family/{ref}
    legal/{ref}
"""

from __future__ import annotations

import base64
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Mapping

import httpx

from . import errors as E
from .errors import PatentApiError
from .http import request_with_retry
from .kipris import normalize_date
from .numbers import OpsReference

SOURCE = "EPO OPS"
TOKEN_MARGIN_SECONDS = 60

# ---------------------------------------------------------------------------
# XML 공통
# ---------------------------------------------------------------------------


def _strip_ns(root: ET.Element) -> ET.Element:
    """네임스페이스를 떼어 내 'exchange-document' 같은 맨 이름으로 찾을 수 있게 한다."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        if el.attrib:
            el.attrib = {(k.split("}", 1)[1] if "}" in k else k): v for k, v in el.attrib.items()}
    return root


def parse_xml(text: str) -> ET.Element:
    stripped = (text or "").strip()
    if not stripped:
        raise PatentApiError(E.UPSTREAM_ERROR, "EPO OPS가 빈 응답을 돌려줬습니다.", retryable=True)
    try:
        return _strip_ns(ET.fromstring(stripped.encode("utf-8")))
    except ET.ParseError as e:
        raise PatentApiError(E.PARSE_ERROR, f"EPO OPS 응답 XML을 해석하지 못했습니다: {e}") from None


def _t(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    v = el.findtext(path)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _all_text(el: ET.Element | None, sep: str = " ") -> str | None:
    if el is None:
        return None
    t = sep.join(s.strip() for s in el.itertext() if s and s.strip())
    return t or None


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _doc_id(ref_el: ET.Element | None, id_type: str = "docdb") -> dict | None:
    """<publication-reference>/<application-reference> 안의 document-id를 dict로."""
    if ref_el is None:
        return None
    doc = None
    for d in ref_el.findall("document-id"):
        if d.get("document-id-type") == id_type:
            doc = d
            break
    if doc is None:
        doc = ref_el.find("document-id")
    if doc is None:
        return None
    country, number, kind = _t(doc, "country"), _t(doc, "doc-number"), _t(doc, "kind")
    out = {
        "number": f"{country or ''}{number or ''}{kind or ''}" or None,
        "country": country,
        "docNumber": number,
        "kind": kind,
        "date": normalize_date(_t(doc, "date")),
    }
    return _drop_none(out)


def _pick_lang(elements: list[ET.Element], prefer: str | None = "en") -> ET.Element | None:
    if not elements:
        return None
    if prefer:
        for el in elements:
            if (el.get("lang") or "").lower() == prefer.lower():
                return el
    return elements[0]


def parse_fault(text: str) -> tuple[str | None, str | None]:
    """OPS fault XML → (code, message). 해석할 수 없으면 (None, None)."""
    try:
        root = _strip_ns(ET.fromstring((text or "").strip().encode("utf-8")))
    except ET.ParseError:
        return None, None
    fault = root if root.tag == "fault" else root.find(".//fault")
    if fault is None:
        return None, None
    return _t(fault, "code"), _t(fault, "message")


# ---------------------------------------------------------------------------
# 순수 파싱 함수 (테스트 대상)
# ---------------------------------------------------------------------------


def _ipcr_text(raw: str) -> str:
    """'H04W  72/   04   A I' → 'H04W 72/04'"""
    m = re.match(r"\s*([A-H]\d{2}[A-Z])\s*(\d+)\s*/\s*(\d+)", raw)
    if m:
        return f"{m.group(1)} {m.group(2)}/{m.group(3)}"
    return " ".join(raw.split())


def _cpc(pc: ET.Element) -> str | None:
    sec, cls, sub = _t(pc, "section"), _t(pc, "class"), _t(pc, "subclass")
    mg, sg = _t(pc, "main-group"), _t(pc, "subgroup")
    if not (sec and cls and sub):
        return None
    code = f"{sec}{cls}{sub}"
    if mg:
        code += f" {mg}/{sg or '00'}"
    return code


def _names(parent: ET.Element | None, tag: str, data_format: str) -> list[str]:
    if parent is None:
        return []
    out = []
    for p in parent.findall(tag):
        if p.get("data-format") == data_format:
            n = _all_text(p.find(f"{tag}-name"))
            if n and n not in out:
                out.append(n.rstrip(","))
    return out


def parse_biblio(xml_text: str) -> dict | None:
    root = parse_xml(xml_text)
    docs = root.findall(".//exchange-document")
    if not docs:
        return None
    out = []
    for doc in docs:
        bib = doc.find("bibliographic-data")
        pub = _doc_id(bib.find("publication-reference") if bib is not None else None) or {}
        titles = bib.findall("invention-title") if bib is not None else []
        title_el = _pick_lang(titles)
        abstracts = doc.findall("abstract")
        abstract_el = _pick_lang(abstracts)
        parties = bib.find("parties") if bib is not None else None
        applicants_parent = parties.find("applicants") if parties is not None else None
        inventors_parent = parties.find("inventors") if parties is not None else None

        ipc = []
        if bib is not None:
            for c in bib.findall("classifications-ipcr/classification-ipcr"):
                txt = _t(c, "text")
                if txt:
                    code = _ipcr_text(txt)
                    if code not in ipc:
                        ipc.append(code)
        cpc = []
        if bib is not None:
            for pc in bib.findall("patent-classifications/patent-classification"):
                code = _cpc(pc)
                if code and code not in cpc:
                    cpc.append(code)

        citations, npl = [], []
        if bib is not None:
            for c in bib.findall("references-cited/citation"):
                patcit = c.find("patcit")
                if patcit is not None:
                    d = _doc_id(patcit) or {}
                    citations.append(
                        _drop_none(
                            {
                                "number": d.get("number"),
                                "date": d.get("date"),
                                "category": _all_text(c.find("category")),
                                "phase": c.get("cited-phase"),
                            }
                        )
                    )
                nplcit = c.find("nplcit")
                if nplcit is not None:
                    t = _all_text(nplcit)
                    if t:
                        npl.append(t)

        priorities = []
        if bib is not None:
            for pc in bib.findall("priority-claims/priority-claim"):
                d = _doc_id(pc, "epodoc") or _doc_id(pc) or {}
                if d.get("number") or d.get("docNumber"):
                    priorities.append(_drop_none({"number": d.get("number") or d.get("docNumber"), "date": d.get("date")}))

        app = _doc_id(bib.find("application-reference") if bib is not None else None) or {}
        item = {
            "publicationNumber": pub.get("number")
            or f"{doc.get('country', '')}{doc.get('doc-number', '')}{doc.get('kind', '')}",
            "country": doc.get("country") or pub.get("country"),
            "docNumber": doc.get("doc-number") or pub.get("docNumber"),
            "kind": doc.get("kind") or pub.get("kind"),
            "publicationDate": pub.get("date"),
            "familyId": doc.get("family-id"),
            "title": (title_el.text or "").strip() if title_el is not None and title_el.text else None,
            "titleLang": title_el.get("lang") if title_el is not None else None,
            "applicants": _names(applicants_parent, "applicant", "epodoc"),
            "applicantsOriginal": _names(applicants_parent, "applicant", "original"),
            "inventors": _names(inventors_parent, "inventor", "epodoc"),
            "inventorsOriginal": _names(inventors_parent, "inventor", "original"),
            "ipc": ipc,
            "cpc": cpc,
            "application": _drop_none({"number": app.get("number"), "date": app.get("date")}),
            "priorities": priorities,
            "abstract": _all_text(abstract_el),
            "abstractLang": abstract_el.get("lang") if abstract_el is not None else None,
            "citations": citations,
            "nonPatentCitations": npl,
        }
        if doc.get("status"):
            item["opsStatus"] = doc.get("status")
        out.append(_drop_none(item))
    return {"documents": out}


def parse_fulltext(xml_text: str, section: str, lang: str | None = None) -> dict | None:
    """claims / description 응답 → {'lang', 'availableLanguages', 'paragraphs'}."""
    root = parse_xml(xml_text)
    blocks = root.findall(f".//{section}")
    if not blocks:
        return None
    chosen = _pick_lang(blocks, lang or "en")
    if section == "claims":
        paras = [_all_text(c) for c in chosen.iter("claim-text")]
        if not any(paras):
            paras = [_all_text(c) for c in chosen.findall("claim")]
    else:
        paras = [_all_text(p) for p in chosen.iter("p")]
        if not any(paras):
            paras = [_all_text(chosen)]
    paras = [p for p in paras if p]
    if not paras:
        return None
    doc_ref = root.find(".//fulltext-document/bibliographic-data/publication-reference")
    pub = _doc_id(doc_ref) if doc_ref is not None else None
    return _drop_none(
        {
            "publicationNumber": (pub or {}).get("number"),
            "lang": chosen.get("lang"),
            "availableLanguages": [b.get("lang") for b in blocks if b.get("lang")],
            "paragraphs": paras,
        }
    )


def parse_abstract(xml_text: str, lang: str | None = None) -> dict | None:
    root = parse_xml(xml_text)
    docs = root.findall(".//exchange-document")
    abstracts = [a for d in docs for a in d.findall("abstract")]
    if not abstracts:
        return None
    chosen = _pick_lang(abstracts, lang or "en")
    paras = [_all_text(p) for p in chosen.findall("p")] or [_all_text(chosen)]
    paras = [p for p in paras if p]
    if not paras:
        return None
    doc = docs[0]
    return _drop_none(
        {
            "publicationNumber": f"{doc.get('country', '')}{doc.get('doc-number', '')}{doc.get('kind', '')}" or None,
            "lang": chosen.get("lang"),
            "availableLanguages": [a.get("lang") for a in abstracts if a.get("lang")],
            "paragraphs": paras,
        }
    )


def _member_family_id(fam: ET.Element) -> str | None:
    """실제 응답은 family-id를 patent-family가 아니라 family-member에만 붙이는 경우가 있다."""
    for m in fam.findall("family-member"):
        if m.get("family-id"):
            return m.get("family-id")
    return None


def parse_family(xml_text: str) -> dict | None:
    root = parse_xml(xml_text)
    fam = root.find(".//patent-family")
    if fam is None:
        return None
    members = []
    countries: list[str] = []
    for m in fam.findall("family-member"):
        pubs = []
        for pr in m.findall("publication-reference"):
            d = _doc_id(pr)
            if d:
                pubs.append(d)
                if d.get("country") and d["country"] not in countries:
                    countries.append(d["country"])
        app = _doc_id(m.find("application-reference")) or {}
        prios = []
        for pc in m.findall("priority-claim"):
            d = _doc_id(pc) or {}
            if d:
                active = _t(pc, "priority-active-indicator")
                prios.append(_drop_none({**{k: d.get(k) for k in ("number", "date")}, "active": active}))
        members.append(
            _drop_none(
                {
                    "publications": pubs,
                    "application": _drop_none({k: app.get(k) for k in ("number", "country", "date")}),
                    "priorities": prios,
                }
            )
        )
    if not members:
        return None
    total = fam.get("total-result-count")
    return _drop_none(
        {
            "familyId": fam.get("family-id") or _member_family_id(fam),
            "totalCount": int(total) if total and total.isdigit() else len(members),
            "countries": sorted(countries),
            "members": members,
        }
    )


def parse_legal(xml_text: str) -> dict | None:
    root = parse_xml(xml_text)
    fam = root.find(".//patent-family")
    if fam is None:
        return None
    members = []
    total_events = 0
    for m in fam.findall("family-member"):
        pubs = [d for d in (_doc_id(pr) for pr in m.findall("publication-reference")) if d]
        events = []
        for lg in m.findall("legal"):
            fields = {child.tag: (child.text or "").strip() for child in lg if child.tag.startswith("L")}
            date = normalize_date(fields.get("L007EP"))
            if not date:
                for k, v in fields.items():
                    if re.fullmatch(r"\d{8}", v or ""):
                        date = normalize_date(v)
                        break
            pre = _all_text(lg.find("pre"))
            events.append(
                _drop_none(
                    {
                        "date": date,
                        "code": (lg.get("code") or "").strip() or None,
                        "description": (lg.get("desc") or "").strip() or None,
                        "influence": (lg.get("infl") or "").strip() or None,
                        "country": fields.get("L001EP"),
                        "text": re.sub(r"\s+", " ", pre).strip() if pre else None,
                    }
                )
            )
        total_events += len(events)
        if pubs or events:
            members.append(_drop_none({"publications": pubs, "events": events}))
    if not members or total_events == 0:
        return None
    return _drop_none(
        {"familyId": fam.get("family-id") or _member_family_id(fam), "eventCount": total_events, "members": members}
    )


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------


class OpsClient:
    def __init__(
        self,
        consumer_key: str | None,
        consumer_secret: str | None,
        *,
        base_url: str,
        http: httpx.AsyncClient,
        on_headers: Callable[[Mapping[str, str]], Any] | None = None,
        clock: Callable[[], float] = time.time,
        key_hint: str = ".env 파일에",
    ):
        self._key = consumer_key
        self._secret = consumer_secret
        self._base = base_url.rstrip("/")
        self._http = http
        self._on_headers = on_headers
        self._clock = clock
        self._key_hint = key_hint
        self._token: str | None = None
        self._token_expires_at = 0.0

    def require_key(self) -> None:
        missing = [n for n, v in (("EPO_OPS_CONSUMER_KEY", self._key), ("EPO_OPS_CONSUMER_SECRET", self._secret)) if not v]
        if missing:
            raise PatentApiError(
                E.CONFIG_MISSING_KEY,
                f"{', '.join(missing)}가 설정되지 않았습니다. {self._key_hint} EPO OPS consumer key/secret을 넣고 "
                "Claude를 다시 시작하세요.",
            )

    async def _fetch_token(self) -> str:
        self.require_key()
        basic = base64.b64encode(f"{self._key}:{self._secret}".encode()).decode()
        resp = await request_with_retry(
            self._http,
            "POST",
            f"{self._base}/auth/accesstoken",
            source=SOURCE,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            content="grant_type=client_credentials",
        )
        if resp.status_code in (400, 401, 403):
            raise PatentApiError(
                E.AUTH_FAILED,
                f"EPO OPS 인증에 실패했습니다(HTTP {resp.status_code}). 넣은 Consumer Key·Secret(EPO_OPS_CONSUMER_KEY·"
                "EPO_OPS_CONSUMER_SECRET)이 My Apps의 값과 같은지, OPS 앱이 활성 상태인지 확인하세요.",
            )
        if resp.status_code >= 500:
            raise PatentApiError(
                E.UPSTREAM_UNAVAILABLE, f"EPO OPS 인증 서버 오류(HTTP {resp.status_code}). 잠시 뒤 다시 시도하세요.",
                retryable=True,
            )
        try:
            body = resp.json()
            token = body["access_token"]
            expires_in = int(body.get("expires_in", 1199))
        except Exception:
            raise PatentApiError(E.PARSE_ERROR, "EPO OPS 인증 응답을 해석하지 못했습니다.") from None
        self._token = token
        self._token_expires_at = self._clock() + max(expires_in - TOKEN_MARGIN_SECONDS, 30)
        return token

    async def _get_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        return await self._fetch_token()

    async def get(self, path: str) -> str | None:
        """rest-services 아래 경로를 조회해 XML 텍스트를 돌려준다. 404(자료 없음)면 None."""
        url = f"{self._base}/rest-services/{path.lstrip('/')}"
        for attempt in range(2):
            token = await self._get_token()
            resp = await request_with_retry(
                self._http,
                "GET",
                url,
                source=SOURCE,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
            )
            if self._on_headers is not None:
                self._on_headers(resp.headers)
            if resp.status_code == 401 and attempt == 0:
                # 토큰 만료 → 한 번 다시 발급
                self._token = None
                continue
            return self._handle(resp)
        raise PatentApiError(E.AUTH_FAILED, "EPO OPS가 새로 받은 토큰도 거부했습니다. consumer key/secret을 확인하세요.")

    def _handle(self, resp: httpx.Response) -> str | None:
        status = resp.status_code
        if status == 200:
            return resp.text
        code, message = parse_fault(resp.text)
        detail = f" ({code}: {message})" if code or message else ""
        if status == 404:
            return None
        if status == 400:
            raise PatentApiError(E.INVALID_INPUT, f"EPO OPS가 번호 형식을 받지 않았습니다{detail}. 번호와 종류코드를 확인하세요.")
        if status == 401:
            raise PatentApiError(E.AUTH_FAILED, f"EPO OPS 인증이 거부되었습니다{detail}. consumer key/secret을 확인하세요.")
        if status == 403:
            reason = resp.headers.get("x-rejection-reason") or ""
            throttle = resp.headers.get("x-throttling-control") or ""
            blob = f"{reason} {code or ''} {message or ''} {throttle}".lower()
            if "quota" in blob or "fair use" in blob or "robot" in blob or "black" in blob:
                raise PatentApiError(
                    E.QUOTA_EXCEEDED,
                    f"EPO OPS 사용 한도를 넘었습니다{detail}{' — ' + reason if reason else ''}. "
                    "재시도하지 않았습니다. quota_status로 사용량을 확인하고 한도가 풀린 뒤 다시 시도하세요.",
                )
            raise PatentApiError(E.PERMISSION_DENIED, f"EPO OPS가 접근을 거부했습니다(HTTP 403){detail}.")
        if status == 429:
            raise PatentApiError(
                E.RATE_LIMITED, f"EPO OPS 호출이 너무 잦습니다(HTTP 429){detail}. 재시도하지 않았습니다. 잠시 뒤 다시 시도하세요."
            )
        if status == 413:
            raise PatentApiError(E.UPSTREAM_BAD_REQUEST, f"EPO OPS: 요청 범위가 너무 큽니다(HTTP 413){detail}.")
        if status == 503:
            raise PatentApiError(
                E.UPSTREAM_UNAVAILABLE, f"EPO OPS가 일시적으로 응답하지 않습니다(HTTP 503, 2회 재시도함){detail}.", retryable=True
            )
        if status >= 500:
            raise PatentApiError(E.UPSTREAM_ERROR, f"EPO OPS 서버 오류(HTTP {status}){detail}.")
        raise PatentApiError(E.UPSTREAM_ERROR, f"EPO OPS 예상하지 못한 응답(HTTP {status}){detail}.")

    # --- 업무 단위 ------------------------------------------------------

    async def biblio(self, ref: OpsReference) -> dict | None:
        xml_text = await self.get(f"published-data/{ref.path()}/biblio")
        return parse_biblio(xml_text) if xml_text is not None else None

    async def fulltext(self, ref: OpsReference, section: str, lang: str | None = None) -> dict | None:
        if section == "abstract":
            xml_text = await self.get(f"published-data/{ref.path()}/abstract")
            return parse_abstract(xml_text, lang) if xml_text is not None else None
        xml_text = await self.get(f"published-data/{ref.path()}/{section}")
        return parse_fulltext(xml_text, section, lang) if xml_text is not None else None

    async def family(self, ref: OpsReference) -> dict | None:
        xml_text = await self.get(f"family/{ref.path()}")
        return parse_family(xml_text) if xml_text is not None else None

    async def legal(self, ref: OpsReference) -> dict | None:
        xml_text = await self.get(f"legal/{ref.path()}")
        return parse_legal(xml_text) if xml_text is not None else None
