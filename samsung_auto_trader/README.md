## 모의투자 자동매매 실행 기록

### 2026-06-22 실행

- 환경: 한국투자증권 모의투자
- 방식: REST API polling
- 종목: 삼성전자 (`005930`)
- 주문수량: 1주
- 매수 주문번호: `0000029504`
- 매수 결과: `FILLED`
- 매수가격: 350,000원
- 매도 주문번호: `0000037148`
- 매도 결과: 현재 `PENDING`
- 왕복 거래 상태: 매도 미체결로 아직 완료되지 않음

실행 과정에서 프로그램은 매수 체결을 확인한 후 매도 주문을 자동 제출하고,
60초 간격으로 매도 체결 여부를 조회했습니다.

### 증빙 파일

- [주문·체결 CSV](records/trades_20260622.csv)
- [자동매매 실행 로그](records/auto_trade_20260622.txt)

> CSV와 로그에는 App Key, App Secret, 토큰 및 전체 계좌번호를 포함하지 않았습니다.



# Samsung Auto Trader

한국투자증권 Open API의 **모의투자 REST API만** 사용해 삼성전자(`005930`)를 상태 기반으로 자동 매매하는 과제용 프로젝트입니다. 웹소켓과 실전투자 URL은 사용하지 않습니다.

## 핵심 동작

왕복 거래 1회는 다음 순서로 완료됩니다.

1. 현재가와 계좌 상태를 확인합니다.
2. `현재가 - 주문가격차`에 지정가 매수 주문을 한 번 제출합니다.
3. 미체결 중에는 주문체결조회만 폴링하고 새 매수 주문을 내지 않습니다.
4. 매수 체결 후 보유·매도가능수량을 확인합니다.
5. `현재가 + 주문가격차`에 지정가 매도 주문을 한 번 제출합니다.
6. 매도 체결까지 조회한 후 CSV에 기록합니다.
7. 목표 왕복 횟수에 도달할 때까지 같은 과정을 반복합니다.

상태는 `IDLE → BUY_PENDING → HOLDING → SELL_PENDING → IDLE/COMPLETED` 순서로 이동합니다. 취소·거부 또는 모호한 상태가 발견되면 `FAILED`로 종료합니다.

## 파일 구조

- `main.py`: CLI 및 객체 조립
- `config.py`: Codespaces 환경변수와 모의투자 설정
- `auth.py`: 토큰 발급, 실제 만료시각 기반 캐시 재사용
- `api_client.py`: 공통 REST 호출, 조회 재시도, 주문 POST 무재시도
- `market_data.py`: 현재가 조회
- `account.py`: 잔고·보유·매도가능수량 조회
- `orders.py`: 모의 현금 매수·매도 주문
- `trader.py`: 상태 머신과 다회 왕복 자동매매
- `state.py`: `runtime_state.json` 및 거래 CSV 관리
- `logger.py`: 콘솔·파일 로그
- `test_*.py`: 실제 네트워크를 사용하지 않는 단위 테스트

## Codespaces Secrets

필수 환경변수는 GitHub Codespaces User Secrets로 저장합니다.

- `GH_ACCOUNT`: 모의계좌 8자리 또는 `12345678-01` 형식
- `GH_APPKEY`: 같은 모의투자 신청 건의 App Key
- `GH_APPSECRET`: 같은 모의투자 신청 건의 App Secret

선택 환경변수:

- `GH_ACCOUNT_PROD`: 기본값 `01`
- `KIS_ORDER_QTY`: 기본값 `1`
- `KIS_ORDER_OFFSET_KRW`: 기본값 `2000`
- `KIS_POLL_INTERVAL_SECONDS`: 기본값 `60`
- `KIS_HTTP_TIMEOUT_SECONDS`: 기본값 `10`
- `KIS_MAX_RETRIES`: 조회 GET 재시도 횟수, 기본값 `2`

프로젝트는 아래 모의투자 URL로 고정되어 있습니다.

```text
https://openapivts.koreainvestment.com:29443
```

## 설치와 테스트

```bash
cd /workspaces/open-trading-api/samsung_auto_trader
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

PYTHONPATH=. python -m unittest discover -p 'test_*.py' -v
python -m compileall .
```

단위 테스트는 실제 토큰 발급·조회·주문을 호출하지 않습니다.

## 이미 체결된 매수 주문에서 이어서 실행

기존 매수 주문이 체결됐고 그 주식을 보유 중이라면, 주문번호를 전달해 매도부터 자동으로 이어갈 수 있습니다.

```bash
TZ=Asia/Seoul python main.py \
  --reset-state \
  --resume-buy-order-no YOUR_BUY_ORDER_NO \
  --resume-order-date YYYYMMDD \
  --max-round-trips 3 \
  --order-qty 1 \
  --order-offset-krw 2000 \
  --poll-interval-seconds 60
```

`--resume-order-date`를 생략하면 실행 당일로 간주합니다. 과거 날짜 주문을 이어받을 때만 날짜를 지정합니다.

위 명령에서 기존 매수+자동 매도는 왕복 1회로 계산되고, 이후 프로그램이 새 매수·매도를 자동으로 반복해 총 3회 왕복을 목표로 합니다.

## 처음부터 자동 실행

계좌에 삼성전자 보유분과 미체결 주문이 없을 때 사용합니다.

```bash
TZ=Asia/Seoul python main.py \
  --reset-state \
  --max-round-trips 3 \
  --order-qty 1 \
  --order-offset-krw 2000 \
  --poll-interval-seconds 60
```

`--reset-state`는 로컬 상태 파일만 지웁니다. 프로그램은 새 주문 전에 서버의 미체결 주문과 계좌 보유수량을 다시 확인합니다.

## 안전한 1단계 확인

```bash
TZ=Asia/Seoul python main.py \
  --resume-buy-order-no YOUR_BUY_ORDER_NO \
  --run-once
```

`--run-once`는 드라이런이 아닙니다. 현재 상태를 한 단계만 진행하므로 상태에 따라 실제 모의주문을 제출할 수 있습니다.

## 생성 파일

- `runtime_state.json`: 재시작 복구용 상태. Git 제외 대상
- `token_cache.json`: 토큰 캐시. Git 제외 대상
- `trader.log`: 실행 로그. Git 제외 대상
- `records/trades_YYYYMMDD.csv`: 교수님 제출용 거래 기록

CSV에는 매수·매도 주문번호, 요청가격, 체결수량, 평균체결가, 잔여수량, 상태와 왕복 번호가 기록됩니다. `realized_profit_krw`는 수수료·세금을 반영하지 않은 단순 가격차 기준 값입니다.

## 주의사항

- 주문 접수와 체결은 다릅니다. 왕복 횟수는 매수와 매도가 모두 체결돼야 증가합니다.
- `BUY_PENDING`과 `SELL_PENDING`에서는 새 주문을 추가하지 않습니다.
- 주문 POST는 HTTP 500·타임아웃·토큰 오류가 발생해도 자동 재시도하지 않습니다.
- 09:10 이상 15:30 미만에만 새 주문을 제출합니다.
- `현재가 ± 2,000원` 지정가는 당일 체결되지 않을 수 있습니다. 교수님이 `1,000원`을 지시하면 `--order-offset-krw 1000`으로 변경합니다.
- 실행 중 `Ctrl+C`로 종료해도 `runtime_state.json`을 이용해 이어갈 수 있습니다.
- App Key, App Secret, 토큰, 전체 계좌번호를 코드·CSV·스크린샷에 노출하지 마세요.
