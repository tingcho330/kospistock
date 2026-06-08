# kospistock — 자동매매 트레이딩 봇

한국 주식(KOSPI / KOSDAQ) 대상 퀀트 자동매매 봇입니다.  
저장소: [github.com/tingcho330/kospistock](https://github.com/tingcho330/kospistock)

> **⚠️ 면책 조항 — 본 코드를 사용하기 전에 반드시 읽으세요**
>
> * 본 저장소는 **알고리즘 트레이딩 학습·연구 목적**의 예제 코드이며, **투자 조언·수익 보장이 아닙니다.**
> * 실제 매매에 따른 **손익·세금·법적 책임은 전적으로 사용자**에게 있습니다.
> * API 장애, 버그, 슬리피지, 급변하는 시장 등으로 **예상치 못한 손실**이 발생할 수 있습니다.
> * 실전 계좌(`prod`) 투입 전 **`vps`(모의투자)로 충분히 검증**할 것을 권장합니다.
> * 상세 문구는 [9. 면책 조항](#9-면책-조항-disclaimer)을 참고하세요.

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [주요 기능](#2-주요-기능)
3. [시스템 아키텍처](#3-시스템-아키텍처-및-데이터-흐름)
4. [모듈 설명](#4-모듈-설명)
   - [4.1 회전 매매 (Rotation)](#41-회전-매매-rotation)
5. [기술 스택](#5-기술-스택)
6. [파이프라인 사전 준비](#6-파이프라인-사전-준비)
7. [설치 및 실행](#7-설치-및-실행)
8. [프로젝트 구조](#8-프로젝트-구조)
9. [면책 조항](#9-면책-조항-disclaimer)

---

## 1. 프로젝트 개요

정해진 스케줄(KST)에 따라 다음을 자동 수행합니다.

| 단계 | 설명 |
|------|------|
| 스크리닝 | 시총·거래대금·재무·기술·시장 국면·섹터 트렌드 기반 종목 후보 생성 |
| 뉴스 수집 | 네이버 검색 API + 스크래핑 |
| 분석 | OpenAI GPT 또는 휴리스틱(키 없을 때) |
| 매매 | KIS Open API 매수/매도 |
| 리스크 | 장중 별도 프로세스에서 손절·익절·전략 매도 |
| 사후 처리 | SQLite 기록, 주문 정합성, 월간 성과 리뷰·산출물 정리 |

- **실행 환경:** Docker Compose (`integrated_manager` + `background_risk_manager`)
- **설정:** `config/config.json`(전략·스케줄) + `config/.env`(비밀값, Git 제외)
- **모듈 연동:** `output/` 아래 JSON·DB 파일 파이프라인
- **알림:** Discord 웹훅(선택)

---

## 2. 주요 기능

- **스케줄 오케스트레이션** — `integrated_manager.py`가 평일 잡·스크리너·파이프라인·잔액·체결확인·리컨실·요약 담당
- **다단계 스크리닝** — 1차 유동성 필터 → 종합 점수 → 모멘텀·변동성·섹터 다양화 (`--debug` 시 퍼널 로그)
- **KIS 시장·섹터 분석** — 업종지수 페이지네이션, MA/RSI 기반 레짐·섹터 트렌드
- **GPT / 휴리스틱 분석** — `OPENAI_API_KEY` 없으면 점수 기반으로 자동 폴백
- **장중 리스크** — `background_risk_manager` 컨테이너에서 ATR·스윙저점·RSI·긴급 낙폭·전략 믹서 기반 매도  
  - **진입가(평단) 기준 목표가/손절가**를 사용하며, 레벨은 SQLite `positions` 테이블에 저장됩니다.  
  - 레벨 갱신은 **유리한 방향으로만** 허용합니다: 손절가는 내려가지 않고(`max`), 목표가는 올라가지 않습니다(`min`).  
  - `direct_execute` 실패 시 같은 사이클에서 `trader.py --sell-only`로 **매도 fallback** (쿨다운 적용).
- **KIS 토큰 공유** — 두 컨테이너가 `output/cache/kis_token.json`을 공유. 파일락·EGW00133 backoff·재인증 쿨다운으로 **1분 1회 발급 제한** 대응.
- **주문 정합성** — `order_reconciler.py`로 DB pending/partial ↔ KIS 체결 동기화 + `order_id` 누락 orphan backfill
- **연속 손실 집계** — `is_countable_loss_sell()` / `count_consecutive_losses()`로 **체결 확정·order_id 있는 손실만** 카운트 (`stop_trading_on_consecutive_losses`, 기본 3회). pending/failed·중복 SELL row 제외
- **중복 SELL 방지** — `risk_manager` direct_execute 후 `trader.run_sell_logic`이 동일 종목을 재매도·재기록하지 않도록 pending 주문 skip + `order_id` 없는 SELL 중복 INSERT 차단
- **월간 튜닝** — `reviewer.py` 성과 분석 후 `config.json` 파라미터 미세 조정(매월 1회 스케줄)
- **회전 매매** — `rotation.enabled` 시 보유 최약 종목을 고득점 후보로 교체(리밸런싱). 공통 정책은 `rotation_policy.py`에서 일원화
- **일일 매매 요약** — `output/daily_balances/` 장시작·종료 스냅샷 비교 후 Discord 전송. 실현 손익은 `trading_data.db` 체결 매도 `profit_loss` 합, 미실현은 계속 보유 종목 평가 변화
- **비밀값 분리** — API 키·계좌·웹훅은 `config/.env`만 사용 (예시: `.env.example`)

---

## 3. 시스템 아키텍처 및 데이터 흐름

모듈 간 통신은 **`output/` JSON·SQLite**와 **`config/`** 를 중심으로 합니다. 비밀값은 `env_loader.py` → `config/.env`에서 로드합니다.

### 3.1 배포 구조 (Docker Compose)

| 서비스 | 진입점 | 역할 |
|--------|--------|------|
| `integrated_manager` | `run_integrated_manager.py` | 평일 스케줄·스크리너·매매 파이프라인·잔액/요약·체결확인·리컨실 |
| `background_risk_manager` | `run_background_risk_manager.py` | 장중 약 5분 주기 `risk_manager._run_cycle()` |

공통: `env_file: ./config/.env`, 볼륨 `./src`, `./config`, `./output`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  config/config.json + config/.env (Git 제외)                             │
│  output/  ← screener_*.json, gpt_trades_*.json, trading_data.db, cache/  │
└─────────────────────────────────────────────────────────────────────────┘
         ▲                              ▲
         │                              │
┌────────┴─────────────┐      ┌─────────┴──────────────────┐
│ integrated_manager    │      │ background_risk_manager   │
│ schedule · subprocess │      │ RiskManager · KIS · 매도   │
└──────────────────────┘      └──────────────────────────┘
```

> 장중 리스크는 **별도 컨테이너** 전용입니다. `integrated_manager`는 스케줄·파이프라인에만 집중합니다.

### 3.2 평일 스케줄 (KST)

`config/config.json`의 `daily_summary`, `schedule_times`, `batch_execution_check`로 오버라이드합니다. **저장소 기본값** 기준:

| 시각 | 작업 | 실행 |
|------|------|------|
| 09:00 | 장시작 잔액 스냅샷 | `integrated_manager.capture_balance_snapshot("open")` → `output/daily_balances/` |
| 09:10 | 스크리너 | `screener.py` |
| 10:15 | 매매 파이프라인 | `health_check` → `news_collector` → `gpt_analyzer` → `trader` |
| 15:20 | 일괄 체결 확인 | `trader.py --batch-check-only` |
| 15:22 | 주문 정합성 | `order_reconciler.py` |
| 15:30 | 장종료 잔액 스냅샷 | `integrated_manager.capture_balance_snapshot("close")` |
| 15:35 | 일일 요약 | Discord (`send_daily_trading_summary`) — `daily_summary.summary_send_time`으로 변경 가능 |

- 휴장일: 스크리너·파이프라인 스킵 (`is_market_open_day`)
- **월간 유지보수:** 매일 점검 후 `monthly_maintenance.day`(기본 1일)에 1회 — `reviewer.py` → `cleanup_output.py` (기본 16:00)

### 3.2.1 일일 매매 요약 (Discord)

`compare_balances()` + `send_daily_trading_summary()`가 `balance_open_YYYYMMDD.json` / `balance_close_YYYYMMDD.json`을 비교합니다.

| 표시 항목 | 계산 방식 |
|-----------|-----------|
| 잔액 변화 · 일일 수익률 | 장시작/종료 `total_balance` 차이 |
| 현금 변화 · 보유평가 변화 | 각 스냅샷 `cash` / `holdings_value` 차이 (합 = 잔액 변화) |
| 실현 손익 | 당일 체결 매도 `trade_records.profit_loss` 합 (`recorder`) |
| 미실현 손익 | **장중 계속 보유** 종목만: 종료 평가액 − 시작 평가액 |
| 수수료·세금 | 당일 체결 거래 `commission + tax` 합 |
| 매매 내역 | 스냅샷 기준 매도/매수 종목 수 (티커 집합 차이) |
| 보유종목 | **장마감** 스냅샷 `holdings_count` |

> 실현 손익이 0으로 나오면 당일 매도가 DB에 없거나 `profit_loss` 미계산일 수 있습니다. `order_reconciler` 실행 후 `recorder.recompute_profit_loss_for_order_id()`로 보정하세요.  
> `account.py`는 수동 잔고 조회·`balance_*.json` 저장용이며, 스케줄 스냅샷 경로(`daily_balances/`)와는 별도입니다.

### 3.3 스크리너 vs 매매 파이프라인

스크리너는 **파이프라인 밖** 별도 스케줄 잡입니다. 당일 `screener_candidates_*.json` 등을 만든 뒤 파이프라인이 읽습니다.

```
[09:10 screener]                         [10:15 pipeline]
screener.py                              health_check.py
  → screener_candidates_*.json             → news_collector.py
  → screener_scores_*.json                   → gpt_analyzer.py
  → market_state_*.json                      → trader.py → recorder → trading_data.db
         └──────────────────────────────────────────┘
                              (장중, 별도 컨테이너) risk_manager.py
```

**`PIPELINE_SCRIPTS` (의존성 순):**

1. `health_check.py`
2. `news_collector.py` ← 스크리너 JSON
3. `gpt_analyzer.py`
4. `trader.py` ← `gpt_trades_*.json`

실패 시 `output/pipeline_state.json`에 저장 후 `STEP_DEPENDENCIES` 기준 **실패 단계부터 재시도** (`MAX_ATTEMPTS`).

### 3.4 장중 리스크

| 항목 | 내용 |
|------|------|
| 주기 | 장중 ~5분 / 장외 ~30분 |
| 진입 | `run_background_risk_manager.py` → `BackgroundRiskManager` → `risk_manager._run_cycle()` |
| 로직 | `risk_manager.RiskManager` + `strategies.StrategyMixer` (hybrid/advanced) |
| 매도 판단 | 기본 규칙(긴급 낙폭·손절·목표·RSI·전일종가) → 고급 전략 순 |
| 매도 실행 | `auto_sell.direct_execute: true` 시 KIS 시장가 직접 주문 |
| fallback | `direct_execute` 실패 종목 수집 후 사이클 종료 시 `trader.py --sell-only` 1회 기동 |
| 중복 방지 | `trader.run_sell_logic`은 `get_open_orders(pending/partial)` SELL ticker skip. `recorder`는 `order_id` 없는 SELL이 10분·동일 qty·가격 1% 이내 기존 행과 겹치면 skip 또는 UPDATE |
| 보유 0 | 조건 충족 시 `trader.py` 전체 파이프라인 자동 기동 (별도 경로) |
| 알림 | `DISCORD_WEBHOOK_URL_RISK` (없으면 `DISCORD_WEBHOOK_URL`) |

**`_run_cycle()` 흐름:**

```
계좌 스냅샷 → (보유 0이면 trader.py) → 각 종목:
  compute_realtime_levels → check_sell_condition
    → SELL + direct_execute → KIS order_cash
    → 실패 시 목록에 추가
→ 실패 종목 있으면 trader.py --sell-only
```

**기본 규칙 우선순위** (`_check_basic_rules`): 긴급 낙폭(`emergency_drop_pct`) → 손절가 → 부분 익절 → 목표가 → RSI → 전일 종가 이탈 → 최대 보유일.

> `EmergencyDrop`은 `rotation.min_holding_days`를 적용하지 않습니다. 손절·RSI 익절에는 최소 보유일이 적용됩니다.

### 3.4.1 KIS 토큰 (`output/cache/`)

두 Docker 서비스가 **동일 앱키·동일 토큰 파일**을 사용합니다. KIS OAuth는 **1분당 1회** 발급 제한(`EGW00133`)이 있습니다.

| 파일 | 역할 |
|------|------|
| `kis_token.json` | 접근 토큰 캐시 (24h, 만료 5분 전 갱신) |
| `kis_token.lock` | 컨테이너 간 발급 경합 방지 (fcntl) |

`api/kis_auth.py` 동작:

- 발급 전 파일락 → 다른 프로세스가 갱신한 토큰 재사용
- `EGW00133` 시 65초 backoff 후 최대 3회 재시도
- 재인증 쿨다운(60초) 내 API 재발급 대신 파일 토큰 재로드
- 재인증 실패 시 기존 토큰 파일을 **선삭제하지 않음** (성공 시에만 덮어쓰기)

환경 변수: `KIS_TOKEN_BACKOFF_SEC`, `KIS_REAUTH_COOLDOWN_SEC`, `KIS_TOKEN_LOCK_TIMEOUT_SEC`, `KIS_TOKEN_FILE`

### 3.5 주요 산출물 (`output/`)

| 패턴 | 모듈 |
|------|------|
| `screener_*`, `market_state_*` | `screener.py` |
| `collected_news_*` | `news_collector.py` |
| `gpt_trades_*` | `gpt_analyzer.py` |
| `balance_*`, `summary_*` | `account.py` (수동 조회) |
| `daily_balances/balance_{open,close}_*.json` | `integrated_manager.capture_balance_snapshot` |
| `trading_data.db` | `recorder.py` (SQLite `trade_records`, `positions`) |
| `debug/db_record_debug.log` | `db_debug.py` (`DB_RECORD_DEBUG=1` 시) |
| `pipeline_state.json`, `monthly_maintenance_state.json` | `integrated_manager.py` |
| `cache/kis_token.json`, `cache/kis_token.lock` | KIS OAuth 토큰·발급 락 |
| `cache/` (`.mst`, `.pkl` 등) | KIS·스크리너 |

Git에는 `output/.gitkeep`만 추적합니다.

### 3.7 거래 DB·주문번호(`order_id`)

| 항목 | 내용 |
|------|------|
| 저장 | `output/trading_data.db` — 주문·체결 메타(`order_id`, `executed_qty`, `order_status`) |
| 포지션 레벨 | `output/trading_data.db` — `positions` 테이블에 티커별 `entry_price/stop_price/target_price` 저장 (진입가 기준) |
| 매매 기록 | `record_trade()` → `upsert_trade_record_by_order_id()` — `order_id` 있으면 UPSERT, 없으면 SELL 중복 검사 후 INSERT/skip |
| 손실 집계 | `is_countable_loss_sell()` — `executed` + `order_id` + `profit_loss < 0` 만 연속·일일 손실에 반영. `pending`/`failed`/중복 row 제외 |
| 중복 SELL | `find_recent_sell_duplicate()` — 동일 ticker·qty, 10분 이내, 가격 1% 이내. 기존 `order_id` 행 있으면 `duplicate_sell_without_order_id_skipped` 로그 후 skip |
| 리컨실 (15:22) | `order_reconciler.py` — `pending`/`partial` + `order_id` 있는 행을 KIS와 상태 동기화 |
| orphan backfill | 리컨실 마지막에 자동 실행 — `order_id` 빈 행을 KIS 일별 주문과 **유일 매칭** 시 backfill |
| 수동 backfill | `python src/order_reconciler.py --since-hours 36 --backfill-only` |

> `risk_manager` direct_execute → `pending` SELL 기록 후 `trader` fallback이 같은 종목을 `order_id` 없이 재기록하면 손실이 이중 집계될 수 있었습니다. 현재는 저장 단계·집계 단계 모두에서 차단합니다.  
> 컨테이너 이미지에 `sqlite3` CLI가 없습니다. DB 확인은 아래 [7.3](#73-수동--단발-실행) Python 예시를 사용하세요.

### 3.8 GPT 월간 회고 (`reviewer.py`)

| 항목 | 내용 |
|------|------|
| 스케줄 | 월 1회 유지보수 (`reviewer.py` → `cleanup_output.py`) |
| 입력 | 체결 매도 승패·`sell_reason`/`structured_context`·포트폴리오 스냅샷·`gpt_trades_*.json` 대조·코스피 뉴스 |
| 출력 | `config.json` 튜닝 제안, `output/review_log.json`, Discord 요약 |
| 최소 표본 | `REVIEWER_MIN_SELL_TRADES`(기본 10) 체결 매도 — 미만 시 skip (`REVIEWER_ALLOW_PARTIAL=1`이면 보수 GPT) |

```bash
# 수동 회고 (드라이런)
REVIEWER_DRY_RUN=1 REVIEWER_ALLOW_PARTIAL=1 docker compose exec integrated_manager python -u /app/src/reviewer.py
```

### 3.6 KIS API 계층

`api/kis_auth.KIS` → `api/domestic_stock/domestic_stock_functions.DomesticStock`  
시세·잔고·`order_cash()` 주문·업종지수 시세(TR `FHKUP03500100`)를 한 경로에서 처리합니다.

- **인증:** `auth()` / `reauthenticate()` — 토큰 파일 캐시 + `request_get`/`request_post` 만료 시 1회 재시도
- **설정:** `config/.env` + `config/kis_devlp.yaml` (`load_kis_config()`)
- **주의:** `integrated_manager`(스크리너·account)와 `background_risk_manager`(장중 매도)가 동시에 KIS를 호출하므로 토큰 파일 공유·락이 필요합니다.

---

## 4. 모듈 설명

### 오케스트레이션

| 파일 | 역할 |
|------|------|
| `integrated_manager.py` | 스케줄 등록, subprocess 파이프라인, `daily_balances` 스냅샷·일일 Discord 요약·리컨실, 파이프라인 상태 복구 |
| `run_integrated_manager.py` | Docker / 로컬 진입점 (`--once`, `--capture-open` 등) |
| `risk_manager.py` | 장중 리스크 사이클, 매도·파이프라인 재기동 |
| `run_background_risk_manager.py` | 리스크 전용 컨테이너 진입점 (`BackgroundRiskManager`) |

### 파이프라인

| 파일 | 역할 |
|------|------|
| `screener.py` | 종목 스크리닝 CLI (`--market`, `--debug`) |
| `screener_core.py` | 지표·점수·`MarketState` / `MarketRegime` |
| `kis_master.py` | KIS `.mst` 마스터 다운로드·캐시 |
| `health_check.py` | KIS 헬스체크(삼성전자 시세) |
| `news_collector.py` | 네이버 뉴스 수집 |
| `gpt_analyzer.py` | GPT 또는 휴리스틱 매매 계획 JSON 생성 |
| `trader.py` | 매수/매도·체결·분할매수·연속 손실 체크·pending SELL skip·`--batch-check-only`·`--sell-only` |

### 기록·정합성·분석

| 파일 | 역할 |
|------|------|
| `recorder.py` | SQLite `trading_data.db` (`trade_records`, `positions`), upsert/backfill, `is_countable_loss_sell`, 중복 SELL 방지 |
| `order_reconciler.py` | KIS 주문 ↔ DB 상태 정합성 + orphan `order_id` backfill |
| `reviewer.py` | 월간 GPT 회고: 승패·매도사유·포트폴리오·gpt_trades 대조 → config 튜닝 |
| `rotation_policy.py` | 회전 매매 공통 정책(최소 보유일·Δscore·비용·1:1 페어·상한) |
| `rotation_manager.py` | 현금 부족 시 회전 시도·시장 동적 임계값·실행 |
| `trader.py` | 슬롯 꽉 참 시 리밸런싱·`run_buy_logic` 회전 한도 관리 |
| `account.py` | 잔고·요약 JSON |
| `cleanup_output.py` | 오래된 `output/` 정리 (월간) |

### 공통·API

| 파일 | 역할 |
|------|------|
| `settings.py` | `config.json` 로드·기본값 |
| `env_loader.py` | `config/.env` 로드 |
| `utils.py` | KST, 캐시, `find_latest_file`, 개장일 등 |
| `notifier.py` | Discord 웹훅 |
| `strategies.py` | 매도 전략 클래스 |
| `db_debug.py` | DB 디버그 로그 (`DB_RECORD_DEBUG=1`) |
| `api/kis_auth.py` | KIS 인증·토큰 캐시·파일락·EGW00133 backoff |
| `api/domestic_stock/domestic_stock_functions.py` | 시세·주문 REST 래퍼 |

### 4.1 회전 매매 (Rotation)

포트폴리오 **슬롯이 꽉 찼거나** (`max_positions`) **신규 매수 예산이 부족**할 때, 스크리너 점수가 낮은 보유 종목을 매도하고 고득점 후보를 매수하는 **리밸런싱(스왑)** 기능입니다.  
기본값은 **비활성** (`rotation.enabled: false`)이며, 실전 적용 전 모의투자에서 충분히 검증하세요.

#### 마스터 스위치

| 설정 | 기본값 | 설명 |
|------|--------|------|
| `rotation.enabled` | `false` | `true`일 때만 회전·리밸런싱 실행. `false`이면 슬롯이 꽉 차도 신규 매수만 생략 |

#### 진입 조건 (`trader.run_buy_logic`)

| 상황 | 경로 | 설명 |
|------|------|------|
| 가용 현금 부족 + `screener_params.affordability_filter` | `RotationManager.try_rotation` | 1페어 스왑 시도 후 잔고 갱신·매수 재개 |
| 보유 슬롯이 꽉 참 | GPT 리밸런싱 → 실패 시 점수 기반 | `_get_enhanced_rebalance_candidates` |

한 번의 `run_buy_logic` 호출(매수 파이프라인 1회)당 **최대 1페어**만 실행합니다 (`rotation.max_pairs_per_run`, 기본 `1`).  
`try_rotation`으로 이미 1회 회전했으면 같은 사이클에서 슬롯 리밸런싱은 **스킵**됩니다.

#### 후보 선정 우선순위

1. **`rebalance_params.use_gpt_analysis: true`** (기본) — GPT가 보유 vs 스크리너 상위 후보 비교 (`gpt_analyzer.get_gpt_enhanced_rebalance_candidates`)
2. **GPT 실패·빈 결과 시에만** — `trader._determine_rebalance_swaps` (점수 최약 보유 ↔ 점수 최강 후보, 1:1 페어)

GPT SELL/BUY 목록은 `pair_gpt_rebalance_lists`로 **1:1 페어**로 맞춘 뒤, 공통 정책을 적용합니다(고아 BUY/SELL 제거).

#### 공통 정책 (`rotation_policy.py`)

모든 경로는 실행 전 아래 검증을 **동일하게** 거칩니다.

| 검증 | 설정·동작 |
|------|-----------|
| 최소 보유일 | `rotation.min_holding_days` (기본 `10`). **미충족 종목은 회전 매도 불가** — `RiskManager` 손절·RSI 익절 등과 동일한 `check_min_holding_period` 사용 |
| 점수 차이 | `신규 점수 − 보유 점수 ≥ delta_score_min` (기본 `0.12`). `use_dynamic_threshold: true` 시 KOSPI 레짐·변동성에 따라 임계값 조정 |
| 예산 | `(가용 현금 + 매도 예상 대금) × (1 − fee_buffer_pct)` 로 매수 1주 가격 감당 가능 |
| 거래 비용 | `min_profit_rate`, `min_cost_effectiveness` — 순수익·비용 대비 효과 (`screener_core.calculate_net_profit_rotation`) |
| 페어 상한 | `max_pairs_per_run` (기본 `1`) |

리밸런싱 매도 전에는 **스왑 시뮬레이션**(매도 후 현금·슬롯으로 매수 1주 가능 여부)을 통과해야 실제 주문이 나갑니다.

#### 설정 예시 (`config/config.json`)

```json
"rotation": {
  "enabled": false,
  "min_holding_days": 10,
  "delta_score_min": 0.12,
  "max_pairs_per_run": 1,
  "use_dynamic_threshold": true,
  "min_profit_rate": 0.02,
  "min_cost_effectiveness": 2.0
},
"rebalance_params": {
  "use_gpt_analysis": true,
  "screener_top_n": 10,
  "min_score_threshold": 0.7
},
"integrated_analysis": {
  "enabled": true,
  "min_confidence_for_rotation": 0.9
}
```

| 키 | 구분 | 설명 |
|----|------|------|
| `rotation.*` | 회전 | 위 공통 정책·한도 |
| `rebalance_params.min_score_threshold` | GPT 후보 | 리밸런싱용 스크리너 상위 후보 최소 점수 (기본 `0.7`) |
| `screener_params.min_score_threshold` | 스크리너 | 1차 스크리닝 통과 최소 점수 (별도, 기본 `0.52` 등) |
| `integrated_analysis.min_confidence_for_rotation` | 로그 | GPT 회전 **샌드박스 제안** 로그 필터용 (실행 경로 필터와는 별도) |

#### 모듈 역할

```
trader.run_buy_logic
  ├─ (현금 부족) rotation_manager.try_rotation  ─┐
  └─ (슬롯 꽉 참)  _get_enhanced_rebalance_candidates ─┤
                                                      ├→ rotation_policy.apply_rotation_policy
                                                      └→ 매도(REBALANCE_SWAP / ROTATION_SWAP) → 매수
```

---

## 5. 기술 스택

| 구분 | 내용 |
|------|------|
| 언어 | Python 3.11 (`Dockerfile`) |
| 스케줄 | `schedule` |
| 데이터 | `pandas`, `numpy`, `pykrx`, `FinanceDataReader` |
| HTTP | `requests`, `httpx` |
| AI | `openai` (선택) |
| 스크래핑 | `beautifulsoup4` |
| 설정 | `python-dotenv`, `PyYAML` |
| DB | SQLite (`output/trading_data.db`) |
| 배포 | Docker, Docker Compose |

**외부 API:** KIS Open API, Naver Search API, OpenAI API(선택), Discord Webhook(선택)

---

## 6. 파이프라인 사전 준비

### 6.1 공통 환경

| 항목 | 필수 | 설명 |
|------|------|------|
| Docker & Compose | ✅ | 두 서비스 실행 |
| `config/.env` | ✅ | `cp config/.env.example config/.env` |
| `config/config.json` | ✅ | `trading_environment`: `vps` 또는 `prod` |
| `output/` | 자동 | 런타임 전용 (Git 제외) |

### 6.2 API별 설정 (`config/.env`)

#### KIS Open API — 필수

| 변수 | 설명 |
|------|------|
| `KIS_MY_APP`, `KIS_MY_SEC` | 실전 App Key / Secret |
| `KIS_MY_ACCT_STOCK`, `KIS_MY_PROD` | 실전 계좌(8자리)·상품코드(`01`) |
| `KIS_PAPER_APP`, `KIS_PAPER_SEC` | 모의 키 |
| `KIS_MY_PAPER_STOCK` | 모의 계좌 |

발급: [KIS Developers](https://apiportal.koreainvestment.com/)

#### Naver Search API — 필수 (뉴스)

| 변수 | 설명 |
|------|------|
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | [네이버 개발자 센터](https://developers.naver.com/) 검색 API |

#### OpenAI — 선택

| 변수 | 설명 |
|------|------|
| `OPENAI_API_KEY` | 없으면 `gpt_analyzer` 휴리스틱 모드 |

#### Discord — 선택

| 변수 | 설명 |
|------|------|
| `DISCORD_WEBHOOK_URL` | 통합 매니저 알림 |
| `DISCORD_WEBHOOK_URL_RISK` | 리스크 매니저 (미설정 시 위 URL 사용) |

#### 실행 파라미터 — 선택

| 변수 | 기본 | 설명 |
|------|------|------|
| `MARKET` | `KOSPI` | `KOSPI` / `KOSDAQ` / `KONEX` |
| `SLOTS` | `3` | GPT 최대 매수 계획 수 |

(선택) `cp config/kis_devlp.yaml.example config/kis_devlp.yaml` — URL·`my_prod` 등 비밀 제외 기본값

### 6.3 단계별 API 의존성

| 시각·구분 | 스크립트 | API |
|-----------|----------|-----|
| 09:00 | 잔액 스냅샷 (`integrated_manager`) | KIS |
| 09:10 | `screener.py` | KIS |
| 10:15~ | `health_check` → `news` → `gpt` → `trader` | KIS, Naver, OpenAI(선택) |
| 장중 | `risk_manager` | KIS |
| 15:20 | `trader.py --batch-check-only` | KIS |
| 15:22 | `order_reconciler` | KIS |
| 15:30 | 잔액 스냅샷 (`integrated_manager`) | KIS |
| 15:35 | `send_daily_trading_summary` | Discord (선택) |
| 월 1회 | `reviewer`, `cleanup_output` | DB·로컬 파일 |

### 6.4 체크리스트

- [ ] `config/.env` 생성 · Git에 올리지 않기
- [ ] `trading_environment` = `vps`로 먼저 검증
- [ ] Naver 키 입력
- [ ] (선택) OpenAI · Discord 웹훅
- [ ] `docker compose config`로 env 파싱 확인
- [ ] 모의투자 1회 이상 E2E 확인

---

## 7. 설치 및 실행

### 7.1 클론 및 설정

```bash
git clone https://github.com/tingcho330/kospistock.git
cd kospistock

cp config/.env.example config/.env
# config/.env 편집 (KIS, Naver, Discord, OpenAI)

# 선택
cp config/kis_devlp.yaml.example config/kis_devlp.yaml
```

`config/config.json`에서 `trading_environment`를 확인하세요. 처음에는 **`vps`** 권장.

### 7.2 Docker 실행 (권장)

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f integrated_manager
docker compose logs -f background_risk_manager
```

중지:

```bash
docker compose down
```

### 7.3 수동 / 단발 실행

컨테이너 **내부** (`WORKDIR=/app`):

```bash
# 통합 매니저: 하루치 순서 (--once)
docker compose exec integrated_manager python /app/run_integrated_manager.py --once

# 잔액·요약만
docker compose exec integrated_manager python /app/run_integrated_manager.py --capture-open
docker compose exec integrated_manager python /app/run_integrated_manager.py --capture-close
docker compose exec integrated_manager python /app/run_integrated_manager.py --send-summary

# 스크리너만
docker compose exec integrated_manager python -u /app/src/screener.py --market KOSPI --debug

# 파이프라인 단계만 (스크리너 결과가 output/에 있어야 함)
docker compose exec integrated_manager python -u /app/src/health_check.py
docker compose exec integrated_manager python -u /app/src/news_collector.py
docker compose exec integrated_manager python -u /app/src/gpt_analyzer.py --market KOSPI --slots 3
docker compose exec integrated_manager python -u /app/src/trader.py

# 체결 확인·리컨실 (pending/partial 갱신 + orphan order_id backfill)
docker compose exec integrated_manager python -u /app/src/trader.py --batch-check-only

# 매도만 (risk_manager direct_execute fallback과 동일)
docker compose exec background_risk_manager python -u /app/src/trader.py --sell-only

# KIS 토큰 상태 확인
docker compose exec background_risk_manager python -c "
from api.kis_auth import KIS
k = KIS(env='prod')
print('token ok:', bool(k.auth_token))
"

docker compose exec integrated_manager python -u /app/src/order_reconciler.py --since-hours 36 --limit 800

# order_id 누락 행만 KIS 일별 주문으로 backfill
docker compose exec integrated_manager python -u /app/src/order_reconciler.py --since-hours 36 --backfill-only

# DB 확인 (컨테이너에 sqlite3 CLI 없음 → Python 사용)
docker compose exec integrated_manager python -c "
import sqlite3
for r in sqlite3.connect('/app/output/trading_data.db').execute(
    'SELECT id, ticker, order_id, executed_qty, order_status FROM trade_records ORDER BY id'
):
    print('|'.join(str(x) for x in r))
"
```

호스트에서 직접 실행 시 (`config/.env`·`output/` 경로 유지):

```bash
export CONFIG_PATH="$(pwd)/config/config.json"
export OUTPUT_DIR="$(pwd)/output"
pip install -r requirements.txt
python run_integrated_manager.py --once
```

> `./src` 볼륨 마운트 시 코드 수정은 **컨테이너 재시작 없이** 반영됩니다. 반영이 안 되면:  
> `docker compose up -d --force-recreate integrated_manager`

### 7.4 환경 변수 참고

| 변수 | 용도 |
|------|------|
| `LOG_LEVEL` | `DEBUG` / `INFO` (기본 `INFO`) |
| `DB_RECORD_DEBUG` | `1` 시 DB 기록 추적 로그 (`[DB_DEBUG]`) |
| `DB_DEBUG_LOG_FILE` | DB 디버그 로그 경로 (기본 `output/debug/db_record_debug.log`) |
| `REVIEWER_LOOKBACK_DAYS` | 회고 조회 기간(일, 기본 30) |
| `REVIEWER_MIN_SELL_TRADES` | 체결 매도 최소 건수 (기본 10, 구 `REVIEWER_MIN_TRADES` 호환) |
| `REVIEWER_ALLOW_PARTIAL` | `1`이면 표본 부족해도 보수 GPT 회고 |
| `REVIEWER_MAX_DIGEST` | GPT 프롬프트 매도 샘플 상한 (기본 15) |
| `REVIEWER_DRY_RUN` | `1`이면 config 미적용 |
| `SCREENER_TIMEOUT_SEC` | 스크리너 subprocess 타임아웃(초) |
| `SCRIPT_TIMEOUT_SEC` | 기타 스크립트 타임아웃 |
| `KIS_TOKEN_FILE` | 토큰 캐시 경로 (기본 `output/cache/kis_token.json`) |
| `KIS_TOKEN_BACKOFF_SEC` | EGW00133 재시도 대기(초, 기본 65) |
| `KIS_REAUTH_COOLDOWN_SEC` | 재인증 API 호출 쿨다운(초, 기본 60) |
| `KIS_TOKEN_LOCK_TIMEOUT_SEC` | 토큰 파일락 대기(초, 기본 120) |
| `DISCORD_WEBHOOK_URL_RISK` | 리스크 매니저 전용 Discord 웹훅 |

---

## 8. 프로젝트 구조

```
kospistock/
├── config/
│   ├── config.json              # 전략·스케줄 (Git OK)
│   ├── .env.example             # 비밀값 템플릿
│   ├── kis_devlp.yaml.example
│   ├── .env                     # 로컬 비밀값 (Git 제외)
│   └── kis_devlp.yaml           # 로컬 KIS 기본값 (Git 제외, 선택)
├── output/
│   ├── .gitkeep                 # 런타임 산출물 루트 (Git 제외)
│   └── daily_balances/          # balance_open_*.json, balance_close_*.json (런타임)
├── src/
│   ├── api/
│   │   ├── kis_auth.py
│   │   └── domestic_stock/
│   │       └── domestic_stock_functions.py
│   ├── integrated_manager.py
│   ├── risk_manager.py
│   ├── screener.py / screener_core.py / kis_master.py
│   ├── health_check.py / news_collector.py / gpt_analyzer.py / trader.py
│   ├── recorder.py / order_reconciler.py / reviewer.py
│   ├── rotation_policy.py / rotation_manager.py
│   ├── account.py / strategies.py
│   ├── settings.py / env_loader.py / utils.py / notifier.py
│   ├── cleanup_output.py / db_debug.py
│   └── ...
├── .devcontainer/
├── run_integrated_manager.py
├── run_background_risk_manager.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 9. 면책 조항 (Disclaimer)

본 프로젝트는 **알고리즘 트레이딩 학습 및 연구 목적**으로 개발되었습니다.

* 제공되는 소스 코드, 설정 예시, 문서는 **투자 권유·투자 자문·수익률 보장이 아닙니다.**
* 본 코드를 다운로드·실행·수정·배포하여 발생하는 **모든 투자 손익, 세금, 법적 분쟁의 책임은 사용자 본인**에게 있습니다.
* 자동매매 시스템은 **소프트웨어 버그**, **증권사·외부 API 장애·지연**, **네트워크 오류**, **시장 급변·유동성 부족·슬리피지** 등으로 인해 의도와 다른 주문·손실이 발생할 수 있습니다.
* GPT·뉴스·기술적 지표 기반 판단은 **오류·편향·지연**을 포함할 수 있으며, 과거 성과가 미래 수익을 보장하지 않습니다.
* 실전 계좌에 연결하기 전 **`vps`(모의투자) 환경에서 충분히 테스트**하고, 본인의 투자 성향·자금·리스크 허용 범위를 스스로 판단하시기 바랍니다.
* 제3자 API(KIS, Naver, OpenAI, Discord) 이용 시 각 서비스의 **이용약관·요금·호출 한도**를 준수해야 합니다.

**본 코드를 사용함으로써, 위 내용을 이해하고 이에 동의한 것으로 간주합니다.**
