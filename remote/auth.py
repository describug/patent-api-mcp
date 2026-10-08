"""얇은 OAuth 2.1 인가 서버. 사람 확인은 구글 로그인에 맡기고, 허용 목록을 통과하면 이 서버의 토큰을 준다.

왜 직접 두는가(docs/원격_배포.md 「인증 설계」): ChatGPT·개인 Gemini는 OAuth 디스커버리와 동적 클라이언트
등록(DCR)을 요구하는데 구글은 DCR을 지원하지 않는다. Gemini Enterprise는 인증·토큰 URL과 클라이언트 ID를
손으로 넣는다. 둘 다 받으려면 MCP 서버 앞에 얇은 인가 서버가 필요하다.

무상태(stateless): 클라이언트 등록, 로그인 중 상태, 인가 코드, 접근·갱신 토큰을 모두 PATENT_API_AUTH_SECRET으로
서명한 문자열로 만든다. 저장소가 없어 Cloud Run이 재시작하거나 인스턴스가 여러 개여도 연결이 끊기지 않는다.
대가: 개별 토큰을 즉시 폐기할 수 없다 — 허용 목록에서 빼면 다음 요청부터 거부되고(매 요청 다시 확인),
전체를 끊으려면 서명 키를 바꾼다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken

from .config import RemoteSettings, redirect_uri_allowed

log = logging.getLogger("patent_api.auth")

SCOPE = "patent"
GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

PENDING_SECONDS = 10 * 60  # 구글 로그인 화면에 머물 수 있는 시간
CODE_SECONDS = 5 * 60  # 인가 코드 유효 시간

# --------------------------------------------------------------------------- 서명 문자열


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """'pa1.<본문>.<HMAC-SHA256>' 형식. 본문은 읽을 수 있지만(JWT처럼) 고칠 수는 없다."""

    PREFIX = "pa1"

    def __init__(self, secret: str):
        self._key = hashlib.sha256(("patent-api-mcp|" + secret).encode()).digest()

    def _mac(self, message: str) -> str:
        return _b64e(hmac.new(self._key, message.encode("ascii"), hashlib.sha256).digest())

    def sign(self, typ: str, payload: dict[str, Any]) -> str:
        body = _b64e(json.dumps({"typ": typ, **payload}, separators=(",", ":"), ensure_ascii=False).encode())
        message = f"{self.PREFIX}.{body}"
        return f"{message}.{self._mac(message)}"

    def verify(self, token: str | None, typ: str, *, now: float | None = None) -> dict[str, Any] | None:
        """서명·종류·만료를 확인한 본문. 하나라도 틀리면 None."""
        if not token or token.count(".") != 2:
            return None
        prefix, body, mac = token.split(".")
        if prefix != self.PREFIX or not hmac.compare_digest(mac, self._mac(f"{prefix}.{body}")):
            return None
        try:
            payload = json.loads(_b64d(body))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("typ") != typ:
            return None
        exp = payload.get("exp")
        if exp is not None and float(exp) <= (time.time() if now is None else now):
            return None
        return payload

    def derive(self, label: str) -> str:
        """서명 키에서 뽑은 고정 값(동적 등록 클라이언트의 client_secret 등)."""
        return _b64e(hmac.new(self._key, ("derive|" + label).encode(), hashlib.sha256).digest())


# --------------------------------------------------------------------------- 클라이언트


class PatentClient(OAuthClientInformationFull):
    """범위(scope)는 하나뿐이라, 클라이언트가 무엇을 요청하든 'patent'만 준다(클라이언트마다 달라 막히지 않게)."""

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        return [SCOPE]


class StaticClient(PatentClient):
    """관리자가 OAuth 값을 손으로 넣는 클라이언트(Gemini Enterprise). 비밀 없이 PKCE로 보호하는 공개 클라이언트다.

    리디렉션 주소를 미리 알 수 없어, 허용 호스트(PATENT_API_OAUTH_REDIRECT_HOSTS)에 속하면 받는다.
    """

    allowed_redirect_hosts: frozenset[str] = frozenset()

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri가 필요합니다")
        if not redirect_uri_allowed(str(redirect_uri), self.allowed_redirect_hosts):
            log.warning("고정 클라이언트 리디렉션 거부: 허용되지 않은 호스트 %s", redirect_uri.host)
            raise InvalidRedirectUriError("허용되지 않은 리디렉션 주소입니다(PATENT_API_OAUTH_REDIRECT_HOSTS 확인)")
        return redirect_uri


class PatentAuthCode(AuthorizationCode):
    hd: str | None = None
    jti: str = ""


class PatentRefreshToken(RefreshToken):
    hd: str | None = None


# --------------------------------------------------------------------------- 구글 로그인


class GoogleSignIn:
    """구글 OAuth(OpenID Connect)로 이메일을 확인한다. 토큰은 구글 토큰 엔드포인트에서 TLS로 직접 받으므로
    ID 토큰 서명 대신 발급자·대상·만료를 확인한다(OpenID Connect Core 3.1.3.7)."""

    def __init__(self, client_id: str, client_secret: str, *, http: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self._client_secret = client_secret
        self._http = http

    def authorization_url(self, *, state: str, redirect_uri: str, hosted_domain: str | None = None) -> str:
        query = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "prompt": "select_account",
        }
        if hosted_domain:
            query["hd"] = hosted_domain  # 로그인 화면에서 회사 계정을 먼저 보여 준다(확인은 콜백에서 따로 한다)
        return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"

    async def exchange(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """구글 인가 코드 → 확인된 ID 토큰 내용. 실패하면 ValueError(메시지에 비밀값 없음)."""
        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if self._http is not None:
            resp = await self._http.post(GOOGLE_TOKEN_URL, data=data)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(GOOGLE_TOKEN_URL, data=data)
        if resp.status_code != 200:
            raise ValueError(f"구글 토큰 교환 실패(HTTP {resp.status_code})")
        id_token = (resp.json() or {}).get("id_token")
        if not isinstance(id_token, str) or id_token.count(".") != 2:
            raise ValueError("구글 응답에 ID 토큰이 없습니다")
        try:
            claims = json.loads(_b64d(id_token.split(".")[1]))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("구글 ID 토큰을 읽지 못했습니다") from exc
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise ValueError("ID 토큰 발급자가 구글이 아닙니다")
        aud = claims.get("aud")
        if aud != self.client_id and not (isinstance(aud, list) and self.client_id in aud):
            raise ValueError("ID 토큰 대상(aud)이 이 서버가 아닙니다")
        if float(claims.get("exp") or 0) <= time.time():
            raise ValueError("ID 토큰이 만료되었습니다")
        if claims.get("email_verified") not in (True, "true"):
            raise ValueError("구글 계정 이메일이 확인되지 않았습니다")
        return claims


# --------------------------------------------------------------------------- 인가 서버


class PatentOAuthProvider:
    """MCP SDK의 OAuthAuthorizationServerProvider 구현 + 구글 콜백 처리."""

    def __init__(self, settings: RemoteSettings, *, google: GoogleSignIn | None = None):
        self.settings = settings
        self.signer = Signer(settings.auth_secret)
        self.google = google or GoogleSignIn(settings.google_client_id, settings.google_client_secret)
        self.google_redirect_uri = settings.public_url + GOOGLE_CALLBACK_PATH
        self._used_codes: dict[str, float] = {}  # 인가 코드 1회용 확인(인스턴스별, 최대 5분 보관)

    # ---- 클라이언트 등록·조회

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if not redirect_uri_allowed(u, self.settings.redirect_hosts)]
        if not uris or bad:
            hosts = sorted({AnyUrl(u).host or "?" for u in bad})
            log.warning("동적 등록 거부: 허용되지 않은 리디렉션 호스트 %s", hosts)
            raise RegistrationError(
                "invalid_redirect_uri",
                f"허용되지 않은 리디렉션 주소입니다({', '.join(hosts)}). 서버 관리자가 PATENT_API_OAUTH_REDIRECT_HOSTS에 추가해야 합니다.",
            )
        method = client_info.token_endpoint_auth_method or "client_secret_post"
        client_id = self.signer.sign(
            "client",
            {
                "ru": uris,
                "am": method,
                "gt": list(client_info.grant_types),
                "n": (client_info.client_name or "")[:80],
            },
        )
        # SDK 등록 처리기는 이 객체를 그대로 응답에 쓴다 → 서명한 client_id와 거기서 뽑은 비밀로 바꿔 둔다.
        client_info.client_id = client_id
        client_info.client_secret = self.signer.derive("client-secret|" + client_id) if method != "none" else None
        client_info.scope = SCOPE
        log.info("동적 등록: %s", client_info.client_name or "(이름 없음)")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        static_id = self.settings.static_client_id
        if static_id and hmac.compare_digest(client_id.encode(), static_id.encode()):
            return StaticClient(
                client_id=static_id,
                client_name="manual",
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=SCOPE,
                allowed_redirect_hosts=self.settings.redirect_hosts,
            )
        data = self.signer.verify(client_id, "client")
        if data is None:
            return None
        method = data.get("am") or "client_secret_post"
        return PatentClient(
            client_id=client_id,
            client_secret=self.signer.derive("client-secret|" + client_id) if method != "none" else None,
            client_secret_expires_at=0 if method != "none" else None,
            redirect_uris=data.get("ru") or [],
            token_endpoint_auth_method=method,
            grant_types=data.get("gt") or ["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPE,
            client_name=data.get("n") or None,
        )

    # ---- /authorize → 구글 로그인 → 콜백

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.signer.sign(
            "pending",
            {
                "cid": client.client_id,
                "ru": str(params.redirect_uri),
                "rx": params.redirect_uri_provided_explicitly,
                "cc": params.code_challenge,
                "st": params.state,
                "res": params.resource,
                "exp": int(time.time()) + PENDING_SECONDS,
            },
        )
        domains = self.settings.allowed_domains
        hint = next(iter(domains)) if len(domains) == 1 and not self.settings.allowed_emails else None
        return self.google.authorization_url(state=state, redirect_uri=self.google_redirect_uri, hosted_domain=hint)

    async def google_callback(self, request: Request) -> Response:
        pending = self.signer.verify(request.query_params.get("state"), "pending")
        if pending is None:
            return _page(400, "로그인 요청이 만료되었거나 올바르지 않습니다. 쓰던 앱에서 연결을 다시 시도하세요.")

        def back(**params: str | None) -> Response:
            url = construct_redirect_uri(pending["ru"], state=pending.get("st"), **params)
            return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

        if request.query_params.get("error") or not request.query_params.get("code"):
            return back(error="access_denied", error_description="구글 로그인이 취소되었습니다")
        try:
            claims = await self.google.exchange(request.query_params["code"], self.google_redirect_uri)
        except (ValueError, httpx.HTTPError) as exc:
            log.warning("구글 로그인 확인 실패: %s", exc if isinstance(exc, ValueError) else type(exc).__name__)
            return back(error="server_error", error_description="구글 로그인 확인에 실패했습니다")

        email = str(claims.get("email") or "").lower()
        hd = str(claims.get("hd") or "").lower() or None
        if not self.settings.is_allowed(email, hd):
            log.warning("허용 목록에 없는 계정 거부(도메인 %s)", email.rpartition("@")[2] or "?")
            return back(error="access_denied", error_description="이 서버를 쓰도록 허용된 계정이 아닙니다")

        code = self.signer.sign(
            "code",
            {
                "cid": pending["cid"],
                "ru": pending["ru"],
                "rx": pending["rx"],
                "cc": pending["cc"],
                "res": pending.get("res"),
                "sub": email,
                "hd": hd,
                "jti": secrets.token_urlsafe(16),
                "exp": int(time.time()) + CODE_SECONDS,
            },
        )
        log.info("로그인 허용(도메인 %s)", email.rpartition("@")[2])
        return back(code=code)

    # ---- /token

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> PatentAuthCode | None:
        data = self.signer.verify(authorization_code, "code")
        if data is None or data.get("cid") != client.client_id or data.get("jti") in self._used_codes:
            return None  # 위조·만료·다른 클라이언트·이미 사용
        return PatentAuthCode(
            code=authorization_code,
            scopes=[SCOPE],
            expires_at=float(data["exp"]),
            client_id=data["cid"],
            code_challenge=data["cc"],
            redirect_uri=AnyUrl(data["ru"]),
            redirect_uri_provided_explicitly=bool(data["rx"]),
            resource=data.get("res"),
            subject=data["sub"],
            hd=data.get("hd"),
            jti=data["jti"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: PatentAuthCode
    ) -> OAuthToken:
        now = time.time()
        self._used_codes = {k: v for k, v in self._used_codes.items() if v > now}
        if authorization_code.jti in self._used_codes:
            raise TokenError("invalid_grant", "이미 사용한 인가 코드입니다")
        self._used_codes[authorization_code.jti] = now + CODE_SECONDS
        return self._issue(client.client_id, authorization_code.subject, authorization_code.hd)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> PatentRefreshToken | None:
        data = self.signer.verify(refresh_token, "refresh")
        if data is None or data.get("cid") != client.client_id:
            return None
        return PatentRefreshToken(
            token=refresh_token,
            client_id=data["cid"],
            scopes=[SCOPE],
            expires_at=int(data["exp"]),
            subject=data["sub"],
            hd=data.get("hd"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: PatentRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return self._issue(client.client_id, refresh_token.subject, refresh_token.hd)

    def _issue(self, client_id: str, email: str | None, hd: str | None) -> OAuthToken:
        if not self.settings.is_allowed(email, hd):
            raise TokenError("invalid_grant", "이 서버를 쓰도록 허용된 계정이 아닙니다")
        now = int(time.time())
        common = {"cid": client_id, "sub": email, "hd": hd, "iat": now}
        access = self.signer.sign(
            "access", {**common, "aud": self.settings.resource_url, "exp": now + self.settings.access_token_seconds}
        )
        refresh = self.signer.sign(
            "refresh", {**common, "jti": secrets.token_urlsafe(8), "exp": now + self.settings.refresh_token_seconds}
        )
        return OAuthToken(
            access_token=access,
            expires_in=self.settings.access_token_seconds,
            scope=SCOPE,
            refresh_token=refresh,
        )

    # ---- /mcp 요청마다

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.signer.verify(token, "access")
        if data is None or data.get("aud") != self.settings.resource_url:
            return None
        if not self.settings.is_allowed(data.get("sub"), data.get("hd")):
            return None
        return AccessToken(
            token=token,
            client_id=data["cid"],
            scopes=[SCOPE],
            expires_at=int(data["exp"]),
            resource=self.settings.resource_url,
            subject=data["sub"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None  # 폐기 엔드포인트는 열지 않는다(무상태). 허용 목록에서 빼거나 서명 키를 바꾼다.


def _page(status: int, message: str) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset='utf-8'><title>patent-api</title>"
        f"<p style='font:16px sans-serif;margin:2em'>{message}</p>"
    )
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})
