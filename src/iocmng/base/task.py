"""
Base class for continuous tasks in the IOC Manager framework.

A task is a long-running process that executes repeatedly in a loop.
User applications should subclass TaskBase and implement the required
abstract methods: initialize(), execute(), and cleanup().
"""

import datetime
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Dict, Optional

from iocmng.core.plugin_spec import PluginSpec, RuleSpec, create_softioc_record
from iocmng.core.safe_eval import safe_eval


class TaskBase(ABC):
    """Abstract base class for all continuous IOC Manager tasks.

    A task runs continuously in its own thread, executing the `execute()` method
    on each cycle. Tasks can optionally integrate with EPICS soft IOC PVs when
    a PV provider is configured.

    Subclasses must implement:
        - initialize(): one-time setup
        - execute(): called each cycle
        - cleanup(): teardown when stopping

    Example::

        from iocmng import TaskBase

        class MyMonitor(TaskBase):
            def initialize(self):
                self.logger.info("Starting monitor")

            def execute(self):
                # do monitoring work
                self.set_pv("VALUE", 42.0)

            def cleanup(self):
                self.logger.info("Stopping monitor")
    """

    # Class-level marker used for validation
    _iocmng_type = "task"

    def __init__(
        self,
        name: str,
        parameters: Optional[Dict[str, Any]] = None,
        pv_definitions: Optional[Dict[str, Any]] = None,
        beamline_config: Optional[Dict[str, Any]] = None,
        ophyd_devices: Optional[Dict[str, object]] = None,
        prefix: Optional[str] = None,
        plugin_prefix: Optional[str] = None,
        device_resolver: Optional[Any] = None,
        plugin_spec: Optional[PluginSpec] = None,
    ):
        """Initialize a task.

        Args:
            name: Unique task name.
            parameters: Task-specific parameters.
            pv_definitions: PV definitions (inputs/outputs) for soft IOC integration.
            beamline_config: Full beamline configuration dict.
            ophyd_devices: Dictionary of Ophyd device instances.
            prefix: Controller/beamline PV prefix.
            plugin_prefix: Optional task-specific PV prefix segment from config.yaml.
            device_resolver: Optional callable(name) -> device for lazy creation.
        """
        self.name = name
        self.plugin_spec = plugin_spec or PluginSpec.from_runtime(
            parameters=parameters,
            pv_definitions=pv_definitions,
            plugin_prefix=plugin_prefix or self.name.upper(),
        )
        self.parameters = dict(self.plugin_spec.parameters)
        self.pv_definitions = self.plugin_spec.pv_definitions
        self.beamline_config = beamline_config or {}
        self.ophyd_devices = ophyd_devices or {}
        self._device_resolver = device_resolver
        self.logger = logging.getLogger(f"iocmng.task.{name}")

        # PV storage
        self.pvs: Dict[str, Any] = {}

        # Task control
        self.enabled = True
        self.running = False
        self.task_lock = threading.Lock()

        # PV prefix
        self.plugin_prefix = self.plugin_spec.prefix or self.name.upper()
        self.pv_prefix = self._get_pv_prefix(prefix)
        self.logger.debug(
            "Task prefix resolution: name=%s controller_prefix=%r plugin_prefix=%r beamline=%r namespace=%r resolved_pv_prefix=%r",
            self.name,
            prefix,
            self.plugin_prefix,
            self.beamline_config.get("beamline"),
            self.beamline_config.get("namespace"),
            self.pv_prefix,
        )

        # Task mode: 'continuous' (default), 'triggered', or 'reactive'
        mode = self.parameters.get("mode") or (
            "triggered" if self.parameters.get("triggered") else "continuous"
        )
        self.mode = str(mode).lower() if isinstance(mode, str) else "continuous"
        if self.mode not in ("continuous", "triggered", "reactive"):
            self.mode = "continuous"

        # Cycle counter
        self.cycle_count = 0
        self._trigger_thread = None
        self._thread: Optional[threading.Thread] = None

        # ── Link state ───────────────────────────────────────────────
        # Current values of all wired inputs, keyed by input name.
        self.link_values: Dict[str, Any] = {}
        # Previous values — used for trigger / on_input_changed detection.
        self._link_prev: Dict[str, Any] = {}
        # Per-input poll timers (for inputs with custom poll_rate).
        self._link_poll_timers: Dict[str, float] = {}
        # Ring buffers for inputs/outputs with buffer_size.
        self._link_buffers: Dict[str, deque] = {}
        # ── Connection tracking ──────────────────────────────────────
        # Ordered lists of wired input/output names (stable index for array PVs).
        self._wired_input_names: list = [n for n, s in self.plugin_spec.inputs.items() if s.wired]
        self._wired_output_names: list = [n for n, s in self.plugin_spec.outputs.items() if s.wired]
        # Per-port connection state: True = connected, False = disconnected.
        self._link_connected: Dict[str, bool] = {}
        # Whether link monitors are currently registered in pv_client.
        self._link_monitors_active = False
        self._init_buffers()

    def _get_pv_prefix(self, controller_prefix: Optional[str] = None) -> str:
        if controller_prefix:
            return f"{controller_prefix}:{self.plugin_prefix}"
        beamline = self.beamline_config.get("beamline", "BEAMLINE").upper()
        namespace = self.beamline_config.get("namespace", "DEFAULT").upper()
        if beamline == namespace:
            return f"{beamline}:{self.plugin_prefix}"
        return f"{beamline}:{namespace}:{self.plugin_prefix}"

    # ------------------------------------------------------------------
    # Soft IOC PV integration
    # ------------------------------------------------------------------

    def build_pvs(self):
        """Build PVs using softioc builder.

        Creates the default control PVs (ENABLE, STATUS, MESSAGE, etc.)
        plus any PVs defined in pv_definitions from the plugin config YAML.
        Called by the controller before IOC initialization.
        """
        from softioc import builder

        builder.SetDeviceName(self.pv_prefix)

        self.pvs["ENABLE"] = builder.boolOut(
            "ENABLE",
            initial_value=1,
            on_update=lambda v: self._on_enable_changed(v),
        )
        self.pvs["STATUS"] = builder.mbbIn(
            "STATUS",
            initial_value=0,
            ZRST="INIT",
            ONST="RUN",
            TWST="PAUSED",
            THST="END",
            FRST="ERROR",
        )
        self.pvs["MESSAGE"] = builder.stringIn("MESSAGE", initial_value="Initialized")

        # VERSION: library or user-specified version
        import iocmng
        version_str = self.plugin_spec.parameters.get("version", iocmng.__version__)
        self.pvs["VERSION"] = builder.stringIn("VERSION", initial_value=str(version_str))

        if self.mode == "triggered":
            self.pvs["RUN"] = builder.boolOut(
                "RUN",
                initial_value=0,
                on_update=lambda v: self._on_run_trigger(v),
            )
        if self.mode in ("continuous", "reactive"):
            self.pvs["CYCLE_COUNT"] = builder.longIn("CYCLE_COUNT", initial_value=0)

        reserved = {"STATUS", "MESSAGE", "ENABLE", "RUN", "CYCLE_COUNT", "VERSION"}
        for pv_name, pv_spec in self.plugin_spec.inputs.items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = create_softioc_record(pv_spec)

        for pv_name, pv_spec in self.plugin_spec.outputs.items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = create_softioc_record(pv_spec)

        # ── Connection-status waveform PVs ───────────────────────────
        # One element per wired port: 1 = connected, 0 = disconnected.
        n_inp = len(self._wired_input_names)
        n_out = len(self._wired_output_names)
        if n_inp > 0:
            self.pvs["CONN_INP"] = builder.WaveformIn(
                "CONN_INP", initial_value=[0] * n_inp, length=n_inp,
            )
            # Companion PV listing port names in the same index order.
            labels = "\n".join(self._wired_input_names)
            self.pvs["CONN_INP_NAMES"] = builder.stringIn(
                "CONN_INP_NAMES", initial_value=labels[:39],
            )
        if n_out > 0:
            self.pvs["CONN_OUT"] = builder.WaveformIn(
                "CONN_OUT", initial_value=[0] * n_out, length=n_out,
            )
            labels = "\n".join(self._wired_output_names)
            self.pvs["CONN_OUT_NAMES"] = builder.stringIn(
                "CONN_OUT_NAMES", initial_value=labels[:39],
            )

        self.logger.info(f"Created {len(self.pvs)} PVs with prefix: {self.pv_prefix}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the task in its own thread."""
        self.logger.info(f"Starting task: {self.name}")
        # AS info dump for traceability at startup.
        self.logger.info(
            "AS_INFO task=%s mode=%s pv_prefix=%s parameters=%s pv_definitions=%s",
            self.name,
            self.mode,
            self.pv_prefix,
            self.parameters,
            self.pv_definitions,
        )
        self.running = True
        self.set_status("RUN")
        self.set_message("Task running")

        # Initial connectivity check: poll all wired inputs once to seed
        # link_values and surface disconnected PVs before monitors take over.
        self._initial_connectivity_check()

        # Start link monitors for wired inputs with mode=monitor
        self._start_link_monitors()

        if self.mode == "continuous":
            self._thread = threading.Thread(
                target=self._run_wrapper, name=f"task-{self.name}", daemon=True
            )
            self._thread.start()
        elif self.mode == "reactive":
            # Reactive: no polling loop.  Heartbeat thread for cycle count.
            self._thread = threading.Thread(
                target=self._reactive_heartbeat, name=f"task-{self.name}-heartbeat", daemon=True
            )
            self._thread.start()
        else:
            self.set_status("INIT")
            self.set_message("Ready for trigger")

    def _run_wrapper(self):
        try:
            while self.running:
                if not self.enabled:
                    time.sleep(0.1)
                    continue
                self._poll_links()
                self._evaluate_transforms()
                self._evaluate_rules()
                self.execute()
                self.step_cycle()
                interval = self.parameters.get("interval", 1.0)
                time.sleep(interval)
        except Exception as e:
            self.logger.error(f"Error in task execution: {e}", exc_info=True)
            self.set_status("ERROR")
            self.set_message(f"Error: {str(e)}")
            self.running = False

    def _reactive_heartbeat(self):
        """Background thread for reactive mode — updates cycle count."""
        interval = self.parameters.get("interval", 1.0)
        try:
            while self.running:
                if not self.enabled:
                    time.sleep(0.1)
                    continue
                self.step_cycle()
                time.sleep(interval)
        except Exception as e:
            self.logger.error(f"Reactive heartbeat error: {e}", exc_info=True)

    def stop(self):
        """Stop the task gracefully."""
        self.logger.info(f"Stopping task: {self.name}")
        self.running = False
        self._stop_link_monitors()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self.set_status("END")
        self.set_message("Task stopped")
        self.cleanup()

    # ------------------------------------------------------------------
    # PV helpers
    # ------------------------------------------------------------------

    def set_pv(self, pv_name: str, value: Any):
        if pv_name in self.pvs:
            self.pvs[pv_name].set(value)
        # Auto-forward to linked external PV for wired outputs
        spec = self.plugin_spec.outputs.get(pv_name)
        if spec is not None and spec.wired:
            try:
                from iocmng.core import pv_client
                timeout = float(self.parameters.get("timeout", 5.0))
                pv_client.put(spec.link, value, timeout=timeout)
            except Exception as exc:
                self.logger.warning("output forward failed: %s -> %s: %s", pv_name, spec.link, exc)

    def get_pv(self, pv_name: str) -> Any:
        if pv_name in self.pvs:
            return self.pvs[pv_name].get()
        return None

    def set_output(self, pv_name: str, value: Any):
        self.set_pv(pv_name, value)

    def get_output(self, pv_name: str) -> Any:
        return self.get_pv(pv_name)

    def set_input(self, pv_name: str, value: Any):
        self.set_pv(pv_name, value)

    def get_input(self, pv_name: str) -> Any:
        return self.get_pv(pv_name)

    def set_status(self, status: str):
        status_map = {"INIT": 0, "RUN": 1, "PAUSED": 2, "END": 3, "ERROR": 4}
        if status.upper() in status_map and "STATUS" in self.pvs:
            try:
                self.pvs["STATUS"].set(status_map[status.upper()])
            except Exception:
                pass

    def set_message(self, message: str):
        if "MESSAGE" in self.pvs:
            try:
                self.pvs["MESSAGE"].set(str(message)[:39])
            except Exception:
                pass

    def _on_enable_changed(self, value):
        self.enabled = bool(value)
        if not self.enabled:
            self.set_status("PAUSED")
            self.set_message("Task paused")
            if self.running:
                self._stop_link_monitors()
        else:
            if self.mode == "triggered":
                self.set_status("INIT")
                self.set_message("Ready for trigger")
            else:
                self.set_status("RUN")
                self.set_message("Task running")
            if self.running:
                self._start_link_monitors()

    # ------------------------------------------------------------------
    # Cycle helpers
    # ------------------------------------------------------------------

    def step_cycle(self):
        if self.mode not in ("continuous", "reactive"):
            return
        self.cycle_count += 1
        if "CYCLE_COUNT" in self.pvs:
            try:
                self.pvs["CYCLE_COUNT"].set(int(self.cycle_count))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Triggered mode
    # ------------------------------------------------------------------

    def _on_run_trigger(self, value: Any):
        if not bool(value):
            return
        try:
            self.pvs["RUN"].set(0)
        except Exception:
            pass
        with self.task_lock:
            if self._trigger_thread and self._trigger_thread.is_alive():
                return
            self._trigger_thread = threading.Thread(
                target=self._trigger_wrapper, name=f"{self.name}-trigger", daemon=True
            )
            self._trigger_thread.start()

    def _trigger_wrapper(self):
        self.set_status("RUN")
        self.set_message("Executing triggered action")
        try:
            self.triggered()
            self.set_status("END")
            self.set_message("Triggered run completed")
        except Exception as e:
            self.logger.error(f"Error in triggered run: {e}", exc_info=True)
            self.set_status("ERROR")
            self.set_message(f"Error: {str(e)}")
        finally:
            time.sleep(2)
            if self.enabled:
                self.set_status("INIT")
                self.set_message("Ready for trigger")

    def triggered(self):
        """Override to implement one-shot action for triggered mode."""
        pass

    # ------------------------------------------------------------------
    # Ring buffers & eval context
    # ------------------------------------------------------------------

    def _init_buffers(self):
        """Create ring buffers for inputs/outputs with ``buffer_size``."""
        for name, spec in list(self.plugin_spec.inputs.items()) + list(self.plugin_spec.outputs.items()):
            if spec.buffer_size is not None:
                self._link_buffers[name] = deque(maxlen=spec.buffer_size)

    def _buffer_append(self, name: str, value: Any) -> None:
        """Append *value* to the ring buffer for *name*, if one exists."""
        buf = self._link_buffers.get(name)
        if buf is not None:
            try:
                buf.append(float(value))
            except (TypeError, ValueError):
                buf.append(value)

    def _build_eval_context(self) -> Dict[str, Any]:
        """Build the variable dict for safe_eval (rules + transforms).

        Contains:
        - All ``link_values`` (latest scalar for each wired PV)
        - ``<name>_buf`` for every ring buffer
        - Numeric/string parameters (won't shadow existing names)
        """
        ctx: Dict[str, Any] = dict(self.link_values)
        for name, buf in self._link_buffers.items():
            ctx[f"{name}_buf"] = list(buf)
        # Expose parameters that don't shadow inputs/outputs
        for key, val in self.parameters.items():
            if key not in ctx:
                ctx[key] = val
        return ctx

    # ------------------------------------------------------------------
    # Wired inputs — link engine
    # ------------------------------------------------------------------

    def _wired_inputs(self):
        """Yield (name, spec) for inputs that have a link."""
        for name, spec in self.plugin_spec.inputs.items():
            if spec.wired:
                yield name, spec

    def _wired_outputs(self):
        """Yield (name, spec) for outputs that have a link."""
        for name, spec in self.plugin_spec.outputs.items():
            if spec.wired:
                yield name, spec

    def _all_wired(self):
        """Yield (name, spec) for all wired PVs (inputs + outputs)."""
        yield from self._wired_inputs()
        yield from self._wired_outputs()

    def _initial_connectivity_check(self) -> None:
        """Poll all wired inputs once at startup to seed link_values and detect unreachable PVs.

        - Connected PVs: ``link_values`` is seeded with the real value.
        - Unreachable PVs: logged as WARNING; value stays at config default.
        - Connection state is recorded in ``_link_connected`` and published
          to the ``CONN_INP`` array PV.
        - Summary is written to MESSAGE and, if declared, ``SYS_CONN``.
        """
        from iocmng.core import pv_client

        timeout = float(self.parameters.get("timeout", 5.0))
        disconnected: list = []
        connected = 0

        self.set_message("Checking connectivity...")
        self.logger.info("[init-conn] Polling %d wired inputs...",
                         sum(1 for _ in self._wired_inputs()))

        for name, spec in self._wired_inputs():
            try:
                value = pv_client.get(spec.link, timeout=timeout)
                self.link_values[name] = value
                self._link_prev[name] = value
                self._buffer_append(name, value)
                self._link_connected[name] = True
                if name in self.pvs:
                    try:
                        self.pvs[name].set(value)
                    except Exception:
                        pass
                self.logger.info("[init-conn] OK    %s (%s) = %s", name, spec.link, value)
                connected += 1
            except Exception as exc:
                disconnected.append(name)
                self._link_connected[name] = False
                self.logger.warning("[init-conn] FAIL  %s (%s): %s", name, spec.link, exc)

        # Update the CONN_INP array PV
        self._update_conn_pv("input")

        total = connected + len(disconnected)
        if disconnected:
            names = ', '.join(disconnected)
            conn_msg = f"DISCONNECTED ({len(disconnected)}/{total}): {names}"
            self.logger.warning("[init-conn] %s", conn_msg)
            self.set_message(conn_msg[:39])
        else:
            conn_msg = "OK"
            self.logger.info("[init-conn] All %d inputs reachable.", total)
            self.set_message(f"All {total} inputs connected")

        if "SYS_CONN" in self.pvs:
            try:
                self.pvs["SYS_CONN"].set(conn_msg)
            except Exception:
                pass

    def _start_link_monitors(self):
        """Set up pv_client monitors for all wired PVs with mode=monitor."""
        from iocmng.core import pv_client

        if self._link_monitors_active:
            return

        for name, spec in self._all_wired():
            # Initialise value cache (unless already seeded by _initial_connectivity_check)
            if name not in self.link_values:
                self.link_values[name] = spec.value
                self._link_prev[name] = spec.value
            if spec.link_mode == "monitor":
                pv_client.monitor(
                    spec.link,
                    callback=self._make_link_callback(name, spec),
                    name=f"_link_{name}",
                    conn_callback=self._make_conn_callback(name, spec),
                )
                self.logger.info("link monitor: %s -> %s", name, spec.link)
        self._link_monitors_active = True

    def _stop_link_monitors(self):
        """Close all link monitors."""
        from iocmng.core import pv_client

        if not self._link_monitors_active:
            return

        for name, spec in self._all_wired():
            if spec.link_mode == "monitor":
                pv_client.unmonitor(f"_link_{name}")
        self._link_monitors_active = False

    def _make_link_callback(self, name, spec):
        """Return a monitor callback for the given wired input."""
        def _cb(value):
            old = self.link_values.get(name)
            self.link_values[name] = value
            self._buffer_append(name, value)
            # Update local PV mirror if it exists
            if name in self.pvs:
                try:
                    self.pvs[name].set(value)
                except Exception:
                    pass
            if spec.trigger and value != old:
                self._link_prev[name] = old
                try:
                    self.on_input_changed(name, value, old)
                except Exception as exc:
                    self.logger.error("on_input_changed(%s) error: %s", name, exc)
                if self.mode == "reactive":
                    self._evaluate_transforms()
                    self._evaluate_rules()
        return _cb

    def _make_conn_callback(self, name, spec):
        """Return a connection-state callback for the given wired PV."""
        def _conn_cb(connected: bool):
            old = self._link_connected.get(name)
            self._link_connected[name] = connected
            direction = "input" if spec.direction == "input" else "output"
            if connected != old:
                if connected:
                    self.logger.info("[conn] %s (%s) CONNECTED", name, spec.link)
                else:
                    self.logger.warning("[conn] %s (%s) DISCONNECTED", name, spec.link)
                self._update_conn_pv(direction)
        return _conn_cb

    def _update_conn_pv(self, direction: str) -> None:
        """Refresh the CONN_INP or CONN_OUT waveform PV from ``_link_connected``.

        Args:
            direction: ``"input"`` or ``"output"``.
        """
        if direction == "input":
            names = self._wired_input_names
            pv_key = "CONN_INP"
        else:
            names = self._wired_output_names
            pv_key = "CONN_OUT"
        if pv_key not in self.pvs or not names:
            return
        arr = [1 if self._link_connected.get(n, False) else 0 for n in names]
        try:
            self.pvs[pv_key].set(arr)
        except Exception:
            pass

    def _poll_links(self):
        """Read all wired PVs with mode=poll (called from _run_wrapper)."""
        from iocmng.core import pv_client

        timeout = float(self.parameters.get("timeout", 5.0))
        now = time.monotonic()

        for name, spec in self._all_wired():
            if spec.link_mode != "poll":
                continue
            # Per-input poll rate gating
            if spec.poll_rate is not None:
                last = self._link_poll_timers.get(name)
                if last is not None and (now - last) < spec.poll_rate:
                    continue
                self._link_poll_timers[name] = now
            try:
                value = pv_client.get(spec.link, timeout=timeout)
            except Exception as exc:
                self.logger.warning("link poll failed: %s (%s): %s", name, spec.link, exc)
                if self._link_connected.get(name) is not False:
                    self._link_connected[name] = False
                    direction = "input" if spec.direction == "input" else "output"
                    self._update_conn_pv(direction)
                continue
            # Mark connected (update array PV only on state change)
            if not self._link_connected.get(name):
                self._link_connected[name] = True
                direction = "input" if spec.direction == "input" else "output"
                self._update_conn_pv(direction)
            old = self.link_values.get(name)
            self.link_values[name] = value
            self._buffer_append(name, value)
            # Update local PV mirror
            if name in self.pvs:
                try:
                    self.pvs[name].set(value)
                except Exception:
                    pass
            if spec.trigger and value != old:
                self._link_prev[name] = old
                try:
                    self.on_input_changed(name, value, old)
                except Exception as exc:
                    self.logger.error("on_input_changed(%s) error: %s", name, exc)

    def link_put(self, key: str, value: Any, timeout: float = 5.0):
        """Write a value to the external PV of a wired input or output.

        This is the primary way for task code to actuate an external PV
        declared as a wired input or output.

        Args:
            key: The PV name (must have a ``link``).
            value: The value to write.
            timeout: CA/PVA put timeout.

        Raises:
            KeyError: if the PV does not exist or is not wired.
        """
        from iocmng.core import pv_client

        spec = self.plugin_spec.inputs.get(key) or self.plugin_spec.outputs.get(key)
        if spec is None or not spec.wired:
            raise KeyError(f"{key!r} is not a wired PV")
        pv_client.put(spec.link, value, timeout=timeout)
        self.logger.debug("link_put(%s, %s) -> %s", key, value, spec.link)

    def on_input_changed(self, key: str, value: Any, old_value: Any):
        """Hook called when a wired input with trigger=true changes value.

        Override in subclasses to react to individual input changes.
        The default implementation does nothing.
        """
        pass

    # ------------------------------------------------------------------
    # Declarative transforms & rule evaluation
    # ------------------------------------------------------------------

    def _evaluate_transforms(self):
        """Evaluate all declarative transforms, writing results to outputs."""
        transforms = self.plugin_spec.transforms
        if not transforms:
            return
        ctx = self._build_eval_context()
        for t in transforms:
            try:
                result = safe_eval(t.expression, ctx)
                self.set_pv(t.output, result)
                # Update context so later transforms can see earlier results
                ctx[t.output] = result
            except Exception as exc:
                self.logger.error("Transform %s error: %s", t.output, exc)

    def _evaluate_rules(self):
        """Evaluate all declarative rules against current link values.

        Applies ``rule_defaults`` first, then fires actuators and sets
        outputs for rules whose condition is met.
        """
        rules = self.plugin_spec.rules
        if not rules:
            return

        # Apply rule defaults before evaluation (reset phase)
        for pv_name, value in self.plugin_spec.rule_defaults.items():
            self.set_pv(pv_name, value)

        for rule in rules:
            try:
                if safe_eval(rule.condition, self._build_eval_context()):
                    self._fire_rule(rule)
            except Exception as exc:
                self.logger.error("Rule %s eval error: %s", rule.id, exc)

    def _fire_rule(self, rule: RuleSpec):
        """Execute the actions for a fired rule."""
        now = datetime.datetime.now().strftime("%d/%m/%y %H:%M:%S")
        msg = f"{now} - {rule.message}" if rule.message else ""
        self.logger.warning("Rule %s fired: %s", rule.id, msg or "(no message)")
        # Write timestamped message to a PV if configured.
        # Truncate to 39 chars to fit EPICS string PV limit (40 incl. null).
        # Wrap in try/except so a write failure never blocks the actuators.
        if rule.message_pv and msg:
            try:
                self.set_pv(rule.message_pv, msg[:39])
            except Exception as exc:
                self.logger.warning("message_pv write failed (%s): %s", rule.message_pv, exc)
        # Set declared outputs
        for pv_name, value in rule.outputs.items():
            self.set_pv(pv_name, value)
        # Fire actuators (write to external wired PVs)
        timeout = float(self.parameters.get("timeout", 5.0))
        for key, value in rule.actuators.items():
            try:
                self.link_put(key, value, timeout=timeout)
                self.logger.info("  actuator: %s -> %s", key, value)
            except Exception as exc:
                self.logger.error("  actuator %s failed: %s", key, exc)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def get_datetime(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def get_timems(self) -> int:
        return int(time.time() * 1000)

    def get_device(self, device_name: str):
        """Get an Ophyd device by name (lazy-created singleton if resolver is set)."""
        if self._device_resolver is not None:
            return self._device_resolver(device_name)
        return self.ophyd_devices.get(device_name)

    def create_device(
        self,
        prefix: str,
        devgroup: str,
        devtype: str,
        name: Optional[str] = None,
        cache: bool = True,
    ):
        """Instantiate an Ophyd device by PV prefix, group and type.

        Uses the same ``DeviceFactory`` registry as the beamline controller so
        device classes, PV component suffixes and metadata are consistent.

        Supported ``(devgroup, devtype)`` pairs (non-exhaustive):

        +-----------+-------------------+------------------------------------------+
        | devgroup  | devtype           | Ophyd class                              |
        +===========+===================+==========================================+
        | ``mot``   | ``asyn``          | ``OphydAsynMotor`` (EPICS motor record)  |
        | ``mot``   | ``tml``           | ``OphydTmlMotor`` (TechnoSoft / TML)     |
        | ``mot``   | ``sim``           | ``OphydMotorSim``                        |
        | ``io``    | ``di``/``do``     | ``OphydDI`` / ``OphydDO``               |
        | ``io``    | ``ai``/``ao``     | ``OphydAI`` / ``OphydAO``               |
        | ``io``    | ``rtd``           | ``OphydRTD``                             |
        | ``mag``   | ``dante``         | ``OphydPSDante``                         |
        | ``mag``   | ``unimag``        | ``OphydPSUnimag``                        |
        | ``diag``  | ``bpm``           | ``SppOphydBpm``                          |
        | ``vac``   | ``ipcmini``       | ``OphydVPC``                             |
        +-----------+-------------------+------------------------------------------+

        Example — TML motor using SPARC:MOT:TML prefix::

            motor = self.create_device(
                prefix="SPARC:MOT:TML:GUNFLG01",
                devgroup="mot",
                devtype="tml",
                name="GUNFLG01",
            )
            pos = motor.user_readback.get()

        Example — standard asyn motor record::

            motor = self.create_device(
                prefix="SPARC:MOT:TML:GUNFLG01",
                devgroup="mot",
                devtype="asyn",
                name="GUNFLG01",
            )
            is_done = motor.motor_done_move.get()

        Args:
            prefix: Full PV prefix for the device (e.g. ``"SPARC:MOT:TML:GUNFLG01"``).
            devgroup: Device group key (``"mot"``, ``"io"``, ``"mag"``, ``"diag"``, ``"vac"``).
            devtype: Device type key (``"asyn"``, ``"tml"``, ``"di"``, ``"dante"`` …).
            name: Ophyd device ``name`` attribute.  Defaults to the last segment of *prefix*.
            cache: If *True* (default) the device is stored in ``self.ophyd_devices``
                under ``name`` and returned on subsequent calls without re-creating it.

        Returns:
            Ophyd device instance, or *None* if ophyd/infn_ophyd_hal are not available.
        """
        device_name = name or prefix.rsplit(":", 1)[-1]

        if cache and device_name in self.ophyd_devices:
            return self.ophyd_devices[device_name]

        try:
            from infn_ophyd_hal.device_factory import DeviceFactory
        except ImportError:
            self.logger.warning(
                "create_device: ophyd / infn_ophyd_hal not installed — returning None"
            )
            return None

        factory = DeviceFactory()
        device = factory.create_device(
            devgroup=devgroup,
            devtype=devtype,
            prefix=prefix,
            name=device_name,
        )
        if device is None:
            self.logger.warning(
                "create_device: DeviceFactory returned None for %s/%s prefix=%s",
                devgroup,
                devtype,
                prefix,
            )
            return None

        if cache:
            self.ophyd_devices[device_name] = device

        self.logger.debug(
            "create_device: %s/%s prefix=%s name=%s -> %s",
            devgroup,
            devtype,
            prefix,
            device_name,
            type(device).__name__,
        )
        return device

    def list_devices(self):
        return list(self.ophyd_devices.keys())

    # ------------------------------------------------------------------
    # ChannelFinder integration
    # ------------------------------------------------------------------

    @property
    def channelfinder(self):
        """Lazy-initialised :class:`~iocmng.core.channelfinder.ChannelFinderClient`.

        Activated when the task parameter ``channelfinder_url`` is set.
        Returns *None* if the URL is not configured or ``requests`` is missing.
        """
        if hasattr(self, "_cf_client"):
            return self._cf_client

        cf_url = self.parameters.get("channelfinder_url")
        if not cf_url:
            self._cf_client = None
            return None

        try:
            from iocmng.core.channelfinder import ChannelFinderClient
            self._cf_client = ChannelFinderClient(
                url=cf_url,
                timeout=float(self.parameters.get("channelfinder_timeout", 10.0)),
            )
            self.logger.info("ChannelFinder client initialised: %s", cf_url)
        except Exception as exc:
            self.logger.warning("ChannelFinder unavailable: %s", exc)
            self._cf_client = None
        return self._cf_client

    def cf_search(self, **kwargs):
        """Search ChannelFinder channels.

        Convenience wrapper around :meth:`ChannelFinderClient.search`.
        Returns an empty list when ChannelFinder is not configured.

        Keyword args are forwarded directly — common filters::

            name      — PV name glob (e.g. ``"SPARC:MOT:TML:*"``)
            devgroup  — ``"mot"``, ``"io"``, ``"mag"``, ``"diag"``, ``"vac"``
            devtype   — ``"tml"``, ``"asyn"``, ``"di"`` …
            zone      — accelerator zone
            iocName   — IOC name as registered by cfeeder

        Example::

            channels = self.cf_search(devgroup="mot", name="SPARC:MOT:TML:*")
        """
        if self.channelfinder is None:
            return []
        try:
            return self.channelfinder.search(**kwargs)
        except Exception as exc:
            self.logger.error("cf_search failed: %s", exc)
            return []

    def cf_discover_devices(self, **kwargs):
        """Discover devices from ChannelFinder metadata.

        Returns a list of device descriptors (dicts) grouped by PV stem.
        Each dict contains ``name``, ``devgroup``, ``devtype``, ``prefix``,
        ``iocname``, ``properties``, ``pvs``.

        Keyword args are the same as :meth:`cf_search`.

        Example::

            motors = self.cf_discover_devices(devgroup="mot", devtype="tml")
            for desc in motors:
                dev = self.cf_create_device(desc)
        """
        if self.channelfinder is None:
            return []
        try:
            return self.channelfinder.discover_devices(**kwargs)
        except Exception as exc:
            self.logger.error("cf_discover_devices failed: %s", exc)
            return []

    def cf_create_device(self, device_descriptor, cache: bool = True):
        """Create an Ophyd device from a ChannelFinder device descriptor.

        *device_descriptor* is a dict as returned by :meth:`cf_discover_devices`
        (keys: ``name``, ``devgroup``, ``devtype``, ``prefix``).

        Uses :meth:`create_device` internally, so the result is cached in
        ``self.ophyd_devices`` by default.

        Returns the Ophyd device instance, or *None* if creation fails.

        Example::

            descs = self.cf_discover_devices(devgroup="io", devtype="do")
            for d in descs:
                shutter = self.cf_create_device(d)
                if shutter:
                    shutter.write(0)
        """
        name = device_descriptor.get("name", "")
        devgroup = device_descriptor.get("devgroup", "")
        devtype = device_descriptor.get("devtype", "")
        prefix = device_descriptor.get("prefix", "")

        if not all([name, devgroup, devtype, prefix]):
            self.logger.warning(
                "cf_create_device: incomplete descriptor %s", device_descriptor
            )
            return None

        return self.create_device(
            prefix=prefix,
            devgroup=devgroup,
            devtype=devtype,
            name=name,
            cache=cache,
        )

    # ------------------------------------------------------------------
    # Abstract methods – subclasses MUST implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def initialize(self):
        """Task-specific initialization. Called once before the run loop."""
        ...

    @abstractmethod
    def execute(self):
        """Called each cycle in continuous mode. Implement task logic here."""
        ...

    @abstractmethod
    def cleanup(self):
        """Task cleanup. Called when the task is stopped."""
        ...
