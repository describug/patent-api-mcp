"""표준 오류 코드와 예외.

KIPRIS resultCode, OPS HTTP 상태 등 출처별 오류를 여기 정의한 코드로 바꿔서 돌려준다.
"""

from __future__ import annotations

# 표준 오류 코드
CONFIG_MISSING_KEY = "CONFIG_MISSING_KEY"      # .env에 키가 없음
AUTH_FAILED = "AUTH_FAILED"                    # 키가 틀렸거나 만료
PERMISSION_DENIED = "PERMISSION_DENIED"        # 키는 맞지만 해당 기능 권한 없음
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"              # 월/주 한도 초과
RATE_LIMITED = "RATE_LIMITED"                  # 단시간 호출 과다
INVALID_INPUT = "INVALID_INPUT"                # 입력 형식 오류
AMBIGUOUS_NUMBER = "AMBIGUOUS_NUMBER"          # 번호 종류(출원/공개/등록)를 특정할 수 없음
UPSTREAM_BAD_REQUEST = "UPSTREAM_BAD_REQUEST"  # 상대 서버가 요청을 거부(파라미터 오류)
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # 상대 서버 일시 장애(503 등)
TIMEOUT = "TIMEOUT"                            # 응답 시간 초과
NETWORK_ERROR = "NETWORK_ERROR"                # 연결 실패
UPSTREAM_ERROR = "UPSTREAM_ERROR"              # 그 밖의 상대 서버 오류
PARSE_ERROR = "PARSE_ERROR"                    # 응답을 해석할 수 없음


class PatentApiError(Exception):
    """도구 응답의 ``error`` 로 그대로 옮겨지는 예외."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def scrub(text: str, secrets: list[str | None]) -> str:
    """메시지에 비밀값이 섞여 있으면 가린다."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text
