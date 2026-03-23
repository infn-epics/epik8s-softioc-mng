# iocmng — IOC Manager Framework

A pluggable task/job framework for IOC Manager applications. Provides base classes for continuous **tasks** and one-shot **jobs** that can be dynamically loaded at runtime via a REST API.

## Features

- **`TaskBase`** — base class for continuous tasks (run in a loop)
- **`JobBase`** — base class for one-shot jobs (run once, return result)
- **REST API** — add/remove tasks and jobs at runtime from git repositories
- **Validation** — plugins are validated (must derive from base class, must compile, abstract methods must be implemented)
- **EPICS soft IOC PVs** — every task and job gets default PVs (STATUS, MESSAGE, etc.) via `softioc`
- **Per-plugin `config.yaml`** — each plugin defines its PVs and parameters in a config file inside its git repo
- **Path support** — specify a sub-directory inside the git repo where the plugin sources live
- **Plugin `requirements.txt`** — plugins can ship their own dependencies
- **Optional Ophyd integration** — device abstraction via `ophyd`/`infn_ophyd_hal` (optional dependency)
- **Docker image** — ready-to-run container with the REST API
- **PyPI package** — `pip install iocmng`

## Quick Start

### Install from PyPI

```bash
pip install iocmng

# With all optional dependencies (ophyd, kubernetes)
pip install iocmng[all]
```

### Run the API Server

```bash
# Using the CLI entry point
iocmng-server

# Or with environment variables
IOCMNG_PORT=8080 IOCMNG_LOG_LEVEL=debug iocmng-server

# Or with Docker
docker run -p 8080:8080 ghcr.io/infn-epics/epik8s-beamline-controller:latest
```

### Create a Task

Create a git repository with:
1. A Python file with a class deriving from `TaskBase`
2. A `config.yaml` defining PVs and parameters

```
my-monitor-repo/
├── my_monitor.py
├── config.yaml
└── requirements.txt    # optional — extra dependencies
```

**my_monitor.py**
```python
from iocmng import TaskBase

class MyMonitor(TaskBase):
    def initialize(self):
        self.logger.info("Starting monitor")

    def execute(self):
        value = self.read_sensor()
        self.set_pv("READING", value)
        if value > self.parameters.get("threshold", 75):
            self.set_pv("ALARM", 1)

    def cleanup(self):
        self.logger.info("Stopping monitor")

    def read_sensor(self):
        return 42.0
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 1.0
  threshold: 75.0

pvs:
  inputs:
    SETPOINT:
      type: float
      value: 50.0
      unit: "%"
      prec: 2
      low: 0
      high: 100
  outputs:
    READING:
      type: float
      value: 0.0
      unit: "arb"
      prec: 3
    ALARM:
      type: bool
      value: 0
      znam: "OK"
      onam: "ALARM"
```

### Create a Job

```python
# my_diagnostics.py
from iocmng import JobBase
from iocmng.base.job import JobResult

class MyDiagnostics(JobBase):
    def initialize(self):
        self.logger.info("Preparing diagnostics")

    def execute(self) -> JobResult:
        info = {"status": "healthy", "uptime": 12345}
        self.set_pv("SYSTEM_NAME", info["status"])
        return JobResult(success=True, data=info, message="Diagnostics OK")
```

### REST API Usage

#### Add a task
```bash
curl -X POST http://localhost:8080/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-monitor",
    "git_url": "https://github.com/user/my-monitor-task.git",
    "pat": "ghp_optional_token",
    "branch": "main",
    "path": "src/monitor",
    "parameters": {"threshold": 80.0}
  }'
```

The `path` field specifies the sub-directory inside the repo where the Python file and `config.yaml` live. Parameters passed in the REST request override values from `config.yaml`.

#### Remove a task
```bash
curl -X DELETE http://localhost:8080/api/v1/tasks/my-monitor
```

#### Add a job
```bash
curl -X POST http://localhost:8080/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-diag",
    "git_url": "https://github.com/user/my-diagnostics-job.git",
    "path": "jobs/diagnostics"
  }'
```

#### Run a job
```bash
curl -X POST http://localhost:8080/api/v1/jobs/my-diag/run
```

#### Remove a job
```bash
curl -X DELETE http://localhost:8080/api/v1/jobs/my-diag
```

#### List all tasks
```bash
curl http://localhost:8080/api/v1/tasks
```

#### Health check
```bash
curl http://localhost:8080/api/v1/health
```

## Plugin Structure

Each plugin lives in a git repository (or a sub-directory of one). The expected layout:

```
<repo-root>/
└── <path>/                 # optional sub-directory (specified via REST "path" field)
    ├── my_plugin.py        # Python module with TaskBase/JobBase subclass
    ├── config.yaml         # Plugin configuration (PVs, parameters)
    └── requirements.txt    # Optional additional pip dependencies
```

### config.yaml Format

```yaml
# Parameters — passed to the plugin constructor as self.parameters
# REST-supplied parameters override these defaults
parameters:
  mode: continuous          # "continuous" or "triggered"
  interval: 1.0             # application-specific
  threshold: 75.0           # application-specific

# PV definitions — created automatically by the IOC Manager
pvs:
  inputs:                   # writable PVs (operator → plugin)
    SETPOINT:
      type: float           # float, int, string, bool
      value: 50.0           # initial value
      unit: "%"             # EGU (float only)
      prec: 2               # precision (float only)
      low: 0                # LOPR (float only)
      high: 100             # HOPR (float only)
  outputs:                  # read-only PVs (plugin → operator)
    READING:
      type: float
      value: 0.0
    ALARM:
      type: bool
      value: 0
      znam: "OK"            # zero-state name (bool only)
      onam: "ALARM"         # one-state name (bool only)
```

## Plugin Validation

When a task or job is added, the framework performs the following checks:

1. **Clone** — the git repository is cloned (with optional PAT for private repos)
2. **Dependencies** — `requirements.txt` is installed if present (from `path` or repo root)
3. **Config** — `config.yaml` is loaded from `path` to read PV definitions and default parameters
4. **Syntax** — Python files are parsed via AST for syntax errors
5. **Import** — the module is imported to check for runtime import errors
6. **Inheritance** — at least one class must derive from `TaskBase` or `JobBase`
7. **Abstract methods** — all abstract methods (`initialize`, `execute`, `cleanup`) must be implemented

If any check fails, the plugin is rejected and the error details are returned.

## Default PVs

Every task automatically gets these PVs (prefix: `BEAMLINE:NAMESPACE:TASKNAME`):

| PV | Type | Description |
|---|---|---|
| `ENABLE` | boolOut | Enable/disable the task |
| `STATUS` | mbbIn | INIT / RUN / PAUSED / END / ERROR |
| `MESSAGE` | stringIn | Human-readable status message |
| `CYCLE_COUNT` | longIn | Cycle counter (continuous mode) |
| `RUN` | boolOut | Trigger execution (triggered mode) |

Every job gets:

| PV | Type | Description |
|---|---|---|
| `STATUS` | mbbIn | IDLE / RUNNING / SUCCESS / FAILED |
| `MESSAGE` | stringIn | Human-readable status message |

Additional PVs are created from the `pvs` section of `config.yaml`.

## Choosing Between Continuous Task, Triggered Task, and Job

| | **Continuous Task** | **Triggered Task** | **Job** |
|---|---|---|---|
| **Execution** | `execute()` loops indefinitely | `execute()` called when `RUN` PV is written | `execute()` called via REST |
| **How triggered** | Automatic (runs on start) | Operator writes `1` to the `RUN` EPICS PV | `POST /api/v1/jobs/{name}/run` |
| **Return value** | None (side effects only) | None (side effects only) | `JobResult` with `success`, `data`, `message` |
| **Has `cleanup()`** | Yes | Yes | No |
| **EPICS PV** | `CYCLE_COUNT` | `RUN` (boolOut) | — |
| **Typical use** | Polling, monitoring, periodic updates | Operator-driven actions from CS-Studio/Phoebus | API-driven actions from scripts or services |

**Rule of thumb:**
- Use a **continuous task** for anything that needs to run on a regular cycle (e.g., reading a sensor every second).
- Use a **triggered task** when the action is initiated from the EPICS control system (e.g., an operator clicks a button in Phoebus that writes to a PV).
- Use a **job** when the action is initiated from software/REST (e.g., a Kubernetes CronJob, a CI script, or another microservice).

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IOCMNG_CONFIG` | (none) | Path to config.yaml |
| `IOCMNG_BEAMLINE_CONFIG` | (none) | Path to values.yaml |
| `IOCMNG_PLUGINS_DIR` | `/data/plugins` | Directory for cloned plugins |
| `IOCMNG_HOST` | `0.0.0.0` | Server bind address |
| `IOCMNG_PORT` | `8080` | Server port |
| `IOCMNG_DISABLE_OPHYD` | `true` | Skip ophyd initialization |
| `IOCMNG_LOG_LEVEL` | `info` | Logging level |

### Optional: Ophyd Device Integration

When `ophyd` and `infn_ophyd_hal` are installed and `IOCMNG_DISABLE_OPHYD=false`, the controller automatically creates Ophyd device instances from your `values.yaml` IOC configuration. Tasks can access devices via `self.get_device()` and `self.list_devices()`.

## Project Structure

```
src/iocmng/
├── __init__.py           # Package entry: exports TaskBase, JobBase
├── base/
│   ├── task.py           # TaskBase — continuous tasks with PV support
│   └── job.py            # JobBase — one-shot jobs with PV support
├── core/
│   ├── controller.py     # Central plugin manager
│   ├── loader.py         # Git clone + config loading + module loading
│   └── validator.py      # Plugin validation
├── api/
│   ├── app.py            # FastAPI application factory
│   ├── models.py         # Pydantic request/response models
│   └── routes.py         # REST API endpoints
└── ophyd/
    └── factory.py        # Optional ophyd device creation
```

## Development

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Format
black .

# Lint
flake8 .
```

## GitHub Actions

The workflow in `.github/workflows/release.yml` triggers on:
- **Git tags** matching `v*` (e.g., `v2.0.0`)
- **Manual dispatch** (workflow_dispatch)

It will:
1. Run tests
2. Build and publish the Python package to PyPI
3. Build and push a Docker image to GitHub Container Registry (ghcr.io)

## License

MIT
