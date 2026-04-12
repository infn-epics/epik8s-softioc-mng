# iocmng — Reference Manual

**Version 2.4.3**

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Installation](#2-installation)
3. [Running iocmng](#3-running-iocmng)
4. [Plugin Structure](#4-plugin-structure)
5. [config.yaml Reference](#5-configyaml-reference)
6. [TaskBase API](#6-taskbase-api)
7. [JobBase API](#7-jobbase-api)
8. [DeclarativeTask](#8-declarativetask)
9. [Wired Inputs and Outputs](#9-wired-inputs-and-outputs)
10. [Declarative Rules](#10-declarative-rules)
11. [Transforms](#11-transforms)
12. [Ring Buffers](#12-ring-buffers)
13. [Built-in Function Library](#13-built-in-function-library)
14. [Safe Expression Evaluator](#14-safe-expression-evaluator)
15. [PV Client](#15-pv-client)
16. [REST API Reference](#16-rest-api-reference)
17. [Ophyd Device Integration](#17-ophyd-device-integration)
18. [Plugin Validation](#18-plugin-validation)
19. [PV Naming and Prefixes](#19-pv-naming-and-prefixes)
20. [Environment Variables](#20-environment-variables)
21. [JSON Schema](#21-json-schema)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     IOC Manager Framework                        │
├────────────────────────┬────────────────────────────────────────┤
│   REST API (FastAPI)   │   Standalone Runner (iocmng-run)       │
│   iocmng-server        │   No HTTP server — single plugin       │
├────────────────────────┴────────────────────────────────────────┤
│                    IocMngController                               │
│            PluginLoader · PluginValidator                         │
├─────────────────────────────────────────────────────────────────┤
│              TaskBase / JobBase / DeclarativeTask                 │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│   │ Soft IOC │  │  Rules   │  │Transforms│  │ Wired I/O    │   │
│   │ (softioc)│  │safe_eval │  │functions │  │ (pv_client)  │   │
│   └──────────┘  └──────────┘  └──────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**iocmng** is a Python framework for building EPICS soft IOC applications. It provides:

- **Base classes** (`TaskBase`, `JobBase`) that handle the lifecycle, threading, soft IOC PV creation, and external PV wiring.
- **A declarative engine** for rules (interlock conditions), transforms (computed outputs), and ring buffers (time-series accumulation) — all configured in YAML.
- **A REST API** (FastAPI) for dynamic plugin management: add, remove, restart plugins at runtime from git repositories.
- **A standalone runner** (`iocmng-run`) for local development without a server.

Plugins are Python modules that subclass `TaskBase` or `JobBase` (or use `DeclarativeTask` for zero-code plugins). Each plugin ships with a `config.yaml` that defines its parameters, PV definitions, wired inputs/outputs, rules, and transforms.

---

## 2. Installation

### From PyPI

```bash
pip install iocmng

# With optional features
pip install iocmng[server]       # FastAPI + uvicorn
pip install iocmng[ophyd]        # Ophyd device support
pip install iocmng[kubernetes]   # Kubernetes client
pip install iocmng[all]          # Everything
```

### From Source

```bash
git clone https://github.com/infn-epics/epik8s-softioc-mng.git
cd epik8s-softioc-mng
pip install -e ".[dev]"
```

### Prerequisites

- Python >= 3.9
- EPICS Base (for CA access — `EPICS_BASE` and `EPICS_HOST_ARCH` set)
- `p4p` for PVA access or `pyepics` for CA access

### Docker

```bash
docker run -p 8080:8080 \
  -e IOCMNG_PREFIX=MY:BEAMLINE \
  ghcr.io/infn-epics/epik8s-beamline-controller:latest
```

---

## 3. Running iocmng

### API Server Mode

Runs a FastAPI server that manages multiple plugins dynamically:

```bash
# CLI entry points
iocmng-server
iocmng-standalone   # alias

# With environment variables
IOCMNG_PORT=8080 \
IOCMNG_PREFIX=SPARC:CONTROL \
IOCMNG_PLUGINS_CONFIG=/etc/iocmng/plugins.yaml \
IOCMNG_LOG_LEVEL=info \
iocmng-server
```

### Standalone Runner Mode

Runs a single plugin directly — no HTTP server, no dynamic loading:

```bash
iocmng-run \
  --module my_plugin \
  --class-name MyPlugin \
  --config config.yaml \
  --prefix MY:IOC \
  --name my-task \
  --pva true \
  --log-level INFO
```

| Argument | Required | Description |
|----------|----------|-------------|
| `-m, --module` | Yes | Python module name (e.g. `my_plugin`) |
| `-c, --class-name` | No | Class name (auto-detected if omitted) |
| `--config` | No | Path to `config.yaml` |
| `--prefix` | No | PV prefix override |
| `--name` | No | IOC name (defaults to class name) |
| `--pva` | No | `true` (PVA) or `false` (CA). Default: `true` |
| `--log-level` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Pre-loading Plugins on Startup

Create a YAML file and point `IOCMNG_PLUGINS_CONFIG` at it:

```yaml
plugins:
  - name: beam-monitor
    git_url: https://github.com/org/beamline-tasks.git
    path: tasks/monitor
    branch: main
    pat: ghp_xxx               # optional for private repos
    auto_start: true
    auto_start_on_boot: true
    autostart_order: 10
    parameters:
      threshold: 80.0

  - name: local-dev
    local_path: /home/user/my-plugin
    auto_start: true
```

Startup loading is ordered by `autostart_order` (ascending), then by name. Failures are logged but do not prevent server startup.

---

## 4. Plugin Structure

Each plugin is a directory containing at minimum:

```
my-plugin/
├── my_plugin.py        # Python module with TaskBase/JobBase subclass
├── config.yaml         # Plugin configuration
└── requirements.txt    # Optional pip dependencies
```

The Python module must contain exactly one class that inherits from `TaskBase`, `JobBase`, or `DeclarativeTask`. The framework auto-detects the class by inspecting the module's AST.

When loaded via REST with a `path` parameter, only the specified sub-directory is staged under `IOCMNG_PLUGINS_DIR/<plugin-name>`.

---

## 5. config.yaml Reference

The `config.yaml` file defines all aspects of a plugin's configuration. It is validated against the JSON schema in `schemas/iocmng-config.schema.json`.

### Complete Example

```yaml
# Optional PV prefix segment (appended to controller prefix)
prefix: MY_TASK

# Parameters — available as self.parameters in the task
parameters:
  mode: continuous          # continuous | triggered | reactive
  interval: 1.0             # Seconds between execute() cycles
  timeout: 5.0              # PV access timeout
  pva: true                 # Use PVA (true) or CA (false)
  threshold: 75.0           # Custom application parameter

# PV definitions (preferred key: "arguments"; legacy "pvs" also accepted)
arguments:
  inputs:
    SETPOINT:
      type: float
      value: 50.0
      unit: "%"
      prec: 2
      low: 0
      high: 100
    SENSOR:
      type: float
      value: 0.0
      link: "EXTERNAL:IOC:SENSOR"     # Wire to external PV
      link_mode: monitor              # poll (default) or monitor
      poll_rate: 2.0                  # Per-input poll interval (poll mode)
      trigger: true                   # Fire on_input_changed on change
      buffer_size: 100                # Ring buffer: keep last 100 values
  outputs:
    READING:
      type: float
      value: 0.0
    ALARM:
      type: bool
      value: 0
      znam: "OK"
      onam: "ALARM"
    AVG:
      type: float
      value: 0.0
    MSG:
      type: string
      value: "Initializing"

# Computed outputs — evaluated each cycle before rules
transforms:
  - output: AVG
    expression: "mean(SENSOR_buf)"

# Output reset values — applied before rule evaluation each cycle
rule_defaults:
  ALARM: 0

# Declarative rules — evaluated in order each cycle
rules:
  - id: OVER_TEMP
    condition: "mean(SENSOR_buf) > threshold"
    message: "Temperature limit exceeded"
    message_pv: MSG                   # Write timestamped message to this PV
    outputs:
      ALARM: 1
  - id: SENSOR_OFFLINE
    condition: "SENSOR == 0"
    message: "Sensor offline"
    actuators:
      SETPOINT: 0                     # Write to external PV of wired input
    outputs:
      ALARM: 1
```

### PV Types

| Type | softioc Record | Python Type | Properties |
|------|---------------|-------------|------------|
| `float` | `aOut` / `aIn` | `float` | `value`, `unit`, `prec`, `low`, `high` |
| `int` | `longOut` / `longIn` | `int` | `value`, `low`, `high` |
| `string` | `stringOut` / `stringIn` | `str` | `value` |
| `bool` | `boolOut` / `boolIn` | `int` (0/1) | `value`, `znam`, `onam` |

### PV Link Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `link` | string | — | External PV name to wire to |
| `link_mode` | string | `"poll"` | `"poll"` or `"monitor"` |
| `poll_rate` | float | — | Per-input poll interval (seconds) |
| `trigger` | bool | `false` | Fire `on_input_changed()` on value change |
| `buffer_size` | int | — | Ring buffer size (keep last N values) |

---

## 6. TaskBase API

`TaskBase` is the abstract base class for all continuous tasks.

### Import

```python
from iocmng import TaskBase
```

### Abstract Methods (must implement)

```python
def initialize(self):
    """Called once before the run loop starts."""

def execute(self):
    """Called each cycle (continuous mode)."""

def cleanup(self):
    """Called when the task stops."""
```

### Optional Overrides

```python
def triggered(self):
    """Called when RUN PV is written (triggered mode only)."""

def on_input_changed(self, key: str, value: Any, old_value: Any):
    """Called when a wired input with trigger=true changes value.
    In reactive mode, this also triggers rule/transform evaluation."""
```

### Constructor

```python
TaskBase(
    name: str,
    parameters: dict = None,
    pv_definitions: dict = None,
    beamline_config: dict = None,
    ophyd_devices: dict = None,
    prefix: str = None,
    plugin_prefix: str = None,
    device_resolver: Callable = None,
    plugin_spec: PluginSpec = None,
)
```

The preferred way is to pass `plugin_spec` (a `PluginSpec` instance parsed from `config.yaml`). All other parameters exist for backward compatibility.

### Key Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Task name |
| `plugin_spec` | `PluginSpec` | Normalized configuration |
| `parameters` | `dict` | Runtime parameters |
| `beamline_config` | `dict` | Full beamline configuration |
| `logger` | `Logger` | Configured logger |
| `pvs` | `dict` | PV name → PV object map |
| `pv_prefix` | `str` | Full PV prefix |
| `mode` | `str` | `"continuous"`, `"triggered"`, or `"reactive"` |
| `link_values` | `dict` | Current wired input/output values |
| `running` | `bool` | Whether the task is running |
| `enabled` | `bool` | Whether the task is enabled |
| `cycle_count` | `int` | Cycle counter |

### PV Access Methods

```python
# Read/write local soft IOC PVs
self.set_pv(pv_name: str, value: Any)
self.get_pv(pv_name: str) -> Any

# Aliases
self.set_output(pv_name, value)
self.get_output(pv_name) -> Any
self.set_input(pv_name, value)
self.get_input(pv_name) -> Any

# Status helpers
self.set_status(status: str)    # "INIT", "RUN", "PAUSED", "END", "ERROR"
self.set_message(message: str)  # Max 40 chars
```

### External PV Access

```python
# Write to the external PV linked to a wired input or output
self.link_put(key: str, value: Any, timeout: float = 5.0)
```

For wired outputs, `set_pv()` automatically forwards the value to the linked external PV.

### Lifecycle

```python
self.start()   # Start the task thread
self.stop()    # Stop gracefully
```

### Task Modes

**Continuous** (default):
```
start() → initialize() → [ _poll_links() → _evaluate_transforms() → _evaluate_rules() → execute() → sleep(interval) ] → cleanup()
```

**Triggered**:
```
start() → initialize() → wait for RUN PV → triggered() → back to waiting
```

**Reactive**:
```
start() → initialize() → _start_link_monitors()
  On each input change: on_input_changed() → _evaluate_transforms() → _evaluate_rules()
  Background heartbeat: step_cycle() every interval
```

---

## 7. JobBase API

`JobBase` is for one-shot operations triggered via REST.

### Import

```python
from iocmng import JobBase
from iocmng.base.job import JobResult
```

### Abstract Methods

```python
def initialize(self):
    """Prepare the job."""

def execute(self) -> JobResult:
    """Run the job and return a result."""
```

### JobResult

```python
@dataclass
class JobResult:
    success: bool
    data: Any = None
    message: str = ""
    timestamp: str = ...   # Auto-set to ISO datetime

    def to_dict(self) -> dict
```

### Example

```python
from iocmng import JobBase
from iocmng.base.job import JobResult

class DiagnosticsJob(JobBase):
    def initialize(self):
        self.logger.info("Preparing diagnostics")

    def execute(self) -> JobResult:
        info = {"uptime": 12345, "status": "healthy"}
        return JobResult(success=True, data=info, message="OK")
```

Trigger via REST:
```bash
curl -X POST http://localhost:8080/api/v1/jobs/diagnostics/run
```

---

## 8. DeclarativeTask

`DeclarativeTask` is a built-in `TaskBase` subclass with empty `initialize()`, `execute()`, and `cleanup()`. All behavior comes from the config.yaml rules, transforms, and wired I/O.

### Import

```python
from iocmng import DeclarativeTask
```

### Usage

Create a thin Python stub:

```python
# my_interlock.py
from iocmng import DeclarativeTask

class MyInterlock(DeclarativeTask):
    """All logic defined in config.yaml."""
    pass
```

Then define all behavior in `config.yaml` using wired inputs, transforms, rule_defaults, and rules.

### When to Use

- **Interlocks**: read N external PVs, apply boolean conditions, write outputs
- **Signal monitors**: accumulate buffers, compute statistics, fire alarms
- **Computed outputs**: derive values from inputs using math/statistics functions
- Any scenario where the logic is purely **read inputs → evaluate conditions → write outputs**

### When to Use TaskBase Instead

- You need custom initialization (connecting to hardware, databases, etc.)
- You need imperative logic that can't be expressed as safe expressions
- You need to call external APIs or services in `execute()`
- You need state machines or complex control flows

---

## 9. Wired Inputs and Outputs

Wired inputs/outputs create an automatic bridge between your plugin's local soft IOC PVs and external EPICS PVs.

### Configuration

```yaml
arguments:
  inputs:
    sensor:
      type: float
      value: 0.0
      link: "EXTERNAL:IOC:SENSOR"     # External PV to read
      link_mode: poll                  # poll (default) or monitor
      poll_rate: 2.0                   # Only for poll mode
      trigger: true                    # Fire on_input_changed
      buffer_size: 100                 # Keep last 100 values
  outputs:
    command:
      type: int
      value: 0
      link: "EXTERNAL:IOC:COMMAND"    # External PV to write to
```

### Behavior

**Wired inputs** (read from external PVs):

| `link_mode` | Behavior |
|-------------|----------|
| `poll` | Read the PV once per cycle (or at `poll_rate` interval) |
| `monitor` | Subscribe to PV updates via a persistent callback |

The latest value is stored in `self.link_values["sensor"]` and also written to the local soft IOC PV.

When `trigger: true`, a value change calls `self.on_input_changed(key, value, old_value)`. In reactive mode, this also triggers transform and rule evaluation.

**Wired outputs** (write to external PVs):

When you call `self.set_pv("command", 1)`, the value is:
1. Written to the local soft IOC PV
2. Automatically forwarded to the linked external PV (`EXTERNAL:IOC:COMMAND`)

You can also explicitly write: `self.link_put("command", 1, timeout=5.0)`.

### Accessing Values

```python
# In execute() or on_input_changed()
value = self.link_values.get("sensor")        # Latest scalar value
```

In rule conditions and transform expressions:
```yaml
condition: "sensor > 50"
expression: "mean(sensor_buf)"
```

---

## 10. Declarative Rules

Rules are boolean conditions evaluated each cycle. When a condition is true, the rule fires: setting outputs and/or writing to actuators.

### Configuration

```yaml
rule_defaults:
  ALARM: 0          # Reset before evaluation each cycle
  MSG: "OK"

rules:
  - id: OVER_TEMP
    condition: "temp > 80 or pressure > 2.0"
    message: "Limit exceeded"
    message_pv: MSG
    outputs:
      ALARM: 1
    actuators:
      heater: 0     # Write 0 to the external PV of wired input "heater"

  - id: SENSOR_FAIL
    condition: "temp == 0 and pressure == 0"
    message: "All sensors offline"
    outputs:
      ALARM: 1
```

### Evaluation Order

Each cycle (continuous mode) or each input change (reactive mode):

1. **rule_defaults** are applied — all listed outputs are reset to their default values
2. Rules are evaluated **in order** (top to bottom)
3. For each rule whose `condition` is true:
   - Log message is emitted
   - If `message_pv` is set, a timestamped message is written to that output PV
   - `outputs` are written to local soft IOC PVs
   - `actuators` write values to external PVs of wired inputs

### Condition Expressions

Conditions are [safe expressions](#14-safe-expression-evaluator) evaluated over:
- All `link_values` (latest scalar for each wired PV)
- All ring buffers as `<name>_buf` (list of floats)
- All task parameters (if they don't shadow input names)
- All [registered functions](#13-built-in-function-library)

Examples:
```
"temp > 80"
"temp > 80 or pressure > 2.0"
"mean(signal_buf) > threshold"
"std(noise_buf) > 0.5 and count_true(a, b, c) >= 2"
"any_of(motor1_busy, motor2_busy, motor3_busy)"
"not all_of(sensor1, sensor2, sensor3)"
```

### RuleSpec Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique rule identifier |
| `condition` | string | Yes | Safe expression returning truthy/falsy |
| `message` | string | No | Log message when fired |
| `message_pv` | string | No | Output PV for timestamped message |
| `outputs` | dict | No | Map of output PV names → values to set |
| `actuators` | dict | No | Map of input keys → values to write to their linked external PVs |

---

## 11. Transforms

Transforms are computed outputs evaluated each cycle. They run **before** rules, so rules can reference transform results.

### Configuration

```yaml
transforms:
  - output: avg_temp
    expression: "mean(temp_buf)"
  - output: noise_level
    expression: "std(signal_buf)"
  - output: clamped_reading
    expression: "clamp(sensor, 0, 100)"
  - output: derived
    expression: "avg_temp * 2 + noise_level"   # Can reference earlier transforms
```

### Behavior

1. Each cycle, transforms are evaluated **in order** (top to bottom)
2. The result of each expression is written to the named output PV via `set_pv()`
3. Later transforms can reference the results of earlier transforms in the same cycle
4. The evaluation context includes: `link_values`, ring buffers (`<name>_buf`), parameters, and all registered functions

### TransformSpec Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `output` | string | Yes | Output PV name to write result to |
| `expression` | string | Yes | Safe expression to evaluate |

### Execution Order

```
_poll_links() → _evaluate_transforms() → _evaluate_rules() → execute()
```

---

## 12. Ring Buffers

Ring buffers accumulate time-series data for use in transforms and rules.

### Configuration

Add `buffer_size` to any wired input or output:

```yaml
arguments:
  inputs:
    signal:
      type: float
      value: 0.0
      link: "DEVICE:SIGNAL"
      buffer_size: 200        # Keep last 200 samples
```

### Behavior

- A `collections.deque(maxlen=buffer_size)` is created at task initialization
- Each new value (from poll or monitor) is appended to the buffer
- Values are converted to `float` when possible
- The buffer is accessible in expressions as `<name>_buf` (e.g., `signal_buf`)
- The buffer contains a Python `list` of floats

### Usage in Expressions

```yaml
transforms:
  - output: avg
    expression: "mean(signal_buf)"
  - output: noise
    expression: "std(signal_buf)"
  - output: trend
    expression: "derivative(signal_buf)"
  - output: recent_avg
    expression: "moving_avg(signal_buf, 10)"

rules:
  - id: HIGH_NOISE
    condition: "std(signal_buf) > 0.5"
    outputs: { alarm: 1 }
```

The buffer variable `signal_buf` is a regular Python list, so it works with all array/statistics functions.

---

## 13. Built-in Function Library

Functions are registered in `iocmng.core.functions` and available in all safe expressions (rules, transforms).

### Math Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `abs` | `abs(x)` | Absolute value |
| `round` | `round(x, ndigits=0)` | Round to N digits |
| `sqrt` | `sqrt(x)` | Square root |
| `log` | `log(x, base=e)` | Natural/base logarithm |
| `exp` | `exp(x)` | e^x |
| `pow` | `pow(base, exp)` | Power |
| `floor` | `floor(x)` | Floor |
| `ceil` | `ceil(x)` | Ceiling |
| `clamp` | `clamp(value, low, high)` | Clamp between bounds |

### Statistics Functions

All statistics functions accept both scalars and lists (scalars are coerced to `[scalar]`).

| Function | Signature | Description |
|----------|-----------|-------------|
| `mean` | `mean(values)` | Arithmetic mean |
| `std` | `std(values)` | Standard deviation (population) |
| `variance` | `variance(values)` | Variance (population) |
| `median` | `median(values)` | Median value |
| `rms` | `rms(values)` | Root mean square |
| `min` | `min(values)` | Minimum |
| `max` | `max(values)` | Maximum |

### Logic Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `any_of` | `any_of(a, b, c, ...)` | True if any argument is truthy |
| `all_of` | `all_of(a, b, c, ...)` | True if all arguments are truthy |
| `count_true` | `count_true(a, b, c, ...)` | Count of truthy arguments |

### Array / Buffer Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `length` | `length(values)` | Number of elements |
| `sum_of` | `sum_of(values)` | Sum of elements |
| `diff` | `diff(values)` | First-order differences `[v[1]-v[0], ...]` |
| `last` | `last(values, n=1)` | Last N elements |
| `moving_avg` | `moving_avg(values, window=None)` | Moving average over last `window` elements |
| `derivative` | `derivative(values)` | Approximate derivative (alias for `diff`) |

### Registering Custom Functions

```python
from iocmng.core.functions import register

def my_filter(values):
    """Custom band-pass filter."""
    return [v for v in values if 0.1 < v < 0.9]

register("bandpass", my_filter)
```

After registration, `bandpass(signal_buf)` becomes available in all expressions.

Register custom functions in your task's `initialize()` method, or in a module-level registration block that runs at import time.

---

## 14. Safe Expression Evaluator

The `safe_eval` function evaluates Python expressions with strict AST validation. It is used for both rule conditions and transform expressions.

### Allowed Constructs

| Construct | Example |
|-----------|---------|
| Comparisons | `x > 5`, `a == 1`, `b != 0` |
| Boolean logic | `a and b`, `a or b`, `not c` |
| Arithmetic | `a + b`, `x * 2`, `y / 3`, `z % 2` |
| Unary | `-x`, `+y` |
| Ternary | `1 if x > 0 else 0` |
| Literals | `42`, `3.14`, `"hello"`, `True`, `None` |
| Variables | Any name from the eval context |
| Function calls | Registered functions only: `mean(buf)`, `sqrt(x)` |
| Tuple/list literals | `(1, 2, 3)`, `[1, 2]` — for function arguments |

### Disallowed Constructs

| Construct | Example | Reason |
|-----------|---------|--------|
| Attribute access | `x.__class__` | Security |
| Subscript | `x[0]` | Security |
| Import | `__import__('os')` | Security |
| Lambda | `(lambda: 1)()` | Security |
| Unregistered calls | `print("hi")`, `eval("1")` | Security |
| Assignment | `x = 1` | Not an expression |
| Comprehensions | `[x for x in buf]` | Complexity |

### Python API

```python
from iocmng.core.safe_eval import safe_eval

# Basic usage
result = safe_eval("x > 5", {"x": 10})  # True

# With functions
result = safe_eval("mean(buf) > 0.5", {"buf": [0.1, 0.9, 0.8]})  # True

# With extra functions
result = safe_eval(
    "double(x) > 8",
    {"x": 5},
    extra_functions={"double": lambda v: v * 2}
)  # True
```

---

## 15. PV Client

`pv_client` abstracts PV access over PVA (p4p) or CA (PyEPICS).

### Import

```python
from iocmng.core import pv_client
```

### Initialization

```python
pv_client.init(pva=True)   # PVA provider (default)
pv_client.init(pva=False)  # CA provider
```

### API

```python
# Read a PV
value = pv_client.get("MY:PV:NAME", timeout=5.0)

# Write a PV
pv_client.put("MY:PV:NAME", 42, timeout=5.0)

# Monitor a PV (subscribe)
key = pv_client.monitor("MY:PV:NAME", callback=lambda v: print(v), name="my-sub")

# Stop monitoring
pv_client.unmonitor(key)

# Stop all monitors
pv_client.unmonitor_all()

# List active monitors
monitors = pv_client.active_monitors()  # {key: pv_name, ...}

# Get current provider
provider = pv_client.get_provider()  # "pva" or "ca"

# Cleanup
pv_client.close()
```

### In Tasks

Most tasks don't need to call `pv_client` directly. Wired inputs handle external PV reads automatically. Use `pv_client` when you need ad-hoc PV access beyond what wired I/O provides:

```python
from iocmng import TaskBase, pv_client

class MyTask(TaskBase):
    def execute(self):
        # Ad-hoc read from an IOC not wired in config
        value = pv_client.get("OTHER:IOC:PV", timeout=2.0)
        self.set_pv("RESULT", value)
```

---

## 16. REST API Reference

Base URL: `http://<host>:<port>/api/v1`

### Plugin Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/plugins` | Add plugin (auto-detects task/job) |
| `DELETE` | `/plugins/{name}` | Remove plugin |
| `GET` | `/plugins` | List all plugins (`?type=task\|job`) |
| `GET` | `/plugins/{name}` | Get plugin details |
| `POST` | `/plugins/{name}/restart` | Hot-reload from git |
| `POST` | `/plugins/{name}/run` | Run a job plugin |

### Type-Scoped Aliases

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/tasks` | Add task (type-checked) |
| `DELETE` | `/tasks/{name}` | Remove task |
| `GET` | `/tasks` | List tasks |
| `GET` | `/tasks/{name}` | Get task details |
| `GET` | `/tasks/{name}/startup` | Get startup metadata |
| `POST` | `/jobs` | Add job (type-checked) |
| `DELETE` | `/jobs/{name}` | Remove job |
| `POST` | `/jobs/{name}/run` | Execute job |
| `GET` | `/jobs` | List jobs |
| `GET` | `/jobs/{name}` | Get job details |

### PV Access

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/pv/get` | Read external PV |
| `POST` | `/pv/put` | Write external PV |
| `POST` | `/pv/monitor` | Start subscription |
| `DELETE` | `/pv/monitor/{key}` | Stop subscription |
| `GET` | `/pv/monitors` | List active monitors |
| `GET` | `/pv/provider` | Get current provider |

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |

### Add Plugin Request Body

```json
{
  "name": "my-plugin",
  "git_url": "https://github.com/org/repo.git",
  "local_path": null,
  "path": "plugins/subfolder/",
  "pat": "ghp_xxx",
  "branch": "main",
  "auto_start": true,
  "auto_start_on_boot": false,
  "autostart_order": 10,
  "parameters": {"threshold": 80.0}
}
```

Either `git_url` or `local_path` is required (not both).

### Startup Metadata Response

```json
{
  "name": "my-monitor",
  "plugin_type": "task",
  "auto_start": true,
  "auto_start_on_boot": true,
  "autostart_order": 10,
  "pv_prefix": "SPARC:CONTROL:MY_MONITOR",
  "plugin_prefix": "MY_MONITOR",
  "mode": "continuous",
  "start_parameters": {"interval": 1.0, "threshold": 80.0},
  "arguments": {"inputs": {...}, "outputs": {...}},
  "built_pvs": ["ENABLE", "STATUS", "MESSAGE", "CYCLE_COUNT", "READING"]
}
```

---

## 17. Ophyd Device Integration

When `ophyd` and `infn_ophyd_hal` are installed (`pip install iocmng[ophyd]`), tasks can interact with hardware devices through the Ophyd abstraction layer.

### Creating Devices

```python
from iocmng import TaskBase

class MotorTask(TaskBase):
    def initialize(self):
        self.motor = self.create_device(
            prefix="SPARC:MOT:TML:AXIS01",
            devgroup="mot",
            devtype="tml",
            name="AXIS01",
        )

    def execute(self):
        if self.motor is None:
            return
        pos = self.motor.user_readback.get()
        self.set_pv("POSITION", pos)
```

### Getting Pre-configured Devices

```python
# From beamline config (values.yaml)
device = self.get_device("tml-ch1")
```

### Supported Device Types

| devgroup | devtype | Ophyd Class |
|----------|---------|-------------|
| `mot` | `asyn` | `OphydAsynMotor` |
| `mot` | `tml` | `OphydTmlMotor` |
| `mot` | `sim` | `OphydMotorSim` |
| `io` | `di` / `do` | `OphydDI` / `OphydDO` |
| `io` | `ai` / `ao` | `OphydAI` / `OphydAO` |
| `io` | `rtd` | `OphydRTD` |
| `mag` | `dante` | `OphydPSDante` |
| `diag` | `bpm` | `SppOphydBpm` |
| `vac` | `ipcmini` | `OphydVPC` |

> Always guard with `if self.motor is None:` — `create_device()` returns `None` when ophyd is unavailable.

---

## 18. Plugin Validation

When a plugin is added (via REST or startup config), the framework validates it:

1. **Clone** — git repository is cloned (or local path staged)
2. **Dependencies** — `requirements.txt` installed if present
3. **Config** — `config.yaml` loaded and structurally validated
4. **Syntax** — Python files parsed via AST
5. **Import** — module imported to check for runtime errors
6. **Inheritance** — at least one class derives from `TaskBase` or `JobBase`
7. **Abstract methods** — `initialize()`, `execute()`, `cleanup()` (tasks) or `initialize()`, `execute()` (jobs) must be implemented

If any check fails, the plugin is rejected with a descriptive error.

---

## 19. PV Naming and Prefixes

PV names follow the pattern:

```
{CONTROLLER_PREFIX}:{PLUGIN_PREFIX}:{PV_NAME}
```

- **CONTROLLER_PREFIX**: Set via `IOCMNG_PREFIX` env var or the controller config
- **PLUGIN_PREFIX**: Set via `prefix` in `config.yaml`, or defaults to the plugin name uppercased
- **PV_NAME**: From the `arguments` section of `config.yaml` plus default PVs

Example with `IOCMNG_PREFIX=SPARC:CONTROL` and plugin prefix `CHECK_MOTOR`:

```
SPARC:CONTROL:CHECK_MOTOR:ENABLE
SPARC:CONTROL:CHECK_MOTOR:STATUS
SPARC:CONTROL:CHECK_MOTOR:MESSAGE
SPARC:CONTROL:CHECK_MOTOR:CYCLE_COUNT
SPARC:CONTROL:CHECK_MOTOR:MOVING        (custom output)
SPARC:CONTROL:CHECK_MOTOR:SHUTTER_CMD   (custom output)
```

---

## 20. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IOCMNG_CONFIG` | — | Path to controller config.yaml |
| `IOCMNG_BEAMLINE_CONFIG` | — | Path to values.yaml |
| `IOCMNG_PLUGINS_CONFIG` | — | Path to startup plugins YAML |
| `IOCMNG_PLUGINS_DIR` | `/data/plugins` | Plugin storage directory |
| `IOCMNG_PREFIX` | — | Override controller PV prefix |
| `IOCMNG_HOST` | `0.0.0.0` | Server bind address |
| `IOCMNG_PORT` | `8080` | Server port |
| `IOCMNG_DISABLE_OPHYD` | `true` | Skip ophyd initialization |
| `IOCMNG_PVA` | `true` | Use PVA (true) or CA (false) |
| `IOCMNG_LOG_LEVEL` | `info` | Logging level |

---

## 21. JSON Schema

The full JSON schema for `config.yaml` validation is at `schemas/iocmng-config.schema.json`.

To use it in your editor (VS Code with YAML extension):

```yaml
# .vscode/settings.json
{
  "yaml.schemas": {
    "./schemas/iocmng-config.schema.json": "config.yaml"
  }
}
```

Or add the schema reference directly in your `config.yaml`:

```yaml
# yaml-language-server: $schema=../../schemas/iocmng-config.schema.json
parameters:
  mode: continuous
  ...
```
