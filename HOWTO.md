# iocmng — HOWTO Guide

Step-by-step recipes for common tasks.

---

## Table of Contents

1. [Create a Simple Monitoring Task](#1-create-a-simple-monitoring-task)
2. [Create a Declarative Interlock (Zero Code)](#2-create-a-declarative-interlock-zero-code)
3. [Wire Inputs to External PVs](#3-wire-inputs-to-external-pvs)
4. [Use Ring Buffers for Signal Processing](#4-use-ring-buffers-for-signal-processing)
5. [Compute Derived Outputs with Transforms](#5-compute-derived-outputs-with-transforms)
6. [Create an Event-Driven Reactive Task](#6-create-an-event-driven-reactive-task)
7. [Write Interlock Rules with Actuators](#7-write-interlock-rules-with-actuators)
8. [Create a Motor Movement Safety Interlock](#8-create-a-motor-movement-safety-interlock)
9. [Create a Noise Monitor with Alarm](#9-create-a-noise-monitor-with-alarm)
10. [Register Custom Functions](#10-register-custom-functions)
11. [Create a One-Shot Job](#11-create-a-one-shot-job)
12. [Deploy a Plugin via REST API](#12-deploy-a-plugin-via-rest-api)
13. [Run a Plugin Standalone (Local Dev)](#13-run-a-plugin-standalone-local-dev)
14. [Use Ophyd Devices for Motor Control](#14-use-ophyd-devices-for-motor-control)
15. [Read/Write External PVs Directly](#15-readwrite-external-pvs-directly)
16. [Create a Triggered Task](#16-create-a-triggered-task)
17. [Pre-load Plugins on Server Startup](#17-pre-load-plugins-on-server-startup)
18. [Chain Transforms Together](#18-chain-transforms-together)
19. [Use CA Instead of PVA](#19-use-ca-instead-of-pva)
20. [Debug a Running Task](#20-debug-a-running-task)

---

## 1. Create a Simple Monitoring Task

**Goal**: Read a sensor value every second and write it to an output PV.

**my_monitor.py**
```python
from iocmng import TaskBase

class MyMonitor(TaskBase):
    def initialize(self):
        self.logger.info("Monitor starting")

    def execute(self):
        reading = self.get_pv("INPUT") or 0.0
        self.set_pv("OUTPUT", reading * 1.5)

        if reading > self.parameters.get("threshold", 75):
            self.set_pv("ALARM", 1)
        else:
            self.set_pv("ALARM", 0)

    def cleanup(self):
        self.logger.info("Monitor stopped")
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 1.0
  threshold: 75.0

arguments:
  inputs:
    INPUT:
      type: float
      value: 0.0
      unit: "V"
      prec: 3
  outputs:
    OUTPUT:
      type: float
      value: 0.0
    ALARM:
      type: bool
      value: 0
      znam: "OK"
      onam: "ALARM"
```

**Run**:
```bash
iocmng-run -m my_monitor --config config.yaml --prefix TEST:IOC --name monitor
```

---

## 2. Create a Declarative Interlock (Zero Code)

**Goal**: Monitor 3 external PVs and trigger an alarm if any condition is met. No Python logic needed.

**interlock.py**
```python
from iocmng import DeclarativeTask

class Interlock(DeclarativeTask):
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
      link: "DEVICE:TEMP"
    pressure:
      type: float
      value: 0.0
      link: "DEVICE:PRESSURE"
    flow:
      type: float
      value: 0.0
      link: "DEVICE:FLOW"
  outputs:
    ALARM:
      type: bool
      value: 0
      znam: "OK"
      onam: "ALARM"
    MSG:
      type: string
      value: "OK"

rule_defaults:
  ALARM: 0
  MSG: "OK"

rules:
  - id: OVER_TEMP
    condition: "temp > 80"
    message: "Temperature exceeded 80°C"
    message_pv: MSG
    outputs:
      ALARM: 1

  - id: OVER_PRESSURE
    condition: "pressure > 5.0"
    message: "Pressure exceeded 5 bar"
    message_pv: MSG
    outputs:
      ALARM: 1

  - id: LOW_FLOW
    condition: "flow < 0.1"
    message: "Coolant flow too low"
    message_pv: MSG
    outputs:
      ALARM: 1
```

**How it works**:
- Every second, the framework reads `DEVICE:TEMP`, `DEVICE:PRESSURE`, `DEVICE:FLOW`
- Resets `ALARM` to 0 and `MSG` to "OK" (rule_defaults)
- Evaluates rules top to bottom
- If any condition is true, sets `ALARM=1` and writes a timestamped message to `MSG`

---

## 3. Wire Inputs to External PVs

**Goal**: Automatically track external PV values without writing any `pv_client.get()` code.

### Option A: Polling (default)

```yaml
arguments:
  inputs:
    beam_current:
      type: float
      value: 0.0
      link: "LINAC:BPM01:CURRENT"
      # link_mode: poll   (default)
      # poll_rate: null    (uses task interval)
```

The value of `LINAC:BPM01:CURRENT` is read once per task cycle and stored in `self.link_values["beam_current"]`.

### Option B: Polling with custom rate

```yaml
    beam_current:
      type: float
      value: 0.0
      link: "LINAC:BPM01:CURRENT"
      poll_rate: 0.5       # Read every 0.5 seconds, even if task interval is 2s
```

### Option C: Monitor (subscription)

```yaml
    beam_current:
      type: float
      value: 0.0
      link: "LINAC:BPM01:CURRENT"
      link_mode: monitor   # Persistent subscription — updates on every PV change
      trigger: true         # Fire on_input_changed() when value changes
```

### In Python code

```python
def execute(self):
    current = self.link_values.get("beam_current")
    self.logger.info("Beam current: %s", current)
```

In declarative rules:
```yaml
rules:
  - id: LOW_BEAM
    condition: "beam_current < 0.01"
    outputs: { alarm: 1 }
```

---

## 4. Use Ring Buffers for Signal Processing

**Goal**: Accumulate the last 200 readings of a signal and compute statistics.

**config.yaml**
```yaml
arguments:
  inputs:
    signal:
      type: float
      value: 0.0
      link: "DETECTOR:ADC:CH1"
      buffer_size: 200          # Keep last 200 samples in a ring buffer
  outputs:
    avg:
      type: float
      value: 0.0
    noise:
      type: float
      value: 0.0

transforms:
  - output: avg
    expression: "mean(signal_buf)"
  - output: noise
    expression: "std(signal_buf)"
```

**How it works**:
- `buffer_size: 200` creates a `deque(maxlen=200)`
- Each poll cycle, the new value is appended to the buffer
- `signal_buf` (note the `_buf` suffix) is a list of up to 200 floats
- `mean(signal_buf)` computes the running average
- `std(signal_buf)` computes the running standard deviation

### In Python code (TaskBase)

```python
def execute(self):
    buf = self._link_buffers.get("signal")
    if buf and len(buf) > 10:
        values = list(buf)
        avg = sum(values) / len(values)
        self.set_pv("AVG", avg)
```

---

## 5. Compute Derived Outputs with Transforms

**Goal**: Create computed PVs from input values using expressions.

**config.yaml**
```yaml
arguments:
  inputs:
    voltage:
      type: float
      value: 0.0
      link: "PS:VOLTAGE"
    current:
      type: float
      value: 0.0
      link: "PS:CURRENT"
  outputs:
    power:
      type: float
      value: 0.0
    resistance:
      type: float
      value: 0.0

transforms:
  - output: power
    expression: "voltage * current"
  - output: resistance
    expression: "voltage / current if current != 0 else 0"
```

### Available in expressions

- Input values: `voltage`, `current`
- Buffer arrays: `voltage_buf`, `current_buf` (if `buffer_size` is set)
- Parameters: any key from `parameters`
- Functions: `mean`, `std`, `sqrt`, `clamp`, etc.
- Results of earlier transforms in the same cycle

---

## 6. Create an Event-Driven Reactive Task

**Goal**: React immediately when a wired input changes, without polling.

**config.yaml**
```yaml
parameters:
  mode: reactive
  interval: 5.0         # Heartbeat interval (for CYCLE_COUNT)
  pva: false

arguments:
  inputs:
    motor_busy:
      type: int
      value: 0
      link: "MOT:AXIS01:BUSY"
      link_mode: monitor
      trigger: true
  outputs:
    shutter:
      type: bool
      value: 0
      znam: "Open"
      onam: "Closed"

rules:
  - id: CLOSE_SHUTTER
    condition: "motor_busy == 1"
    outputs:
      shutter: 1
```

**How it works**:
- `mode: reactive` — no polling loop
- `link_mode: monitor` — subscribes to `MOT:AXIS01:BUSY`
- `trigger: true` — when the value changes, fires `on_input_changed()`
- In reactive mode, every trigger also runs transforms → rules

**With Python override**:

```python
from iocmng import TaskBase

class ShutterController(TaskBase):
    def initialize(self):
        pass

    def execute(self):
        pass  # Not called in reactive mode

    def cleanup(self):
        pass

    def on_input_changed(self, key, value, old_value):
        self.logger.info("%s changed: %s -> %s", key, old_value, value)
        if key == "motor_busy" and value == 1:
            self.set_pv("shutter", 1)
```

---

## 7. Write Interlock Rules with Actuators

**Goal**: When a condition is detected, not only set an alarm but also write a command to an external PV.

**config.yaml**
```yaml
arguments:
  inputs:
    rf_power:
      type: int
      value: 0
      link: "LLRF:APP:RF_CTRL"
    chiller_running:
      type: int
      value: 0
      link: "CHL:STATUS:RUN"
  outputs:
    INTLK_ACT:
      type: bool
      value: 0
      znam: "OK"
      onam: "TRIGGERED"

rule_defaults:
  INTLK_ACT: 0

rules:
  - id: CHILLER_FAIL
    condition: "chiller_running == 0 and rf_power == 1"
    message: "Chiller stopped while RF is active — shutting down RF"
    actuators:
      rf_power: 0          # Write 0 to LLRF:APP:RF_CTRL
    outputs:
      INTLK_ACT: 1
```

**Actuators** write values to the **external** PV that a wired input is linked to. This is how the framework implements protective actions — e.g., shutting down RF when cooling fails.

---

## 8. Create a Motor Movement Safety Interlock

**Goal**: Monitor 20+ motor BUSY PVs. If any motor is moving, close the safety shutters.

**check_motor_movement.py**
```python
from iocmng import DeclarativeTask

class CheckMotorMovement(DeclarativeTask):
    pass
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 0.5
  pva: false

arguments:
  inputs:
    motor1_busy:
      type: int
      value: 0
      link: "SPARC:MOT:TML:AXIS01:BUSY"
      trigger: true
    motor2_busy:
      type: int
      value: 0
      link: "SPARC:MOT:TML:AXIS02:BUSY"
      trigger: true
    motor3_busy:
      type: int
      value: 0
      link: "SPARC:MOT:TML:AXIS03:BUSY"
      trigger: true
    # ... add as many motors as needed
  outputs:
    MOVING:
      type: bool
      value: 0
      znam: "Stopped"
      onam: "Moving"
    SHUTTER_CMD:
      type: bool
      value: 1
      link: "SPARC:SHT:ICP:CATLAS01:CMD"   # Wired output — auto-forwards

rule_defaults:
  MOVING: 0
  SHUTTER_CMD: 1    # Default: shutters open

rules:
  - id: MOTOR_MOVING
    condition: "any_of(motor1_busy, motor2_busy, motor3_busy)"
    message: "Motor moving — closing shutters"
    outputs:
      MOVING: 1
      SHUTTER_CMD: 0   # Close shutters (auto-forwarded to external PV)
```

The `any_of()` function returns true if any argument is truthy.

---

## 9. Create a Noise Monitor with Alarm

**Goal**: Accumulate 500 samples of a signal, compute RMS and standard deviation, alarm if noise exceeds threshold.

**noise_monitor.py**
```python
from iocmng import DeclarativeTask

class NoiseMonitor(DeclarativeTask):
    pass
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 0.1        # 10 Hz sampling
  noise_threshold: 0.5

arguments:
  inputs:
    signal:
      type: float
      value: 0.0
      link: "ADC:CH1:VALUE"
      buffer_size: 500
  outputs:
    rms_value:
      type: float
      value: 0.0
    std_value:
      type: float
      value: 0.0
    mean_value:
      type: float
      value: 0.0
    alarm:
      type: bool
      value: 0
      znam: "OK"
      onam: "NOISY"

transforms:
  - output: rms_value
    expression: "rms(signal_buf)"
  - output: std_value
    expression: "std(signal_buf)"
  - output: mean_value
    expression: "mean(signal_buf)"

rule_defaults:
  alarm: 0

rules:
  - id: HIGH_NOISE
    condition: "std(signal_buf) > noise_threshold"
    message: "Signal noise exceeds threshold"
    outputs:
      alarm: 1
```

Note how `noise_threshold` is a parameter (not a PV) and is accessible in rule conditions because the eval context includes task parameters.

---

## 10. Register Custom Functions

**Goal**: Add a custom function (e.g., a band-pass filter) usable in expressions.

**my_plugin.py**
```python
from iocmng import TaskBase
from iocmng.core.functions import register

# Register at module level (available as soon as the module is imported)
def bandpass(values, low=0.1, high=0.9):
    """Keep only values within [low, high]."""
    return [v for v in values if low <= v <= high]

register("bandpass", bandpass)

def peak_to_peak(values):
    """Peak-to-peak amplitude."""
    if not values:
        return 0.0
    return max(values) - min(values)

register("peak_to_peak", peak_to_peak)


class MyPlugin(TaskBase):
    def initialize(self):
        pass

    def execute(self):
        pass

    def cleanup(self):
        pass
```

**config.yaml**
```yaml
arguments:
  inputs:
    signal:
      type: float
      value: 0.0
      link: "ADC:CH1"
      buffer_size: 200
  outputs:
    filtered_count:
      type: int
      value: 0
    amplitude:
      type: float
      value: 0.0

transforms:
  - output: filtered_count
    expression: "length(bandpass(signal_buf, 0.2, 0.8))"
  - output: amplitude
    expression: "peak_to_peak(signal_buf)"
```

---

## 11. Create a One-Shot Job

**Goal**: Run a diagnostic/report action triggered via REST API.

**diagnostics.py**
```python
from iocmng import JobBase
from iocmng.base.job import JobResult

class Diagnostics(JobBase):
    def initialize(self):
        self.logger.info("Preparing diagnostics")

    def execute(self) -> JobResult:
        # Run checks
        checks = {
            "pv_connectivity": self._check_pvs(),
            "memory_usage_mb": self._get_memory(),
        }

        all_ok = all(v for v in checks.values() if isinstance(v, bool))
        return JobResult(
            success=all_ok,
            data=checks,
            message="All checks passed" if all_ok else "Some checks failed"
        )

    def _check_pvs(self):
        from iocmng.core import pv_client
        try:
            pv_client.get("MY:IOC:HEARTBEAT", timeout=2.0)
            return True
        except Exception:
            return False

    def _get_memory(self):
        import os
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
```

**config.yaml**
```yaml
parameters: {}
arguments:
  outputs:
    RESULT:
      type: string
      value: ""
```

**Run via REST**:
```bash
curl -X POST http://localhost:8080/api/v1/jobs/diagnostics/run
```

**Response**:
```json
{
  "success": true,
  "data": {"pv_connectivity": true, "memory_usage_mb": 45.2},
  "message": "All checks passed",
  "timestamp": "2026-04-12T10:30:00.123456"
}
```

---

## 12. Deploy a Plugin via REST API

### From a Git Repository

```bash
curl -X POST http://localhost:8080/api/v1/plugins \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-interlock",
    "git_url": "https://github.com/org/beamline-tasks.git",
    "path": "plugins/interlock/",
    "branch": "main",
    "pat": "ghp_xxx",
    "auto_start": true,
    "auto_start_on_boot": true,
    "autostart_order": 5,
    "parameters": {"interval": 0.5}
  }'
```

### From a Local Directory

```bash
curl -X POST http://localhost:8080/api/v1/plugins \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-interlock",
    "local_path": "/home/user/my-interlock",
    "auto_start": true
  }'
```

### Hot-reload After Code Change

```bash
curl -X POST http://localhost:8080/api/v1/plugins/my-interlock/restart
```

Re-clones the repo, validates, and swaps the running instance only if validation passes.

### Remove

```bash
curl -X DELETE http://localhost:8080/api/v1/plugins/my-interlock
```

### List All

```bash
curl http://localhost:8080/api/v1/plugins
curl "http://localhost:8080/api/v1/plugins?type=task"
```

---

## 13. Run a Plugin Standalone (Local Dev)

No server needed — just run the plugin directly:

```bash
# From the plugin directory
iocmng-run \
  -m my_interlock \
  --config config.yaml \
  --prefix TEST:IOC \
  --name my-interlock \
  --pva false \
  --log-level DEBUG
```

This creates the soft IOC, sets up all PVs, wires inputs/outputs, and starts the task loop.

**Test with EPICS tools**:
```bash
# In another terminal
caget TEST:IOC:MY-INTERLOCK:STATUS
caget TEST:IOC:MY-INTERLOCK:ALARM
camonitor TEST:IOC:MY-INTERLOCK:MSG
```

---

## 14. Use Ophyd Devices for Motor Control

**Goal**: Control a motor through Ophyd and expose position/status via PVs.

**motor_task.py**
```python
from iocmng import TaskBase

class MotorTask(TaskBase):
    def initialize(self):
        self.motor = self.create_device(
            prefix="SPARC:MOT:TML:GUNFLG01",
            devgroup="mot",
            devtype="tml",
            name="GUNFLG01",
        )
        if self.motor is None:
            self.logger.warning("Motor not available (ophyd not installed?)")

    def execute(self):
        if self.motor is None:
            return
        pos = self.motor.user_readback.get()
        busy = self.motor.motor_done_move.get()
        self.set_pv("POSITION", pos)
        self.set_pv("MOVING", int(not busy))

        # Check for move command
        target = self.get_pv("TARGET")
        if self.get_pv("MOVE_CMD"):
            self.motor.move(target, wait=False)
            self.set_pv("MOVE_CMD", 0)

    def cleanup(self):
        if self.motor:
            self.motor.stop()
```

**config.yaml**
```yaml
parameters:
  mode: continuous
  interval: 0.5

arguments:
  inputs:
    TARGET:
      type: float
      value: 0.0
      unit: "mm"
    MOVE_CMD:
      type: bool
      value: 0
      znam: "Idle"
      onam: "Move"
  outputs:
    POSITION:
      type: float
      value: 0.0
      unit: "mm"
      prec: 4
    MOVING:
      type: bool
      value: 0
      znam: "Stopped"
      onam: "Moving"
```

---

## 15. Read/Write External PVs Directly

**Goal**: Access PVs that aren't wired in the config.

```python
from iocmng import TaskBase, pv_client

class MyTask(TaskBase):
    def execute(self):
        # Read
        temp = pv_client.get("OTHER:IOC:TEMP", timeout=2.0)

        # Write
        pv_client.put("OTHER:IOC:COMMAND", 1, timeout=2.0)

        # Subscribe (one-time setup)
        if not hasattr(self, '_sub_key'):
            self._sub_key = pv_client.monitor(
                "OTHER:IOC:HEARTBEAT",
                callback=lambda v: self.logger.info("HB: %s", v)
            )

    def cleanup(self):
        if hasattr(self, '_sub_key'):
            pv_client.unmonitor(self._sub_key)
```

> **Prefer wired inputs** over direct `pv_client` calls. Wired inputs handle buffering, triggering, and eval context integration automatically.

---

## 16. Create a Triggered Task

**Goal**: Execute an action only when an operator clicks a button in CS-Studio/Phoebus.

**triggered_task.py**
```python
from iocmng import TaskBase, pv_client

class TriggeredTask(TaskBase):
    def initialize(self):
        self.logger.info("Ready for trigger")

    def execute(self):
        pass  # Not used in triggered mode

    def cleanup(self):
        pass

    def triggered(self):
        """Called when operator writes 1 to the RUN PV."""
        self.logger.info("Triggered! Running calibration...")
        self.set_status("RUN")
        self.set_message("Calibrating...")

        # Perform work
        result = self._run_calibration()

        self.set_pv("RESULT", result)
        self.set_message(f"Done: {result}")
        self.logger.info("Calibration complete: %s", result)

    def _run_calibration(self):
        return 42.0
```

**config.yaml**
```yaml
parameters:
  mode: triggered

arguments:
  outputs:
    RESULT:
      type: float
      value: 0.0
```

**Trigger from CS-Studio**: Write `1` to `PREFIX:TASK_NAME:RUN`.

---

## 17. Pre-load Plugins on Server Startup

**Goal**: Automatically start plugins when the server boots.

Create `plugins.yaml`:

```yaml
plugins:
  - name: softinterlock
    git_url: https://baltig.infn.it/lnf-da-control/epik8-sparc.git
    path: config/iocs/softinterlock/
    branch: main
    auto_start: true
    auto_start_on_boot: true
    autostart_order: 1

  - name: check-motor-movement
    git_url: https://baltig.infn.it/lnf-da-control/epik8-sparc.git
    path: config/iocs/check_motor_movement/
    branch: main
    auto_start: true
    auto_start_on_boot: true
    autostart_order: 2

  - name: noise-monitor
    local_path: /opt/plugins/noise-monitor
    auto_start: true
    autostart_order: 10
```

Start the server:
```bash
IOCMNG_PLUGINS_CONFIG=plugins.yaml iocmng-server
```

Plugins are loaded in `autostart_order` order, then alphabetically.

---

## 18. Chain Transforms Together

**Goal**: Build multi-step computations where later transforms use earlier results.

```yaml
arguments:
  inputs:
    raw:
      type: float
      value: 0.0
      link: "SENSOR:RAW"
      buffer_size: 100
  outputs:
    scaled:
      type: float
      value: 0.0
    offset:
      type: float
      value: 0.0
    final:
      type: float
      value: 0.0

transforms:
  # Step 1: Scale the raw mean
  - output: scaled
    expression: "mean(raw_buf) * 2.5"

  # Step 2: Apply offset (using parameter)
  - output: offset
    expression: "scaled - calibration_offset"

  # Step 3: Clamp the result (using step 2 output)
  - output: final
    expression: "clamp(offset, 0, 100)"
```

Transforms are evaluated **in order**: `scaled` is computed first, then `offset` can reference `scaled`, then `final` can reference `offset`.

---

## 19. Use CA Instead of PVA

**Goal**: Force Channel Access transport instead of PV Access.

### In config.yaml

```yaml
parameters:
  pva: false
```

### In standalone runner

```bash
iocmng-run -m my_plugin --config config.yaml --pva false
```

### Via environment variable

```bash
IOCMNG_PVA=false iocmng-server
```

---

## 20. Debug a Running Task

### Enable debug logging

```bash
iocmng-run -m my_plugin --config config.yaml --log-level DEBUG
```

### Monitor PVs with EPICS tools

```bash
# Watch all changes on a PV
camonitor MY:IOC:TASK:ALARM

# Read STATUS
caget MY:IOC:TASK:STATUS

# Read MESSAGE
caget MY:IOC:TASK:MESSAGE

# Check cycle count (is the task running?)
camonitor MY:IOC:TASK:CYCLE_COUNT

# Disable/enable
caput MY:IOC:TASK:ENABLE 0
caput MY:IOC:TASK:ENABLE 1
```

### Inspect via REST API

```bash
# Plugin details
curl http://localhost:8080/api/v1/plugins/my-task

# Startup metadata (PV list, parameters, mode)
curl http://localhost:8080/api/v1/tasks/my-task/startup

# Health check
curl http://localhost:8080/api/v1/health
```

### Common Issues

| Symptom | Check |
|---------|-------|
| PVs not found | `EPICS_CA_ADDR_LIST` set? PVA vs CA mismatch? |
| Task stuck at INIT | Error in `initialize()` — check logs |
| `link_values` empty | Wired PVs unreachable? Timeout too short? |
| `_buf` always empty | `buffer_size` not set in config? |
| Rule never fires | Check condition syntax. Test with `safe_eval()` directly |
| Transform returns None | Expression error — check logs for "Transform error" |
| `on_input_changed` not called | `trigger: true` not set? Wrong `link_mode`? |
