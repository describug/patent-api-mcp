"""원격(HTTP) 모드. 로컬 stdio 모드에서는 불러오지 않는다.

- config.py  원격 모드 설정 읽기·검증(허용 목록이 비면 기동 거부)
- auth.py    얇은 OAuth 인가 서버: 로그인은 구글에 맡기고, 허용 목록 확인 후 이 서버의 토큰을 발급
- app.py     인증을 붙인 MCPServer를 만들어 streamable-http(/mcp)로 띄운다
"""
