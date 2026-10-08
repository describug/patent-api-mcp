"""원격(HTTP) 모드: 인증을 붙인 MCPServer를 만들어 streamable-http로 띄운다. 엔드포인트는 /mcp."""

from __future__ import annotations

import logging
import sys
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import ProtectedResourceMetadata

from .auth import GOOGLE_CALLBACK_PATH, SCOPE, GoogleSignIn, PatentOAuthProvider
from .config import ConfigError, RemoteSettings

MCP_PATH = "/mcp"
# Cloud Run은 요청을 여러 인스턴스로 나눌 수 있어 세션을 메모리에 두지 않는다(stateless).
# 도구는 모두 한 번 묻고 한 번 답하는 조회라 SSE 스트림 대신 JSON 응답으로 충분하다.
HTTP_OPTIONS: dict[str, Any] = {"streamable_http_path": MCP_PATH, "stateless_http": True, "json_response": True}

log = logging.getLogger("patent_api.remote")


def create_http_server(
    base: MCPServer, settings: RemoteSettings, *, google: GoogleSignIn | None = None
) -> MCPServer:
    """server.py의 로컬 서버(base)에 등록된 도구를 그대로 옮기고, 인증·디스커버리·구글 콜백을 붙인 새 서버.

    도구 정의는 server.py 한곳에만 둔다. 로컬 서버는 인증 없이 만들어지므로(MCPServer는 생성할 때만 인증을 받는다)
    원격용은 같은 Tool 객체로 새로 만든다.
    """
    provider = PatentOAuthProvider(settings, google=google)
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.resource_url,
        client_registration_options=ClientRegistrationOptions(enabled=True, default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=False),
        required_scopes=None,
        # 토큰의 대상(aud)은 provider.load_access_token이 직접 확인한다.
        validate_token_resource=False,
    )
    mcp = MCPServer(
        base.name,
        instructions=base.instructions,
        tools=base._tool_manager.list_tools(),  # SDK 2.x에 공개 접근자가 없다. test_remote가 도구 목록을 확인한다
        auth=auth,
        auth_server_provider=provider,
    )

    mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])(provider.google_callback)

    # SDK는 /.well-known/oauth-protected-resource/mcp(RFC 9728 경로 방식)에 둔다.
    # 경로 없이 루트만 찾는 클라이언트도 있어 같은 내용을 루트에도 둔다.
    prm = ProtectedResourceMetadataHandler(
        ProtectedResourceMetadata(
            resource=settings.resource_url,
            authorization_servers=[settings.public_url],
            resource_name="patent-api",
        )
    )
    mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])(prm.handle)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True})

    return mcp


def create_http_app(base: MCPServer, settings: RemoteSettings, **kwargs: Any):
    """테스트용 ASGI 앱(run_http와 같은 구성)."""
    mcp = create_http_server(base, settings, **kwargs)
    return mcp.streamable_http_app(**HTTP_OPTIONS, host=settings.host)


class _DropQueryString(logging.Filter):
    """접근 로그에서 '?' 뒤를 지운다(구글 콜백의 인가 코드·상태값이 로그에 남지 않게)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0] + "?…", *args[3:])
        return True


def run_http(base: MCPServer) -> None:
    try:
        settings = RemoteSettings.from_env()
    except ConfigError as exc:
        print(f"[patent-api] {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    mcp = create_http_server(base, settings)
    logging.getLogger("uvicorn.access").addFilter(_DropQueryString())
    logging.getLogger("patent_api").setLevel(logging.INFO)
    print(
        f"[patent-api] HTTP 모드: {settings.public_url}{MCP_PATH} (수신 {settings.host}:{settings.port}), "
        f"허용 도메인 {sorted(settings.allowed_domains) or '없음'}, 허용 이메일 {len(settings.allowed_emails)}개",
        file=sys.stderr,
    )
    mcp.run("streamable-http", host=settings.host, port=settings.port, **HTTP_OPTIONS)
