# SystemTradingS3 시스템 현황 리포트

작성일: 2026-05-29

범위: MVP0-MVP9 핵심 구현, 로컬 대시보드, 데모 run artifact, Yahoo Finance 다운로드 스크립트, drop-in US tech 100 synthetic dataset/config까지 포함한 현재 저장소 상태.

## 요약

SystemTradingS3는 이제 설계 초안이 아니라 실행 가능한 시뮬레이션 트레이딩 스켈레톤이다. 현재 흐름은 다음과 같다.

```text
fixture/config
  -> audit context
  -> simulate
  -> exported run artifacts
  -> audit self-check
  -> validate_run accounting replay
  -> metrics
  -> optional local dashboard
```

핵심 강점은 책임 분리다.

- `audit`: output-shaped dataset 구조 감사
- `simulate`: 시뮬레이션 실행과 artifact 생성
- `validate_run`: 생성된 artifact의 독립 회계 재검증
- `metrics`: artifact 기반 사후 지표 계산

이 구조 덕분에 live trading, broker integration, optimization, ML 없이도 재현 가능한 신뢰층이 생겼다. 시스템은 계속 profit promise를 하지 않는 방향을 유지해야 한다.

## 저장소 상태

현재 브랜치:

- `master`
- `origin/master` 기준 `8eeaac9`에서 drop-in simulator 확장 작업 진행 중
- 기준 HEAD: `8eeaac9`

주요 커밋 흐름:

- `20911cf` - MVP0 dataset audit baseline
- `3c6c3ff` - MVP1-MVP3 simulation baseline
- `35ee4b8` - MVP4 config-driven strategy registry
- `a5e38cf` - MVP5 friction and metrics
- `7083d47` - MVP6 benchmark logging and relative metrics
- `a3efdc8` - MVP7 multi-symbol architecture
- `77eb2d4` - MVP8 target-weight rebalancing
- `25e198d` - MVP9 factor ingestion and periodic rebalancing
- `41dec64` - dashboard and demo run artifacts
- `8eeaac9` - stability pass for dashboard/downloader/manifest/tests

## 구현된 계층

### MVP0: Dataset Audit

실행:

```powershell
rtk python -m system_trading_s3.audit <dataset_dir>
```

주요 파일:

- `system_trading_s3/audit.py`
- `schemas/*.schema.json`

역할:

- `equity_curve.csv`, `trades.csv` 형태의 dataset을 감사한다.
- `PASS`, `INCONCLUSIVE`, `FAIL`을 구분한다.
- 선택적 benchmark/factor/cost 누락은 실패가 아니라 gap으로 보고한다.
- read-only로 동작한다.
- 성과 지표나 trades/equity 재무 일관성은 계산하지 않는다.

### MVP1-MVP2: Simulation and Export

실행:

```powershell
rtk python -m system_trading_s3.simulate <dataset_dir>
rtk python -m system_trading_s3.simulate <dataset_dir> --export-dir <run_dir> --run-id <id>
```

주요 파일:

- `system_trading_s3/simulate.py`

핵심 컴포넌트:

- `DataFeed`
- `BenchmarkFeed`
- `SimulatedAccount`
- `ExecutionSimulator`
- `PortfolioRebalancer`
- `SimulationEngine`
- strategy registry

시뮬레이션 입력:

- `market_prices.csv` 또는 정렬된 `*_prices.csv`
- 선택적 `benchmark_prices.csv`
- 선택적 `factors.csv`
- 선택적 JSON config

생성 artifact:

- `run_manifest.json`
- `equity_curve.csv`
- `trades.csv`
- `orders.csv`
- `fills.csv`
- `account_summary.json`
- `audit_summary.json`

현재 `valid_multisymbol` + `sample_config.json` 실행 결과:

- simulation status: `PASS`
- strategy: `PeriodicFactorWeight`
- initial cash: `1000`
- final cash: `26.5660`
- final positions: `BBB:18`
- order count: `3`
- fill count: `3`
- final equity: `1016.5660`

### MVP3: Run Artifact Validation

실행:

```powershell
rtk python -m system_trading_s3.validate_run <run_artifact_dir>
```

주요 파일:

- `system_trading_s3/validate_run.py`

역할:

- 필수 artifact 존재 여부 확인
- manifest/account summary 정합성 확인
- order/fill 참조 관계 확인
- `Decimal` 기반 fill replay
- final cash, positions, equity 검증
- audit summary 상태 검증

현재 `runs/demo-run` 검증 결과는 `PASS`다.

### MVP4-MVP9: Strategy and Factor-Aware Simulation

현재 등록된 전략:

- `BuyAndHold`
- `MovingAverageCross`
- `EqualWeightRebalance`
- `PeriodicFactorWeight`

현재 샘플 설정:

- `tests/fixtures/sample_config.json`
- `configs/strategies/periodic_momentum_top10.json`
- `configs/strategies/equal_weight_rebalance.json`
- `configs/strategies/buy_and_hold_tech001.json`
- `configs/strategies/moving_average_tech001.json`

현재 구조:

- `PeriodicFactorWeight`는 `factors.csv`에서 factor 값을 읽는다.
- 설정된 tick 간격마다 리밸런싱한다.
- 특정 factor 기준 상위 K개 심볼을 고른다.
- target weight를 반환한다.
- 실제 수량 산정과 현금 제약은 `PortfolioRebalancer`가 담당한다.

이 분리는 유지해야 한다.

- Strategy는 target exposure만 결정한다.
- Rebalancer는 integer-share order를 계산한다.
- Execution/accounting은 fill과 계좌 상태 변경을 독점한다.

### Integrated Drop-In Simulator

새로 추가된 기준선:

- `datasets/us_tech_100_simulated`
- `configs/strategies/*.json`
- `docs/integrated-simulator.md`
- `scripts/generate_simulated_universe.py`

`datasets/us_tech_100_simulated`는 100개 synthetic US tech-style symbol을 가진 과거형 dataset이다. 실제 시장 데이터가 아니라 deterministic fixture다.

검증된 실행:

```powershell
rtk python -m system_trading_s3.simulate datasets/us_tech_100_simulated --config configs/strategies/periodic_momentum_top10.json
```

현재 결과:

- simulation status: `PASS`
- strategy: `PeriodicFactorWeight`
- final equity: `101983.711395`
- order count: `16`
- fill count: `16`

이 계층의 목적은 "전략 코드를 고치지 않고 dataset/config를 drop-in하여 실행"하는 것이다.

### MVP5-MVP6: Metrics

실행:

```powershell
rtk python -m system_trading_s3.metrics <run_artifact_dir>
```

주요 파일:

- `system_trading_s3/metrics.py`

현재 계산 지표:

- total return percent
- 252 trading days/year 기준 row-interval CAGR
- max drawdown
- 사용 가능한 `realized_pnl` 기준 win rate
- profit factor
- total trade count
- alpha
- beta
- Sharpe ratio
- tracking error
- information ratio

해석 주의:

- 이 값들은 재현 가능한 계산 결과일 뿐, 금융적으로 의미 있는 성과 증거가 아니다.
- fixture가 3개 equity row뿐이라 CAGR, Sharpe 같은 연율화 지표가 과장되어 보일 수 있다.
- 이 시스템은 어떤 전략도 수익성 있다고 주장하면 안 된다.

## 추가된 편의 계층

### Dashboard

파일:

- `dashboard/server.py`
- `dashboard/index.html`

역할:

- `http://localhost:8000`에서 실행되는 로컬 HTTP 대시보드
- `runs/` 아래 저장된 run 목록 조회
- artifact 상세 조회
- `/api/simulate`를 통한 새 시뮬레이션 실행
- export artifact와 metrics 생성

안정화 반영:

- README의 `No dashboard` 모순을 `No production, hosted, or broker-connected dashboard`로 수정했다.
- README의 "zero-dependency dashboard" 표현을 수정했다.
- Python server는 stdlib 기반이지만, 브라우저 UI는 Chart.js, Lucide, Google Fonts CDN을 쓴다고 명시했다.
- dashboard API smoke test를 추가했다.
- `/api/runs`가 `dataset_dir`과 `audit_summary.json`의 실제 필드를 읽도록 정리했다.
- `datasets/`와 `configs/strategies/`의 drop-in object를 dashboard에서 선택할 수 있게 했다.
- 선택한 전략 config를 dashboard에서 JSON으로 직접 수정해 실행할 수 있게 했다.

남은 gap:

- UI 렌더링/브라우저 상호작용 테스트는 없다.
- `index.html`은 CDN에 의존하므로 오프라인 재현성은 없다.
- dashboard는 같은 `run_id`에 대해 `runs/<run_id>`를 overwrite한다.

### Demo Run Artifacts

파일:

- `runs/demo-run/*`

역할:

- 완성된 MVP9 run artifact 예시

현재 상태:

- `validate_run runs/demo-run`: `PASS`
- `metrics runs/demo-run`: `PASS`
- `audit runs/demo-run`: 선택적 `benchmark.csv`, `factor_exposure.csv` 누락으로 `INCONCLUSIVE`

안정화 반영:

- `run_manifest.json`의 오래된 `"one symbol only"` assumption을 제거했다.
- 새 assumption은 `"multi-symbol portfolio accounting with forward-filled prices"`다.

### Data Download Script

파일:

- `scripts/download_data.py`

의도:

- Yahoo Finance에서 가격, benchmark, momentum factor 데이터를 내려받아 simulator-ready fixture를 만든다.

안정화 반영:

- `--help`가 optional dependency 없이 실행되도록 import 순서를 고쳤다.
- `args.output-dir` 버그를 `args.output_dir`로 수정했다.
- simulator가 요구하는 ISO datetime 형태인 `YYYY-MM-DDT00:00:00`을 출력하도록 수정했다.
- `yfinance`, `pandas`는 core dependency가 아니라 실제 다운로드 실행 시에만 필요한 optional dependency로 남겼다.

남은 gap:

- 실제 다운로드 실행은 이 환경에 optional dependency가 없어 검증하지 않았다.
- 다운로드된 장기 historical dataset에 대한 fixture/test는 아직 없다.

## 검증 증거

안정화 중 확인한 명령:

```powershell
rtk python -m unittest discover -s tests
rtk python -m system_trading_s3.simulate tests/fixtures/valid_multisymbol --config tests/fixtures/sample_config.json
rtk python -m system_trading_s3.validate_run runs/demo-run
rtk python -m system_trading_s3.audit runs/demo-run
rtk python -m system_trading_s3.metrics runs/demo-run
rtk python scripts/download_data.py --help
```

결과:

- unit tests: `98 tests OK`
- MVP9 sample simulation: `PASS`
- US tech 100 simulated dataset run: `PASS`
- demo run validation: `PASS`
- demo run audit: `INCONCLUSIVE`, optional gap만 있음
- demo run metrics: `PASS`
- download script help: optional dependency 없이 실행 가능

## 아키텍처 강점

1. 신뢰 경계가 명확하다.

   `audit`, `simulate`, `validate_run`, `metrics`가 서로 다른 책임을 갖고 있어 한 계층의 오류가 다른 계층에서 잡힐 가능성이 높다.

2. Artifact가 재현 가능하다.

   run artifact 세트가 작고 deterministic하다. 회귀 테스트와 비교 검증의 기반으로 좋다.

3. 회계가 `Decimal` 기반이다.

   cash, fill, fee, slippage, equity 계산에서 binary float drift를 피한다.

4. Strategy와 state mutation이 분리되어 있다.

   전략은 order intent 또는 target weight만 반환한다. 실제 계좌 변경은 engine/account가 담당한다.

5. Factor-aware 방향이 실제 코드로 들어왔다.

   factor data가 `MarketState`로 흐르고, `PeriodicFactorWeight`가 주기적 cross-sectional factor rebalancing을 증명한다.

## 남은 안정화 과제

### P1: Dashboard UI 테스트 부재

현재는 HTTP API smoke test만 있다.

권장:

- 다음 단계에서 브라우저 기반 수동/자동 smoke test를 추가한다.
- 최소 확인 항목: run list 표시, demo-run 상세 표시, 새 simulation 실행 버튼.

### P1: Historical downloader 실사용 검증 필요

현재는 `--help`와 코드 컴파일만 검증했다.

권장:

- optional dependency가 있는 환경에서 작은 날짜 범위로 실제 다운로드 smoke test를 수행한다.
- 생성된 dataset이 `simulate`에 바로 들어가는지 확인한다.

### P2: Metrics 오해 가능성

작은 fixture의 연율화 지표는 과장되어 보일 수 있다.

권장:

- equity row 수가 적을 때 warning/gap을 표시한다.
- 모든 문구는 profit-neutral하게 유지한다.

## 다음 방향성 논의 후보

안정화 이후 MVP10 후보는 세 가지다.

1. Factor-aware reporting

   현재 factor로 리밸런싱은 하지만, 결과가 의도한 factor exposure에 맞았는지 설명하는 리포트는 없다. 가장 제품 thesis에 가깝다.

2. Loss classification groundwork

   normal factor-driven loss, excessive relative loss, execution loss, data/system error loss 같은 분류 체계를 artifact 기반으로 만들 수 있다. 단, 너무 빨리 넓히면 복잡도가 커진다.

3. Risk rule engine v0

   position/exposure/cash/cooldown/kill switch 같은 base rule을 engine 앞단에 넣는다. 실제 시스템 트레이딩 MVP로 가려면 중요하지만, factor reporting보다 구현 범위가 크다.

추천은 1번이다. 다음 제품 질문은 "수익이 났나?"가 아니라 아래여야 한다.

```text
이 run은 의도한 market/factor exposure에 맞게 움직였는가?
설명 가능한 factor-driven movement와 설명 불가능한 손실을 구분할 준비가 되었는가?
```
