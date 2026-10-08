"""설정 읽기. 비밀값은 환경변수(확장 설치 시 입력값) 또는 .env에서 읽고, 어디에도 출력하지 않는다."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from . import products as P

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"

DAY = 24 * 60 * 60
APP_NAME = "patent-api-mcp"
LEGACY_CACHE_PATH = PROJECT_DIR / "data" / "cache.sqlite3"


def user_data_dir() -> Path:
    """캐시·호출 기록을 둘 사용자 폴더. 확장(.mcpb)을 업데이트해도 지워지지 않는 곳."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def remote_cache_path() -> Path:
    """원격 모드 기본 캐시 위치. 컨테이너에서 쓰기 가능한 임시 폴더(Cloud Run은 재시작 시 비워진다)."""
    return Path(tempfile.gettempdir()) / APP_NAME / "cache.sqlite3"


def default_cache_path() -> Path:
    path = user_data_dir() / "cache.sqlite3"
    # 예전 위치(프로젝트 안 data/)에 있던 캐시·호출 기록은 한 번 옮겨 온다.
    if not path.exists() and LEGACY_CACHE_PATH.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_CACHE_PATH, path)
    return path


def _clean_value(value: str | None) -> str | None:
    """빈 값, 또는 치환되지 않은 '${user_config.x}' 같은 자리표시는 '없음'으로 본다."""
    if value is None:
        return None
    value = value.strip()
    if not value or (value.startswith("${") and value.endswith("}")):
        return None
    return value


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 형식의 .env를 읽는다. 따옴표·주석·빈 줄을 허용한다."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def normalize_service_key(key: str | None) -> str | None:
    """공공데이터포털에서 복사한 키가 percent-encoded(예: %2B, %3D)이면 한 번 풀어 둔다.

    요청 시 httpx가 다시 인코딩하므로, 여기서 풀지 않으면 이중 인코딩되어 인증에 실패한다.
    """
    key = _clean_value(key)
    if not key:
        return None
    if "%" in key:
        key = unquote(key)
    return key or None


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = _clean_value(env.get(name)) or ""
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(env: dict[str, str], name: str, default: float) -> float:
    raw = _clean_value(env.get(name)) or ""
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    kipris_service_key: str | None = None
    epo_consumer_key: str | None = None
    epo_consumer_secret: str | None = None
    kipris_monthly_limit: int = 1000
    kipris_warn_at: int = 900
    kipris_base_url: str = "https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice"
    # 공개·등록공보 밖의 상품 대부분은 이 게이트웨이(인증 파라미터 accessKey)로 제공된다.
    kipris_rest_base_url: str = "https://plus.kipris.or.kr/openapi/rest"
    # 켤 KIPRIS 상품(core/products.py의 key). 기본은 '특허·실용 공개·등록공보'만.
    kipris_products: frozenset[str] = P.DEFAULT_KEYS
    config_warnings: list[str] = field(default_factory=list)
    ops_base_url: str = "https://ops.epo.org/3.2"
    cache_path: Path = field(default_factory=lambda: user_data_dir() / "cache.sqlite3")
    http_timeout: float = 20.0
    # 보존 기간(초)
    ttl_biblio: int = 30 * DAY
    ttl_text: int = 30 * DAY
    ttl_family: int = 7 * DAY
    ttl_legal: int = 1 * DAY
    ttl_search: int = 1 * DAY
    ttl_docs: int = 1 * DAY  # 심사 서류·마감기한·청구항 이력·등록사항·법적 상태(자주 바뀜)
    # 긴 응답 상한(문자 수)
    max_text_chars: int = 20000
    env_path: Path = DEFAULT_ENV_PATH

    runtime: str = "local"  # "mcpb"면 Claude 데스크톱 확장, "remote"면 원격 서버

    def key_hint(self) -> str:
        """키가 없을 때 어디에 넣으라고 안내할지."""
        if self.runtime == "mcpb":
            return "Claude 데스크톱 앱의 설정 > 확장 프로그램 > Patent API에서"
        if self.runtime == "remote":
            return "서버 관리자가 서버 환경변수(Cloud Run이면 Secret Manager 연결)에"
        return f"{self.env_path} 파일에"

    def product_enabled(self, key: str) -> bool:
        return key in self.kipris_products

    def tool_enabled(self, tool: str) -> bool:
        return P.tool_enabled(tool, self.kipris_products)

    @property
    def kipris_kipi_root(self) -> str:
        """kipo-api 게이트웨이 뿌리(…/kipo-api/kipi). 서비스명은 호출 때 붙인다."""
        return self.kipris_base_url.rstrip("/").rsplit("/", 1)[0]

    def secrets(self) -> list[str | None]:
        return [self.kipris_service_key, self.epo_consumer_key, self.epo_consumer_secret]

    @classmethod
    def load(cls, env_path: Path | None = None, *, remote: bool = False) -> "Settings":
        """remote=True(원격 HTTP 모드)면 캐시 기본 위치를 임시 폴더로, runtime 기본값을 'remote'로 둔다."""
        env_path = env_path or DEFAULT_ENV_PATH
        env = load_env_file(env_path)
        # 실제 환경변수(확장 설치 시 입력값 포함)가 .env보다 우선한다. 빈 값은 덮어쓰지 않는다.
        for k, v in os.environ.items():
            if k.startswith(("KIPRIS_", "EPO_", "CACHE_TTL_", "PATENT_API_")) and _clean_value(v) is not None:
                env[k] = v

        def days(name: str, default_days: float) -> int:
            return int(_float(env, name, default_days) * DAY)

        cache_path = _clean_value(env.get("PATENT_API_CACHE_PATH"))
        products, warnings = P.resolve_enabled(
            _clean_value(env.get("PATENT_API_KIPRIS_PRODUCTS")),
            {p.key: _clean_value(env.get(P.env_flag_name(p.key))) for p in P.PRODUCTS},
        )
        return cls(
            kipris_service_key=normalize_service_key(env.get("KIPRIS_SERVICE_KEY")),
            epo_consumer_key=_clean_value(env.get("EPO_OPS_CONSUMER_KEY")),
            epo_consumer_secret=_clean_value(env.get("EPO_OPS_CONSUMER_SECRET")),
            kipris_monthly_limit=_int(env, "KIPRIS_MONTHLY_LIMIT", 1000),
            kipris_warn_at=_int(env, "KIPRIS_WARN_AT", 900),
            kipris_base_url=(env.get("KIPRIS_BASE_URL") or cls.kipris_base_url).rstrip("/"),
            kipris_rest_base_url=(env.get("KIPRIS_REST_BASE_URL") or cls.kipris_rest_base_url).rstrip("/"),
            kipris_products=products,
            config_warnings=warnings,
            ops_base_url=(env.get("EPO_OPS_BASE_URL") or cls.ops_base_url).rstrip("/"),
            cache_path=Path(cache_path).expanduser() if cache_path else (remote_cache_path() if remote else default_cache_path()),
            http_timeout=_float(env, "PATENT_API_HTTP_TIMEOUT", 20.0),
            ttl_biblio=days("CACHE_TTL_BIBLIO_DAYS", 30),
            ttl_text=days("CACHE_TTL_TEXT_DAYS", 30),
            ttl_family=days("CACHE_TTL_FAMILY_DAYS", 7),
            ttl_legal=days("CACHE_TTL_LEGAL_DAYS", 1),
            ttl_search=days("CACHE_TTL_SEARCH_DAYS", 1),
            ttl_docs=days("CACHE_TTL_DOCS_DAYS", 1),
            max_text_chars=_int(env, "PATENT_API_MAX_TEXT_CHARS", 20000),
            env_path=env_path,
            runtime=(_clean_value(env.get("PATENT_API_RUNTIME")) or ("remote" if remote else "local")).lower(),
        )
