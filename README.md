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
- **장중 리스크** — `background_risk_manager` 컨테이너에서 ATR·스윙저점·RSI·전략 믹서 기반 매도
- **주문 정합성** — `order_reconciler.py`로 DB pending/partial ↔ KIS 체결 동기화
- **월간 튜닝** — `reviewer.py` 성과 분석 후 `config.json` 파라미터 미세 조정(매월 1회 스케줄)
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
│  output/  ← screener_*.json, gpt_trades_*.json, trading_log.db, cache/  │
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
| 09:00 | 장시작 잔액 | `account.py` |
| 09:10 | 스크리너 | `screener.py` |
| 10:15 | 매매 파이프라인 | `health_check` → `news_collector` → `gpt_analyzer` → `trader` |
| 15:20 | 일괄 체결 확인 | `trader.py --batch-check-only` |
| 15:22 | 주문 정합성 | `order_reconciler.py` |
| 15:30 | 장종료 잔액 | `account.py` |
| 15:35 | 일일 요약 | Discord (`send_daily_trading_summary`) |

- 휴장일: 스크리너·파이프라인 스킵 (`is_market_open_day`)
- **월간 유지보수:** 매일 점검 후 `monthly_maintenance.day`(기본 1일)에 1회 — `reviewer.py` → `cleanup_output.py` (기본 16:00)

### 3.3 스크리너 vs 매매 파이프라인

스크리너는 **파이프라인 밖** 별도 스케줄 잡입니다. 당일 `screener_candidates_*.json` 등을 만든 뒤 파이프라인이 읽습니다.

```
[09:10 screener]                         [10:15 pipeline]
screener.py                              health_check.py
  → screener_candidates_*.json             → news_collector.py
  → screener_scores_*.json                   → gpt_analyzer.py
  → market_state_*.json                      → trader.py → recorder → trading_log.db
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
| 로직 | `risk_manager.RiskManager` + `strategies.StrategyMixer` |
| 매도 | `risk_params.auto_sell.direct_execute` 시 KIS 직접, 아니면 `trader.py` subprocess |
| 알림 | `DISCORD_WEBHOOK_URL_RISK` (없으면 `DISCORD_WEBHOOK_URL`) |

### 3.5 주요 산출물 (`output/`)

| 패턴 | 모듈 |
|------|------|
| `screener_*`, `market_state_*` | `screener.py` |
| `collected_news_*` | `news_collector.py` |
| `gpt_trades_*` | `gpt_analyzer.py` |
| `balance_*`, `daily_balances/`, `summary_*` | `account.py` / 통합 매니저 |
| `trading_log.db` | `recorder.py` |
| `pipeline_state.json`, `monthly_maintenance_state.json` | `integrated_manager.py` |
| `cache/` (토큰, `.mst`, `.pkl` 등) | KIS·스크리너 |

Git에는 `output/.gitkeep`만 추적합니다.

### 3.6 KIS API 계층

`api/kis_auth.KIS` → `api/domestic_stock/domestic_stock_functions.DomesticStock`  
시세·잔고·`order_cash()` 주문·업종지수 시세(TR `FHKUP03500100`)를 한 경로에서 처리합니다.

---

## 4. 모듈 설명

### 오케스트레이션

| 파일 | 역할 |
|------|------|
| `integrated_manager.py` | 스케줄 등록, subprocess 파이프라인, 잔액·요약·리컨실, 파이프라인 상태 복구 |
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
| `trader.py` | 매수/매도·체결·분할매수·`--batch-check-only` |

### 기록·정합성·분석

| 파일 | 역할 |
|------|------|
| `recorder.py` | SQLite `trading_log.db` |
| `order_reconciler.py` | KIS 주문 ↔ DB 상태 정합성 |
| `reviewer.py` | FIFO 손익·승률·`config.json` 자동 튜닝 (월간) |
| `rotation_manager.py` | 회전 매매 (`rotation.enabled`) |
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
| `api/kis_auth.py` | KIS 인증·토큰 캐시 |
| `api/domestic_stock/domestic_stock_functions.py` | 시세·주문 REST 래퍼 |

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
| DB | SQLite (`output/trading_log.db`) |
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
| 09:00 | `account.py` | KIS |
| 09:10 | `screener.py` | KIS |
| 10:15~ | `health_check` → `news` → `gpt` → `trader` | KIS, Naver, OpenAI(선택) |
| 장중 | `risk_manager` | KIS |
| 15:22 | `order_reconciler` | KIS |
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

# 체결 확인·리컨실
docker compose exec integrated_manager python -u /app/src/trader.py --batch-check-only
docker compose exec integrated_manager python -u /app/src/order_reconciler.py --since-hours 36 --limit 800
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
| `DB_RECORD_DEBUG` | `1` 시 DB 디버그 로그 |
| `SCREENER_TIMEOUT_SEC` | 스크리너 subprocess 타임아웃(초) |
| `SCRIPT_TIMEOUT_SEC` | 기타 스크립트 타임아웃 |

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
│   └── .gitkeep                 # 런타임 산출물 루트 (Git 제외)
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
│   ├── rotation_manager.py / account.py / strategies.py
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
