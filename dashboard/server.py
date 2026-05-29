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
        create_strategy,
        FrictionModel,
        export_run_artifacts,
        PASS
    )
    from system_trading_s3.metrics import write_metrics
except ImportError as e:
    print(f"Error importing simulation packages: {e}")
    print("Please make sure you run the server from the project root directory.")
    sys.exit(1)

PORT = 8000
RUNS_DIR = ROOT_DIR / "runs"
FIXTURES_DIR = ROOT_DIR / "tests" / "fixtures"

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
                    
                    manifest = json_to_dict(manifest_path)
                    metrics = json_to_dict(metrics_path)
                    
                    runs.append({
                        "run_id": entry.name,
                        "timestamp": manifest.get("timestamp", ""),
                        "strategy": manifest.get("strategy_name", "Unknown"),
                        "dataset": manifest.get("dataset", "Unknown"),
                        "audit_status": manifest.get("audit_status", "Unknown"),
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
        
        equity_curve = csv_to_dict_list(run_path / "equity_curve.csv")
        trades = csv_to_dict_list(run_path / "trades.csv")
        orders = csv_to_dict_list(run_path / "orders.csv")
        fills = csv_to_dict_list(run_path / "fills.csv")
        
        data = {
            "run_id": run_id,
            "manifest": manifest,
            "account_summary": account_summary,
            "audit_summary": audit_summary,
            "metrics": metrics,
            "equity_curve": equity_curve,
            "trades": trades,
            "orders": orders,
            "fills": fills
        }
        self.send_json_response(200, data)

    def handle_list_datasets(self):
        datasets = []
        # Check standard fixtures
        if FIXTURES_DIR.exists() and FIXTURES_DIR.is_dir():
            for entry in FIXTURES_DIR.iterdir():
                if entry.is_dir() and ((entry / "market_prices.csv").exists() or any(entry.glob("*_prices.csv"))):
                    datasets.append({
                        "name": entry.name,
                        "path": str(entry.relative_to(ROOT_DIR))
                    })
        # Check root level data if any (or just search for any dataset with price files)
        self.send_json_response(200, {"datasets": datasets})

    def handle_list_configs(self):
        configs = []
        if FIXTURES_DIR.exists() and FIXTURES_DIR.is_dir():
            for entry in FIXTURES_DIR.glob("*.json"):
                configs.append({
                    "name": entry.name,
                    "path": str(entry.relative_to(ROOT_DIR))
                })
        self.send_json_response(200, {"configs": configs})

    def handle_run_simulation(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            params = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON body: {str(e)}")
            return

        dataset_name = params.get("dataset_name")
        config_name = params.get("config_name")
        initial_cash_str = params.get("initial_cash", "100000")
        run_id = params.get("run_id", "web-run")

        if not dataset_name:
            self.send_error_json(400, "dataset_name is required")
            return

        dataset_path = FIXTURES_DIR / dataset_name
        if not dataset_path.exists() or not dataset_path.is_dir():
            self.send_error_json(400, f"Dataset {dataset_name} does not exist")
            return

        config_path = None
        if config_name:
            config_path = FIXTURES_DIR / config_name
            if not config_path.exists() or not config_path.is_file():
                self.send_error_json(400, f"Config {config_name} does not exist")
                return

        # Sanitize run_id
        run_id = "".join(c for c in run_id if c.isalnum() or c in "-_").strip()
        if not run_id:
            run_id = "web-run"

        try:
            # 1. Parse config or use defaults
            if config_path:
                sim_config = load_simulation_config(config_path)
                initial_cash = sim_config.initial_cash
                strategy = create_strategy(sim_config.strategy_name, sim_config.strategy_params)
                friction = sim_config.friction
                risk_free_rate = sim_config.risk_free_rate
            else:
                initial_cash = Decimal(initial_cash_str)
                strategy = None  # Will default to BuyAndHoldOneUnitStrategy
                friction = FrictionModel()
                risk_free_rate = Decimal("0")

            # 2. Run simulation
            result = run_simulation(
                dataset_dir=dataset_path,
                initial_cash=initial_cash,
                strategy=strategy,
                friction=friction,
                risk_free_rate=risk_free_rate
            )

            if result.status != PASS:
                self.send_json_response(400, {
                    "success": False,
                    "status": result.status,
                    "error": result.error or "Simulation failed without explicit error."
                })
                return

            # 3. Export artifacts
            export_dir = RUNS_DIR / run_id
            export_run_artifacts(
                result=result,
                dataset_dir=dataset_path,
                export_dir=export_dir,
                run_id=run_id,
                overwrite=True
            )

            # 4. Generate metrics
            write_metrics(export_dir)

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
    httpd = HTTPServer(server_address, DashboardHandler)
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
