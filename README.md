# 자동매매 트레이딩 봇 (Automated Trading Bot)

> **⚠️ 면책 조항 — 본 코드를 사용하기 전에 반드시 읽으세요**
>
> * 본 저장소는 **알고리즘 트레이딩 학습·연구 목적**의 예제 코드이며, **투자 조언·수익 보장이 아닙니다.**
> * 실제 매매에 따른 **손익·세금·법적 책임은 전적으로 사용자**에게 있습니다.
> * API 장애, 버그, 슬리피지, 급변하는 시장 등으로 **예상치 못한 손실**이 발생할 수 있습니다.
> * 실전 계좌(`prod`) 투입 전 **`vps`(모의투자)로 충분히 검증**할 것을 권장합니다.
> * 상세 문구는 문서 맨 아래 [면책 조항 (Disclaimer)](#9-면책-조항-disclaimer)을 참고하세요.

## 1\. 프로젝트 개요

본 프로젝트는 한국 주식 시장(KOSPI, KOSDAQ)을 대상으로 하는 완전 자동화된 퀀트 트레이딩 봇입니다. 정해진 스케줄에 따라 종목 선정(Screener), 뉴스 수집(News Collector), AI 기반 분석(GPT Analyzer) 및 자동 매매(Trader)까지의 전 과정을 수행합니다. 또한, 지속적인 성과 분석(Reviewer)을 통해 스스로 전략 파라미터를 조정하는 피드백 루프를 갖추고 있습니다.

모든 과정은 Docker 컨테이너 환경에서 실행되도록 설계되어 이식성과 확장성을 확보했으며, Discord 웹훅을 통해 실시간으로 주요 이벤트와 거래 내역을 사용자에게 알립니다.

-----

## 2\. 주요 기능

  * **📈 자동화된 데이터 파이프라인**: 통합 매니저(`integrated_manager.py`)의 스케줄에 의해 종목 발굴부터 매매까지의 전 과정이 자동으로 실행됩니다.
  * **🔎 다단계 종목 스크리닝**: 시총·거래대금 1차 필터 → 기술적/재무/시장국면/섹터 트렌드 종합 점수 → 모멘텀·변동성·섹터 다양화 최종 선별까지 단계별로 종목을 좁힙니다. 각 단계의 생존/탈락 종목과 점수 구성요소를 상세 로그(퍼널)로 남겨 추적이 가능합니다(`--debug`).
  * **🌐 KIS 기반 시장·섹터 분석**: KIS 업종지수(0001 등)를 페이지네이션으로 200봉 이상 모아 MA50/MA200/RSI로 시장 국면(레짐)을 판정하고, 업종지수 추세로 섹터 트렌드 점수를 산출하여 종목 점수에 반영합니다.
  * **🧠 AI 기반 투자 결정**: 기술적/기본적 분석 점수와 최신 뉴스를 종합하여 OpenAI의 GPT 모델이 최종 매수/보류 결정을 내립니다. (API 미설정 시 휴리스틱 모드로 자동 전환)
  * **🛡️ 견고한 리스크 관리**:
      * `risk_manager.py`가 `background_risk_manager` 서비스로 독립 실행되어 보유 종목의 상태를 실시간으로 모니터링합니다.
      * ATR, 스윙 저점, RSI 등 다양한 지표를 조합하여 동적으로 손절/익절 라인을 설정합니다.
      * 포트폴리오가 비었을 경우, 자동으로 매매 파이프라인을 재가동하여 거래 연속성을 확보합니다.
  * **🧾 주문 정합성 관리**: `order_reconciler.py`가 장 마감 후 미체결/부분체결 주문을 KIS 주문 조회로 재검증하여 DB 상태를 실제 체결 내역과 일치시킵니다.
  * **🔄 자동 파라미터 튜닝**: `reviewer.py`가 매매 성과(승률, 손익비)를 주기적으로 분석하고, 성과가 부진할 경우 손절/익절 등 핵심 전략 파라미터를 자동으로 미세 조정합니다.
  * **🐳 Docker 기반 배포**: `docker-compose.yml`을 통해 통합 매니저(`integrated_manager`)와 백그라운드 리스크 매니저(`background_risk_manager`)를 별도의 서비스로 실행하여 안정성을 높였습니다.
  * **📢 실시간 알림**: 거래 체결, 오류 발생, 파이프라인 시작/종료 등 주요 이벤트를 Discord 웹훅으로 실시간 전송합니다.
  * **🗃️ 상세한 거래 기록**: 모든 거래 내역과 AI의 분석 결과는 SQLite 데이터베이스(`trading_log.db`)에 영구적으로 기록되어 상세한 사후 분석을 지원합니다.

-----

## 3\. 시스템 아키텍처 및 데이터 흐름

본 시스템은 모듈화된 파이썬 스크립트들이 파일 기반 데이터 파이프라인을 통해 상호작용하는 구조로 설계되었습니다. 실행은 \*\*두 개의 독립 서비스(컨테이너)\*\*로 구성됩니다.

  * **`integrated_manager`** (`run_integrated_manager.py` → `src/integrated_manager.py`): 메인 오케스트레이터. 스케줄링 + 파이프라인 실행 + 장시작/종료 잔액 캡처 + 일일 요약 + 리스크 관리를 통합 담당합니다. (기존 `scheduler.py` + `risk_manager.py`를 통합·대체)
  * **`background_risk_manager`** (`run_background_risk_manager.py` → `src/risk_manager.py`): 장중 보유 종목을 지속적으로 모니터링하는 실시간 리스크 매니저.

> 위 두 서비스 외에, 스크리너를 단독으로 돌려보기 위한 `screener` 서비스가 `docker-compose.yml`에 `profiles: ["tools"]`로 정의되어 있습니다. 평상시 `docker compose up`에서는 자동 기동되지 않으며, 필요할 때 `docker compose run --rm screener`로만 실행합니다(7장 사용법 참고).

```
┌───────────────────────────────┐        ┌────────────────────────────────┐
│  integrated_manager (서비스 1) │        │ background_risk_manager (서비스 2)│
│  - 스케줄러                     │        │  - 장중 보유종목 실시간 모니터링  │
│  - 파이프라인 오케스트레이션     │        │  - ATR/스윙저점/RSI 매도 판단     │
│  - 장시작/종료 잔액 캡처/요약    │        │  - 조건 충족 시 trader 매도 실행  │
└───────────────┬───────────────┘        └────────────────────────────────┘
                │ (요일별 스케줄 실행, KST)
                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              Trading Pipeline                              │
│                                                                            │
│  [1] screener.py  →  [2] news_collector.py  →  [3] gpt_analyzer.py         │
│  (종목 후보 생성)      (뉴스 데이터 수집)        (AI/휴리스틱 분석)          │
│       │                     │                          │                   │
│       ▼                     ▼                          ▼                   │
│  screener_...json      collected_news...json      gpt_trades_...json       │
│                                                        │                   │
│                                          ┌─────────────▼────────────┐      │
│                                          │        trader.py         │      │
│                                          │   (KIS API 매수/매도 실행) │      │
│                                          └─────────────┬────────────┘      │
│                                                        ▼                   │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐       │
│  │ order_reconciler │←─ │   recorder.py    │   │   reviewer.py    │       │
│  │ (주문 정합성 점검)│   │ (SQLite DB 기록) │ → │ (성과분석/자동튜닝)│       │
│  └──────────────────┘   └────────┬─────────┘   └──────────────────┘       │
│                                  ▼                                         │
│                            trading_log.db                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

**데이터 흐름:**

1.  **`screener.py`**: 시장 데이터(시총, 거래대금 5일 평균, 재무, 기술적 지표)를 분석하여 조건에 맞는 \*\*종목 후보군 파일(`screener_candidates_*.json`)\*\*을 생성합니다. 종목 마스터/지표 계산은 `kis_master.py`·`screener_core.py`가 보조합니다.
2.  **`news_collector.py`**: 후보군 파일을 입력받아 종목별 최신 뉴스를 스크레이핑하여 \*\*뉴스 데이터 파일(`collected_news_*.json`)\*\*을 생성합니다.
3.  **`gpt_analyzer.py`**: 종목 정보와 뉴스 데이터를 종합하여 AI가 분석 후 최종 \*\*매매 계획 파일(`gpt_trades_*.json`)\*\*을 생성합니다.
4.  **`trader.py`**: 매매 계획 파일을 읽어 한국투자증권(KIS) API를 통해 실제 **매수/매도 주문을 실행**합니다. (분할매수, 체결 확인, 재시도, 시장가 폴백 등 포함)
5.  **`recorder.py`**: `trader.py`에서 발생한 모든 거래 내역을 \*\*SQLite DB(`trading_log.db`)\*\*에 기록합니다.
6.  **`order_reconciler.py`**: 장 마감 후 DB의 미체결/부분체결(pending/partial) 레코드를 KIS 당일 주문 조회로 재검증하여 실제 체결 상태로 최신화합니다.
7.  **`reviewer.py`**: DB의 거래 기록을 FIFO 방식으로 분석하여 성과(승률, 손익비)를 평가하고, 필요시 \*\*설정 파일(`config.json`)\*\*의 전략 파라미터를 자동 조정합니다.
8.  **`risk_manager.py`**: 장중 보유 종목의 가격 변동을 실시간으로 모니터링하고, 자체 리스크 로직(ATR/스윙저점/RSI 등)에 따라 매도 조건을 판단·실행합니다.

> **참고(스케줄, KST):** 평일 기준 `09:00` 장시작 잔액 캡처 → `09:10` 스크리너 → `10:15` 매매 파이프라인 → `15:20` 일괄 체결 확인 → `15:22` 주문 정합성 점검(reconcile) → `15:30` 장종료 잔액 캡처 → `15:35` 일일 요약 전송. (시각은 `config/config.json`의 `schedule_times`·`daily_summary`·`batch_execution_check`로 조정 가능)

-----

## 4\. 모듈 설명

### `src/` 디렉토리 주요 스크립트:

**오케스트레이션 / 실행**

  * **`integrated_manager.py`**: 메인 오케스트레이터. 요일별 스케줄을 등록하고 스크리너·파이프라인·잔액 캡처·일일 요약·일괄 체결 확인·주문 정합성 점검 작업을 순서대로 실행합니다. 백그라운드 리스크 모니터링 스레드도 함께 관리합니다. (`run_integrated_manager.py`가 실행 진입점)
  * **`risk_manager.py`**: 장중 보유 종목의 가격 변동을 실시간으로 모니터링하여 ATR, 스윙 저점, RSI 과열 등의 조건에 따라 매도 시점을 판단·실행합니다. `background_risk_manager` 서비스로 독립 실행됩니다.

**파이프라인**

  * **`screener.py`**: 종목 스크리닝의 메인 흐름을 담당합니다. ① 시가총액·거래대금(직전 5거래일 평균) 1차 필터 → ② 재무(PER/PBR)·기술적 지표·섹터 트렌드·시장 국면을 종합한 점수 산정 → ③ 모멘텀·변동성·섹터 다양화 기반 최종 선별 순으로 진행합니다. 적자(음수 PER)·결측 재무는 페널티 처리하며, 상장일·섹터맵·섹터 트렌드 등은 **버전키가 포함된 캐시 파일**(예: `kis_listing_v2_*.pkl`)로 관리해 버그 수정 시 자동 무효화됩니다. `--debug` 실행 시 단계별 퍼널(생존/탈락 종목)과 점수 구성요소를 상세 로깅합니다.
  * **`screener_core.py`**: 스크리너의 핵심 연산 모듈. 기술적 지표 계산, 점수 산정, 시장 분석(`MarketAnalyzer`/`MarketState`), 거래 비용·기본 리스크 계산 등을 제공합니다. 시장 국면 판정용 지수 종가는 KIS 업종지수를 페이지네이션으로 누적(부족 시 KODEX ETF 폴백)하여 MA200까지 산출합니다.
  * **`kis_master.py`**: KIS 종목정보파일(.mst, KOSPI/KOSDAQ/KONEX)을 다운로드·파싱하여 티커/종목명/업종코드/기준가/상장주식수 등을 캐시 기반으로 제공합니다.
  * **`news_collector.py`**: `screener.py`가 선정한 종목들의 최신 뉴스를 네이버 API 및 웹 스크레이핑을 통해 수집합니다.
  * **`gpt_analyzer.py`**: 스크리닝된 종목 정보와 수집된 뉴스를 바탕으로 OpenAI GPT 모델을 활용하여 최종 투자 결정을 내립니다. API 키가 없는 경우, 점수 기반의 휴리스틱 분석으로 대체됩니다.
  * **`trader.py`**: `gpt_analyzer.py`의 결정을 바탕으로 실제 매수 주문을 실행하고, 리스크 매도 조건에 따라 보유 종목을 매도합니다. 분할매수, 체결 확인/재시도, 동적 호가, 시장가 폴백 등을 지원합니다.

**기록 / 정합성 / 분석**

  * **`recorder.py`**: 모든 매매 기록을 SQLite 데이터베이스에 저장하고 조회하는 인터페이스를 제공합니다.
  * **`order_reconciler.py`**: DB의 pending/partial 거래 레코드를 KIS 당일 주문 조회로 재검증하여 executed/partial/pending/cancelled 상태를 최신화합니다. (pending 누적 방지)
  * **`reviewer.py`**: 데이터베이스의 거래 기록을 FIFO 방식으로 손익 계산하여 승률·손익비 등의 성과 지표를 분석하고, 결과에 따라 `config.json`의 전략 파라미터를 자동 튜닝합니다.
  * **`rotation_manager.py`**: 포트폴리오 회전 매매(리밸런싱) 전용 관리자. 동적 임계값 계산과 거래 비용 최적화를 통해 보유 종목 교체를 판단합니다. (`config.json`의 `rotation.enabled`로 on/off)
  * **`account.py`**: KIS API로 계좌 잔고/요약을 조회하여 JSON으로 저장합니다. 토큰 만료/일시 오류 자동 복구 및 실패 시 본 파일을 덮지 않는 degraded 처리 기능을 포함합니다.

**전략 / 공통 / 유틸**

  * **`strategies.py`**: 다양한 매도 전략(RSI 역추세, 추세 추종, ATR 기반 등)의 로직을 정의합니다.
  * **`settings.py`**: `config.json`을 로드하고 섹션별 기본값을 보장하는 설정 관리자(`settings`)를 제공합니다.
  * **`utils.py`**: 로깅 설정, 경로 관리, 시간대(KST) 설정, 파일 검색, 캐시, 개장일 판단 등 프로젝트 전반에서 사용되는 공통 유틸리티 함수를 포함합니다.
  * **`notifier.py`**: Discord 웹훅을 통해 메시지와 임베드를 전송하는 기능을 담당합니다.
  * **`api/kis_auth.py`**: 한국투자증권 API 인증 및 토큰 관리를 담당합니다. 토큰 만료 시 자동 재인증 기능이 포함되어 있습니다.
  * **`api/domestic_stock/domestic_stock_functions.py`**: 국내주식 시세 조회·현금 주문(`order_cash` 메서드) API. 업종/지수 기간별시세(`inquire-daily-indexchartprice`, TR `FHKUP03500100`)로 시장 국면·섹터 트렌드 계산에 사용합니다.
  * **`health_check.py`**: KIS API의 정상 작동 여부를 간단히 확인하는 스크립트입니다.
  * **`cleanup_output.py`**: 오래된 로그 및 결과 파일을 주기적으로 삭제하여 디스크 공간을 관리합니다.
  * **`db_debug.py`**: DB 기록 동작을 추적하는 디버그 로깅 유틸리티(`DB_RECORD_DEBUG` 환경변수로 활성화).

-----

## 5\. 기술 스택

  * **언어**: Python 3.11
  * **주요 라이브러리**:
      * `pandas`, `numpy`: 데이터 분석 및 처리
      * `requests`, `httpx`: API 요청 및 웹 통신
      * `schedule`: 작업 스케줄링
      * `pykrx`, `FinanceDataReader`: 국내 주식 데이터 수집
      * `openai`: GPT 모델 연동
      * `beautifulsoup4`: 뉴스 본문 스크레이핑
      * `python-dotenv`: 환경 변수 관리
  * **API**:
      * 한국투자증권(KIS) REST API: 실시간 시세 조회 및 주문 실행
      * Naver Search API: 뉴스 검색
      * OpenAI API: 투자 분석 및 의사결정
  * **데이터베이스**: SQLite
  * **배포**: Docker, Docker Compose

-----

## 6\. 파이프라인 사전 준비

자동매매 파이프라인을 돌리기 전에 아래 항목을 준비합니다. **필수/선택**과 **사용 모듈**을 구분해 두었습니다.

### 6.1 공통 환경

| 항목 | 필수 여부 | 설명 |
|------|-----------|------|
| **Docker & Docker Compose** | 필수 | `integrated_manager`, `background_risk_manager` 컨테이너 실행 |
| **`config/.env`** | 필수 | API 키·계좌·웹훅 등 비밀값 (`cp config/.env.example config/.env`) |
| **`config/config.json`** | 필수 | 전략·스케줄·`trading_environment` (`prod` / `vps`) |
| **`output/` 디렉터리** | 자동 생성 | 런타임 JSON·DB·캐시 (Git 미추적) |

### 6.2 외부 API·서비스별 준비

#### 한국투자증권(KIS) Open API — **필수**

파이프라인 전 구간(스크리너·시세·주문·잔고·정합성·리스크)에서 사용합니다.

| 준비 항목 | 설정 위치 | 비고 |
|-----------|-----------|------|
| 실전 App Key / Secret | `config/.env` → `KIS_MY_APP`, `KIS_MY_SEC` | [KIS Developers](https://apiportal.koreainvestment.com/)에서 발급 |
| 실전 계좌번호(앞 8자리) | `KIS_MY_ACCT_STOCK` | 예: `12345678` |
| 계좌상품코드(뒤 2자리) | `KIS_MY_PROD` | 일반적으로 `01` (종합) |
| 모의 App Key / Secret | `KIS_PAPER_APP`, `KIS_PAPER_SEC` | 모의투자 전용 키 |
| 모의 계좌번호 | `KIS_MY_PAPER_STOCK` | 모의 계좌 8자리 |
| 거래 환경 | `config/config.json` → `trading_environment` | `prod`(실전) 또는 `vps`(모의) |

```env
# config/.env 예시
KIS_MY_APP=실전_APP_KEY
KIS_MY_SEC=실전_APP_SECRET
KIS_MY_ACCT_STOCK=12345678
KIS_MY_PROD=01
KIS_PAPER_APP=모의_APP_KEY
KIS_PAPER_SEC=모의_APP_SECRET
KIS_MY_PAPER_STOCK=50123456
```

> 처음에는 `trading_environment`를 **`vps`**로 두고 모의 계좌로 동작을 검증한 뒤 실전(`prod`)으로 전환하는 것을 권장합니다.

**사용 모듈:** `screener.py`, `health_check.py`, `trader.py`, `risk_manager.py`, `account.py`, `order_reconciler.py`, `kis_master.py` 등

---

#### 네이버 검색 API — **필수** (뉴스 수집)

| 준비 항목 | 설정 위치 | 비고 |
|-----------|-----------|------|
| Client ID | `NAVER_CLIENT_ID` | [네이버 개발자 센터](https://developers.naver.com/) → Application → 검색 API |
| Client Secret | `NAVER_CLIENT_SECRET` | 동일 애플리케이션에서 발급 |

```env
NAVER_CLIENT_ID=your_client_id
NAVER_CLIENT_SECRET=your_client_secret
```

**사용 모듈:** `news_collector.py` → `gpt_analyzer.py` 입력

> 키가 없으면 뉴스 수집 단계가 실패하거나 결과가 비어 GPT 분석 품질이 떨어질 수 있습니다.

---

#### OpenAI API — **선택** (GPT 분석)

| 준비 항목 | 설정 위치 | 비고 |
|-----------|-----------|------|
| API Key | `OPENAI_API_KEY` | [OpenAI Platform](https://platform.openai.com/) |
| 모델 | `config/config.json` → `gpt_params.openai_model` | 기본 `gpt-4o-mini` 등 |

```env
OPENAI_API_KEY=sk-...
```

**사용 모듈:** `gpt_analyzer.py`

> 미설정 시 **휴리스틱(점수 기반) 분석**으로 자동 전환됩니다. 파이프라인은 계속 동작하지만 AI 판단은 사용되지 않습니다.

---

#### Discord Webhook — **선택** (알림)

| 준비 항목 | 설정 위치 | 비고 |
|-----------|-----------|------|
| 통합 매니저용 웹훅 | `DISCORD_WEBHOOK_URL` | 파이프라인·잔액·요약·오류 알림 |
| 리스크 매니저용 웹훅 | `DISCORD_WEBHOOK_URL_RISK` | 장중 매도·리스크 알림 (없으면 위 URL 사용) |

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_URL_RISK=https://discord.com/api/webhooks/...
```

**생성 방법:** Discord 서버 → 채널 설정 → 연동 → 웹후크 → 새 웹후크 → URL 복사

**사용 모듈:** `notifier.py` (전 모듈에서 호출)

> 웹훅이 없어도 매매 로직은 동작하나, Discord 알림·에러 푸시는 비활성화됩니다.

---

### 6.3 파이프라인 단계 ↔ API 의존성

| 순서 | 스크립트 | 필요 API / 설정 |
|------|----------|-----------------|
| 장시작 | `account.py` (잔액) | KIS |
| 09:10 | `screener.py` | KIS (+ pykrx/FDR 보조) |
| 10:15+ | `health_check.py` | KIS |
| | `news_collector.py` | **Naver** |
| | `gpt_analyzer.py` | OpenAI(선택), Naver 결과 파일 |
| | `trader.py` | KIS |
| 장중 | `risk_manager.py` | KIS |
| 장마감 | `order_reconciler.py` | KIS |
| 월 1회 | `reviewer.py`, `cleanup_output.py` | DB·설정 파일 (KIS 간접) |

**공통:** `config/.env` 로드 (`env_loader.py`), 알림은 Discord(선택).

### 6.4 사전 준비 체크리스트

실행 전 아래를 확인하세요.

- [ ] `config/.env` 생성 및 **Git 미커밋** 확인
- [ ] `trading_environment`가 의도한 환경(`vps` / `prod`)인지 확인
- [ ] KIS 실전·모의 키/계좌가 각각 올바른지 확인
- [ ] Naver Client ID/Secret 입력
- [ ] (선택) OpenAI API Key — 없으면 휴리스틱 모드 인지
- [ ] (선택) Discord 웹훅 — 알림 채널 분리 시 `DISCORD_WEBHOOK_URL_RISK` 설정
- [ ] Docker 데몬 실행 가능
- [ ] 모의투자로 1회 이상 파이프라인·리스크 동작 검증

**로컬 설정 검증 예시:**

```bash
cp config/.env.example config/.env
# .env 편집 후
docker compose config   # env_file 파싱 확인
docker compose up --build -d
docker compose logs -f integrated_manager
```

-----

## 7\. 설치 및 실행

### 설치 과정

1.  **프로젝트 클론**:

    ```bash
    git clone <repository_url>
    cd my_trading_bot
    ```

2.  **설정 파일 생성**:

      * 비밀값(API 키, 계좌, 웹훅)은 **`config/.env`** 에만 둡니다. 저장소에는 예시만 포함됩니다.

        ```bash
        cp config/.env.example config/.env
        # config/.env 를 편집해 실제 키·계좌·웹훅 입력
        ```

        KIS App Key/Secret·계좌번호는 `KIS_MY_APP`, `KIS_MY_SEC`, `KIS_MY_ACCT_STOCK` 등 환경 변수로 설정합니다. (`config/.env.example` 참고)

      * (선택) URL·상품코드 등 비밀 제외 기본값은 `kis_devlp.yaml` 로 관리할 수 있습니다.

        ```bash
        cp config/kis_devlp.yaml.example config/kis_devlp.yaml
        ```

        `kis_devlp.yaml` 과 `config/.env` 는 **git에 올리지 않습니다** (`.gitignore` 처리).

      * `config/config.json` 은 전략·스케줄 파라미터용이며, 저장소에 포함해도 됩니다.

3.  **Docker 이미지 빌드 및 컨테이너 실행**:
    프로젝트 루트 디렉토리에서 아래 명령어를 실행합니다.

    ```bash
    docker-compose up --build -d
    ```

    이 명령어는 `Dockerfile`을 사용하여 이미지를 빌드하고, `docker-compose.yml`에 정의된 `integrated_manager`와 `background_risk_manager` 두 개의 서비스를 백그라운드에서 실행합니다.

### 사용법

  * **실행**: `docker-compose up` 명령어로 컨테이너가 실행되면 `integrated_manager`가 자동으로 시작되어 정해진 스케줄에 따라 파이프라인을 실행합니다.
  * **로그 확인**:
    ```bash
    # 통합 매니저(스케줄러) 로그 확인
    docker-compose logs -f integrated_manager

    # 백그라운드 리스크 매니저 로그 확인
    docker-compose logs -f background_risk_manager
    ```
  * **단발 실행 / 수동 작업** (스케줄 없이 즉시 실행):
    ```bash
    # 전체 파이프라인 1회 실행
    python run_integrated_manager.py --once

    # 개별 작업
    python run_integrated_manager.py --capture-open    # 장시작 잔액 캡처
    python run_integrated_manager.py --capture-close   # 장종료 잔액 캡처
    python run_integrated_manager.py --send-summary    # 일일 요약 전송
    ```
  * **스크리너만 단독 실행** (디버그 로그 포함, `tools` 프로필):
    ```bash
    # 컨테이너로 1회 실행 (./src 가 마운트되어 코드 수정이 즉시 반영됨)
    docker compose run --rm screener

    # 시장/옵션 변경 예시
    docker compose run --rm screener python -u screener.py --market KOSDAQ --debug
    ```
    > 코드 수정 후에도 결과가 그대로라면, 장기 실행 중인 `integrated_manager`가 메모리에 옛 모듈을 들고 있을 수 있습니다. `docker compose up -d --force-recreate integrated_manager`로 재기동하면 반영됩니다.
  * **중지**:
    ```bash
    docker-compose down
    ```

-----

## 8\. 프로젝트 구조

```
my_trading_bot/
├── config/
│   ├── config.json            # 시스템 주요 파라미터 (Git OK)
│   ├── .env.example           # 비밀값 템플릿
│   ├── kis_devlp.yaml.example # KIS 비밀 제외 기본값 템플릿
│   ├── .env                   # 실제 API 키·계좌 (Git 제외)
│   └── kis_devlp.yaml         # 로컬 KIS 기본값 (Git 제외, 선택)
├── output/                # 런타임 산출물 (Git 제외, .gitkeep 만 추적)
│   └── .gitkeep
├── src/
│   ├── api/                  # 외부 API 연동 모듈
│   │   ├── kis_auth.py       # KIS 인증/토큰 관리
│   │   └── domestic_stock/   # 국내주식 시세조회·주문 래퍼
│   ├── __init__.py
│   ├── integrated_manager.py # 메인 오케스트레이터(스케줄러+잔액+리스크 통합)
│   ├── risk_manager.py       # 장중 실시간 리스크 모니터링
│   ├── screener.py           # 종목 스크리닝
│   ├── screener_core.py      # 스크리너 핵심 연산/지표/시장분석
│   ├── kis_master.py         # KIS 종목 마스터(.mst) 다운로드/파싱
│   ├── news_collector.py     # 뉴스 수집
│   ├── gpt_analyzer.py       # AI/휴리스틱 분석
│   ├── trader.py             # 매수/매도 주문 실행
│   ├── recorder.py           # 거래 기록(SQLite)
│   ├── order_reconciler.py   # 주문 체결 상태 정합성 점검
│   ├── reviewer.py           # 성과 분석 및 파라미터 자동 튜닝
│   ├── rotation_manager.py   # 포트폴리오 회전 매매 관리
│   ├── account.py            # 계좌 잔고/요약 조회
│   ├── strategies.py         # 매도 전략 정의
│   ├── settings.py           # 설정(config.json) 로더
│   ├── utils.py              # 공통 유틸리티
│   ├── notifier.py           # Discord 알림
│   ├── health_check.py       # KIS API 헬스체크
│   ├── cleanup_output.py     # 오래된 산출물 정리
│   └── db_debug.py           # DB 기록 디버그 로깅
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run_integrated_manager.py       # integrated_manager 실행 진입점
└── run_background_risk_manager.py  # background_risk_manager 실행 진입점
```

-----

## 9\. 면책 조항 (Disclaimer)

본 프로젝트는 **알고리즘 트레이딩 학습 및 연구 목적**으로 개발되었습니다.

* 제공되는 소스 코드, 설정 예시, 문서는 **투자 권유·투자 자문·수익률 보장이 아닙니다.**
* 본 코드를 다운로드·실행·수정·배포하여 발생하는 **모든 투자 손익, 세금, 법적 분쟁의 책임은 사용자 본인**에게 있습니다.
* 자동매매 시스템은 **소프트웨어 버그**, **증권사·외부 API 장애·지연**, **네트워크 오류**, **시장 급변·유동성 부족·슬리피지** 등으로 인해 의도와 다른 주문·손실이 발생할 수 있습니다.
* GPT·뉴스·기술적 지표 기반 판단은 **오류·편향·지연**을 포함할 수 있으며, 과거 성과가 미래 수익을 보장하지 않습니다.
* 실전 계좌에 연결하기 전 **`vps`(모의투자) 환경에서 충분히 테스트**하고, 본인의 투자 성향·자금·리스크 허용 범위를 스스로 판단하시기 바랍니다.
* 제3자 API(KIS, Naver, OpenAI, Discord) 이용 시 각 서비스의 **이용약관·요금·호출 한도**를 준수해야 합니다.

**본 코드를 사용함으로써, 위 내용을 이해하고 이에 동의한 것으로 간주합니다.**
