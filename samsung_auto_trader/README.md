# Samsung Auto Trader

REST API only로 동작하는 한국투자증권 모의투자용 자동매매 예제입니다. 삼성전자 005930만 대상으로 하며, 웹소켓은 사용하지 않습니다.

## 구조

- `main.py`: 실행 진입점
- `config.py`: 환경변수 로드와 설정값 정의
- `auth.py`: 토큰 발급 및 같은 날 캐시 재사용
- `api_client.py`: 공통 REST 호출, 재시도, 에러 처리
- `market_data.py`: 현재가 조회
- `account.py`: 잔고/보유 종목 조회
- `orders.py`: 매수/매도 주문
- `trader.py`: 거래 윈도우 루프와 전략 로직
- `logger.py`: 콘솔/파일 로깅
- `token_cache.json`: 발급된 토큰 캐시

## 환경 변수

필수:

- `GH_ACCOUNT`: 계좌번호. `12345678-01` 형식 또는 `12345678` 형식 모두 지원
- `GH_APPKEY`: 모의투자 앱키
- `GH_APPSECRET`: 모의투자 앱시크릿

선택:

- `GH_ACCOUNT_PROD`: `GH_ACCOUNT`에 상품코드가 없을 때 기본값으로 사용할 2자리 상품코드
- `KIS_BASE_URL`: 기본값은 모의투자 URL
- `KIS_SYMBOL`: 기본값 `005930`
- `KIS_ORDER_QTY`: 기본값 `1`
- `KIS_ORDER_OFFSET_KRW`: 기본값 `2000`
- `KIS_POLL_INTERVAL_SECONDS`: 기본값 `300`
- `KIS_HTTP_TIMEOUT_SECONDS`: 기본값 `10`
- `KIS_MAX_RETRIES`: 기본값 `2`

## 실행

```bash
cd samsung_auto_trader
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export GH_ACCOUNT="12345678-01"
export GH_APPKEY="your_app_key"
export GH_APPSECRET="your_app_secret"

python main.py
```

## 동작 방식

- 09:10 이전에 실행하면 시작 시각까지 대기합니다.
- 15:30 이후에는 주문하지 않고 종료합니다.
- 같은 날에는 토큰을 재사용합니다.
- 각 사이클마다 현재가 1회, 잔고/보유 1회, 매수 주문 1회, 매수 후 확인 1회, 매도 주문 1회, 매도 후 확인 1회를 사용합니다.
- 매도 주문은 보유 수량이 있을 때만 넣어 불필요한 실패를 줄입니다.

## 주의

- 모의투자 전용입니다.
- 실제 주문 전환은 하지 마십시오.
- KIS 응답 필드나 TR ID가 계정/환경에 따라 다를 수 있으므로, 필요하면 `orders.py`와 `account.py`만 수정하면 됩니다.
