"""HTTP 공통: 일시 오류(타임아웃, 503)만 짧은 간격으로 최대 2회 재시도한다."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .errors import NETWORK_ERROR, TIMEOUT, PatentApiError

log = logging.getLogger("patent_api")

# httpx는 INFO 로그에 요청 URL(=ServiceKey 포함)을 남긴다. 키가 새지 않게 막아 둔다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_RETRIES = 2
RETRY_DELAYS = (0.5, 1.5)
RETRY_STATUS = {503}


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    source: str,
    sleep=asyncio.sleep,
    on_send=None,
    **kwargs,
) -> httpx.Response:
    """요청을 보내고 응답을 돌려준다. 재시도 후에도 타임아웃·연결 실패면 PatentApiError.

    on_send: 실제로 요청을 보낼 때마다(재시도 포함) 부르는 함수. 호출 수 집계에 쓴다.
    """
    attempt = 0
    while True:
        if on_send is not None:
            on_send()
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            if attempt < MAX_RETRIES:
                await sleep(RETRY_DELAYS[attempt])
                attempt += 1
                continue
            raise PatentApiError(
                TIMEOUT, f"{source} 서버 응답이 없습니다(시간 초과, {MAX_RETRIES}회 재시도함). 잠시 뒤 다시 시도해 주세요.",
                retryable=True,
            ) from None
        except httpx.TransportError as e:
            # 예외 메시지에 URL(키 포함)이 들어갈 수 있어 종류만 남긴다.
            raise PatentApiError(
                NETWORK_ERROR, f"{source} 서버에 연결하지 못했습니다({type(e).__name__}). 네트워크를 확인해 주세요.",
                retryable=True,
            ) from None
        if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            await sleep(RETRY_DELAYS[attempt])
            attempt += 1
            continue
        return resp
