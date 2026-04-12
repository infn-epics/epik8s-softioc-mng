# iocmng — IOC Manager Framework

A pluggable task/job framework for EPICS soft IOC applications on Kubernetes. Provides base classes for continuous **tasks** and one-shot **jobs**, a **declarative rule/transform engine**, and a **REST API** for dynamic plugin management at runtime.

## Key Features

- **`TaskBase`** — base class for continuous, triggered, or reactive tasks
- **`JobBase`** — base class for one-shot jobs returning structured results
- **`DeclarativeTask`** — zero-code tasks driven entirely by `config.yaml` rules and transforms
- **Wired inputs/outputs** — automatically read/write external PVs (poll or monitor)
- **Declarative rules** — safe boolean expressions that fire actuators and set outputs
- **Transforms** — computed outputs using built-in math/statistics/array functions
- **Ring buffers** — `buffer_size` accumulates time-series data for signal processing
- **Built-in function library** — `mean`, `std`, `sqrt`, `clamp`, `moving_avg`, `derivative`, and more
- **Safe expression evaluator** — AST-validated expressions; no arbitrary code execution
- **REST API** — add/remove/restart plugins at runtime from git repos or local paths
- **EPICS soft IOC PVs** — every task gets STATUS, MESSAGE, ENABLE, CYCLE_COUNT PVs
- **Per-plugin `config.yaml`** — parameters, directional PVs, rules, transforms in one file
- **PV client abstraction** — transparent PVA (p4p) or CA (PyEPICS) access
- **Plugin validation** — syntax, inheritance, abstract methods checked before acceptance
- **Docker image** — ready-to-run container with the REST API
- **Standalone runner** — `iocmng-run` for local development without a server
- **Optional Ophyd** — device abstraction via `ophyd`/`infn_ophyd_hal`

## Quick Start

### Install

```bash
pip install iocmng

# With all optional dependencies
pip install iocmng[all]
```

### Run the API Server

```bash
iocmng-server

# With configuration
IOCMNG_PORT=8080 IOCMNG_PREFIX=SPARC:CONTROL iocmng-server
```

### Create a Task (Python)

**my_monitor.py**
```python
from iocmng import TaskBase

class MyMonitor(TaskBase):
    def initialize(self):
        self.logger.info("Starting monitor")

    def execute(self):
        value = self.get_pv("READING")
        if value and value > self.parameters.get("threshold", 75):
            self.set_pv("ALARM", 1)

    def cleanup(self):
        pass
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 1.0
  threshold: 75.0

arguments:
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
    ALARM:
      type: bool
      value: 0
      znam: "OK"
      onam: "ALARM"
```

### Create a Declarative Task (Zero Code)

No Python needed — all logic lives in `config.yaml`:

**my_interlock.py**
```python
from iocmng import DeclarativeTask

class MyInterlock(DeclarativeTask):
    pass
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 1.0
  pva: false

arguments:
  inputs:
    temp:
      type: float
      value: 0.0
      link: "DEVICE:TEMP"         # Wired to external PV
      buffer_size: 100            # Keep last 100 readings
    pressure:
      type: float
      value: 0.0
      link: "DEVICE:PRESSURE"
  outputs:
    avg_temp:
      type: float
      value: 0.0
    alarm:
      type: bool
      value: 0
      znam: "OK"
      onam: "ALARM"

transforms:
  - output: avg_temp
    expression: "mean(temp_buf)"

rule_defaults:
  alarm: 0

rules:
  - id: OVER_TEMP
    condition: "mean(temp_buf) > 80 or pressure > 2.0"
    message: "Temperature or pressure limit exceeded"
    outputs:
      alarm: 1
```

### Run Standalone (no server)

```bash
iocmng-run -m my_interlock --config config.yaml --prefix MY:IOC --name interlock
```

### Deploy via REST API

```bash
curl -X POST http://localhost:8080/api/v1/plugins \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-interlock",
    "git_url": "https://github.com/org/my-tasks.git",
    "path": "plugins/interlock/",
    "auto_start": true
  }'
```

## Documentation

| Document | Description |
|----------|-------------|
| [MANUAL.md](MANUAL.md) | Complete reference: architecture, API, configuration, all features |
| [HOWTO.md](HOWTO.md) | Step-by-step recipes for common tasks |
| [INSTALL.md](INSTALL.md) | Installation and environment setup |

## Task Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `continuous` | `execute()` loops with `interval` sleep | Monitoring, polling, periodic updates |
| `triggered` | `triggered()` called when `RUN` PV is written | Operator-driven actions from CS-Studio |
| `reactive` | `on_input_changed()` fires on wired input change | Event-driven interlocks, fast response |

## Built-in Functions

Available in rule conditions and transform expressions:

| Category | Functions |
|----------|-----------|
| **Math** | `abs`, `round`, `sqrt`, `log`, `exp`, `pow`, `floor`, `ceil`, `clamp` |
| **Statistics** | `mean`, `std`, `variance`, `median`, `rms`, `min`, `max` |
| **Logic** | `any_of`, `all_of`, `count_true` |
| **Array** | `length`, `sum_of`, `diff`, `last`, `moving_avg`, `derivative` |

Extend with `register("my_fn", callable)` from `iocmng.core.functions`.

## Default PVs

Every task automatically gets:

| PV | Type | Description |
|----|------|-------------|
| `ENABLE` | boolOut | Enable/disable the task |
| `STATUS` | mbbIn | INIT / RUN / PAUSED / END / ERROR |
| `MESSAGE` | stringIn | Human-readable status |
| `CYCLE_COUNT` | longIn | Cycle counter |
| `RUN` | boolOut | Trigger execution (triggered mode) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IOCMNG_PREFIX` | — | Controller PV prefix |
| `IOCMNG_PORT` | `8080` | Server port |
| `IOCMNG_HOST` | `0.0.0.0` | Server bind address |
| `IOCMNG_PLUGINS_DIR` | `/data/plugins` | Plugin clone directory |
| `IOCMNG_PLUGINS_CONFIG` | — | Startup plugins YAML |
| `IOCMNG_LOG_LEVEL` | `info` | Logging level |
| `IOCMNG_PVA` | `true` | Use PVA (`true`) or CA (`false`) |
| `IOCMNG_DISABLE_OPHYD` | `true` | Skip ophyd initialization |

## Project Structure

```
src/iocmng/
├── __init__.py           # Exports: TaskBase, JobBase, DeclarativeTask, pv_client, run_ioc
├── declarative.py        # DeclarativeTask (zero-code tasks)
├── runner.py             # Standalone CLI runner (iocmng-run)
├── base/
│   ├── task.py           # TaskBase — continuous/triggered/reactive tasks
│   └── job.py            # JobBase — one-shot jobs
├── core/
│   ├── controller.py     # Central plugin manager
│   ├── loader.py         # Git clone + config loading
│   ├── validator.py      # Plugin validation
│   ├── plugin_spec.py    # PvArgumentSpec, PluginSpec, RuleSpec, TransformSpec
│   ├── safe_eval.py      # AST-validated expression evaluator
│   ├── functions.py      # Built-in function registry
│   └── pv_client.py      # PVA/CA abstraction layer
├── api/
│   ├── app.py            # FastAPI application
│   ├── routes.py         # REST endpoints
│   └── models.py         # Pydantic models
└── ophyd/
    └── factory.py        # Optional ophyd device creation
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
black .
flake8 .
```

## License

MIT
