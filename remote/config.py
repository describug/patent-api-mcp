"""원격 모드 설정. 값은 환경변수(Cloud Run이면 Secret Manager 연결)에서 읽고, 비밀값은 어디에도 출력하지 않는다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from core.config import DEFAULT_ENV_PATH, _clean_value, load_env_file

# 동적 등록(DCR)·고정 클라이언트가 쓸 수 있는 리디렉션 주소의 호스트. 정확히 일치해야 한다.
# PATENT_API_OAUTH_REDIRECT_HOSTS를 넣으면 이 목록을 통째로 바꾼다.
DEFAULT_REDIRECT_HOSTS = (
    "chatgpt.com",  # ChatGPT 커넥터
    "chat.openai.com",
    "gemini.google.com",  # 개인 Gemini 앱
    "vertexaisearch.cloud.google.com",  # Gemini Enterprise
    "claude.ai",  # Claude 사용자 지정 커넥터
    "localhost",  # Claude Code·MCP Inspector 등 내 컴퓨터의 클라이언트
    "127.0.0.1",
)
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
MIN_SECRET_LENGTH = 32


class ConfigError(Exception):
    """원격 모드를 띄울 수 없는 설정. 메시지는 비개발자가 읽고 고칠 수 있게 쓴다(비밀값은 넣지 않는다)."""


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    parts = value.replace(";", ",").replace("\n", ",").replace(" ", ",").split(",")
    return tuple(p.strip().lower() for p in parts if p.strip())


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _clean_value(env.get(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}은 숫자여야 합니다.") from exc


def redirect_uri_allowed(uri: str, hosts: frozenset[str]) -> bool:
    """리디렉션 주소가 허용 호스트인지. 내 컴퓨터(loopback)가 아니면 https만 받는다."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host or parts.fragment or host not in hosts:
        return False
    if host in LOOPBACK_HOSTS:
        return parts.scheme in ("http", "https")
    return parts.scheme == "https"


@dataclass(frozen=True)
class RemoteSettings:
    public_url: str  # 예: https://patent-api-xxxx.a.run.app (끝의 / 없이)
    auth_secret: str  # 토큰 서명 키
    google_client_id: str
    google_client_secret: str
    allowed_domains: frozenset[str] = frozenset()
    allowed_emails: frozenset[str] = frozenset()
    host: str = "127.0.0.1"
    port: int = 8080
    static_client_id: str | None = None  # Gemini Enterprise처럼 OAuth 값을 손으로 넣는 클라이언트용
    redirect_hosts: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_REDIRECT_HOSTS))
    access_token_seconds: int = 60 * 60
    refresh_token_seconds: int = 30 * 24 * 60 * 60

    @property
    def resource_url(self) -> str:
        return self.public_url + "/mcp"

    def is_allowed(self, email: str | None, hosted_domain: str | None) -> bool:
        """허용 목록 확인. 도메인 허용은 그 도메인의 구글 워크스페이스 계정(hd)만 인정한다."""
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return False
        if email in self.allowed_emails:
            return True
        hd = (hosted_domain or "").strip().lower()
        return bool(hd) and hd in self.allowed_domains and email.endswith("@" + hd)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, env_path: Path | None = None) -> "RemoteSettings":
        env = load_env_file(env_path or DEFAULT_ENV_PATH)
        for k, v in (os.environ if environ is None else environ).items():
            if _clean_value(v) is not None:
                env[k] = v

        def get(name: str) -> str | None:
            return _clean_value(env.get(name))

        domains = frozenset(d.lstrip("@") for d in _split(get("PATENT_API_ALLOWED_DOMAINS")))
        emails = frozenset(_split(get("PATENT_API_ALLOWED_EMAILS")))
        if not domains and not emails:
            raise ConfigError(
                "허용 목록이 비어 있어 HTTP 모드를 시작하지 않습니다. 인증 없는 공개 서버를 실수로 여는 것을 막기 위해서입니다. "
                "PATENT_API_ALLOWED_DOMAINS(예: example.com) 또는 PATENT_API_ALLOWED_EMAILS(예: me@gmail.com)를 넣으세요."
            )

        problems: list[str] = []
        public_url = (get("PATENT_API_PUBLIC_URL") or "").rstrip("/")
        if not public_url:
            problems.append("PATENT_API_PUBLIC_URL(이 서버의 바깥 주소, 예: https://patent-api-xxxx.a.run.app)")
        else:
            parts = urlsplit(public_url)
            local = (parts.hostname or "") in LOOPBACK_HOSTS
            if parts.scheme != "https" and not (local and parts.scheme == "http"):
                problems.append("PATENT_API_PUBLIC_URL은 https:// 주소여야 합니다(내 컴퓨터 시험만 http://127.0.0.1 허용)")
            elif parts.path not in ("", "/") or parts.query or parts.fragment:
                problems.append("PATENT_API_PUBLIC_URL에는 경로 없이 주소만 넣습니다(/mcp는 붙이지 않음)")
        secret = get("PATENT_API_AUTH_SECRET")
        if not secret or len(secret) < MIN_SECRET_LENGTH:
            problems.append(f"PATENT_API_AUTH_SECRET({MIN_SECRET_LENGTH}자 이상 무작위 값, 예: openssl rand -base64 48)")
        google_id, google_secret = get("GOOGLE_OAUTH_CLIENT_ID"), get("GOOGLE_OAUTH_CLIENT_SECRET")
        if not google_id:
            problems.append("GOOGLE_OAUTH_CLIENT_ID(구글 로그인용 OAuth 클라이언트 ID)")
        if not google_secret:
            problems.append("GOOGLE_OAUTH_CLIENT_SECRET(같은 클라이언트의 보안 비밀)")
        if problems:
            raise ConfigError("HTTP 모드 설정이 빠졌거나 잘못되었습니다: " + "; ".join(problems))

        redirect_hosts = _split(get("PATENT_API_OAUTH_REDIRECT_HOSTS")) or DEFAULT_REDIRECT_HOSTS
        return cls(
            public_url=public_url,
            auth_secret=secret,  # type: ignore[arg-type]
            google_client_id=google_id,  # type: ignore[arg-type]
            google_client_secret=google_secret,  # type: ignore[arg-type]
            allowed_domains=domains,
            allowed_emails=emails,
            host=get("PATENT_API_HOST") or "127.0.0.1",
            port=_int(env, "PORT", 8080),
            static_client_id=get("PATENT_API_OAUTH_CLIENT_ID"),
            redirect_hosts=frozenset(redirect_hosts),
            access_token_seconds=_int(env, "PATENT_API_ACCESS_TOKEN_MINUTES", 60) * 60,
            refresh_token_seconds=_int(env, "PATENT_API_REFRESH_TOKEN_DAYS", 30) * 24 * 60 * 60,
        )

    def __repr__(self) -> str:  # 비밀값이 로그·오류 메시지에 섞이지 않게
        return (
            f"RemoteSettings(public_url={self.public_url!r}, host={self.host!r}, port={self.port}, "
            f"domains={sorted(self.allowed_domains)}, emails={len(self.allowed_emails)}개)"
        )
