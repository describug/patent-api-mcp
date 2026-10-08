"""원격(HTTP) 모드: 설정 검증, OAuth 디스커버리·동적 등록·로그인·토큰, 인증 거부/허용, 도구 목록 (네트워크 없이)."""

import asyncio
import base64
import hashlib
import importlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from core.config import Settings
from remote.app import create_http_app
from remote.auth import GoogleSignIn, PatentOAuthProvider, Signer
from remote.config import ConfigError, RemoteSettings, redirect_uri_allowed

BASE = "http://127.0.0.1:8080"
AUTH_SECRET = "test-signing-secret-0123456789-abcdefghijklmnop"
GOOGLE_ID = "google-client.apps.googleusercontent.com"
GOOGLE_SECRET = "google-secret-s3cr3tXQ"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
GEMINI_ENT_REDIRECT = "https://vertexaisearch.cloud.google.com/oauth-redirect"
NO_ENV_FILE = Path(__file__).parent / "fixtures" / "no-such.env"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

VALID_ENV = {
    "PATENT_API_PUBLIC_URL": BASE,
    "PATENT_API_AUTH_SECRET": AUTH_SECRET,
    "GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID,
    "GOOGLE_OAUTH_CLIENT_SECRET": GOOGLE_SECRET,
    "PATENT_API_ALLOWED_DOMAINS": "example.com",
    "PATENT_API_ALLOWED_EMAILS": "friend@gmail.com",
}


@pytest.fixture(scope="module")
def server_module(tmp_path_factory):
    """server.py를 불러온다(캐시는 임시 폴더로, 전송 방식은 기본 stdio)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATENT_API_CACHE_PATH", str(tmp_path_factory.mktemp("cache") / "cache.sqlite3"))
        mp.delenv("PATENT_API_TRANSPORT", raising=False)
        return importlib.import_module("server")


def settings(**overrides) -> RemoteSettings:
    env = {**VALID_ENV, **overrides}
    return RemoteSettings.from_env(env, env_path=NO_ENV_FILE)


def fake_id_token(**claims) -> str:
    body = {
        "iss": "https://accounts.google.com",
        "aud": GOOGLE_ID,
        "exp": int(time.time()) + 300,
        "email_verified": True,
        **claims,
    }
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(body)}.sig"


class FakeGoogle:
    """구글 토큰 엔드포인트 흉내. 다음 로그인에 쓸 계정을 정해 둔다."""

    def __init__(self):
        self.claims = {"email": "kim@example.com", "hd": "example.com"}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"id_token": fake_id_token(**self.claims), "access_token": "g"})

    def sign_in(self) -> GoogleSignIn:
        return GoogleSignIn(GOOGLE_ID, GOOGLE_SECRET, http=httpx.AsyncClient(transport=httpx.MockTransport(self)))


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def client(server_module, google):
    app = create_http_app(server_module.mcp, settings(PATENT_API_OAUTH_CLIENT_ID="gemini-enterprise"), google=google.sign_in())
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(c: TestClient, redirect=CHATGPT_REDIRECT, **extra) -> dict:
    body = {"redirect_uris": [redirect], "client_name": "ChatGPT", **extra}
    return c.post("/register", json=body).json()


def sign_in(c: TestClient, client_id: str, redirect: str) -> tuple[dict, str]:
    """/authorize → 구글 → 콜백. 클라이언트 redirect로 돌아온 쿼리와 PKCE verifier를 돌려준다."""
    verifier, challenge = pkce()
    r = c.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-123",
        },
    )
    assert r.status_code == 302, r.text
    google_url = urlsplit(r.headers["location"])
    assert google_url.netloc == "accounts.google.com"
    q = parse_qs(google_url.query)
    assert q["client_id"] == [GOOGLE_ID]
    assert q["redirect_uri"] == [BASE + "/oauth/google/callback"]
    r = c.get("/oauth/google/callback", params={"state": q["state"][0], "code": "google-code"})
    assert r.status_code == 302
    back = urlsplit(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == redirect
    return {k: v[0] for k, v in parse_qs(back.query).items()}, verifier


def token(c: TestClient, reg: dict, code: str, verifier: str, redirect: str) -> httpx.Response:
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect, "client_id": reg["client_id"]}
    if reg.get("client_secret"):
        data["client_secret"] = reg["client_secret"]
    return c.post("/token", data=data)


def mcp_call(c: TestClient, method: str, params: dict | None = None, access: str | None = None) -> httpx.Response:
    headers = dict(ACCEPT)
    if access:
        headers["Authorization"] = f"Bearer {access}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return c.post("/mcp", json=body, headers=headers)


def initialize(c: TestClient, access: str | None) -> httpx.Response:
    params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
    return mcp_call(c, "initialize", params, access)


def login_access_token(c: TestClient) -> str:
    reg = register(c)
    q, verifier = sign_in(c, reg["client_id"], CHATGPT_REDIRECT)
    return token(c, reg, q["code"], verifier, CHATGPT_REDIRECT).json()["access_token"]


# --------------------------------------------------------------------------- 전송 방식·설정


def test_select_transport(server_module):
    pick = server_module.select_transport
    assert pick([], {}) == "stdio"
    assert pick([], {"PATENT_API_TRANSPORT": "http"}) == "http"
    assert pick([], {"PATENT_API_TRANSPORT": "streamable-http"}) == "http"
    assert pick(["--transport", "http"], {}) == "http"
    assert pick(["--transport=stdio"], {"PATENT_API_TRANSPORT": "http"}) == "stdio"
    assert pick(["--http"], {}) == "http"
    with pytest.raises(SystemExit):
        pick(["--transport", "sse"], {})


def test_stdio_server_has_no_auth(server_module):
    assert server_module.TRANSPORT == "stdio"
    assert server_module.mcp.settings.auth is None


@pytest.mark.parametrize("blank", ["", "  ", ",", "${user_config.x}"])
def test_empty_allowlist_refuses_to_start(blank):
    with pytest.raises(ConfigError, match="허용 목록이 비어"):
        settings(PATENT_API_ALLOWED_DOMAINS=blank, PATENT_API_ALLOWED_EMAILS=blank)


def test_run_http_exits_when_allowlist_empty(server_module, monkeypatch, capsys):
    from remote import app as remote_app
    from remote import config as remote_config

    monkeypatch.setattr(remote_config, "DEFAULT_ENV_PATH", NO_ENV_FILE)
    for k, v in VALID_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATENT_API_ALLOWED_DOMAINS", "")
    monkeypatch.delenv("PATENT_API_ALLOWED_EMAILS")
    with pytest.raises(SystemExit) as exc:
        remote_app.run_http(server_module.mcp)
    assert exc.value.code == 2
    assert "허용 목록이 비어" in capsys.readouterr().err


def test_missing_settings_listed_without_secrets():
    with pytest.raises(ConfigError) as exc:
        settings(PATENT_API_PUBLIC_URL="", PATENT_API_AUTH_SECRET="short", GOOGLE_OAUTH_CLIENT_SECRET="")
    msg = str(exc.value)
    assert "PATENT_API_PUBLIC_URL" in msg and "PATENT_API_AUTH_SECRET" in msg and "GOOGLE_OAUTH_CLIENT_SECRET" in msg
    assert "short" not in msg


def test_public_url_must_be_https_unless_local():
    with pytest.raises(ConfigError, match="https"):
        settings(PATENT_API_PUBLIC_URL="http://patent.example.com")
    with pytest.raises(ConfigError, match="경로"):
        settings(PATENT_API_PUBLIC_URL="https://patent.example.com/mcp")
    s = settings(PATENT_API_PUBLIC_URL="https://patent.example.com/")
    assert s.public_url == "https://patent.example.com"
    assert s.resource_url == "https://patent.example.com/mcp"


def test_settings_repr_hides_secrets():
    s = settings()
    assert AUTH_SECRET not in repr(s) and GOOGLE_SECRET not in repr(s)


def test_allowlist_rules():
    s = settings(PATENT_API_ALLOWED_DOMAINS="Example.com, @corp.co.kr", PATENT_API_ALLOWED_EMAILS="Friend@Gmail.com")
    assert s.is_allowed("kim@example.com", "example.com")
    assert s.is_allowed("lee@corp.co.kr", "corp.co.kr")
    assert s.is_allowed("friend@gmail.com", None)
    # 도메인 허용은 워크스페이스 계정(hd)만 — 같은 주소로 만든 개인 구글 계정은 거부
    assert not s.is_allowed("kim@example.com", None)
    assert not s.is_allowed("kim@example.com", "other.com")
    assert not s.is_allowed("other@gmail.com", None)
    assert not s.is_allowed("", "example.com")


def test_redirect_uri_rules():
    hosts = frozenset({"chatgpt.com", "localhost"})
    assert redirect_uri_allowed(CHATGPT_REDIRECT, hosts)
    assert redirect_uri_allowed("http://localhost:33418/callback", hosts)
    assert not redirect_uri_allowed("http://chatgpt.com/cb", hosts)  # https만
    assert not redirect_uri_allowed("https://evil.example/cb", hosts)
    assert not redirect_uri_allowed("https://chatgpt.com.evil.example/cb", hosts)


def test_remote_cache_path_is_temp(monkeypatch, tmp_path):
    monkeypatch.delenv("PATENT_API_CACHE_PATH", raising=False)
    monkeypatch.delenv("PATENT_API_RUNTIME", raising=False)
    s = Settings.load(tmp_path / "none.env", remote=True)
    assert "patent-api-mcp" in str(s.cache_path) and s.runtime == "remote"
    assert "서버" in s.key_hint()


# --------------------------------------------------------------------------- 디스커버리


def test_protected_resource_metadata(client):
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        meta = client.get(path).json()
        assert meta["resource"] == BASE + "/mcp"
        assert meta["authorization_servers"] == [BASE]


def test_authorization_server_metadata(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == BASE
    assert meta["authorization_endpoint"] == BASE + "/authorize"
    assert meta["token_endpoint"] == BASE + "/token"
    assert meta["registration_endpoint"] == BASE + "/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"ok": True}


# --------------------------------------------------------------------------- 인증 거부


def test_mcp_without_token_is_401(client):
    r = initialize(client, None)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]
    assert BASE + "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


@pytest.mark.parametrize("bad", ["garbage", "pa1.e30.xxxx", "Bearer"])
def test_mcp_with_bad_token_is_401(client, bad):
    assert initialize(client, bad).status_code == 401


def test_tampered_or_foreign_tokens_rejected(client):
    access = login_access_token(client)
    prefix, body, mac = access.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["sub"] = "boss@example.com"
    forged_body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert initialize(client, f"{prefix}.{forged_body}.{mac}").status_code == 401
    # 다른 키로 서명한 토큰, 다른 서버(aud)용 토큰, 만료 토큰
    other = Signer("another-secret-0123456789-0123456789-xyz")
    fields = {"cid": "x", "sub": "kim@example.com", "hd": "example.com", "aud": BASE + "/mcp", "exp": int(time.time()) + 60}
    assert initialize(client, other.sign("access", fields)).status_code == 401
    mine = Signer(AUTH_SECRET)
    assert initialize(client, mine.sign("access", {**fields, "aud": "https://other.example/mcp"})).status_code == 401
    assert initialize(client, mine.sign("access", {**fields, "exp": int(time.time()) - 1})).status_code == 401
    # 갱신 토큰·인가 코드는 접근 토큰으로 못 쓴다
    assert initialize(client, mine.sign("refresh", fields)).status_code == 401
    assert initialize(client, mine.sign("access", fields)).status_code == 200


# --------------------------------------------------------------------------- 동적 등록 + 구글 로그인 (ChatGPT·개인 Gemini 흐름)


def test_full_dcr_flow_lists_tools(client, google, server_module):
    reg = register(client)
    assert reg["client_id"].startswith("pa1.") and reg["client_secret"]
    q, verifier = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    assert q["state"] == "st-123" and "code" in q

    # 구글 토큰 교환은 서버가 비밀과 함께 직접 한다
    sent = parse_qs(google.requests[-1].content.decode())
    assert sent["client_secret"] == [GOOGLE_SECRET] and sent["code"] == ["google-code"]

    r = token(client, reg, q["code"], verifier, CHATGPT_REDIRECT)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["refresh_token"] and tok["expires_in"] == 3600

    r = initialize(client, tok["access_token"])
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "patent-api"

    r = mcp_call(client, "tools/list", access=tok["access_token"])
    assert r.status_code == 200, r.text
    names = [t["name"] for t in r.json()["result"]["tools"]]
    # 로컬(stdio) 서버와 같은 도구가 그대로 옮겨진다
    local = [t.name for t in asyncio.run(server_module.mcp.list_tools())]
    assert names == local and {"kr_biblio", "ep_biblio", "family", "quota_status"} <= set(names)


def test_code_is_single_use_and_needs_pkce(client):
    reg = register(client)
    q, verifier = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    assert token(client, reg, q["code"], "wrong-verifier-" + "x" * 40, CHATGPT_REDIRECT).status_code == 400
    assert token(client, reg, q["code"], verifier, CHATGPT_REDIRECT).status_code == 200
    assert token(client, reg, q["code"], verifier, CHATGPT_REDIRECT).status_code == 400


def test_dcr_client_secret_required(client):
    reg = register(client)
    q, verifier = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    r = token(client, {**reg, "client_secret": "wrong"}, q["code"], verifier, CHATGPT_REDIRECT)
    assert r.status_code == 401


def test_public_dcr_client(client):
    reg = register(client, redirect="http://localhost:33418/callback", token_endpoint_auth_method="none")
    assert not reg.get("client_secret")
    q, verifier = sign_in(client, reg["client_id"], "http://localhost:33418/callback")
    assert token(client, reg, q["code"], verifier, "http://localhost:33418/callback").status_code == 200


def test_dcr_rejects_unknown_redirect_host(client):
    r = client.post("/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_forged_client_id_rejected(client):
    verifier, challenge = pkce()
    forged = Signer("another-secret-0123456789-0123456789-xyz").sign("client", {"ru": ["https://evil.example/cb"], "am": "none"})
    r = client.get(
        "/authorize",
        params={"client_id": forged, "redirect_uri": "https://evil.example/cb", "response_type": "code", "code_challenge": challenge},
    )
    assert r.status_code == 400  # 리디렉션하지 않고 오류


def test_disallowed_account_gets_access_denied(client, google):
    google.claims = {"email": "stranger@gmail.com"}
    reg = register(client)
    q, _ = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    assert q["error"] == "access_denied" and "code" not in q and q["state"] == "st-123"


def test_personal_account_with_company_address_denied(client, google):
    google.claims = {"email": "kim@example.com"}  # hd 없음 = 워크스페이스 계정이 아님
    reg = register(client)
    q, _ = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    assert q["error"] == "access_denied"


def test_listed_personal_email_allowed(client, google):
    google.claims = {"email": "Friend@gmail.com"}
    reg = register(client)
    q, _ = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    assert "code" in q


def test_google_cancel_and_bad_state(client):
    reg = register(client)
    verifier, challenge = pkce()
    r = client.get(
        "/authorize",
        params={"client_id": reg["client_id"], "redirect_uri": CHATGPT_REDIRECT, "response_type": "code", "code_challenge": challenge, "state": "s"},
    )
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = client.get("/oauth/google/callback", params={"state": state, "error": "access_denied"})
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["access_denied"]
    assert client.get("/oauth/google/callback", params={"state": "forged", "code": "c"}).status_code == 400


def test_refresh_token_flow(client):
    reg = register(client)
    q, verifier = sign_in(client, reg["client_id"], CHATGPT_REDIRECT)
    tok = token(client, reg, q["code"], verifier, CHATGPT_REDIRECT).json()
    r = client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg["client_id"], "client_secret": reg["client_secret"]},
    )
    assert r.status_code == 200, r.text
    assert initialize(client, r.json()["access_token"]).status_code == 200


def test_tokens_stop_working_when_removed_from_allowlist(google):
    """무상태라도 매 요청 허용 목록을 다시 본다: 목록에서 빼고 재배포하면 기존 토큰이 거부된다."""
    import asyncio

    before = PatentOAuthProvider(settings(), google=google.sign_in())
    after = PatentOAuthProvider(settings(PATENT_API_ALLOWED_DOMAINS="other.com"), google=google.sign_in())
    issued = before._issue("cid", "kim@example.com", "example.com")
    assert asyncio.run(before.load_access_token(issued.access_token)) is not None
    assert asyncio.run(after.load_access_token(issued.access_token)) is None


def test_tokens_survive_restart(google):
    """같은 서명 키면 새 인스턴스(재시작·다른 인스턴스)도 등록·토큰을 알아본다."""
    import asyncio

    from mcp.shared.auth import OAuthClientInformationFull

    a = PatentOAuthProvider(settings(), google=google.sign_in())
    info = OAuthClientInformationFull(client_id="tmp", client_secret="tmp", redirect_uris=[CHATGPT_REDIRECT])
    asyncio.run(a.register_client(info))
    b = PatentOAuthProvider(settings(), google=google.sign_in())
    restored = asyncio.run(b.get_client(info.client_id))
    assert restored is not None and restored.client_secret == info.client_secret
    assert asyncio.run(b.load_access_token(a._issue(info.client_id, "kim@example.com", "example.com").access_token))


# --------------------------------------------------------------------------- 고정 클라이언트 (Gemini Enterprise 흐름)


def test_static_client_flow(client):
    static = {"client_id": "gemini-enterprise"}
    q, verifier = sign_in(client, "gemini-enterprise", GEMINI_ENT_REDIRECT)
    r = token(client, static, q["code"], verifier, GEMINI_ENT_REDIRECT)
    assert r.status_code == 200, r.text
    assert mcp_call(client, "tools/list", access=r.json()["access_token"]).status_code == 200


def test_static_client_rejects_unknown_redirect(client):
    verifier, challenge = pkce()
    r = client.get(
        "/authorize",
        params={"client_id": "gemini-enterprise", "redirect_uri": "https://evil.example/cb", "response_type": "code", "code_challenge": challenge},
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- 비밀값 비노출


def test_secrets_not_in_responses_or_logs(client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    access = login_access_token(client)
    texts = [client.get(p).text for p in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp")]
    texts.append(initialize(client, access).text)
    # 서버 쪽 로그만 본다(테스트 클라이언트 자신의 요청 로그는 제외)
    server_log = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    texts.append(server_log)
    for t in texts:
        assert AUTH_SECRET not in t and GOOGLE_SECRET not in t
    assert access not in server_log and "google-code" not in server_log
