# 변경 기록

## 0.5.0 (2026-10-14)

- **2000년 이전 일본 번호(연호)**: `JPS60012345A`, `JP S60-012345 A`, `JPH10123456A`처럼 연호(S·H·T)가 붙은 번호를 받는다.
  OPS에는 docdb `JP.S60012345.A` / epodoc `JPS60012345`로 보낸다(둘 다 조회됨, 연호 없는 `JP.60012345.A`는 자료 없음 — 실측).
  연호 없이 적은 구번호(연도 2자리 + 6자리)가 자료 없음이면 연호 후보(H01~H12는 H 먼저, 그 밖은 S)로 한 번 더 찾는다.
  `ep_biblio`·`ep_text`·`family`·`legal_status`·`pdf_download`에 모두 적용.
- **틀린 종류코드**: 넣은 종류코드로 자료 없음이면 종류코드 없이(epodoc) 한 번 더 찾고, `notes`에 실제 종류코드(예: A2, A3, B1, C0)를 적는다.
  `pdf_download`는 자동으로 고르지 않고 후보(`error.candidates`)를 돌려준다. 한국 번호는 epodoc으로 찾지 못해 다시 묻지 않는다.
- **공개 직후 안내**: EP·WO 번호가 끝내 자료 없음이면 공개 직후(1~2주)에는 OPS에 아직 없을 수 있다고 알리고 `ep_register`를 권한다.
- **`ep_register` 새 도구**: OPS Register(`register/{application|publication}/epodoc/{번호}/biblio,events,procedural-steps`)로
  진행 상태·갱신일, 공개번호·일자, 출원인·대리인, 모출원(분할 관계), 검색보고서 종류·발송일, 인용문헌(카테고리·관련 청구항·단계),
  심사청구일 등 일자, 지정국, 최근 절차 단계·이벤트를 돌려준다. 응답 기한은 계산하지 않는다. 서류 원문(PDF)은 OPS가 제공하지 않는다.
  캐시 6시간(`CACHE_TTL_REGISTER_DAYS`). 검색보고서는 있는데 인용문헌이 비어 있으면(OPS에 늦게 반영) 그렇다고 알린다.
- `quota_status`: OPS 서비스별 혼잡도(`opsThrottle`)를 풀어 보여 준다. Register는 헤더에 따로 항목이 없다(실측).

## 0.4.0 (2026-10-13)

- `ep_search` 새 도구: EPO OPS 키워드 검색(CQL, `published-data/search/biblio`).
