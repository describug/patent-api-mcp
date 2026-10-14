"""EPO OPS Register(EP Register의 절차·상태) 응답 파싱.

조회: register/{application|publication}/epodoc/{번호}/biblio,events,procedural-steps (2026-10 실측, 한 번에 세 가지)
  - 출원번호는 epodoc 'EP26123456'(검증숫자 없음), 공개번호는 'EP4123456'(종류코드 없음), 유럽 단계에 들어온 PCT는 'WO2022123456'
  - 번호가 없으면 HTTP 404(SERVER.EntityNotFound)
응답(reg: 네임스페이스, 아래는 접두어를 뗀 이름):
  register-document/ep-patent-statuses/ep-patent-status[@change-date, @status-code]  진행 상태(여러 개, 최신이 앞)
  register-document/bibliographic-data  공개·출원·우선권·당사자·지정국·심사청구일·분할 관계·검색보고서·인용문헌
    references-cited/citation[@cited-phase]/patcit(document-id, text "(출원인) [X] 1-15 * paragraph … *") + category
  register-document/procedural-data/procedural-step  절차 단계(코드·설명·일자 종류별)
  register-document/events-data/dossier-event        이벤트(일자·코드·설명·공보 호수)
응답 기한은 계산하지 않는다 — 들어 있는 일자만 그대로 옮긴다.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .kipris import normalize_date
from .ops import _all_text, _drop_none, _pick_lang, _t, parse_xml


def _doc_number(doc: ET.Element | None) -> str | None:
    if doc is None:
        return None
    country, number, kind = _t(doc, "country"), _t(doc, "doc-number"), _t(doc, "kind")
    if not number:
        return None
    return f"{country or ''}{number}{kind or ''}"


def _ep_application(country: str | None, number: str | None) -> str | None:
    """분할 관계의 출원번호 'EP' + '20190123456'(연도 4자리 + 일련번호 7자리) → 'EP19123456'(epodoc 8자리)."""
    if not number:
        return None
    if country == "EP" and re.fullmatch(r"(19|20)\d{2}0\d{6}", number):
        number = number[2:4] + number[5:]
    return f"{country or ''}{number}"


def _names(parent: ET.Element | None, tag: str) -> list[str]:
    if parent is None:
        return []
    out = []
    for p in parent.findall(tag):
        name = _t(p, "addressbook/name")
        if name and name not in out:
            out.append(name)
    return out


# 검색보고서 종류(search-type) → EP Register 화면의 이름
SEARCH_REPORT_LABELS = {
    "isr": "International search report",
    "national": "European search report",
    "national-supplementary": "(Supplementary) European search report",
}


# 인용 단계(cited-phase) → EP Register 화면의 'Cited in'. 실측 값은 search·examination·description(2026-10).
# description은 출원인이 명세서에 적은 문헌으로, 화면의 'by applicant'에 해당하는 것으로 본다.
CITED_PHASE_LABELS = {"search": "search", "examination": "examination", "description": "by applicant"}


def _squash(text: str | None) -> str | None:
    return " ".join(text.split()) if text else None


def _sort_key_date(item: dict) -> str:
    return item.get("date") or ""


def parse_register(xml_text: str, max_items: int = 20) -> dict | None:
    """register 응답 → 가공한 dict. 문서가 없으면 None. max_items: 절차 단계·이벤트를 최근 몇 건까지 담을지."""
    root = parse_xml(xml_text)
    doc = root.find(".//register-document")
    if doc is None:
        return None
    bib = doc.find("bibliographic-data")

    statuses = [
        _drop_none({"status": (s.text or "").strip() or None, "code": s.get("status-code"), "since": normalize_date(s.get("change-date"))})
        for s in doc.findall("ep-patent-statuses/ep-patent-status")
    ]
    statuses = [s for s in statuses if s.get("status")]
    # 최신이 앞에 오지만, 순서를 믿지 않고 날짜로 정렬한다(날짜가 빈 것은 그대로 뒤).
    statuses.sort(key=lambda s: s.get("since") or "", reverse=True)

    out: dict = {"status": statuses[0] if statuses else None, "statusHistory": statuses[1:]}
    if bib is None:
        return _drop_none(out)

    app_doc = bib.find("application-reference/document-id")
    out["application"] = _drop_none(
        {
            "number": _ep_application(_t(app_doc, "country"), _t(app_doc, "doc-number")) if app_doc is not None else None,
            "filingDate": normalize_date(_t(app_doc, "date")) if app_doc is not None else None,
            "filingLanguage": _t(bib, "language-of-filing"),
        }
    )

    pubs = []
    for ref in bib.findall("publication-reference"):
        d = ref.find("document-id")
        number = _doc_number(d)
        if number:
            pubs.append(_drop_none({"number": number, "date": normalize_date(_t(d, "date")), "lang": d.get("lang"),
                                    "gazette": ref.get("change-gazette-num")}))
    out["publications"] = pubs

    title = _pick_lang(bib.findall("invention-title"), "en")
    out["title"] = (title.text or "").strip() if title is not None and title.text else None

    parties = bib.find("parties")
    out["applicants"] = _names(parties.find("applicants") if parties is not None else None, "applicant")
    out["agents"] = _names(parties.find("agents") if parties is not None else None, "agent")
    out["inventors"] = _names(parties.find("inventors") if parties is not None else None, "inventor")

    ipc: list[str] = []
    for c in bib.findall("classifications-ipcr/classification-ipcr"):
        for code in (_t(c, "text") or "").split(","):
            code = " ".join(code.split())
            if code and code not in ipc:
                ipc.append(code)
    out["ipc"] = ipc

    out["priorities"] = [
        _drop_none({"number": f"{_t(p, 'country') or ''}{_t(p, 'doc-number') or ''}" or None, "date": normalize_date(_t(p, "date"))})
        for p in bib.findall("priority-claims/priority-claim")
    ]

    # 심사청구일, 첫 심사 통지 발송일 등: 들어 있는 항목을 이름 그대로
    dates = {}
    for el in bib.findall("dates-rights-effective/*"):
        value = normalize_date(_t(el, "date"))
        if value:
            dates[el.tag] = value
    out["dates"] = dates

    states = []
    for el in bib.findall("designation-of-states//regional/country"):
        if el.text and el.text.strip() and el.text.strip() not in states:
            states.append(el.text.strip())
    out["designatedStates"] = states

    parents = []
    for rel in bib.findall("related-documents/*"):
        for parent in rel.findall("relation/parent-doc"):
            item = {"relation": rel.tag}
            for d in parent.findall("document-id"):
                kind_of_id = d.get("document-id-type") or ""
                if "application" in kind_of_id:
                    item["application"] = _ep_application(_t(d, "country"), _t(d, "doc-number"))
                elif "publication" in kind_of_id:
                    item["publication"] = f"{_t(d, 'country') or ''}{_t(d, 'doc-number') or ''}" or None
            if len(item) > 1:
                parents.append(_drop_none(item))
    out["parentApplications"] = parents

    reports = []
    for sr in bib.findall("search-reports-information/search-report-information"):
        pub_doc = sr.find("search-report-publication/document-id")
        reports.append(
            _drop_none(
                {
                    "office": sr.get("office"),
                    "type": sr.get("search-type"),
                    "label": SEARCH_REPORT_LABELS.get(sr.get("search-type") or ""),
                    "publishedWith": _doc_number(pub_doc),
                    "publishedDate": normalize_date(_t(pub_doc, "date")) if pub_doc is not None else None,
                    "mailed": normalize_date(_t(sr, "date-search-report-mailed/date")),
                }
            )
        )
    out["searchReports"] = reports

    citations = []
    for c in bib.findall("references-cited/citation"):
        patcit = c.find("patcit")
        if patcit is not None:
            text = _squash(_all_text(patcit.find("text")))
            citations.append(
                _drop_none(
                    {
                        "number": _doc_number(patcit.find("document-id")),
                        "category": _all_text(c.find("category")),
                        "phase": c.get("cited-phase"),
                        "citedIn": CITED_PHASE_LABELS.get(c.get("cited-phase") or ""),
                        "office": c.get("office"),
                        "text": text,  # 예: "(출원인 et al.) [X] 1-15 * paragraph [0036]; figures 1,3 *"
                    }
                )
            )
            continue
        nplcit = c.find("nplcit")
        if nplcit is not None:
            citations.append(
                _drop_none({"nonPatent": _squash(_all_text(nplcit)), "category": _all_text(c.find("category")), "phase": c.get("cited-phase")})
            )
    out["citations"] = citations

    steps = []
    for st in doc.findall("procedural-data/procedural-step"):
        texts = {t.get("step-text-type") or "": (t.text or "").strip() for t in st.findall("procedural-step-text")}
        description = texts.pop("STEP_DESCRIPTION", None)
        step_dates = {d.get("step-date-type") or "date": normalize_date(_t(d, "date")) for d in st.findall("procedural-step-date")}
        step_dates = {k: v for k, v in step_dates.items() if v}
        limit = st.find("time-limit")
        steps.append(
            _drop_none(
                {
                    "code": _t(st, "procedural-step-code"),
                    "description": description,
                    "phase": st.get("procedure-step-phase"),
                    "dates": step_dates,
                    "details": {k: v for k, v in texts.items() if k and v},
                    "timeLimit": _drop_none(dict(limit.attrib)) if limit is not None and limit.attrib else None,
                    "date": max(step_dates.values()) if step_dates else None,
                }
            )
        )
    steps.sort(key=_sort_key_date, reverse=True)
    out["proceduralStepsTotal"] = len(steps)
    out["proceduralSteps"] = steps[:max_items]

    events = []
    for ev in doc.findall("events-data/dossier-event"):
        events.append(
            _drop_none(
                {
                    "date": normalize_date(_t(ev, "event-date/date")),
                    "code": _t(ev, "event-code"),
                    "description": _squash(_all_text(ev.find("event-text"))),
                    "gazette": _t(ev, "gazette-reference/gazette-num"),
                }
            )
        )
    events.sort(key=_sort_key_date, reverse=True)
    out["eventsTotal"] = len(events)
    out["events"] = events[:max_items]
    return _drop_none(out)
