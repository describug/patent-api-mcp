"""EPO OPS v3.2 인증·호출·파싱.

- 인증: POST {base}/auth/accesstoken (OAuth2 client credentials). 토큰 수명 약 20분.
  만료 1분 전이거나 401을 받으면 다시 발급한다.
- 조회: {base}/rest-services/...
    published-data/{ref}/biblio | claims | description | abstract
    family/{ref}
    legal/{ref}
    published-data/{ref}/images → 원본 이미지 목록(FullDocument: 쪽 수·링크)
    published-data/images/{CC}/{번호}/{종류}/fullimage?Range=N → N쪽 PDF(한 번에 한 쪽만 온다, 2026-10 실측)
    published-data/search/biblio?q={CQL} (X-OPS-Range: 1-25) → 키워드 검색 + 서지를 한 번에
    register/{application|publication}/epodoc/{번호}/biblio,events,procedural-steps → EP Register(절차·상태), core/register.py
      (OPS 3.2 reference guide v1.3.20: 한 번에 최대 100건, 앞 2000건까지만, total-result-count는 10000에서 잘림)
"""

from __future__ import annotations

import base64
import calendar
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
    return {"documents": [_biblio_document(doc) for doc in docs]}


def _biblio_document(doc: ET.Element) -> dict:
    """<exchange-document> 하나 → 서지 dict (biblio 조회와 검색 결과가 함께 쓴다)."""
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
    return _drop_none(item)


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


IMAGE_KINDS = {"FullDocument": "full", "Drawing": "drawings", "FirstPageClipping": "firstPage"}


def parse_images(xml_text: str) -> list[dict] | None:
    """images 조회 응답 → 문헌별 원본 이미지 목록. 없으면 None.

    [{'publicationNumber', 'country', 'docNumber', 'kind',
      'instances': {'full'|'drawings'|'firstPage': {'link', 'pages', 'formats', 'sections'}}}]
    종류코드 없이(epodoc) 물으면 같은 번호의 문헌(A1, B1 …)마다 inquiry-result가 하나씩 온다.
    """
    root = parse_xml(xml_text)
    out = []
    for result in root.iter("inquiry-result"):
        pub = _doc_id(result.find("publication-reference")) or {}
        instances: dict[str, dict] = {}
        for inst in result.findall("document-instance"):
            key = IMAGE_KINDS.get(inst.get("desc") or "")
            if not key or not inst.get("link"):
                continue
            pages = inst.get("number-of-pages") or ""
            sections = {
                s.get("name"): int(s.get("start-page"))
                for s in inst.findall("document-section")
                if s.get("name") and (s.get("start-page") or "").isdigit()
            }
            instances[key] = _drop_none(
                {
                    "link": inst.get("link"),
                    "pages": int(pages) if pages.isdigit() else None,
                    "formats": [f.text.strip() for f in inst.iter("document-format") if f.text and f.text.strip()],
                    "sections": dict(sorted(sections.items(), key=lambda kv: kv[1])),
                }
            )
        if instances:
            out.append(
                _drop_none(
                    {
                        "publicationNumber": pub.get("number"),
                        "country": pub.get("country"),
                        "docNumber": pub.get("docNumber"),
                        "kind": pub.get("kind"),
                        "instances": instances,
                    }
                )
            )
    return out or None


def section_range(sections: dict[str, int], name: str, total_pages: int) -> tuple[int, int] | None:
    """FullDocument의 구간(예: DRAWINGS) → (시작 쪽, 끝 쪽). 끝은 다음 구간 시작 전 쪽 또는 마지막 쪽."""
    start = sections.get(name)
    if not start or start > total_pages:
        return None
    later = [p for n, p in sections.items() if n != name and p > start]
    end = min(later) - 1 if later else total_pages
    return start, max(start, end)


def throttle_state(headers: Mapping[str, str] | None, service: str) -> tuple[str, int] | None:
    """X-Throttling-Control의 한 서비스 항목 → (색, 분당 허용 수). 예: 'busy (search=yellow:15, …)' → ('yellow', 15)."""
    value = (headers or {}).get("x-throttling-control") or (headers or {}).get("X-Throttling-Control") or ""
    m = re.search(rf"\b{re.escape(service)}=(\w+):(\d+)", value)
    return (m.group(1).lower(), int(m.group(2))) if m else None


def images_throttle(headers: Mapping[str, str] | None) -> tuple[str, int] | None:
    """X-Throttling-Control의 images 항목 → (색, 분당 허용 수). 예: 'busy (images=yellow:60, …)' → ('yellow', 60)."""
    return throttle_state(headers, "images")


# ---------------------------------------------------------------------------
# 키워드 검색 (published-data/search, CQL)
# ---------------------------------------------------------------------------

SEARCH_MAX_RANGE = 100  # 한 번에 받을 수 있는 최대 건수
SEARCH_MAX_TOTAL = 2000  # 결과 목록에서 볼 수 있는 최대 순번
_CLASS_RE = re.compile(r"[A-HY]\d{2}[A-Z](\d{1,4}(/\d{1,6})?)?")
_COUNTRY_RE = re.compile(r"[A-Z]{2}")


def _cql_term(index: str, value: str | None) -> str | None:
    """사용자 입력 → 'ti=word' 또는 'ti all "w1 w2"'. 따옴표·역슬래시·괄호는 공백으로 바꿔 CQL 구조를 못 건드리게 한다.

    여러 낱말은 모두 들어 있어야 맞는 것으로 본다(all). 순서·붙어 있음까지 따지려면 cql을 직접 쓴다.
    끝자르기 기호(*, ?, #)는 그대로 둔다.
    """
    words = re.sub(r'["\\()]', " ", value or "").split()
    if not words:
        return None
    if len(words) == 1:
        return f'{index}="{words[0]}"'
    return f'{index} all "{" ".join(words)}"'


def _cql_classes(index: str, value: str | None, label: str) -> str | None:
    """'H04W 72/04, H02J50' → '(ipc=H04W72/04 or ipc=H02J50)'. 형식이 틀리면 오류."""
    if not value or not value.strip():
        return None
    codes = []
    for part in re.split(r"[,;]", value):
        code = re.sub(r"\s+", "", part).upper()
        if not code:
            continue
        if not _CLASS_RE.fullmatch(code):
            raise PatentApiError(E.INVALID_INPUT, f"{label} 형식이 아닙니다: {part.strip()} (예: H04W 72/04, H02J, 여러 개는 쉼표로)")
        codes.append(f"{index}={code}")
    if not codes:
        return None
    return codes[0] if len(codes) == 1 else "(" + " or ".join(codes) + ")"


def _search_date(value: str | None, label: str, end: bool) -> str | None:
    """YYYY, YYYY-MM, YYYY-MM-DD → YYYYMMDD. 끝 날짜는 그 해·달의 마지막 날로 채운다."""
    if not value or not value.strip():
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    try:
        if len(digits) == 4:
            return digits + ("1231" if end else "0101")
        if len(digits) == 6:
            year, month = int(digits[:4]), int(digits[4:])
            last = calendar.monthrange(year, month)[1]
            return digits + (f"{last:02d}" if end else "01")
        if len(digits) == 8:
            year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
            if day <= calendar.monthrange(year, month)[1]:
                return digits
    except (ValueError, calendar.IllegalMonthError):
        pass
    raise PatentApiError(E.INVALID_INPUT, f"{label}은 YYYY-MM-DD(또는 YYYY, YYYY-MM) 형식이어야 합니다(입력: {value}).")


def build_cql(
    *,
    keywords: str | None = None,
    title: str | None = None,
    applicant: str | None = None,
    inventor: str | None = None,
    ipc: str | None = None,
    cpc: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    countries: list[str] | None = None,
) -> str:
    """따로 받은 검색 조건 → CQL. 빈 값은 빼고 and로 잇는다."""
    clauses = [
        _cql_term("ta", keywords),
        _cql_term("ti", title),
        _cql_term("pa", applicant),
        _cql_term("in", inventor),
        _cql_classes("ipc", ipc, "IPC"),
        _cql_classes("cpc", cpc, "CPC"),
    ]
    if not any(clauses):
        raise PatentApiError(E.INVALID_INPUT, "keywords, title, applicant, inventor, ipc, cpc 중 하나는 넣거나 cql을 직접 넣어야 합니다.")
    d_from = _search_date(date_from, "date_from", end=False)
    d_to = _search_date(date_to, "date_to", end=True)
    if d_from and d_to:
        if d_from > d_to:
            raise PatentApiError(E.INVALID_INPUT, f"date_from({date_from})이 date_to({date_to})보다 늦습니다.")
        clauses.append(f'pd within "{d_from} {d_to}"')
    elif d_from:
        clauses.append(f"pd>={d_from}")
    elif d_to:
        clauses.append(f"pd<={d_to}")
    codes = []
    for c in countries or []:
        cc = (c or "").strip().upper()
        if not cc:
            continue
        if not _COUNTRY_RE.fullmatch(cc):
            raise PatentApiError(E.INVALID_INPUT, f"countries에는 두 글자 국가 코드만 넣습니다(입력: {c}). 예: EP, US, WO, CN, JP")
        if cc not in codes:
            codes.append(cc)
    if codes:
        terms = [f"pn={cc}" for cc in codes]
        clauses.append(terms[0] if len(terms) == 1 else "(" + " or ".join(terms) + ")")
    return " and ".join(c for c in clauses if c)


def parse_search(xml_text: str) -> dict | None:
    """published-data/search(/biblio) 응답 → {'totalCount', 'range', 'items'}. 결과가 없으면 None.

    constituent=biblio면 exchange-document에서 서지를 뽑고, 아니면 publication-reference의 번호만 쓴다.
    """
    root = parse_xml(xml_text)
    search = root if root.tag == "biblio-search" else root.find(".//biblio-search")
    if search is None:
        return None
    total_raw = search.get("total-result-count") or ""
    rng = search.find("range")
    items: list[dict] = []
    docs = search.findall(".//exchange-document")
    if docs:
        for doc in docs:
            d = _biblio_document(doc)
            status = (doc.get("status") or "").strip()
            item = {
                "publicationNumber": d.get("publicationNumber"),
                "country": d.get("country"),
                "kind": d.get("kind"),
                "date": d.get("publicationDate"),
                "title": d.get("title"),
                "applicants": d.get("applicants") or d.get("applicantsOriginal"),
                "ipc": d.get("ipc"),
                "familyId": d.get("familyId"),
            }
            if status:
                item["unavailable"] = status  # 'invalid result' 등: OPS가 내용을 주지 않는 자리
            items.append(_drop_none(item))
    else:
        for pr in search.findall(".//search-result/publication-reference"):
            d = _doc_id(pr) or {}
            items.append(
                _drop_none(
                    {
                        "publicationNumber": d.get("number"),
                        "country": d.get("country"),
                        "kind": d.get("kind"),
                        "date": d.get("date"),
                        "familyId": pr.get("family-id"),
                    }
                )
            )
    if not items:
        return None
    total = int(total_raw) if total_raw.isdigit() else len(items)
    begin, end = (rng.get("begin"), rng.get("end")) if rng is not None else (None, None)
    return _drop_none(
        {
            "totalCount": total,
            "range": f"{begin}-{end}" if begin and end else None,
            "items": items,
        }
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
        self.last_headers: Mapping[str, str] | None = None  # 마지막 응답 헤더(이미지 받을 때 속도 조절에 씀)

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
        resp = await self._request(path, "application/xml")
        return resp.text if resp is not None else None

    async def get_bytes(self, path: str, accept: str, params: dict | None = None) -> bytes | None:
        """이미지처럼 바이너리 응답. 404(자료 없음)면 None."""
        resp = await self._request(path, accept, params)
        return resp.content if resp is not None else None

    async def _request(
        self,
        path: str,
        accept: str,
        params: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        bad_request: str | None = None,
    ) -> httpx.Response | None:
        """bad_request: HTTP 400일 때 쓸 안내(기본은 번호 형식 안내)."""
        url = f"{self._base}/rest-services/{path.lstrip('/')}"
        for attempt in range(2):
            token = await self._get_token()
            resp = await request_with_retry(
                self._http,
                "GET",
                url,
                source=SOURCE,
                headers={"Authorization": f"Bearer {token}", "Accept": accept, **(headers or {})},
                params=params,
            )
            # 이미지 요청도 사용량 헤더(X-Throttling-Control의 images=… 등)를 기록한다.
            self.last_headers = resp.headers
            if self._on_headers is not None:
                self._on_headers(resp.headers)
            if resp.status_code == 401 and attempt == 0:
                # 토큰 만료 → 한 번 다시 발급
                self._token = None
                continue
            return self._handle(resp, bad_request)
        raise PatentApiError(E.AUTH_FAILED, "EPO OPS가 새로 받은 토큰도 거부했습니다. consumer key/secret을 확인하세요.")

    def _handle(self, resp: httpx.Response, bad_request: str | None = None) -> httpx.Response | None:
        status = resp.status_code
        if status == 200:
            return resp
        code, message = parse_fault(resp.text)
        detail = f" ({code}: {message})" if code or message else ""
        if status == 404:
            return None
        if status == 400:
            raise PatentApiError(
                E.INVALID_INPUT, (bad_request or "EPO OPS가 번호 형식을 받지 않았습니다{detail}. 번호와 종류코드를 확인하세요.").format(detail=detail)
            )
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

    async def search(self, cql: str, begin: int, end: int) -> dict | None:
        """CQL 검색 + 서지(constituent=biblio)를 한 번에. 결과가 없으면(404) None."""
        resp = await self._request(
            "published-data/search/biblio",
            "application/xml",
            {"q": cql},
            headers={"X-OPS-Range": f"{begin}-{end}"},
            bad_request="EPO OPS가 검색식을 받지 않았습니다{detail}. CQL 문법(필드=값, and/or/not, 따옴표)을 확인하세요.",
        )
        return parse_search(resp.text) if resp is not None else None

    async def register(self, ref_type: str, epodoc: str, max_items: int) -> dict | None:
        """EP Register 서지·이벤트·절차 단계를 한 번에. epodoc: 'EP26123456'(출원) / 'EP4123456'·'WO2022123456'(공개)."""
        from .register import parse_register

        xml_text = await self.get(f"register/{ref_type}/epodoc/{epodoc}/biblio,events,procedural-steps")
        return parse_register(xml_text, max_items) if xml_text is not None else None

    async def family(self, ref: OpsReference) -> dict | None:
        xml_text = await self.get(f"family/{ref.path()}")
        return parse_family(xml_text) if xml_text is not None else None

    async def legal(self, ref: OpsReference) -> dict | None:
        xml_text = await self.get(f"legal/{ref.path()}")
        return parse_legal(xml_text) if xml_text is not None else None

    async def images(self, ref: OpsReference) -> list[dict] | None:
        xml_text = await self.get(f"published-data/{ref.path()}/images")
        return parse_images(xml_text) if xml_text is not None else None

    async def image_page(self, link: str, page: int) -> bytes:
        """images 조회가 준 link(published-data/images/…/fullimage)의 page쪽 PDF. 한 번에 한 쪽만 온다."""
        if not link.startswith("published-data/images/"):
            raise PatentApiError(E.PARSE_ERROR, "EPO OPS 이미지 링크 형식이 예상과 다릅니다.")
        data = await self.get_bytes(f"{link}.pdf", "application/pdf", {"Range": str(page)})
        if data is None:
            raise PatentApiError(E.UPSTREAM_ERROR, f"EPO OPS가 원본 {page}쪽을 주지 않았습니다(자료 없음).")
        if not data[:1024].lstrip().startswith(b"%PDF"):
            raise PatentApiError(E.PARSE_ERROR, f"EPO OPS 원본 {page}쪽이 PDF가 아닙니다.")
        return data
