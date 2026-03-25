# iocmng — IOC Manager Framework

A pluggable task/job framework for IOC Manager applications. Provides base classes for continuous **tasks** and one-shot **jobs** that can be dynamically loaded at runtime via a REST API.

## Features

- **`TaskBase`** — base class for continuous tasks (run in a loop)
- **`JobBase`** — base class for one-shot jobs (run once, return result)
- **REST API** — add/remove tasks and jobs at runtime from git repositories
- **Task startup metadata API** — inspect effective startup parameters and PV definitions for each loaded task
- **Validation** — plugins are validated (must derive from base class, must compile, abstract methods must be implemented)
- **EPICS soft IOC PVs** — every task and job gets default PVs (STATUS, MESSAGE, etc.) via `softioc`
- **Per-plugin `config.yaml`** — each plugin defines its PVs and parameters in a config file inside its git repo
- **Path support** — specify a sub-directory inside the git repo where the plugin sources live
- **Staged plugin path** — when `path` is provided only that sub-directory is stored under `IOCMNG_PLUGINS_DIR/<plugin-name>`
- **Autostart persistence** — uploaded tasks can be persisted for automatic reload on IOC Manager startup
- **Autostart ordering** — define deterministic startup order for autostart tasks
- **On-disk plugin discovery** — `/api/v1/plugins` also reports plugin directories present on disk even when they are not loaded in memory
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

#### Add a plugin (task or job — type auto-detected)
```bash
curl -X POST http://sparc-beamline-controller.k8sda.lnf.infn.it/api/v1/plugins \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-monitor",
    "git_url": "https://baltig.infn.it/lnf-da-control/epik8-sparc.git",
    "pat": "",
    "branch": "main",
    "path": "config/iocs/beamline-controller/check_motor_movement/",
    "auto_start": true,
    "auto_start_on_boot": true,
    "autostart_order": 10,
    "parameters": {"threshold": 80.0}
  }'
```

The plugin type (task / job) is determined automatically from the class found in the repo. The `/tasks` and `/jobs` endpoints are still available as type-checked aliases.

#### Hot-reload a plugin (restart)
```bash
curl -X POST http://sparc-beamline-controller.k8sda.lnf.infn.it/api/v1/plugins/my-monitor/restart
```

Re-clones the repository into a temporary directory, validates the new code, and only updates the running instance if all checks pass. The original branch and PAT are reused. If validation fails the running plugin is left untouched.

#### Run a job
```bash
curl -X POST http://localhost:8080/api/v1/plugins/my-monitor/run
```

#### Remove a plugin
```bash
curl -X DELETE http://localhost:8080/api/v1/plugins/my-monitor
```

#### List all plugins
```bash
curl http://localhost:8080/api/v1/plugins

# Filter by type
curl "http://localhost:8080/api/v1/plugins?type=task"
curl "http://localhost:8080/api/v1/plugins?type=job"
```

The unified plugin list includes:

- loaded plugins currently running or available in memory
- plugin directories already present under `IOCMNG_PLUGINS_DIR`
- per-plugin validation details and a `status` such as `running`, `loaded`, `available`, or `invalid`

#### Type-scoped aliases
```bash
# Tasks
curl -X POST   http://localhost:8080/api/v1/tasks
curl -X DELETE http://localhost:8080/api/v1/tasks/my-monitor
curl           http://localhost:8080/api/v1/tasks
curl           http://localhost:8080/api/v1/tasks/my-monitor/startup

# Jobs
curl -X POST http://localhost:8080/api/v1/jobs
curl -X POST http://localhost:8080/api/v1/jobs/my-diag/run
curl -X DELETE http://localhost:8080/api/v1/jobs/my-diag
```

#### Get startup metadata for a task
```bash
curl http://localhost:8080/api/v1/tasks/my-monitor/startup
```

Example response:
```json
{
  "name": "my-monitor",
  "plugin_type": "task",
  "auto_start": true,
  "auto_start_on_boot": true,
  "autostart_order": 10,
  "plugin_prefix": "MY_MONITOR",
  "start_parameters": {
    "mode": "continuous",
    "interval": 1.0,
    "threshold": 80.0
  },
  "pv_definitions": {
    "outputs": {
      "VALUE": {"type": "float", "value": 0.0}
    }
  },
  "built_pvs": ["ENABLE", "STATUS", "MESSAGE", "CYCLE_COUNT", "VALUE"]
}
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

  If the REST request uses `path`, IOC Manager clones the repository into a temporary location, validates the selected sub-directory, and stores only that staged plugin directory under `IOCMNG_PLUGINS_DIR/<plugin-name>`. If a repository-level `requirements.txt` exists and the selected sub-directory does not provide its own, the requirements file is copied alongside the staged plugin so dependency installation still works.

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

`config.yaml`, `config.yml`, and `config.json` are supported. The config file is structurally validated before the plugin is accepted.

You may also define an optional top-level `prefix` in the plugin config. This is the task/job-specific PV prefix segment appended to the controller prefix.

Example:

```yaml
prefix: CHECK_MOTOR
parameters:
  mode: continuous
```

If the controller prefix is `SPARC:CONTROL`, the task PVs become:

- `SPARC:CONTROL:CHECK_MOTOR:STATUS`
- `SPARC:CONTROL:CHECK_MOTOR:MESSAGE`
- `SPARC:CONTROL:CHECK_MOTOR:<CUSTOM_PV>`

If `prefix` is omitted, IOC Manager falls back to the plugin name uppercased.

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

## Task Startup Logging (AS Info)

When a plugin is loaded and when a task starts, IOC Manager emits `INFO` log lines with effective metadata:

- task name
- plugin type
- mode
- PV prefix
- effective start parameters
- PV definitions
- effective PV list

Load-time example:

```text
AS_INFO_LOAD plugin=my-monitor type=task pv_prefix=SPARC:CONTROL:CHECK_MOTOR parameters={'interval': 1.0, 'threshold': 80.0} pv_definitions={'outputs': {'VALUE': {'type': 'float', 'value': 0.0}}} built_pvs=['ENABLE', 'STATUS', 'MESSAGE', 'CYCLE_COUNT', 'VALUE']
```

Example log line:

```text
AS_INFO task=my-monitor mode=continuous pv_prefix=SPARC:CONTROL:MY-MONITOR parameters={'interval': 1.0, 'threshold': 80.0} pv_definitions={'outputs': {'VALUE': {'type': 'float', 'value': 0.0}}}
```

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

### Initial Plugins (`IOCMNG_PLUGINS_CONFIG`)

Set `IOCMNG_PLUGINS_CONFIG` to a YAML file path to pre-load plugins on startup:

```yaml
# plugins.yaml
plugins:
  - name: beam-monitor
    git_url: https://github.com/org/beamline-tasks.git
    path: tasks/monitor          # sub-directory inside the repo
    branch: main
    pat: ghp_xxx                 # optional — for private repos
    auto_start: true             # start immediately after load
    auto_start_on_boot: true     # persist and reload on next IOCMNG start
    autostart_order: 10          # lower starts first
    parameters:
      threshold: 80.0            # override config.yaml defaults

  - name: daily-report
    git_url: https://github.com/org/beamline-jobs.git
    path: jobs/report
    auto_start: false            # jobs default to false; tasks default to true
```

```bash
export IOCMNG_PLUGINS_CONFIG=/etc/iocmng/plugins.yaml
iocmng-server
```

Startup behavior details:

- Entries from `IOCMNG_PLUGINS_CONFIG` are loaded at startup.
- Tasks added via REST with `auto_start_on_boot=true` are persisted under `IOCMNG_PLUGINS_DIR/autostart_plugins.yaml` and auto-loaded on next startup.
- If both sources define the same plugin `name`, the config-file entry wins and duplicates are skipped.
- Startup loading is ordered by `autostart_order` (ascending), then by plugin name.
- Failures are logged but do not prevent server startup.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IOCMNG_CONFIG` | (none) | Path to config.yaml |
| `IOCMNG_BEAMLINE_CONFIG` | (none) | Path to values.yaml |
| `IOCMNG_PLUGINS_CONFIG` | (none) | Path to initial plugins YAML |
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
