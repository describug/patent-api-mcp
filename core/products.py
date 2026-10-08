"""KIPRIS Plus Open API 상품 목록.

KIPRIS Plus는 상품(데이터)마다 따로 신청한다. 사용자마다 신청한 상품이 다르므로
- 어떤 상품의 도구를 켤지는 설정(PATENT_API_KIPRIS_PRODUCTS 또는 KIPRIS_PRODUCT_<KEY>)으로 고르고,
- 실제 신청 여부는 처음 호출할 때의 응답으로 알아내 SQLite에 기억한다(quota.py).
KIPRIS에는 '내가 신청한 상품 목록'을 알려주는 API가 없다(2026-10 공식 API 목록 확인).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    key: str           # 설정·기록에 쓰는 이름
    name: str          # KIPRIS Plus '데이터 목록 > API'에 보이는 상품명(안내문에 그대로 씀)
    tools: tuple[str, ...]
    default: bool = False
    always: bool = False  # 끌 수 없음(설정 화면에도 체크칸을 두지 않는다)


PRODUCTS: tuple[Product, ...] = (
    # 기본 상품은 항상 켠다. 확장(.mcpb)을 업데이트하면 Claude 앱이 새로 생긴 체크칸에 기본값을
    # 넣지 않고 '꺼짐'으로 보여 줘서, 다른 상품만 켜고 저장하면 서지·검색 도구가 사라졌다(2026-10-08).
    # 신청하지 않았다면 첫 조회 때 신청 안내가 나가므로 켜 둬도 손해가 없다.
    Product("publication", "특허·실용 공개·등록공보", ("kr_biblio", "kr_search"), default=True, always=True),
    Product("opinion", "의견제출통지서", ("kr_exam_documents",)),
    Product("rejection", "거절결정서", ("kr_exam_documents",)),
    Product("allowance", "등록결정서", ("kr_exam_documents",)),
    Product("deadline", "특허·실용 통지서 마감기한", ("kr_deadlines",)),
    Product("claim_history", "청구항 변동 이력", ("kr_claim_history",)),
    Product("registration", "등록사항", ("kr_registration",)),
    Product("citation", "특허·실용 인용문헌", ("kr_citations",)),
    Product("citing", "특허·실용 피인용문헌", ("kr_citations",)),
    Product("legal_status", "법적 상태 이력(ST.27)", ("kr_legal_history",)),
    Product("family", "특허 패밀리", ("kr_family",)),
)

BY_KEY: dict[str, Product] = {p.key: p for p in PRODUCTS}
DEFAULT_KEYS: frozenset[str] = frozenset(p.key for p in PRODUCTS if p.default)
ALWAYS_KEYS: frozenset[str] = frozenset(p.key for p in PRODUCTS if p.always)

# 도구별로 꼭 있어야 하는 상품(이 중 하나라도 켜져 있으면 도구를 등록한다).
# kr_registration은 등록사항 상품이 있어야 하고, 공개·등록공보는 출원번호→등록번호 변환에만 쓴다.
TOOL_PRODUCTS: dict[str, tuple[str, ...]] = {
    "kr_biblio": ("publication",),
    "kr_search": ("publication",),
    "kr_exam_documents": ("opinion", "rejection", "allowance"),
    "kr_deadlines": ("deadline",),
    "kr_claim_history": ("claim_history",),
    "kr_registration": ("registration",),
    "kr_citations": ("citation", "citing"),
    "kr_legal_history": ("legal_status",),
    "kr_family": ("family",),
}

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def env_flag_name(key: str) -> str:
    return f"KIPRIS_PRODUCT_{key.upper()}"


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def resolve_enabled(product_list: str | None, flags: dict[str, str | None]) -> tuple[frozenset[str], list[str]]:
    """켤 상품 집합과 경고(알 수 없는 이름 등)를 돌려준다.

    product_list: PATENT_API_KIPRIS_PRODUCTS 값. 쉼표 목록, 'all', 'none'. 없으면 기본값.
    flags: {key: KIPRIS_PRODUCT_<KEY> 값}. 값이 있으면 목록보다 우선한다.
    """
    warnings: list[str] = []
    enabled = set(DEFAULT_KEYS)
    if product_list is not None and product_list.strip():
        enabled = set()
        for raw in product_list.replace(";", ",").split(","):
            name = raw.strip().lower()
            if not name:
                continue
            if name == "all":
                enabled |= set(BY_KEY)
            elif name == "none":
                continue
            elif name in BY_KEY:
                enabled.add(name)
            else:
                warnings.append(f"알 수 없는 KIPRIS 상품 이름 '{raw.strip()}'(쓸 수 있는 이름: {', '.join(BY_KEY)})")
    for key, value in flags.items():
        b = parse_bool(value)
        if b is True:
            enabled.add(key)
        elif b is False:
            enabled.discard(key)
    return frozenset(enabled | ALWAYS_KEYS), warnings


def tool_enabled(tool: str, enabled: frozenset[str]) -> bool:
    return any(k in enabled for k in TOOL_PRODUCTS.get(tool, ()))
