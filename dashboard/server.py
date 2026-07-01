import os
import sys
import json
import csv
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
from decimal import Decimal

# Ensure the root directory is in the path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Import simulation engine and metrics from our codebase
try:
    from system_trading_s3.simulate import (
        run_simulation,
        load_simulation_config,
        simulation_config_from_dict,
        create_strategy,
        strategy_catalog,
        FrictionModel,
        RiskConfig,
        ExecutionConfig,
        export_run_artifacts,
        PASS
    )
    from system_trading_s3.metrics import write_metrics
    from system_trading_s3.factor_attribution import write_factor_attribution
    from system_trading_s3.factor_report import write_factor_report
    from system_trading_s3.factor_risk_model import write_factor_risk_model
    from system_trading_s3.loss_classification import write_loss_classification
except ImportError as e:
    print(f"Error importing simulation packages: {e}")
    print("Please make sure you run the server from the project root directory.")
    sys.exit(1)

PORT = 8000
RUNS_DIR = ROOT_DIR / "runs"
FIXTURES_DIR = ROOT_DIR / "tests" / "fixtures"
DATASETS_DIR = ROOT_DIR / "datasets"
STRATEGY_CONFIGS_DIR = ROOT_DIR / "configs" / "strategies"

class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


# Helper to serialize Decimals to float/str in JSON
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj) if obj.is_finite() else str(obj)
        return super().default(obj)

def csv_to_dict_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = [h.strip() for h in next(reader, [])]
            rows = []
            for row in reader:
                if not row or all(c.strip() == "" for c in row):
                    continue
                rows.append(dict(zip(headers, [c.strip() for c in row])))
            return rows
    except Exception as e:
        print(f"Error reading CSV {path.name}: {e}")
        return []

def json_to_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading JSON {path.name}: {e}")
        return {}


def resolve_drop_in_path(value: object, roots: list[Path], expect_dir: bool) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    requested = Path(value.strip())
    candidates: list[Path] = []
    if not requested.is_absolute():
        candidates.append(ROOT_DIR / requested)
        for root in roots:
            candidates.append(root / requested)
    candidates.append(requested)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not any(_is_relative_to(resolved, root.resolve()) for root in roots):
            continue
        if expect_dir and resolved.is_dir():
            return resolved
        if not expect_dir and resolved.is_file():
            return resolved
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def portable_artifact_path(path: Path) -> Path:
    try:
        return path.resolve().relative_to(ROOT_DIR.resolve())
    except ValueError:
        return path


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Override to log cleanly
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format%args))

    def end_headers(self):
        # Add CORS and standard headers
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        # 1. Static Files (UI)
        if path == "/" or path == "/index.html":
            self.serve_static_file(ROOT_DIR / "dashboard" / "index.html", "text/html")
            return
        elif path == "/favicon.ico":
            self.send_response(404)
            self.end_headers()
            return

        # 2. API Endpoints
        # GET /api/runs - List all run directories
        if path == "/api/runs":
            self.handle_list_runs()
            return
        
        # GET /api/runs/<run_id> - Get details of a single run
        elif path.startswith("/api/runs/"):
            run_id = path[len("/api/runs/"):]
            self.handle_run_details(run_id)
            return

        # GET /api/datasets - List available datasets
        elif path == "/api/datasets":
            self.handle_list_datasets()
            return

        # GET /api/configs - List available sample configurations
        elif path == "/api/configs":
            self.handle_list_configs()
            return

        # GET /api/strategies - List strategy catalog for form-driven config editing
        elif path == "/api/strategies":
            self.handle_list_strategies()
            return

        # Fallback to 404
        self.send_error_json(404, f"Path {path} not found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/simulate":
            self.handle_run_simulation()
            return

        self.send_error_json(404, f"Path {path} not found")

    # --- Route Handlers ---

    def serve_static_file(self, filepath: Path, content_type: str):
        if not filepath.exists() or not filepath.is_file():
            self.send_error_json(404, f"Static file {filepath.name} not found")
            return
        try:
            content = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error_json(500, f"Error serving file: {str(e)}")

    def handle_list_runs(self):
        runs = []
        if RUNS_DIR.exists() and RUNS_DIR.is_dir():
            for entry in sorted(RUNS_DIR.iterdir()):
                if entry.is_dir():
                    manifest_path = entry / "run_manifest.json"
                    metrics_path = entry / "metrics.json"
                    audit_path = entry / "audit_summary.json"
                    
                    manifest = json_to_dict(manifest_path)
                    metrics = json_to_dict(metrics_path)
                    audit_summary = json_to_dict(audit_path)
                    
                    runs.append({
                        "run_id": entry.name,
                        "timestamp": manifest.get("timestamp", ""),
                        "strategy": manifest.get("strategy_name", "Unknown"),
                        "dataset": manifest.get("dataset_dir", "Unknown"),
                        "input_audit_status": manifest.get("input_audit_status", "Unknown"),
                        "audit_status": audit_summary.get("audit_status", "Unknown"),
                        "total_return": metrics.get("total_return_pct", None),
                        "max_drawdown": metrics.get("max_drawdown_pct", None)
                    })
        self.send_json_response(200, {"runs": runs[::-1]})  # Newest first

    def handle_run_details(self, run_id: str):
        # Sanitize run_id to avoid directory traversal
        run_id = os.path.basename(run_id)
        run_path = RUNS_DIR / run_id
        
        if not run_path.exists() or not run_path.is_dir():
            self.send_error_json(404, f"Run {run_id} not found")
            return
            
        manifest = json_to_dict(run_path / "run_manifest.json")
        account_summary = json_to_dict(run_path / "account_summary.json")
        audit_summary = json_to_dict(run_path / "audit_summary.json")
        metrics = json_to_dict(run_path / "metrics.json")
        factor_report = json_to_dict(run_path / "factor_report.json")
        factor_attribution = json_to_dict(run_path / "factor_attribution.json")
        factor_risk_model = json_to_dict(run_path / "factor_risk_model.json")
        loss_classification = json_to_dict(run_path / "loss_classification.json")
        
        equity_curve = csv_to_dict_list(run_path / "equity_curve.csv")
        trades = csv_to_dict_list(run_path / "trades.csv")
        orders = csv_to_dict_list(run_path / "orders.csv")
        order_events = csv_to_dict_list(run_path / "order_events.csv")
        risk_events = csv_to_dict_list(run_path / "risk_events.csv")
        fills = csv_to_dict_list(run_path / "fills.csv")
        
        data = {
            "run_id": run_id,
            "manifest": manifest,
            "account_summary": account_summary,
            "audit_summary": audit_summary,
            "metrics": metrics,
            "factor_report": factor_report,
            "factor_attribution": factor_attribution,
            "factor_risk_model": factor_risk_model,
            "loss_classification": loss_classification,
            "equity_curve": equity_curve,
            "trades": trades,
            "orders": orders,
            "order_events": order_events,
            "risk_events": risk_events,
            "fills": fills
        }
        self.send_json_response(200, data)

    def handle_list_datasets(self):
        datasets = []
        for root, source in [(DATASETS_DIR, "datasets"), (FIXTURES_DIR, "fixtures")]:
            if root.exists() and root.is_dir():
                for entry in sorted(root.iterdir()):
                    if entry.is_dir() and ((entry / "market_prices.csv").exists() or any(entry.glob("*_prices.csv"))):
                        datasets.append({
                            "name": entry.name,
                            "path": str(entry.relative_to(ROOT_DIR)),
                            "source": source,
                        })
        self.send_json_response(200, {"datasets": datasets})

    def handle_list_configs(self):
        configs = []
        for root, source in [(STRATEGY_CONFIGS_DIR, "strategy_configs"), (FIXTURES_DIR, "fixtures")]:
            if root.exists() and root.is_dir():
                for entry in sorted(root.glob("*.json")):
                    configs.append({
                        "name": entry.name,
                        "path": str(entry.relative_to(ROOT_DIR)),
                        "source": source,
                        "payload": json_to_dict(entry),
                    })
        self.send_json_response(200, {"configs": configs})

    def handle_list_strategies(self):
        self.send_json_response(200, {"strategies": strategy_catalog()})

    def handle_run_simulation(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            params = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON body: {str(e)}")
            return

        dataset_name = params.get("dataset_name")
        dataset_path_text = params.get("dataset_path")
        config_name = params.get("config_name")
        config_path_text = params.get("config_path")
        config_json = params.get("config_json")
        initial_cash_str = params.get("initial_cash", "100000")
        run_id = params.get("run_id", "web-run")
        overwrite = params.get("overwrite", False)

        if not dataset_name and not dataset_path_text:
            self.send_error_json(400, "dataset_name or dataset_path is required")
            return
        if not isinstance(overwrite, bool):
            self.send_error_json(400, "overwrite must be a boolean when provided")
            return

        dataset_path = resolve_drop_in_path(dataset_path_text or dataset_name, [DATASETS_DIR, FIXTURES_DIR], expect_dir=True)
        if dataset_path is None:
            self.send_error_json(400, f"Dataset {dataset_path_text or dataset_name} does not exist")
            return

        config_path = None
        if config_path_text or config_name:
            config_path = resolve_drop_in_path(config_path_text or config_name, [STRATEGY_CONFIGS_DIR, FIXTURES_DIR], expect_dir=False)
            if config_path is None:
                self.send_error_json(400, f"Config {config_path_text or config_name} does not exist")
                return

        # Sanitize run_id
        run_id = "".join(c for c in run_id if c.isalnum() or c in "-_").strip()
        if not run_id:
            run_id = "web-run"
        export_dir = RUNS_DIR / run_id
        if export_dir.exists() and any(export_dir.iterdir()) and not overwrite:
            self.send_error_json(409, f"Run {run_id} already exists. Set overwrite=true to replace it.")
            return

        try:
            # 1. Parse config or use defaults
            if config_json:
                if isinstance(config_json, str):
                    config_payload = json.loads(config_json)
                else:
                    config_payload = config_json
                if not isinstance(config_payload, dict):
                    raise ValueError("config_json must be a JSON object")
                sim_config = simulation_config_from_dict(config_payload)
                initial_cash = sim_config.initial_cash
                strategy = create_strategy(sim_config.strategy_name, sim_config.strategy_params)
                friction = sim_config.friction
                execution = sim_config.execution
                risk_free_rate = sim_config.risk_free_rate
                risk = sim_config.risk
            elif config_path:
                sim_config = load_simulation_config(config_path)
                initial_cash = sim_config.initial_cash
                strategy = create_strategy(sim_config.strategy_name, sim_config.strategy_params)
                friction = sim_config.friction
                execution = sim_config.execution
                risk_free_rate = sim_config.risk_free_rate
                risk = sim_config.risk
            else:
                initial_cash = Decimal(initial_cash_str)
                strategy = None  # Will default to BuyAndHoldOneUnitStrategy
                friction = FrictionModel()
                execution = ExecutionConfig()
                risk_free_rate = Decimal("0")
                risk = RiskConfig()

            # 2. Run simulation
            result = run_simulation(
                dataset_dir=dataset_path,
                initial_cash=initial_cash,
                strategy=strategy,
                friction=friction,
                risk_free_rate=risk_free_rate,
                risk=risk,
                execution=execution,
            )

            if result.status != PASS:
                self.send_json_response(400, {
                    "success": False,
                    "status": result.status,
                    "error": result.error or "Simulation failed without explicit error."
                })
                return

            # 3. Export artifacts
            export_run_artifacts(
                result=result,
                dataset_dir=portable_artifact_path(dataset_path),
                export_dir=export_dir,
                run_id=run_id,
                overwrite=overwrite
            )

            # 4. Generate metrics
            write_metrics(export_dir)
            write_factor_report(export_dir)
            write_factor_attribution(export_dir)
            write_factor_risk_model(export_dir)
            write_loss_classification(export_dir)

            self.send_json_response(200, {
                "success": True,
                "run_id": run_id,
                "status": "PASS",
                "message": f"Simulation run {run_id} completed and metrics exported."
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_error_json(500, f"Simulation execution error: {str(e)}")

    # --- Helper methods ---

    def send_json_response(self, status_code: int, data: dict):
        try:
            content = json.dumps(data, cls=DecimalEncoder).encode('utf-8')
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            print(f"Error encoding JSON: {e}")
            # Fallback in case encoding fails
            self.send_response(500)
            self.end_headers()

    def send_error_json(self, status_code: int, message: str):
        self.send_json_response(status_code, {"error": message})

def run_server():
    server_address = ('', PORT)
    httpd = ReusableHTTPServer(server_address, DashboardHandler)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n=========================================")
    print(f"   SystemTradingS3 Interactive Dashboard  ")
    print(f"=========================================")
    print(f"Server is running on: http://localhost:{PORT}")
    print(f"Press Ctrl+C to stop the server.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
        httpd.server_close()

if __name__ == '__main__':
    run_server()
