# kospistock — 자동매매 트레이딩 봇

한국 주식(KOSPI / KOSDAQ) 대상 퀀트 자동매매 봇입니다.  
저장소: [github.com/tingcho330/kospistock](https://github.com/tingcho330/kospistock)

> **⚠️ 면책 조항 — 본 코드를 사용하기 전에 반드시 읽으세요**
>
> * 본 저장소는 **알고리즘 트레이딩 학습·연구 목적**의 예제 코드이며, **투자 조언·수익 보장이 아닙니다.**
> * 실제 매매에 따른 **손익·세금·법적 책임은 전적으로 사용자**에게 있습니다.
> * API 장애, 버그, 슬리피지, 급변하는 시장 등으로 **예상치 못한 손실**이 발생할 수 있습니다.
> * 실전 계좌(`prod`) 투입 전 **`vps` → `kis_paper`(KIS 모의 API) 순으로 충분히 검증**할 것을 권장합니다.
> * 상세 문구는 [9. 면책 조항](#9-면책-조항-disclaimer)을 참고하세요.

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [주요 기능](#2-주요-기능)
3. [시스템 아키텍처](#3-시스템-아키텍처-및-데이터-흐름)
4. [모듈 설명](#4-모듈-설명)
   - [4.1 회전 매매 (Rotation)](#41-회전-매매-rotation)
   - [4.2 자산 배분 (Asset Allocation)](#42-자산-배분-asset-allocation)
   - [4.3 거래 환경 (trading_environment)](#43-거래-환경-trading_environment)
5. [기술 스택](#5-기술-스택)
6. [파이프라인 사전 준비](#6-파이프라인-사전-준비)
7. [설치 및 실행](#7-설치-및-실행)
   - [7.5 Phase 6 검증 (asset_allocation)](#75-phase-6-검증-asset_allocation)
8. [프로젝트 구조](#8-프로젝트-구조)
9. [면책 조항](#9-면책-조항-disclaimer)

---

## 1. 프로젝트 개요

**KOSPI 주간 리밸런싱**을 기본 운용 모드로 하는 퀀트 자동매매 봇입니다.  
전략 철학: **외국인·기관 수급 + 실적 성장 + 신고가 돌파 + KOSPI 대비 상대 모멘텀(RS)**.

정해진 스케줄(KST)에 따라 다음을 자동 수행합니다.

| 단계 | 설명 |
|------|------|
| 스크리닝 | 7축 점수·Breakout Gate·프리스코어로 Top 20 후보 생성 (`ConvictionScore` 기반 `target_weight`) |
| 뉴스 수집 | 네이버 검색 API + 스크래핑 |
| 분석 | OpenAI GPT 또는 휴리스틱(키 없을 때) |
| 매매 | KIS Open API 매수/매도 (`target_weight` 기반 사이징). 선택: **70:20:10 자산 배분** 가드(`asset_allocation`) |
| 리스크 | 장중 별도 프로세스에서 손절·익절·전략 매도 (459580 등 bond ETF 제외 가능) |
| 사후 처리 | SQLite 기록, 주문 정합성, 월간 성과 리뷰·산출물 정리 |

- **운용 시장:** KOSPI (`MARKET=KOSPI`, `screener_params.portfolio.rebalance_frequency: weekly`)
- **주간 스케줄:** 금요일 15:45 스크리너 → 월요일 09:30 매매 파이프라인
- **실행 환경:** Docker Compose (`integrated_manager` + `background_risk_manager`)
- **설정:** `config/config.json`(전략·스케줄) + `config/.env`(비밀값, Git 제외)
- **모듈 연동:** `output/` 아래 JSON·DB 파일 파이프라인
- **알림:** Discord 웹훅(선택)

---

## 2. 주요 기능

- **스케줄 오케스트레이션** — `integrated_manager.py`가 평일 잡·스크리너·매매 파이프라인·잔액·체결확인·리컨실·요약 담당
- **KOSPI 7축 스크리닝** — Flow 25% · RS Momentum 25% · Growth 15% · Breakout 15% · Fin 10% · Mkt 5% · Sector 5% (Tech 0%, 진입 보조만)
- **Breakout Gate (2단)** — Tier1 `Br≥0.25 OR Pos≥0.90` → 부족 시 Tier2 `Br≥0.20 OR Pos≥0.85` 종료 (Tier3 없음). Pos52w 단독 통과 시 `Breakout≥0.05` 필수
- **스코어 조정** — `BreakoutBonus`(최대 +0.08), `NearHighPenalty`(최대 −0.05), `WeakBreakoutPenalty`(Br<0.10 ×0.95, Br<0.05 추가 ×0.90)
- **확신도·해석 필드** — `ConvictionScore`, `rank_reason`, `selection_summary`, `penalty_reason`, `WeakBreakoutMultiplier`, `gate_tier` / `gate_reason` (후보 JSON schema **1.7**)
- **포트폴리오 비중** — `weight_mode: conviction` (`ConvictionScore` 비례, min **2%** / max **7.5%**). `rank_tier` 모드도 config로 선택 가능
- **다단계 스크리닝** — 1차 유동성 필터 → 프리스코어(50종) → 상세 스코어링 → 변동성·게이트·섹터 다양화 (`--debug` 시 퍼널 로그)
- **KIS 시장·섹터 분석** — 업종지수 페이지네이션, MA/RSI 기반 레짐·섹터 트렌드
- **GPT / 휴리스틱 분석** — 1차 필터(뉴스·점수) → 전술 계획(차트·패턴·예산 가드) 2단계. `OPENAI_API_KEY` 없으면 휴리스틱 폴백. 1차 필터 탈락 시 `gpt_trades_*.json`에 **`결정: "미진입"`** 기록(탈락 사유·`initial_filter` 메타 포함). `trader.py`는 **`결정 == "매수"`** 만 실행
- **장중 리스크** — `background_risk_manager` 컨테이너에서 ATR·스윙저점·RSI·긴급 낙폭·전략 믹서 기반 매도  
  - **진입가(평단) 기준 목표가/손절가**를 사용하며, 레벨은 SQLite `positions` 테이블에 저장됩니다.  
  - 레벨 갱신은 **유리한 방향으로만** 허용합니다: 손절가는 내려가지 않고(`max`), 목표가는 올라가지 않습니다(`min`).  
  - `direct_execute` 실패 시 같은 사이클에서 `trader.py --sell-only`로 **매도 fallback** (쿨다운 적용).
- **KIS 토큰 공유** — 두 컨테이너가 `output/cache/kis_token.json`을 공유. 파일락·EGW00133 backoff·재인증 쿨다운으로 **1분 1회 발급 제한** 대응.
- **KIS API 속도 제한** — `config.kis_limits` 환경별 RPS·동시성·EGW00201 백오프(`kis_rate_limit.py`). 스크리너 워커 자동 축소·시세/OHLCV 캐시
- **주문 정합성** — `order_reconciler.py`로 DB pending/partial ↔ KIS 체결 동기화 + orphan `order_id` backfill. KIS 주문조회 miss 시 **계좌 잔고 holding fallback**(`[RECONCILE_BY_HOLDING]`). 체결가 `pchs_avg_pric` 반영 시 `amount` 동기화·`structured_context`에 `original_order_price` 보존. 이미 `executed` 행은 idempotent skip
- **거래 기록 분류** — `paper_executed`(order_id 없음) = `paper_db_only` 테스트 기록 vs `kis_paper` 실주문(`order_id` 있는 `executed`). `[TRADE_RECORD_CLASSIFY]` 로그. 일일 요약 BUY 집계에서 `paper_db_only` 제외
- **매수 관측성** — `[STOCK_BUDGET_USAGE]`(예산·GPT 후보·실행 금액·미사용 사유), `[REBUY_GUARD]`(보유 종목 재매수 차단), `[BUY_SELECTION_SUMMARY]`(blocked_rebuy 포함)
- **연속 손실 집계** — `is_countable_loss_sell()` / `count_consecutive_losses()`로 **체결 확정·order_id 있는 손실만** 카운트 (`stop_trading_on_consecutive_losses`, 기본 3회). pending/failed·중복 SELL row 제외
- **중복 SELL 방지** — `risk_manager` direct_execute 후 `trader.run_sell_logic`이 동일 종목을 재매도·재기록하지 않도록 pending 주문 skip + `order_id` 없는 SELL 중복 INSERT 차단
- **매수 오기록 방지** — `trader._check_delayed_execution`은 **ODNO·매수/매도 구분** 필수. `split_buy.enabled=false` 시 분할 분기 미진입. `order_id` 없는 체결 BUY/SELL은 `recorder`에서 INSERT 차단
- **월간 튜닝** — `reviewer.py` 성과 분석 후 `config.json` 파라미터 미세 조정(매월 1회 스케줄)
- **회전 매매** — `rotation.enabled` 시 보유 최약 종목을 고득점 후보로 교체(리밸런싱). 공통 정책은 `rotation_policy.py`에서 일원화. **459580(bond ETF)은 회전·GPT 리밸런싱 대상에서 제외**
- **자산 배분 (선택)** — `asset_allocation.enabled` 시 주문 실행 단계에서만 70% 주식 / 20% 459580 / 10% 현금(최소 5%) 가드. 스크리너·GPT 전략은 변경 없음. 주식 매수 후 잔여 현금으로 459580 자동 매수(국내주식 현금주문 경로)
- **일일 매매 요약** — `output/daily_balances/` 장시작·종료 스냅샷 + KIS 공식 수치(`nass_amt`, `rlzt_pfls`, `thdt_*`) 기반 Discord 전송. DB `profit_loss`는 보조·불일치 시 ⚠️ 표시
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

`config/config.json`의 `daily_summary`, `schedule_times`, `screener_params.portfolio`로 오버라이드합니다.

#### 주간 리밸런싱 모드 (기본, `rebalance_frequency: weekly`)

| 시각 | 요일 | 작업 | 실행 |
|------|------|------|------|
| 09:00 | 평일 | 장시작 잔액 스냅샷 | `capture_balance_snapshot("open")` |
| **15:45** | **금요** | **스크리너** | `screener.py` → 후보·`target_weight` 산출 |
| **09:30** | **월요** | **주간 매매 파이프라인** | `health_check` → `news` → `gpt` → `trader` |
| 11:00 · 14:00 | 평일 | 장중 경량 리컨실 | `order_reconciler` |
| 15:20 | 평일 | 일괄 체결 확인 | `trader.py --batch-check-only` |
| 15:22 | 평일 | 주문 정합성 | `order_reconciler.py` |
| 15:30 | 평일 | 장종료 잔액 스냅샷 | `capture_balance_snapshot("close")` |
| 15:35 | 평일 | 일일 요약 | Discord (`daily_summary.summary_send_time` 변경 가능) |

- 휴장일: 스크리너·파이프라인 스킵 (`is_market_open_day`)
- `intraday_trading_enabled: false` 시 주간 모드에서 **화~목 일간 파이프라인·09:10 스크리너는 비활성**
- **월간 유지보수:** 매월 1일 — `reviewer.py` → `cleanup_output.py` (기본 16:00)

#### 일간 모드 (`rebalance_frequency` ≠ `weekly`)

| 시각 | 작업 |
|------|------|
| 09:10 | 스크리너 |
| 10:10~ | 매매 파이프라인 (`schedule_times.pipeline_time`) |

### 3.2.1 일일 매매 요약 (Discord)

`compare_balances()` + `send_daily_trading_summary()`가 `balance_open_YYYYMMDD.json` / `balance_close_YYYYMMDD.json`을 비교합니다.  
스냅샷 캡처 시 `account.py`가 KIS **`inquire-balance`** + **`inquire-balance-rlz-pl`**(TTTC8494R)을 호출해 `output/summary_rlz_*.json`에 실현손익을 저장합니다.

| 표시 항목 | 계산 방식 (1차 소스) |
|-----------|----------------------|
| 순자산 변화 · 일일 수익률 | 장시작/종료 **`nass_amt`**(순자산) 차이 — `tot_evlu_amt` 대신 KIS 앱과 동일 필드 우선 |
| 금일 매수 / 매도 | KIS `thdt_buy_amt` / `thdt_sll_amt` |
| 실현 손익 (KIS) | `inquire-balance-rlz-pl` → **`rlzt_pfls`** |
| 실현 손익 (DB) | 당일 체결 매도 `trade_records.profit_loss` 합 — KIS와 ±3,000원 초과 시 ⚠️. BUY `trade_count`는 **`kis_paper_order`/`kis_prod_order`만** 집계(`paper_db_only` 제외) |
| 미실현 손익 | **장중 계속 보유** 종목만: 종료 평가액 − 시작 평가액 |
| 금일 제비용 | KIS `thdt_tlex_amt` (없으면 DB `commission + tax`) |
| D+1 / D+2 정산 | `nxdy_excc_amt` / `prvs_rcdl_excc_amt` |
| 매매 내역 | 스냅샷 기준 매도/매수 종목 수 (티커 집합 차이) |
| 보유종목 | **장마감** 스냅샷 `holdings_count` |

> 예수금(`dnca_tot_amt`)은 T+2 결제 환경에서 당일 매매 직후 변하지 않을 수 있어 **「현금 변화」 단독 표시는 하지 않습니다.**  
> DB 실현손익이 KIS와 다르면 `order_reconciler` 실행·체결가(`avg_prvs` / holding `pchs_avg_pric`)·`amount` 정합성·phantom BUY row(`order_id` 없는 `paper_executed`) 여부를 점검하세요.  
> `account.py`는 `balance_*.json`·`summary_*.json`·`summary_rlz_*.json` 저장용이며, 스케줄 스냅샷은 `daily_balances/`에 별도 저장됩니다.

### 3.3 스크리너 vs 매매 파이프라인

스크리너는 **파이프라인 밖** 별도 스케줄 잡입니다. 주간 모드에서는 **금요 후보 JSON**을 월요 파이프라인이 읽습니다.

```
[금 15:45 screener]                    [월 09:30 pipeline]
screener.py                            health_check.py
  → screener_candidates_*.json             → news_collector.py
  → screener_candidates_full_*.json        → gpt_analyzer.py
  → screener_scores_*.json                 → trader.py
  → market_state_*.json                        ├─ target_weight 사이징
         └──────────────────────────────────────┤
                                                ├─ [asset_allocation] stock_buy_budget
                                                ├─ 주식 매수 (REBUY / 신규 / 리밸런스)
                                                ├─ [POST_STOCK] 459580 매수 (enabled 시)
                                                └─ recorder → trading_data.db
         └──────────────────────────────────────────┘
                              (장중, 별도 컨테이너) risk_manager.py
```

> **`asset_allocation.enabled=false`(기본·회귀 모드):** 기존 파이프라인과 동일 — `available_cash` passthrough, `dynamic_cash_management` 적용, 459580 자동 매수 없음.  
> **`asset_allocation.enabled=true`:** `compute_allocation()` → `stock_buy_budget`로 주식 매수 상한, 주문 후 `_buy_bond_etf_if_needed()`로 459580 매수. `dynamic_cash_management`는 **우회**.

#### 3.3.1 KOSPI 7축 스코어링 (`screener_core.py`)

| 축 | 비중 | 설명 |
|----|------|------|
| FlowScore | 25% | 외국인·기관 누적·가속·보유비중 변화 |
| MomentumScore | 25% | KOSPI 대비 RS (20/60/120일, `relative_strength_mode: primary`) |
| GrowthScore | 15% | 매출·영업이익·EPS YoY (+ 영업이익 턴어라운드 보너스 +0.05) |
| BreakoutScore | 15% | 52주 고점 근접·거래량·박스 돌파 |
| FinScore | 10% | ROE·부채·PER·PBR |
| MktScore | 5% | 시장 레짐 보정 |
| SectorScore | 5% | 섹터 트렌드 |
| TechScore | 0% | 선정 점수 제외, 진입 타이밍 보조 |

**최종 Score 조정 (게이트 이전):**

```
subtotal = clamp(0, 1, 7축합 + BreakoutBonus − NearHighPenalty)
Score    = subtotal × WeakBreakoutMultiplier
```

| 조정 | 조건 | 값 |
|------|------|-----|
| BreakoutBonus | Br≥0.40 / 0.60 / 0.80 | +0.03 / +0.05 / +0.08 |
| NearHighPenalty | Pos≥0.90 & Br<0.10 | −0.05 |
| NearHighPenalty | Pos≥0.85 & Br<0.15 | −0.03 |
| WeakBreakoutMultiplier | Br<0.10 | ×0.95 |
| WeakBreakoutMultiplier | Br<0.05 (누적) | ×0.90 |

**Breakout Gate (후보 필터, 최대 2단):**

1. Tier1: `Breakout≥0.25` OR (`Pos52w≥0.90` AND `Breakout≥0.05`)
2. Tier2 (후보<20 시): `Breakout≥0.20` OR (`Pos52w≥0.85` AND `Breakout≥0.05`) → **종료**

**ConvictionScore** (비중 산정·해석):

```
0.40×Flow + 0.30×Momentum + 0.20×Breakout + 0.10×Growth
```

**목표 비중 (`portfolio_allocator.py`):**

| `weight_mode` | 설명 |
|---------------|------|
| `conviction` (기본) | `ConvictionScore / Σ` 후 min/max 클립·재정규화 |
| `rank_tier` | 순위 구간 고정 비중 (7% / 5% / 3.5%) |
| `equal` / `score_proportional` | 균등·Score 비례 |

```json
"portfolio": {
  "weight_mode": "conviction",
  "min_weight": 0.02,
  "max_weight": 0.075
}
```

**후보 JSON 주요 필드 (schema 1.7):** `Score`, `target_weight`, `BreakoutBonus`, `NearHighPenalty`, `WeakBreakoutMultiplier`, `penalty_reason`, `ConvictionScore`, `gate_tier`, `gate_reason`, `rank_reason`, `selection_summary`

`rank_reason` 태그 예: `high_flow`, `high_rs`, `breakout`, `new_high`, `near_high`, `high_growth`, `turnaround`, `sector_leader`, `high_quality`

**오프라인 검증:**

```bash
# 기본 리플레이 (게이트·감점·Conviction 비중)
python scripts/replay_screener_logic.py output/screener_candidates_full_YYYYMMDD_KOSPI.json

# 거래대금 200억 vs 300억 시나리오 (Amount5D 컬럼 필요, 운영값 변경 없음)
python scripts/replay_screener_logic.py --amount5d-test [풀JSON]
```

**`PIPELINE_SCRIPTS` (의존성 순):**

1. `health_check.py`
2. `news_collector.py` ← 스크리너 JSON
3. `gpt_analyzer.py`
4. `trader.py` ← `gpt_trades_*.json` (`plans` 중 `결정 == "매수"`만 매수)

실패 시 `output/pipeline_state.json`에 저장 후 `STEP_DEPENDENCIES` 기준 **실패 단계부터 재시도** (`MAX_ATTEMPTS`).

#### `gpt_trades_*.json` 스키마 (`gpt_analyzer.py`)

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-06-10T10:15:28+09:00",
  "market": "KOSPI",
  "date": "20260610",
  "plans": [ ... ]
}
```

| `plans[].결정` | 의미 | `trader` 매수 |
|----------------|------|---------------|
| `매수` | 전술 계획까지 통과·매수 추천 | ✅ 실행 |
| `보류` | 전술 분석 후 관망 | ❌ (`integrated_analysis.respect_gpt_hold` 시 후보 제외) |
| `미진입` | **1차 필터 탈락** — 전술 GPT/휴리스틱 미실행 | ❌ (감사·디버그용 로그) |

`미진입` 항목 예시 필드: `단계: "initial_filter"`, `분석`(탈락 사유), `initial_filter`(GPT 1차 `decision`/`reason`), `stock_info`(스크리너 스냅샷).

**1차 필터 탈락 조건 (요약):**

| 조건 | 설명 |
|------|------|
| `NO_NEWS` + Score &lt; 0.65 | 뉴스 없고 점수 낮음 |
| GPT 1차 `보류` | 뉴스·점수 기반 빠른 스크리닝 (`INITIAL_FILTER_PROMPT`) |
| 휴리스틱 | Score &lt; 0.6 이고 뉴스 200자 미만 (OpenAI 미사용·GPT 실패 시) |

**`config.json` → `gpt_params` (주요):**

| 키 | 기본·예시 | 설명 |
|----|-----------|------|
| `openai_model` | `gpt-4o-mini` | OpenAI 모델 |
| `budget_guard` | `true` | 전술 프롬프트에 가용 현금·최대 진입가 주입 |
| `max_entry_price_ratio` | `0.2` | `max_allowed = usable_cash × ratio` — 초과 종목 매수 비권장 |
| `analysis_expansion.enabled` | `true` | 통합 분석 모드(더 많은 후보·결과 허용) |
| `analysis_expansion.max_total_analysis` | `15` | 1차 필터·전술 분석 대상 상한 |
| `analysis_expansion.max_primary_candidates` | `10` | 전술 계획(`매수`/`보류`) 결과 상한 (`미진입`은 별도 카운트) |

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
> `asset_allocation.bond_etf_exclude_from_risk_sell: true`(기본)이면 **459580 등 bond ETF**는 `check_sell_condition` 진입 전 `KEEP` 처리(손절·익절·RSI·EmergencyDrop 모두 스킵).

### 3.4.1 KIS 토큰 (`output/cache/`)

두 Docker 서비스가 **동일 앱키·동일 토큰 파일**을 사용합니다. KIS OAuth는 **1분당 1회** 발급 제한(`EGW00133`)이 있습니다.

| 파일 | 역할 |
|------|------|
| `kis_token.json` | 접근 토큰 캐시 (`expires_in` 기반, 만료 5분 전 갱신) |
| `kis_token.lock` | 컨테이너 간 발급 경합 방지 (fcntl) |

`kis_token.json` 메타: `issued_at`, `expires_at`, `env`, `app_key_hash`(앞 4자)

`api/kis_auth.py` 동작:

- 발급 전 파일락 → 다른 프로세스가 갱신한 토큰 재사용
- `EGW00133` 시 65초 backoff 후 최대 3회 재시도
- **`EGW00123`(서버 토큰 만료) 시 캐시 삭제(`[KIS_TOKEN_INVALIDATED]`) → OAuth 재발급 → 1회 재시도**
- 재인증 쿨다운(60초) 내 API 재발급 대신 파일 토큰 재로드 (**`force_new` 재인증 시 쿨다운 우회**)
- env·app_key 불일치 시 캐시 무효 처리
- 네트워크 오류·EGW00133 시 기존 토큰 파일 **선삭제하지 않음** (성공 시에만 덮어쓰기)

환경 변수: `KIS_TOKEN_BACKOFF_SEC`, `KIS_REAUTH_COOLDOWN_SEC`, `KIS_TOKEN_LOCK_TIMEOUT_SEC`, `KIS_TOKEN_FILE`, `KIS_HEALTHCHECK_ENV`

**헬스체크 실패 트러블슈팅**

1. 로그에 `EGW00123` → 자동 재발급 실패 시: `rm -f output/cache/kis_token.json output/cache/kis_token.lock` 후 컨테이너 **순차** 재시작 (1분 간격, EGW00133 방지)
2. `EGW00133` → 65초 대기 후 재시도
3. env/키 불일치 → `config/.env`의 `KIS_MY_*`(prod) / `KIS_PAPER_*`(vps) 확인
4. 수동 확인: `docker compose exec integrated_manager python /app/src/health_check.py`

### 3.5 주요 산출물 (`output/`)

| 패턴 | 모듈 |
|------|------|
| `screener_*`, `market_state_*` | `screener.py` |
| `collected_news_*` | `news_collector.py` |
| `gpt_trades_*`, `gpt_rotations_*` | `gpt_analyzer.py` (매매 계획·회전 샌드박스 제안) |
| `balance_*`, `summary_*`, `summary_rlz_*` | `account.py` (`inquire-balance` + `inquire-balance-rlz-pl`) |
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
| 매매 기록 | `record_trade()` → `upsert_trade_record_by_order_id()` — `order_id` 있으면 UPSERT. **`executed`/`partial` BUY·SELL은 `order_id` 필수**(모의 `completed` 제외) |
| 손실 집계 | `is_countable_loss_sell()` — `executed` + `order_id` + `profit_loss < 0` 만 연속·일일 손실에 반영. `pending`/`failed`/중복 row 제외 |
| 중복 SELL | `find_recent_sell_duplicate()` — 동일 ticker·qty, 10분 이내, 가격 1% 이내. 기존 `order_id` 행 있으면 `duplicate_sell_without_order_id_skipped` 로그 후 skip |
| 리컨실 (15:22 · 장중 11:00/14:00) | `order_reconciler.py` — `pending`/`partial` + `order_id` 행을 KIS `inquire-orders` / `inquire-daily-ccld`로 동기화. miss 시 **잔고 holding fallback**(BUY: `hldg_qty`/`thdt_buyqty`). 체결 시 `price`·`amount` 갱신 + `structured_context.reconciled_*` + PnL 재계산. summary: `updated_by_order_query` / `updated_by_daily_query` / `updated_by_holding_fallback` / `still_missing_after_all` |
| 거래 기록 분류 | `recorder.classify_trade_record()` — `paper_db_only` \| `kis_paper_order` \| `kis_prod_order`. `reason_code=PAPER_DB_ONLY` 또는 `paper_executed`+무`order_id` = 테스트 기록 |
| orphan backfill | 리컨실 마지막에 자동 실행 — `order_id` 빈 행을 KIS 일별 주문과 **유일 매칭** 시 backfill |
| 수동 backfill | `python /app/src/order_reconciler.py --since-hours 36 --backfill-only` |
| subprocess 종료 | 성공 시 stdout JSON 1줄 (`{"ok": true, "updated": ...}`), 실패 시 exit≠0 |

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
시세·잔고·주문·업종지수 시세(TR `FHKUP03500100`)를 한 경로에서 처리합니다.

| 엔드포인트 | TR_ID | 용도 |
|------------|-------|------|
| `inquire-balance` | TTTC8434R | 잔고·요약 (`nass_amt`, `thdt_*`, `tot_evlu_amt`) |
| `inquire-balance-rlz-pl` | TTTC8494R | **실현손익** (`rlzt_pfls`) 포함 잔고 |
| `inquire-orders` | TTTC8001R | 미체결·부분체결 조회 |
| `inquire-daily-ccld` | TTTC8001R | 일별 주문체결 (`avg_prvs`, `tot_ccld_qty`) |
| `inquire-period-trade-profit` | TTTC8715R | 기간별 매매손익(종목별 검증용) |
| `order-cash` | TTTC0801U/0802U (prod) · **VTTC0801U/0802U** (`kis_paper`/`vps` 모의) | 현금 매수/매도 |

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
| `screener.py` | 종목 스크리닝 CLI (`--market KOSPI`, `--debug`) |
| `screener_core.py` | 7축·RS·Growth Bonus·Breakout Gate·가산/감점·Conviction·`MarketState` |
| `portfolio_allocator.py` | `conviction` / `rank_tier` / equal / score_proportional 목표 비중 (**스크리너·리플레이 전용**, `trader.py`는 import하지 않음) |
| `asset_allocator.py` | 70:20:10 예산 계산 (`compute_allocation`, `stock_buy_budget`, `initial_bond_buy_budget`, `is_bond_etf`) — **주문 실행 없음** |
| `kis_master.py` | KIS `.mst` 마스터 다운로드·캐시 |
| `health_check.py` | KIS 헬스체크(삼성전자 시세) |
| `news_collector.py` | 네이버 뉴스 수집 |
| `gpt_analyzer.py` | GPT/휴리스틱 2단계 분석 → `gpt_trades_*.json` (`매수`/`보류`/`미진입`), 리밸런싱 GPT 헬퍼, 예산 가드 |
| `trader.py` | 매수/매도·체결·분할매수·지연체결(ODNO·side 가드)·연속 손실 체크·pending SELL skip·`asset_allocation` 주문 가드·459580 매수·`[STOCK_BUDGET_USAGE]`·`[REBUY_GUARD]`·`--batch-check-only`·`--sell-only` |

### 기록·정합성·분석

| 파일 | 역할 |
|------|------|
| `recorder.py` | SQLite `trading_data.db` (`trade_records`, `positions`), upsert/backfill, `classify_trade_record`·`is_real_kis_trade_record`, `is_countable_loss_sell`, 중복 SELL 방지 |
| `order_reconciler.py` | KIS 주문 ↔ DB 상태 정합성 + holding fallback + `amount`/`structured_context` 동기화 + orphan `order_id` backfill |
| `kis_rate_limit.py` | 환경별 `kis_limits` RPS·동시성·EGW00201 백오프·인메모리 캐시 |
| `reviewer.py` | 월간 GPT 회고: 승패·매도사유·포트폴리오·gpt_trades 대조 → config 튜닝 |
| `rotation_policy.py` | 회전 매매 공통 정책(최소 보유일·Δscore·비용·1:1 페어·상한) |
| `rotation_manager.py` | 현금 부족 시 회전 시도·시장 동적 임계값·실행 |
| `trader.py` | 슬롯 꽉 참 시 리밸런싱·`run_buy_logic` 회전 한도 관리 |
| `account.py` | KIS 잔고·실현손익 JSON (`balance_*`, `summary_*`, `summary_rlz_*`) |
| `cleanup_output.py` | 오래된 `output/` 정리 (월간) |

### 공통·API

| 파일 | 역할 |
|------|------|
| `settings.py` | `config.json` 로드·기본값 |
| `env_loader.py` | `config/.env` 로드 |
| `utils.py` | KST, 캐시, `extract_kis_official_summary`, `load_summary_rlz_dict`, 개장일 등 |
| `notifier.py` | Discord 웹훅 |
| `strategies.py` | 매도 전략 클래스 |
| `db_debug.py` | DB 디버그 로그 (`DB_RECORD_DEBUG=1`) |
| `api/kis_auth.py` | KIS 인증·토큰 캐시·파일락·EGW00133 backoff |
| `api/domestic_stock/domestic_stock_functions.py` | 시세·잔고·실현손익·주문 REST 래퍼 |

### 4.1 회전 매매 (Rotation)

포트폴리오 **슬롯이 꽉 찼거나** (`max_positions`) **신규 매수 예산이 부족**할 때, 스크리너 점수가 낮은 보유 종목을 매도하고 고득점 후보를 매수하는 **리밸런싱(스왑)** 기능입니다.  
`rotation.enabled`가 `true`일 때만 동작합니다. 실전 적용 전 **`vps`에서 충분히 검증**하세요. **459580(bond ETF) 보유분은 `_convert_holdings` / `lists_to_pairs`에서 회전 대상에서 제외**됩니다.

#### 마스터 스위치

| 설정 | 기본값 | 설명 |
|------|--------|------|
| `rotation.enabled` | config 기본 `true` | `true`일 때만 회전·리밸런싱 실행. `false`이면 슬롯이 꽉 차도 신규 매수만 생략 |

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
| 최소 보유일 | `rotation.min_holding_days` (config 기본 `5`). **미충족 종목은 회전 매도 불가** — `RiskManager` 손절·RSI 익절 등과 동일한 `check_min_holding_period` 사용 |
| bond ETF | `asset_allocation.bond_etfs`(예: 459580) — 회전 매도·GPT 페어에서 **제외** |
| 점수 차이 | `신규 점수 − 보유 점수 ≥ delta_score_min` (기본 `0.12`). `use_dynamic_threshold: true` 시 KOSPI 레짐·변동성에 따라 임계값 조정 |
| 예산 | `(가용 현금 + 매도 예상 대금) × (1 − fee_buffer_pct)` 로 매수 1주 가격 감당 가능 |
| 거래 비용 | `min_profit_rate`, `min_cost_effectiveness` — 순수익·비용 대비 효과 (`screener_core.calculate_net_profit_rotation`) |
| 페어 상한 | `max_pairs_per_run` (기본 `1`) |

리밸런싱 매도 전에는 **스왑 시뮬레이션**(매도 후 현금·슬롯으로 매수 1주 가능 여부)을 통과해야 실제 주문이 나갑니다.

#### 설정 예시 (`config/config.json`)

```json
"rotation": {
  "enabled": true,
  "min_holding_days": 5,
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

### 4.2 자산 배분 (Asset Allocation)

스크리너·GPT·회전 **전략 로직은 변경하지 않고**, `trader.py` **주문 실행 단계**에서만 포트폴리오 가중(70:20:10)을 적용합니다.

#### 마스터 스위치

| 설정 | 설명 |
|------|------|
| `asset_allocation.enabled` | `false`: 기존 파이프라인 회귀 모드. `true`: 예산 가드 + 459580 자동 매수 |
| `trading_environment` | 아래 [4.3 거래 환경](#43-거래-환경-trading_environment) 참고 |

#### 목표 비중 (enabled 시)

| 자산군 | 목표 | 비고 |
|--------|------|------|
| 주식 | 70% | `stock_buy_budget = min(buyable_cash, stock_gap)` |
| 459580 (KODEX CD금리액티브) | 20% | 주식 매수 **이후** 잔여 현금으로 매수 |
| 현금 | 10% (최소 5%) | `min_cash_amount` 미만이면 주식·459580 매수 스킵 |

#### `trader.py` 실행 흐름 (enabled)

```
get_account_info_from_files()
  └─ dynamic_cash_management SKIP (asset_allocation 우선)
run_buy_logic(available_cash)
  └─ _resolve_stock_buy_budget() → [ASSET_ALLOCATION] 로그
  └─ buy_cash = stock_buy_budget (주식·회전·순차 매수 예산)
  └─ (모든 주식 주문 후)
       _buy_bond_etf_if_needed()
         ├─ sleep 1.5s → _load_snapshot() → post_stock_cash
         ├─ final_bond_buy_budget = max(0, min(initial_bond, post_cash − min_cash))
         └─ _order_cash_retry → kis.order_cash (국내주식 현금주문, 장내채권 API 아님)
```

#### 459580 제외 (enabled 시)

| 영역 | 동작 |
|------|------|
| `risk_manager` | `is_bond_etf` → `KEEP` (개별 종목 리스크 매도 스킵) |
| `rotation_manager` / `rotation_policy` | 회전·GPT 페어에서 제외 |
| `gpt_analyzer` | rotation sandbox holdings 루프에서 제외 |
| `_count_stock_holding_slots` | 슬롯 집계에서 제외 |

#### prod fail-safe

`trading_environment=prod` + `asset_allocation.enabled=true` + **`ASSET_ALLOCATION_ALLOW_PROD` 미설정** 시 주문 차단. **`kis_paper`/`vps`에서는 fail-safe 미적용.**

#### 설정 예시 (`config/config.json`)

```json
{
  "trading_environment": "kis_paper",
  "trading_params": {
    "buy_enabled": true,
    "dynamic_cash_management": { "enabled": true }
  },
  "asset_allocation": {
    "enabled": true,
    "stock_target_weight": 0.70,
    "bond_target_weight": 0.20,
    "cash_target_weight": 0.10,
    "min_cash_weight": 0.05,
    "bond_buy_enabled": true,
    "stock_buy_block_when_overweight": true,
    "bond_etf_exclude_from_risk_sell": true,
    "bond_etf_exclude_from_rotation": true,
    "bond_etfs": [
      { "ticker": "459580", "name": "KODEX CD금리액티브(합성)", "target_weight": 1.0 }
    ]
  }
}
```

> **`vps`:** KIS 주문 API 미사용 — DB `paper_executed`만 기록(`paper_db_only`). allocation·예산·로그 검증용.  
> **`kis_paper`:** KIS 모의 API(`openapivts`)로 **실제 모의 주문** 접수·체결. `order_id` 있는 `executed` 기록. `order_reconciler` holding fallback 지원.  
> **`portfolio_allocator.py`**는 `scripts/replay_screener_logic.py` 리플레이 전용이며 **`trader.py`는 import하지 않습니다.**

#### 오프라인 검증

```bash
python3 scripts/replay_asset_allocation.py          # Case 1–7 (KIS 호출 없음)
python3 scripts/dry_run_asset_allocation.py --scenario all   # [ASSET_ALLOCATION] 로그 포맷
bash scripts/check_vps_deploy_ready.sh              # vps 배포 전 점검
bash scripts/phase6_regression.sh                   # enabled=false 회귀 (config 백업 후)
```

### 4.3 거래 환경 (`trading_environment`)

`config.json`의 `trading_environment`는 **주문 실행 경로**를 결정합니다. 스크리너·GPT·회전 전략은 동일합니다.

| 모드 | KIS 엔드포인트 | 주문 API | DB 기록 | 용도 |
|------|----------------|----------|---------|------|
| `vps` | `openapivts` | **차단** (`paper_db_only`) | `paper_executed`, `order_id` 없음 | allocation·예산·파이프라인 오프라인 검증 |
| `kis_paper` | `openapivts` | **활성** (VTTC0801U/0802U) | `pending` → 리컨실 → `executed` + `order_id` | **KIS 모의계좌 E2E** (권장 검증 단계) |
| `prod` | `openapi` | **활성** (TTTC0801U/0802U) | 실계좌 주문·체결 | 실전 (fail-safe·`ASSET_ALLOCATION_ALLOW_PROD` 필수) |

**권장 검증 순서:** `vps`(로직) → **`kis_paper`(모의 API E2E)** → `prod`(실계좌, 의도적 플래그 후)

| 플래그 (`trader.py`) | `vps` | `kis_paper` | `prod` |
|----------------------|-------|-------------|--------|
| `kis_order_api_enabled` | false | true | true |
| `is_paper_db_only` | true | false | false |
| `is_prod_order_api` | false | false | true |

**`kis_paper` 리컨실:** KIS `inquire-orders` / daily 조회가 0건이어도 계좌 `balance_*.json`의 `hldg_qty`·`thdt_buyqty`로 BUY pending을 `executed`로 보정할 수 있습니다 (`[RECONCILE_BY_HOLDING]`).

**거래 기록 구분 (`[TRADE_RECORD_CLASSIFY]`):**

```
id=85  ticker=459580 status=paper_executed order_id=-  classification=paper_db_only   # vps 테스트
id=87  ticker=459580 status=executed     order_id=... classification=kis_paper_order  # 실제 모의 주문
```

**매수 종료 시 관측 로그 (`kis_paper`/`prod`):**

```
[STOCK_BUDGET_USAGE] total_assets=... stock_buy_budget=... gpt_buy_count=... final_buy_count=...
  executed_order_amount=... unused_stock_budget=... unused_reason=insufficient_gpt_buy_candidates_and_per_ticker_weight_limit
[REBUY_GUARD] ticker=004170 ... decision=skip reason=rebuy_disabled
[BUY_SELECTION_SUMMARY] candidates=... gpt_buy=... blocked_rebuy=... final_buy=... reason=...
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
| `config/config.json` | ✅ | `trading_environment`: **`vps` → `kis_paper` 권장 순 검증** 또는 `prod` (실계좌) |
| `output/` | 자동 | 런타임 전용 (Git 제외) |

### 6.2 API별 설정 (`config/.env`)

#### KIS Open API — 필수

| 변수 | 설명 |
|------|------|
| `KIS_PAPER_APP`, `KIS_PAPER_SEC` | **모의** App Key / Secret (`vps` / **`kis_paper`** 시 사용) |
| `KIS_MY_PAPER_STOCK` | **모의** 계좌 (8자리) |
| `KIS_MY_APP`, `KIS_MY_SEC`, `KIS_MY_ACCT_STOCK` | **실전** (`trading_environment=prod` 시 사용) |

`kis_paper`와 `vps`는 동일한 모의 API 키·계좌를 쓰지만, **`kis_paper`만 KIS 주문 API를 호출**합니다.

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

#### asset_allocation / 모의투자 검증 — 금지·필수

| 항목 | 모의 검증 시 |
|------|-------------|
| `trading_environment` | **`vps`**(로직만) 또는 **`kis_paper`**(모의 API E2E, **권장**) |
| `asset_allocation.enabled` | `true`로 검증 시 설정 |
| `ASSET_ALLOCATION_ALLOW_PROD` | **설정 금지** (prod 실계좌 주문 허용 플래그) |
| `trading_params.buy_enabled` | **`true`** (매수·459580 경로 검증) |
| `kis_limits` | `kis_paper` 기본 `max_rps=1`, `screener_workers=1` — EGW00201 완화 |

### 6.3 단계별 API 의존성

| 시각·구분 | 스크립트 | API |
|-----------|----------|-----|
| 09:00 | 잔액 스냅샷 (`integrated_manager`) | KIS |
| 09:10 | `screener.py` | KIS |
| 10:15~ | `health_check` → `news` → `gpt` → `trader` | KIS, Naver, OpenAI(선택) |
| 장중 11:00/14:00 | `order_reconciler` (경량) | KIS |
| 장중 | `risk_manager` | KIS |
| 15:20 | `trader.py --batch-check-only` | KIS |
| 15:22 | `order_reconciler` | KIS (`inquire-orders`, `inquire-daily-ccld`) |
| 15:30 | 잔액 스냅샷 (`integrated_manager` + `account.py`) | KIS (`inquire-balance`, `inquire-balance-rlz-pl`) |
| 15:35 | `send_daily_trading_summary` | Discord (선택) |
| 월 1회 | `reviewer`, `cleanup_output` | DB·로컬 파일 |

### 6.4 체크리스트

- [ ] `config/.env` 생성 · Git에 올리지 않기
- [ ] `KIS_PAPER_*` / `KIS_MY_PAPER_STOCK` 입력
- [ ] `trading_environment` = **`vps`**(로직) → **`kis_paper`**(모의 API E2E) 순 검증
- [ ] `bash scripts/check_vps_deploy_ready.sh` 통과
- [ ] `python3 scripts/replay_asset_allocation.py` 7/7 PASS
- [ ] Naver 키 입력
- [ ] (선택) OpenAI · Discord 웹훅
- [ ] `docker compose config`로 env 파싱 확인
- [ ] 모의투자 1회 이상 E2E 확인 (`kis_paper` + `trader.py` + `[STOCK_BUDGET_USAGE]` / `[ASSET_ALLOCATION]` 로그)
- [ ] `order_reconciler.py` 실행 — `still_missing_after_all=0`, holding fallback 동작 확인
- [ ] prod 전환 전: `kis_paper` 2주 모니터링 → `ASSET_ALLOCATION_ALLOW_PROD=1` (의도적) → `prod`

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

`config/config.json`에서 `trading_environment`를 확인하세요. 처음에는 **`vps`**(로직) → **`kis_paper`**(모의 API) 순을 권장합니다.  
`asset_allocation` 검증 시 `enabled: true` + **`kis_paper`**(또는 `vps`) 조합을 사용하고, **`ASSET_ALLOCATION_ALLOW_PROD`는 설정하지 마세요.**

```bash
# 배포 전 점검 (호스트)
bash scripts/check_vps_deploy_ready.sh
python3 scripts/replay_asset_allocation.py
python3 scripts/dry_run_asset_allocation.py --scenario all
```

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

# 스크리너 로직 오프라인 리플레이 (게이트·감점·Conviction 비중·Amount5D 시나리오)
python scripts/replay_screener_logic.py output/screener_candidates_full_YYYYMMDD_KOSPI.json
python scripts/replay_screener_logic.py --amount5d-test output/screener_candidates_full_YYYYMMDD_KOSPI.json

# 파이프라인 단계만 (스크리너 결과가 output/에 있어야 함)
docker compose exec integrated_manager python -u /app/src/health_check.py
docker compose exec integrated_manager python -u /app/src/news_collector.py
docker compose exec integrated_manager python -u /app/src/gpt_analyzer.py --market KOSPI --slots 3
docker compose exec integrated_manager python -u /app/src/trader.py

# 체결 확인·리컨실 (pending/partial 갱신 + orphan order_id backfill)
docker compose exec integrated_manager python -u /app/src/trader.py --batch-check-only

# 매도만 (risk_manager direct_execute fallback과 동일)
docker compose exec background_risk_manager python -u /app/src/trader.py --sell-only

# KIS 토큰 상태 확인 (config의 trading_environment에 맞게 env 지정)
docker compose exec background_risk_manager python -c "
from api.kis_auth import KIS
k = KIS(env='vps')
print('token ok:', bool(k.auth_token))
"

# asset_allocation 단위·dry-run (장외 가능)
docker compose exec integrated_manager python /app/scripts/replay_asset_allocation.py
docker compose exec integrated_manager python /app/scripts/dry_run_asset_allocation.py --scenario all

# Phase 6 회귀 (enabled=false 비교 시 config 백업 후)
bash scripts/phase6_regression.sh

# trader 로그에서 allocation 확인 (장중 09:10–14:20)
docker compose exec integrated_manager python -u /app/src/trader.py 2>&1 | tee output/phase6_trader_vps.log
grep -E 'ASSET_ALLOCATION|459580|dynamic_cash' output/phase6_trader_vps.log

docker compose exec integrated_manager python -u /app/src/order_reconciler.py --since-hours 6 --limit 20

# 리컨실·분류·예산 로그 확인
docker compose exec integrated_manager python -u /app/src/trader.py 2>&1 | tee output/trader_run.log
grep -E 'STOCK_BUDGET_USAGE|REBUY_GUARD|BUY_SELECTION_SUMMARY|TRADE_RECORD_CLASSIFY|RECONCILE_BY_HOLDING' output/trader_run.log

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
| `KIS_HEALTHCHECK_ENV` | 헬스체크 KIS env (기본 `prod`) |
| `DISCORD_WEBHOOK_URL_RISK` | 리스크 매니저 전용 Discord 웹훅 |
| `ASSET_ALLOCATION_ALLOW_PROD` | **`1`/`true`일 때만** prod + `asset_allocation.enabled` 주문 허용. 모의 검증 중 **설정 금지** |

### 7.5 Phase 6 검증 (asset_allocation)

| 구분 | 명령 | 장중 | 장외 |
|------|------|:----:|:----:|
| 배포 준비 | `bash scripts/check_vps_deploy_ready.sh` | ✅ | ✅ |
| allocation 단위 | `python3 scripts/replay_asset_allocation.py` | ✅ | ✅ |
| 로그 dry-run | `python3 scripts/dry_run_asset_allocation.py --scenario all` | ✅ | ✅ |
| enabled=false 회귀 | `bash scripts/phase6_regression.sh` | ✅ | ✅ |
| 스크리너 리플레이 | `python3 scripts/replay_screener_logic.py <json>` | ✅ | ✅ |
| trader E2E | `docker compose exec … python -u /app/src/trader.py` | ✅ 09:10–14:20 | ❌ |
| `--batch-check-only` | 15:20 KST | ✅ 15:20 | ❌ |

**장중 trader 검증 시 기대 로그 (`kis_paper` + `asset_allocation.enabled=true`):**

```
env=kis_paper, kis_order_api_enabled=True
[ASSET_ALLOCATION] … stock_buy_budget: … initial_bond_buy_budget: …
[STOCK_BUDGET_USAGE] … unused_reason=…
[REBUY_GUARD] ticker=… decision=skip reason=rebuy_disabled
[BUY_SELECTION_SUMMARY] … blocked_rebuy=… final_buy=…
[ASSET_ALLOCATION_POST_STOCK_ORDER] … post_stock_cash … final_bond_buy_budget …
```

**`kis_paper` E2E 후 리컨실:**

```
[RECONCILE_BY_HOLDING] order_id=… ticker=… status=executed
리컨실 결과: {…, 'updated_by_holding_fallback': N, 'still_missing_after_all': 0}
```

**`vps` + enabled 시:** KIS 주문 없이 `[모의]` paper 로그만. `[STOCK_BUDGET_USAGE]`는 동일하게 출력.

**enabled=false 회귀 시:** 위 `[ASSET_ALLOCATION]*` 로그 **없음**, `dynamic_cash_management` 적용 로그 **있음**.

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
│   ├── summary_rlz_*.json       # KIS 실현손익 스냅샷 (account.py, 런타임)
│   └── daily_balances/          # balance_open_*.json, balance_close_*.json (런타임)
├── src/
│   ├── api/
│   │   ├── kis_auth.py
│   │   └── domestic_stock/
│   │       └── domestic_stock_functions.py
│   ├── asset_allocator.py         # 70:20:10 예산 계산 (trader 연동)
│   ├── kis_rate_limit.py          # KIS RPS·EGW00201 백오프·캐시
│   ├── portfolio_allocator.py     # 스크리너 리플레이 전용 (trader 미사용)
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
├── scripts/
│   ├── replay_screener_logic.py   # 게이트·가산/감점·Conviction·비중 오프라인 검증
│   ├── replay_asset_allocation.py # asset_allocator Case 1–7
│   ├── dry_run_asset_allocation.py# [ASSET_ALLOCATION] 로그 dry-run (KIS 없음)
│   ├── check_vps_deploy_ready.sh  # vps + asset_allocation 배포 전 점검
│   └── phase6_regression.sh       # enabled=false 회귀 테스트
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
* 실전 계좌에 연결하기 전 **`vps` → `kis_paper`(KIS 모의 API) 순으로 충분히 테스트**하고, 본인의 투자 성향·자금·리스크 허용 범위를 스스로 판단하시기 바랍니다.
* 제3자 API(KIS, Naver, OpenAI, Discord) 이용 시 각 서비스의 **이용약관·요금·호출 한도**를 준수해야 합니다.

**본 코드를 사용함으로써, 위 내용을 이해하고 이에 동의한 것으로 간주합니다.**
