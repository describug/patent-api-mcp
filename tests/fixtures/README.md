# 테스트용 응답 샘플

- `kipris_*.xml`, `ops_*.xml`: 공식 문서·실측 구조를 따라 만든 응답 샘플. 값은 가짜다.
- `real_*.xml`: 스모크 테스트 때 실제 API에서 받아 저장한 응답(공개 문헌). 키 값은 들어 있지 않다.
- `real_kipris_<상품>_*.xml`: KIPRIS 추가 상품의 실제 응답(2026-10-08, 공개 출원 10-2020-0168607 삼성전자 등).
  `real_kipris_rest_not_subscribed.xml`(openapi/rest 미신청 → resultCode 101),
  `real_kipris_kipi_not_subscribed.xml`(kipo-api 미신청 → resultCode 31), `real_kipris_rest_nodata.xml`(자료 없음: 빈 items).
- `real_kipris_fulltext_{pub,ann}.xml`: 공개전문·등록(공고)전문 PDF 주소(getPubFullTextInfoSearch·getAnnFullTextInfoSearch, 2026-10-12, 10-2020-0168607).
  `real_kipris_fulltext_nodata.xml`: 등록 전 출원(10-2025-0098404)의 등록전문 — 자료 없음은 빈 `<item/>`.
- `real_ops_images.xml`(EP1000000B1), `real_ops_images_epodoc.xml`(종류코드 없이 EP1000000 → A1·B1 두 건),
  `real_ops_images_kr.xml`(KR20220079145A), `real_ops_images_wo.xml`(WO2022119250A1, 도면 33쪽·조사보고서 41쪽): OPS images 조회(2026-10-12).
- `real_ops_biblio_kr.xml`: KR20220079145A 서지 — 출원번호 KR20200168607A(공개번호→KIPRIS 출원번호 변환에 씀).
- `real_ops_search.xml`: OPS 키워드 검색 + 서지(published-data/search/biblio, 2026-10-13) — `ta="wireless charging" and pa=samsung and pd within "2020 2022"`, 1~5번째(전체 164건).
  `real_ops_search_nodata.xml`: 결과 0건(HTTP 404 `SERVER.EntityNotFound`).
- `real_ops_biblio_epodoc.xml`: 종류코드 없이(epodoc) EP1000000 서지 → A1·B1 두 건(틀린 종류코드 다시 묻기, 2026-10-14).
- `real_ops_biblio_jp_era.xml`, `real_ops_images_jp_era.xml`: 연호가 붙은 2000년 이전 일본 번호 JPH10123456A의 서지·원본 이미지 목록(2026-10-14).
- `real_ops_register.xml`: EP Register(`register/publication/epodoc/EP1000000/biblio,events,procedural-steps`, 2026-10-14) — 상태·공개·인용문헌·절차·이벤트.
  `real_ops_register_nodata.xml`: 없는 번호(HTTP 404 `SERVER.EntityNotFound`).
