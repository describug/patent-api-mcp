# 테스트용 응답 샘플

- `kipris_*.xml`, `ops_*.xml`: 공식 문서·실측 구조를 따라 만든 응답 샘플. 값은 가짜다.
- `real_*.xml`: 스모크 테스트 때 실제 API에서 받아 저장한 응답(공개 문헌). 키 값은 들어 있지 않다.
- `real_kipris_<상품>_*.xml`: KIPRIS 추가 상품의 실제 응답(2026-10-08, 공개 출원 10-2020-0168607 삼성전자 등).
  `real_kipris_rest_not_subscribed.xml`(openapi/rest 미신청 → resultCode 101),
  `real_kipris_kipi_not_subscribed.xml`(kipo-api 미신청 → resultCode 31), `real_kipris_rest_nodata.xml`(자료 없음: 빈 items).
