# SystemTradingS3 시스템 현황 리포트

작성일: 2026-05-29

범위: MVP0-MVP9 핵심 구현, 로컬 대시보드, 데모 run artifact, Yahoo Finance 다운로드 스크립트, drop-in US tech 100 synthetic dataset/config, dashboard 전략 form 편집까지 포함한 현재 저장소 상태.

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
  -> factor_report
  -> factor_attribution
  -> factor_risk_model
  -> loss_classification
  -> optional local dashboard
```

핵심 강점은 책임 분리다.

- `audit`: output-shaped dataset 구조 감사
- `simulate`: 시뮬레이션 실행과 artifact 생성
- `validate_run`: 생성된 artifact의 독립 회계 재검증
- `metrics`: artifact 기반 사후 지표 계산
- `factor_report`: fill과 factor data를 연결해 buy-side factor exposure 정렬 상태를 보고
- `factor_attribution`: holdings/fills/prices/factor ranks 기반 factor-return proxy와 PnL reconciliation 보고
- `factor_risk_model`: 충분한 complete observations와 복수 factor가 있을 때만 deterministic OLS risk model 보고

이 구조 덕분에 live trading, broker integration, optimization, ML 없이도 재현 가능한 신뢰층이 생겼다. 시스템은 계속 profit promise를 하지 않는 방향을 유지해야 한다.

## 저장소 상태

현재 브랜치:

- `master`
- `origin/master`와 동기화됨
- 최신 안정화 baseline: `eb19e0e`
- 이 리포트 자체가 커밋되면 HEAD는 더 앞선 문서 커밋이 된다.

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
- `7054db3` - drop-in historical-style US tech 100 dataset/config/dashboard selection
- `ded9eec` - dashboard strategy catalog and form-driven config editing
- `eb19e0e` - dashboard/downloader/metrics stabilization closure
- latest - factor-aware buy-side exposure reporting

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

### MVP10: Factor-Aware Reporting

실행:

```powershell
rtk python -m system_trading_s3.factor_report <run_artifact_dir>
```

주요 파일:

- `system_trading_s3/factor_report.py`

역할:

- exported `fills.csv`와 source dataset의 `factors.csv`를 읽는다.
- 각 buy fill 시점까지 factor를 forward-fill하여 해당 심볼의 factor 값을 확인한다.
- factor별 average buy factor value, average/best/worst buy factor rank, top-rank buy count, missing factor count를 기록한다.
- `factor_report.json`을 deterministic하게 쓴다.
- 수익성 평가가 아니라 의도한 factor exposure에 맞게 매수되었는지를 확인하는 얇은 리포트다.

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
- `/api/strategies`가 registered strategy catalog와 editable parameter metadata를 제공한다.
- dashboard modal에서 전략을 선택하고 파라미터를 입력하면 run에 사용할 `config_json`이 자동 생성된다.
- JSON textarea는 advanced override로 남아 있어 파일을 수정하지 않고도 run별 전략 변경이 가능하다.

남은 gap:

- 브라우저 수동 smoke는 수행했지만, CI에 묶인 browser automation test는 아직 없다.
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
- `audit runs/demo-run`: `PASS` after optional `benchmark.csv` and `factor_exposure.csv` exports are present

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
rtk python -m py_compile dashboard/server.py scripts/generate_simulated_universe.py scripts/download_data.py system_trading_s3/simulate.py
rtk git diff --check
```

결과:

- unit tests: `99 tests OK`
- MVP9 sample simulation: `PASS`
- US tech 100 simulated dataset run: `PASS`
- demo run validation: `PASS`
- demo run audit: `INCONCLUSIVE`, optional gap만 있음
- demo run metrics: `PASS`
- factor report: `PASS` when source `factors.csv` is available; `INCONCLUSIVE` when factor data is missing
- download script help: optional dependency 없이 실행 가능
- py_compile: `PASS`
- diff whitespace check: `PASS`
- dashboard `/api/strategies`: `PASS`
- dashboard strategy form browser smoke: `PeriodicFactorWeight` 선택 시 `factor_name`, `rebalance_interval`, `top_k` 입력과 deterministic JSON 생성 확인

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

## 안정화 완료 내역

### Dashboard UI 자동 계약 테스트

기존에는 HTTP API smoke와 수동 browser smoke만 있었다. 현재는 CI에서 dashboard HTML/JS 계약도 검사한다.

닫힌 항목:

- `/api/strategies`가 registered strategy catalog를 반환하는지 검사한다.
- `/` HTML에 `strategySelect`, `strategyParamFields`, `strategyConfigJson`가 포함되는지 검사한다.
- form-driven JSON 생성 함수가 HTML payload에서 빠지지 않는지 검사한다.

### Downloader 출력 계약 smoke

현재 환경에는 `yfinance`가 없으므로 실제 Yahoo 네트워크 다운로드를 CI 안정화 조건으로 삼지 않는다. 대신 stdlib-only `--offline-smoke`가 downloader의 simulator-ready 출력 계약을 검증한다.

닫힌 항목:

- `scripts/download_data.py --offline-smoke --output-dir <dir>`가 `market_prices.csv`, `benchmark_prices.csv`, `factors.csv`, `dataset_manifest.json`을 생성한다.
- 생성된 dataset이 `system_trading_s3.simulate`에 바로 들어가 `PASS`가 되는지 테스트한다.
- 실제 Yahoo 다운로드는 optional dependency와 네트워크가 있는 환경에서 수행하는 별도 hardening 후보로 남긴다.

### Metrics sample-size gap

작은 fixture의 연율화 지표는 과장되어 보일 수 있다. 현재 metrics는 equity row가 20개 미만이면 gap을 기록한다.

닫힌 항목:

- `metrics.json`의 `gaps`에 sample-size warning이 기록된다.
- CLI output의 `GAPS` 섹션에 같은 warning이 표시된다.
- profit-neutral 문구를 유지한다.

## 추가 hardening 후보

- 실제 Yahoo Finance 다운로드 smoke: optional `yfinance`, `pandas`, 네트워크가 준비된 환경에서만 수행한다.
- Headless browser E2E: dashboard run 생성 버튼까지 실제 브라우저에서 누르는 CI test.
- Offline dashboard bundle: CDN 의존성을 제거해야 할 경우 별도 asset packaging이 필요하다.

## 다음 방향성 논의 후보

남은 후보는 신규 기반 작업이 아니라 현재 기반을 더 정교하게 만드는 쪽이다.

1. Richer statistical multi-factor risk attribution

   Current `factor_risk_model.json` gates deterministic OLS on top of `factor_attribution.json`. Later work can add rolling windows, confidence diagnostics, target exposure definitions, and model comparison.

2. Richer loss classification

   Current `loss_classification.json` separates benchmark-explained, excess relative, strategy-specific, and data-gap loss periods. Later work can add finer execution, data, and system-loss classes.

3. Richer risk rule policies

   Current risk rules cover position weight, cash buffer, order notional, cooldown, and drawdown rejection. Later work can add portfolio-level exposure budgets and richer policy reporting.

추천은 1번이다. 다음 제품 질문은 "수익이 났나?"가 아니라 아래여야 한다.

```text
이 run은 의도한 market/factor exposure에 맞게 움직였는가?
설명 가능한 factor-driven movement와 설명 불가능한 손실을 구분할 준비가 되었는가?
```
