#!/usr/bin/env python3
"""
Generate IOC Manager OPI (BOB) file dynamically from beamline configuration.

This script reads the beamline configuration and generates a Phoebus display
file with rows for each IOC defined in the configuration.
"""

import argparse
import yaml
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom import minidom


def create_element(tag, text=None, **attrs):
    """Create an XML element with optional text and attributes."""
    elem = ET.Element(tag, **attrs)
    if text is not None:
        elem.text = str(text)
    return elem


def create_color(red, green, blue):
    """Create a color element."""
    color = ET.Element("color", red=str(red), green=str(green), blue=str(blue))
    return color


def create_font(
    name="Liberation Sans", family="Liberation Sans", style="REGULAR", size="14.0"
):
    """Create a font element."""
    font = ET.Element("font")
    ET.SubElement(font, "font", name=name, family=family, style=style, size=size)
    return font


def create_widget(widget_type, name, x, y, width=100, height=30, **extra):
    """Create a widget element with common properties."""
    widget = ET.Element("widget", type=widget_type, version="2.0.0")
    ET.SubElement(widget, "name").text = name
    ET.SubElement(widget, "x").text = str(x)
    ET.SubElement(widget, "y").text = str(y)
    ET.SubElement(widget, "width").text = str(width)
    ET.SubElement(widget, "height").text = str(height)

    # Add extra elements
    for key, value in extra.items():
        if isinstance(value, ET.Element):
            widget.append(value)
        else:
            ET.SubElement(widget, key).text = str(value)

    return widget


def create_label(
    name,
    text,
    x,
    y,
    width=100,
    height=30,
    font_name="Default",
    font_size="14.0",
    font_style="REGULAR",
    bold=False,
    horizontal_alignment=0,
    transparent=True,
    foreground_color=None,
    background_color=None,
):
    """Create a label widget."""
    widget = create_widget("label", name, x, y, width, height)
    ET.SubElement(widget, "text").text = text

    if bold or font_size != "14.0":
        font = ET.SubElement(widget, "font")
        style = "BOLD" if bold else font_style
        ET.SubElement(
            font,
            "font",
            name=font_name,
            family="Liberation Sans",
            style=style,
            size=font_size,
        )

    if horizontal_alignment != 0:
        ET.SubElement(widget, "horizontal_alignment").text = str(horizontal_alignment)

    if not transparent:
        ET.SubElement(widget, "transparent").text = "false"

    if foreground_color:
        widget.append(create_element("foreground_color", None))
        widget.find("foreground_color").append(foreground_color)

    if background_color:
        widget.append(create_element("background_color", None))
        widget.find("background_color").append(background_color)

    return widget


def create_textupdate(
    name, pv_name, x, y, width=100, height=30, horizontal_alignment=0
):
    """Create a textupdate widget."""
    widget = create_widget("textupdate", name, x, y, width, height)
    ET.SubElement(widget, "pv_name").text = pv_name

    if horizontal_alignment != 0:
        ET.SubElement(widget, "horizontal_alignment").text = str(horizontal_alignment)

    return widget


def create_multi_state_led(name, pv_name, x, y, width=20, height=20):
    """Create a multi-state LED widget."""
    widget = create_widget("multi_state_led", name, x, y, width, height)
    ET.SubElement(widget, "pv_name").text = pv_name

    # Default fallback color (red)
    fallback_color = ET.SubElement(widget, "fallback_color")
    fallback_color.append(create_color(200, 0, 0))  # Red

    return widget


def create_action_button(
    name, text, pv_name, x, y, width=100, height=30, fg_color=None, bg_color=None
):
    """Create an action button widget."""
    widget = create_widget("action_button", name, x, y, width, height)
    ET.SubElement(widget, "text").text = text
    ET.SubElement(widget, "pv_name").text = pv_name

    # Add write action
    actions = ET.SubElement(widget, "actions")
    action = ET.SubElement(actions, "action", type="write_pv")
    ET.SubElement(action, "description").text = text
    ET.SubElement(action, "pv_name").text = pv_name
    ET.SubElement(action, "value").text = "1"

    if fg_color:
        fg_elem = ET.SubElement(widget, "foreground_color")
        fg_elem.append(fg_color)

    if bg_color:
        bg_elem = ET.SubElement(widget, "background_color")
        bg_elem.append(bg_color)

    return widget


def create_column_headers(parent_element, y_pos):
    """Create column headers for IOC table."""
    gray_bg = create_color(200, 200, 200)

    parent_element.append(
        create_label(
            "ColHeader_IOC",
            "IOC Name",
            10,
            y_pos,
            200,
            25,
            bold=True,
            transparent=False,
            background_color=gray_bg,
        )
    )
    parent_element.append(
        create_label(
            "ColHeader_AppStatus",
            "App Status",
            220,
            y_pos,
            100,
            25,
            bold=True,
            transparent=False,
            background_color=gray_bg,
            horizontal_alignment=1,
        )
    )
    parent_element.append(
        create_label(
            "ColHeader_SyncStatus",
            "Sync",
            330,
            y_pos,
            100,
            25,
            bold=True,
            transparent=False,
            background_color=gray_bg,
            horizontal_alignment=1,
        )
    )
    parent_element.append(
        create_label(
            "ColHeader_HealthStatus",
            "Health",
            440,
            y_pos,
            100,
            25,
            bold=True,
            transparent=False,
            background_color=gray_bg,
            horizontal_alignment=1,
        )
    )
    parent_element.append(
        create_label(
            "ColHeader_LastSync",
            "Last Sync",
            550,
            y_pos,
            180,
            25,
            bold=True,
            transparent=False,
            background_color=gray_bg,
            horizontal_alignment=1,
        )
    )
    parent_element.append(
        create_label(
            "ColHeader_HealthChange",
            "Last Health Change",
            740,
            y_pos,
            180,
            25,
            bold=True,
            transparent=False,
            background_color=gray_bg,
            horizontal_alignment=1,
        )
    )


def create_bool_button(name, pv_name, x, y, width=120, height=30):
    """Create a bool button widget."""
    widget = create_widget("bool_button", name, x, y, width, height)
    ET.SubElement(widget, "pv_name").text = pv_name
    ET.SubElement(widget, "off_label").text = "Disabled"
    ET.SubElement(widget, "on_label").text = "Enabled"

    # Off color (red)
    off_color_elem = ET.SubElement(widget, "off_color")
    off_color_elem.append(create_color(200, 0, 0))

    # On color (green)
    on_color_elem = ET.SubElement(widget, "on_color")
    on_color_elem.append(create_color(0, 200, 0))

    return widget


def create_ioc_row(ioc_name, prefix, y_pos, task_name="IOCMNG", namespace=None):
    """Create widgets for a single IOC row."""
    widgets = []

    # Sanitize IOC name for PV (uppercase, replace hyphens)
    ioc_pv_name = ioc_name.upper().replace("-", "_")

    # Ensure EPICS record name length limits are respected
    # Mirror the truncation logic used by the IOC task so PV names match at runtime.
    try:
        max_record_length = 60
        prefix_overhead = len(prefix) + 1  # separator
        longest_suffix = len("_LAST_HEALTH")
        max_ioc_prefix_len = max_record_length - prefix_overhead - longest_suffix
        if max_ioc_prefix_len > 0 and len(ioc_pv_name) > max_ioc_prefix_len:
            original = ioc_pv_name
            ioc_pv_name = ioc_pv_name[:max_ioc_prefix_len]
            print(
                f"Warning: IOC PV name '{original}' truncated to '{ioc_pv_name}' to fit EPICS {max_record_length}-char limit"
            )
    except Exception:
        # On any unexpected error, fall back to the full sanitized name
        pass

    # Build the expected ArgoCD application name using the beamline namespace
    app_name = None
    if namespace:
        app_name = f"{namespace}-{ioc_name}-ioc"
    else:
        app_name = f"{ioc_name}-ioc"

    # IOC Name label
    widgets.append(
        create_label(f"IOC_{ioc_pv_name}_Name", ioc_name, 10, y_pos, 200, 30)
    )

    # Show the expected ArgoCD application name under the IOC name (smaller font)
    widgets.append(
        create_label(
            f"IOC_{ioc_pv_name}_AppName",
            app_name,
            10,
            y_pos + 18,
            300,
            18,
            font_size="10.0",
        )
    )

    # App Status
    app_status_widget = create_textupdate(
        f"IOC_{ioc_pv_name}_AppStatus",
        f"{prefix}:{task_name}:{ioc_pv_name}_APP_STATUS",
        220,
        y_pos,
        100,
        30,
        horizontal_alignment=1,
    )
    # Add rules for application status background color
    rules = ET.SubElement(app_status_widget, "rules")

    # Rule for Running -> Green background
    rule_running = ET.SubElement(rules, "rule", name="Running")
    ET.SubElement(rule_running, "prop_id").text = "background_color"
    expr_running = ET.SubElement(rule_running, "expression")
    ET.SubElement(expr_running, "value").text = 'pv0=="Running"'
    pv_running = ET.SubElement(expr_running, "pv")
    ET.SubElement(pv_running, "name").text = "pv0"
    ET.SubElement(pv_running, "trigger").text = "true"
    val_running = ET.SubElement(rule_running, "value")
    val_running.append(create_color(0, 200, 0))  # Green

    widgets.append(app_status_widget)

    # Sync LED
    sync_led = create_multi_state_led(
        f"IOC_{ioc_pv_name}_SyncLED",
        f"{prefix}:{task_name}:{ioc_pv_name}_SYNC_STATUS",
        355,
        y_pos + 5,
        20,
        20,
    )
    # Add states for sync status colors
    states = ET.SubElement(sync_led, "states")

    # State 0: Synced -> Green
    state_synced = ET.SubElement(states, "state", value="0")
    color_synced = ET.SubElement(state_synced, "color")
    color_synced.append(create_color(0, 200, 0))  # Green

    # State 1: OutOfSync -> Yellow
    state_outofsync = ET.SubElement(states, "state", value="1")
    color_outofsync = ET.SubElement(state_outofsync, "color")
    color_outofsync.append(create_color(255, 255, 0))  # Yellow

    # State 2: Unknown -> Orange
    state_unknown = ET.SubElement(states, "state", value="2")
    color_unknown = ET.SubElement(state_unknown, "color")
    color_unknown.append(create_color(255, 140, 0))  # Orange

    # State 3: Error -> Red
    state_error = ET.SubElement(states, "state", value="3")
    color_error = ET.SubElement(state_error, "color")
    color_error.append(create_color(200, 0, 0))  # Red

    widgets.append(sync_led)  # Sync Status text
    widgets.append(
        create_textupdate(
            f"IOC_{ioc_pv_name}_SyncStatus",
            f"{prefix}:{task_name}:{ioc_pv_name}_SYNC_STATUS",
            380,
            y_pos,
            50,
            30,
            horizontal_alignment=1,
        )
    )

    # Health LED
    health_led = create_multi_state_led(
        f"IOC_{ioc_pv_name}_HealthLED",
        f"{prefix}:{task_name}:{ioc_pv_name}_HEALTH_STAT",
        465,
        y_pos + 5,
        20,
        20,
    )
    # Add states for health status colors
    states = ET.SubElement(health_led, "states")

    # State 0: Healthy -> Green
    state_healthy = ET.SubElement(states, "state", value="0")
    color_healthy = ET.SubElement(state_healthy, "color")
    color_healthy.append(create_color(0, 200, 0))  # Green

    # State 1: Progressing -> Yellow
    state_progressing = ET.SubElement(states, "state", value="1")
    color_progressing = ET.SubElement(state_progressing, "color")
    color_progressing.append(create_color(255, 255, 0))  # Yellow

    # State 5: Warning -> Yellow
    state_warning = ET.SubElement(states, "state", value="5")
    color_warning = ET.SubElement(state_warning, "color")
    color_warning.append(create_color(255, 255, 0))  # Yellow

    # Other states (2,3,4,6): Red (fallback color will handle this)

    widgets.append(health_led)  # Health Status text
    widgets.append(
        create_textupdate(
            f"IOC_{ioc_pv_name}_HealthStatus",
            f"{prefix}:{task_name}:{ioc_pv_name}_HEALTH_STAT",
            490,
            y_pos,
            50,
            30,
            horizontal_alignment=1,
        )
    )

    # Last Sync Time
    widgets.append(
        create_textupdate(
            f"IOC_{ioc_pv_name}_LastSync",
            f"{prefix}:{task_name}:{ioc_pv_name}_LAST_SYNC",
            550,
            y_pos,
            180,
            30,
            horizontal_alignment=1,
        )
    )

    # Last Health Change
    widgets.append(
        create_textupdate(
            f"IOC_{ioc_pv_name}_HealthChange",
            f"{prefix}:{task_name}:{ioc_pv_name}_LAST_HEALTH",
            740,
            y_pos,
            180,
            30,
            horizontal_alignment=1,
        )
    )

    # START button
    widgets.append(
        create_action_button(
            f"IOC_{ioc_pv_name}_Start",
            "START",
            f"{prefix}:{task_name}:{ioc_pv_name}_START",
            930,
            y_pos,
            100,
            30,
            fg_color=create_color(255, 255, 255),
            bg_color=create_color(0, 150, 0),
        )
    )

    # STOP button
    widgets.append(
        create_action_button(
            f"IOC_{ioc_pv_name}_Stop",
            "STOP",
            f"{prefix}:{task_name}:{ioc_pv_name}_STOP",
            1040,
            y_pos,
            100,
            30,
            fg_color=create_color(255, 255, 255),
            bg_color=create_color(200, 0, 0),
        )
    )

    # RESTART button
    widgets.append(
        create_action_button(
            f"IOC_{ioc_pv_name}_Restart",
            "RESTART",
            f"{prefix}:{task_name}:{ioc_pv_name}_RESTART",
            1150,
            y_pos,
            100,
            30,
            fg_color=create_color(255, 255, 255),
            bg_color=create_color(255, 140, 0),
        )
    )

    return widgets


def create_service_row(service_name, prefix, y_pos, task_name="IOCMNG", namespace=None):
    """Create widgets for a single service row."""
    widgets = []

    # Sanitize service name for PV (uppercase, replace hyphens)
    service_pv_name = service_name.upper().replace("-", "_")

    # Ensure EPICS record name length limits are respected
    # Mirror the truncation logic used by the service task so PV names match at runtime.
    try:
        max_record_length = 60
        prefix_overhead = len(prefix) + 1  # separator
        longest_suffix = len("_LAST_HEALTH")
        max_service_prefix_len = max_record_length - prefix_overhead - longest_suffix
        if max_service_prefix_len > 0 and len(service_pv_name) > max_service_prefix_len:
            original = service_pv_name
            service_pv_name = service_pv_name[:max_service_prefix_len]
            print(
                f"Warning: Service PV name '{original}' truncated to '{service_pv_name}' to fit EPICS {max_record_length}-char limit"
            )
    except Exception:
        # On any unexpected error, fall back to the full sanitized name
        pass

    # Build the expected ArgoCD application name using the beamline namespace
    app_name = None
    if namespace:
        app_name = f"{namespace}-{service_name}"
    else:
        app_name = service_name

    # Service Name label
    widgets.append(
        create_label(
            f"Service_{service_pv_name}_Name", service_name, 10, y_pos, 200, 30
        )
    )

    # Show the expected ArgoCD application name under the service name (smaller font)
    widgets.append(
        create_label(
            f"Service_{service_pv_name}_AppName",
            app_name,
            10,
            y_pos + 18,
            300,
            18,
            font_size="10.0",
        )
    )

    # App Status
    app_status_widget = create_textupdate(
        f"Service_{service_pv_name}_AppStatus",
        f"{prefix}:{task_name}:{service_pv_name}_APP_STATUS",
        220,
        y_pos,
        100,
        30,
        horizontal_alignment=1,
    )
    # Add rules for application status background color
    rules = ET.SubElement(app_status_widget, "rules")

    # Rule for Running -> Green background
    rule_running = ET.SubElement(rules, "rule", name="Running")
    ET.SubElement(rule_running, "prop_id").text = "background_color"
    expr_running = ET.SubElement(rule_running, "expression")
    ET.SubElement(expr_running, "value").text = 'pv0=="Running"'
    pv_running = ET.SubElement(expr_running, "pv")
    ET.SubElement(pv_running, "name").text = "pv0"
    ET.SubElement(pv_running, "trigger").text = "true"
    val_running = ET.SubElement(rule_running, "value")
    val_running.append(create_color(0, 200, 0))  # Green

    widgets.append(app_status_widget)

    # Sync LED
    sync_led = create_multi_state_led(
        f"Service_{service_pv_name}_SyncLED",
        f"{prefix}:{task_name}:{service_pv_name}_SYNC_STATUS",
        355,
        y_pos + 5,
        20,
        20,
    )
    # Add states for sync status colors
    states = ET.SubElement(sync_led, "states")

    # State 0: Synced -> Green
    state_synced = ET.SubElement(states, "state", value="0")
    color_synced = ET.SubElement(state_synced, "color")
    color_synced.append(create_color(0, 200, 0))  # Green

    # State 1: OutOfSync -> Yellow
    state_outofsync = ET.SubElement(states, "state", value="1")
    color_outofsync = ET.SubElement(state_outofsync, "color")
    color_outofsync.append(create_color(255, 255, 0))  # Yellow

    # State 2: Unknown -> Orange
    state_unknown = ET.SubElement(states, "state", value="2")
    color_unknown = ET.SubElement(state_unknown, "color")
    color_unknown.append(create_color(255, 140, 0))  # Orange

    # State 3: Error -> Red
    state_error = ET.SubElement(states, "state", value="3")
    color_error = ET.SubElement(state_error, "color")
    color_error.append(create_color(200, 0, 0))  # Red

    widgets.append(sync_led)  # Sync Status text
    widgets.append(
        create_textupdate(
            f"Service_{service_pv_name}_SyncStatus",
            f"{prefix}:{task_name}:{service_pv_name}_SYNC_STATUS",
            380,
            y_pos,
            50,
            30,
            horizontal_alignment=1,
        )
    )

    # Health LED
    health_led = create_multi_state_led(
        f"Service_{service_pv_name}_HealthLED",
        f"{prefix}:{task_name}:{service_pv_name}_HEALTH_STAT",
        465,
        y_pos + 5,
        20,
        20,
    )
    # Add states for health status colors
    states = ET.SubElement(health_led, "states")

    # State 0: Healthy -> Green
    state_healthy = ET.SubElement(states, "state", value="0")
    color_healthy = ET.SubElement(state_healthy, "color")
    color_healthy.append(create_color(0, 200, 0))  # Green

    # State 1: Progressing -> Yellow
    state_progressing = ET.SubElement(states, "state", value="1")
    color_progressing = ET.SubElement(state_progressing, "color")
    color_progressing.append(create_color(255, 255, 0))  # Yellow

    # State 5: Warning -> Yellow
    state_warning = ET.SubElement(states, "state", value="5")
    color_warning = ET.SubElement(state_warning, "color")
    color_warning.append(create_color(255, 255, 0))  # Yellow

    # Other states (2,3,4,6): Red (fallback color will handle this)

    widgets.append(health_led)  # Health Status text
    widgets.append(
        create_textupdate(
            f"Service_{service_pv_name}_HealthStatus",
            f"{prefix}:{task_name}:{service_pv_name}_HEALTH_STAT",
            490,
            y_pos,
            50,
            30,
            horizontal_alignment=1,
        )
    )

    # Last Sync Time
    widgets.append(
        create_textupdate(
            f"Service_{service_pv_name}_LastSync",
            f"{prefix}:{task_name}:{service_pv_name}_LAST_SYNC",
            550,
            y_pos,
            180,
            30,
            horizontal_alignment=1,
        )
    )

    # Last Health Change
    widgets.append(
        create_textupdate(
            f"Service_{service_pv_name}_HealthChange",
            f"{prefix}:{task_name}:{service_pv_name}_LAST_HEALTH",
            740,
            y_pos,
            180,
            30,
            horizontal_alignment=1,
        )
    )

    # START button
    widgets.append(
        create_action_button(
            f"Service_{service_pv_name}_Start",
            "START",
            f"{prefix}:{task_name}:{service_pv_name}_START",
            930,
            y_pos,
            100,
            30,
            fg_color=create_color(255, 255, 255),
            bg_color=create_color(0, 150, 0),
        )
    )

    # STOP button
    widgets.append(
        create_action_button(
            f"Service_{service_pv_name}_Stop",
            "STOP",
            f"{prefix}:{task_name}:{service_pv_name}_STOP",
            1040,
            y_pos,
            100,
            30,
            fg_color=create_color(255, 255, 255),
            bg_color=create_color(200, 0, 0),
        )
    )

    # RESTART button
    widgets.append(
        create_action_button(
            f"Service_{service_pv_name}_Restart",
            "RESTART",
            f"{prefix}:{task_name}:{service_pv_name}_RESTART",
            1150,
            y_pos,
            100,
            30,
            fg_color=create_color(255, 255, 255),
            bg_color=create_color(255, 140, 0),
        )
    )

    return widgets


def generate_IOCMNG_bob(beamline_path, output_path, prefix=None, config_path=None):
    """Generate IOC Manager BOB file from beamline configuration."""

    # Load beamline configuration
    with open(beamline_path, "r") as f:
        beamline_config = yaml.safe_load(f)

    # Get prefix from beamline config or use provided
    if prefix is None:
        prefix = beamline_config.get("prefix", "SPARC:CONTROL2")

    # Load config to find task name
    task_name = "IOCMNG"  # Default
    if config_path:
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            # Find the iocmng task
            for task in config.get("tasks", []):
                if task.get("module") == "iocmng_task":
                    task_name = task.get("name", "IOCMNG").upper()
                    break
        except Exception as e:
            print(f"Warning: Could not load config from {config_path}: {e}")
            print("Using default task name 'IOCMNG'")

    # Beamline namespace (used to construct ArgoCD application names)
    namespace = beamline_config.get("namespace", None)
    # Get IOC list - support both formats
    iocs = []
    if (
        "epicsConfiguration" in beamline_config
        and "iocs" in beamline_config["epicsConfiguration"]
    ):
        iocs_data = beamline_config["epicsConfiguration"]["iocs"]
    elif "iocs" in beamline_config:
        iocs_data = beamline_config["iocs"]
    else:
        print("Warning: No IOCs found in beamline configuration")
        iocs_data = []

    # Handle both dict and list formats
    if isinstance(iocs_data, dict):
        iocs = [{"name": name, **config} for name, config in iocs_data.items()]
    elif isinstance(iocs_data, list):
        iocs = iocs_data

    # Merge iocDefaults into each IOC entry
    ioc_defaults = beamline_config.get("iocDefaults") or {}
    if ioc_defaults:
        merged_iocs = []
        for ioc in iocs:
            if isinstance(ioc, dict):
                tmpl = ioc.get("template") or ioc.get("devtype") or ""
                tmpl_defaults = ioc_defaults.get(tmpl)
                merged_iocs.append({**tmpl_defaults, **ioc} if tmpl_defaults else ioc)
            else:
                merged_iocs.append(ioc)
        iocs = merged_iocs

    # Filter out disabled IOCs
    iocs = [ioc for ioc in iocs if not ioc.get("disable", False)]

    print(f"Found {len(iocs)} IOCs in beamline configuration")

    # Get services list
    services = []
    if (
        "epicsConfiguration" in beamline_config
        and "services" in beamline_config["epicsConfiguration"]
    ):
        services_data = beamline_config["epicsConfiguration"]["services"]
        if isinstance(services_data, dict):
            services = list(services_data.keys())
        elif isinstance(services_data, list):
            services = services_data

    print(f"Found {len(services)} services in beamline configuration")

    # Calculate display height - increased for combined IOC/service tabs
    # Title + control + combined tabs + instructions
    row_height = 40  # Still needed for tab content layout
    display_height = 60 + 180 + 600 + 80  # Increased task control area and tab area

    # Create root display element
    display = ET.Element("display", version="2.0.0")
    ET.SubElement(display, "name").text = "IOC Manager"

    # Add macros
    macros = ET.SubElement(display, "macros")
    ET.SubElement(macros, "P").text = prefix
    # Expose the beamline namespace as a macro for convenience
    if namespace:
        ET.SubElement(macros, "NAMESPACE").text = str(namespace)

    ET.SubElement(display, "width").text = "1400"
    ET.SubElement(display, "height").text = str(display_height)

    # Background color
    bg_color_elem = ET.SubElement(display, "background_color")
    bg_color_elem.append(create_color(240, 240, 240))

    # Title
    display.append(
        create_label(
            "Title",
            "IOC & Service Management - ArgoCD Application Monitor & Control",
            10,
            10,
            1380,
            40,
            font_name="Header 1",
            font_size="22.0",
            bold=True,
            horizontal_alignment=1,
            foreground_color=create_color(0, 0, 128),
        )
    )

    # Task Control Group
    task_group = ET.Element("widget", type="group", version="3.0.0")
    ET.SubElement(task_group, "name").text = "TaskControl"
    ET.SubElement(task_group, "x").text = "10"
    ET.SubElement(task_group, "y").text = "60"
    ET.SubElement(task_group, "width").text = "1380"
    ET.SubElement(task_group, "height").text = "170"
    ET.SubElement(task_group, "style").text = "3"

    group_bg = ET.SubElement(task_group, "background_color")
    group_bg.append(create_color(220, 220, 220))

    # Task enable button
    task_group.append(
        create_bool_button("TaskEnable", f"{prefix}:{task_name}:ENABLE", 20, 30, 120, 30)
    )

    # Status label and value
    task_group.append(
        create_label(
            "StatusLabel", "Status:", 160, 30, 80, 30, bold=True, horizontal_alignment=2
        )
    )
    task_group.append(
        create_textupdate("TaskStatus", f"{prefix}:{task_name}:STATUS", 250, 30, 120, 30)
    )

    # Cycles label and value
    task_group.append(
        create_label(
            "CyclesLabel", "Cycles:", 390, 30, 80, 30, bold=True, horizontal_alignment=2
        )
    )
    task_group.append(
        create_textupdate(
            "CycleCount", f"{prefix}:{task_name}:CYCLE_COUNT", 480, 30, 100, 30
        )
    )

    # Summary counters: total IOCs, healthy, progressing, other
    task_group.append(
        create_label(
            "TotalIOCsLabel",
            "Total IOCs:",
            600,
            30,
            80,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate("TotalIOCs", f"{prefix}:{task_name}:TOTAL_IOCS", 690, 30, 60, 30)
    )

    task_group.append(
        create_label(
            "HealthyLabel",
            "Healthy:",
            760,
            30,
            70,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "HealthyCount", f"{prefix}:{task_name}:HEALTHY_COUNT", 840, 30, 60, 30
        )
    )

    task_group.append(
        create_label(
            "ProgressingLabel",
            "Progressing:",
            910,
            30,
            100,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ProgressingCount", f"{prefix}:{task_name}:PROGRESSING_COUNT", 1015, 30, 60, 30
        )
    )

    task_group.append(
        create_label(
            "OtherLabel", "Other:", 1090, 30, 60, 30, bold=True, horizontal_alignment=2
        )
    )
    task_group.append(
        create_textupdate(
            "OtherCount", f"{prefix}:{task_name}:OTHER_COUNT", 1160, 30, 60, 30
        )
    )

    # Summary counters for services
    task_group.append(
        create_label(
            "TotalServicesLabel",
            "Total Services:",
            600,
            70,
            100,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "TotalServices", f"{prefix}:{task_name}:TOTAL_SERVICES", 710, 70, 60, 30
        )
    )

    task_group.append(
        create_label(
            "ServicesHealthyLabel",
            "Services Healthy:",
            780,
            70,
            120,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ServicesHealthyCount",
            f"{prefix}:{task_name}:SERVICES_HEALTHY_COUNT",
            910,
            70,
            60,
            30,
        )
    )

    task_group.append(
        create_label(
            "ServicesProgressingLabel",
            "Services Progressing:",
            980,
            70,
            140,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ServicesProgressingCount",
            f"{prefix}:{task_name}:SERVICES_PROGRESSING_COUNT",
            1130,
            70,
            60,
            30,
        )
    )

    task_group.append(
        create_label(
            "ServicesOtherLabel",
            "Services Other:",
            1200,
            70,
            110,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ServicesOtherCount",
            f"{prefix}:{task_name}:SERVICES_OTHER_COUNT",
            1320,
            70,
            60,
            30,
        )
    )

    # Archiver monitoring counters
    task_group.append(
        create_label(
            "ArchiverStatusLabel",
            "Archiver:",
            20,
            110,
            80,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ArchiverStatus", f"{prefix}:{task_name}:ARCHIVER_STATUS", 110, 110, 100, 30
        )
    )

    task_group.append(
        create_label(
            "ArchiverTotalLabel",
            "Total PVs:",
            220,
            110,
            80,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ArchiverTotalPVs", f"{prefix}:{task_name}:ARCHIVER_TOTAL_PVS", 310, 110, 80, 30
        )
    )

    task_group.append(
        create_label(
            "ArchiverConnectedLabel",
            "Connected:",
            400,
            110,
            80,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ArchiverConnectedPVs",
            f"{prefix}:{task_name}:ARCHIVER_CONNECTED_PVS",
            490,
            110,
            80,
            30,
        )
    )

    task_group.append(
        create_label(
            "ArchiverDisconnectedLabel",
            "Disconnected:",
            580,
            110,
            100,
            30,
            bold=True,
            horizontal_alignment=2,
        )
    )
    task_group.append(
        create_textupdate(
            "ArchiverDisconnectedPVs",
            f"{prefix}:{task_name}:ARCHIVER_DISCONNECTED_PVS",
            690,
            110,
            80,
            30,
        )
    )

    # Message label and value
    task_group.append(
        create_label("MessageLabel", "Message:", 20, 140, 80, 30, bold=True)
    )
    task_group.append(
        create_textupdate("TaskMessage", f"{prefix}:{task_name}:MESSAGE", 110, 140, 1250, 30)
    )

    display.append(task_group)

    # Parse devgroups from beamline config - combine IOCs and services
    all_devgroups = set()

    # Get IOC devgroups
    for ioc in iocs:
        if isinstance(ioc, dict):
            devgroup = ioc.get("devgroup", "default")
            all_devgroups.add(devgroup)

    # Get service devgroups
    if (
        "epicsConfiguration" in beamline_config
        and "services" in beamline_config["epicsConfiguration"]
    ):
        services_data = beamline_config["epicsConfiguration"]["services"]
        if isinstance(services_data, dict):
            for service_name, service_config in services_data.items():
                if isinstance(service_config, dict):
                    devgroup = service_config.get("devgroup", "services")
                    all_devgroups.add(devgroup)

    all_devgroups = sorted(all_devgroups)

    # Group IOCs and services by devgroup
    iocs_by_devgroup = {}
    services_by_devgroup = {}

    for ioc in iocs:
        devgroup = (
            ioc.get("devgroup", "default") if isinstance(ioc, dict) else "default"
        )
        if devgroup not in iocs_by_devgroup:
            iocs_by_devgroup[devgroup] = []
        iocs_by_devgroup[devgroup].append(ioc)

    if (
        "epicsConfiguration" in beamline_config
        and "services" in beamline_config["epicsConfiguration"]
    ):
        services_data = beamline_config["epicsConfiguration"]["services"]
        if isinstance(services_data, dict):
            for service_name, service_config in services_data.items():
                devgroup = (
                    service_config.get("devgroup", "services")
                    if isinstance(service_config, dict)
                    else "services"
                )
                if devgroup not in services_by_devgroup:
                    services_by_devgroup[devgroup] = []
                services_by_devgroup[devgroup].append(service_name)

    # IOC & Service Status & Control Tabs
    app_tabs_widget = ET.Element("widget", type="tabs", version="2.0.0")
    ET.SubElement(app_tabs_widget, "name").text = "ApplicationTabs"
    ET.SubElement(app_tabs_widget, "x").text = "10"
    ET.SubElement(app_tabs_widget, "y").text = "240"
    ET.SubElement(app_tabs_widget, "width").text = "1380"
    ET.SubElement(app_tabs_widget, "height").text = str(display_height - 240)

    # Tabs container
    app_tabs_container = ET.SubElement(app_tabs_widget, "tabs")

    # Add ALL tab first
    all_tab = ET.SubElement(app_tabs_container, "tab")
    ET.SubElement(all_tab, "name").text = "ALL"
    all_children = ET.SubElement(all_tab, "children")

    # Table header for ALL tab
    all_children.append(
        create_label(
            "AllTableHeader",
            "IOC & Service Status & Control - All Applications",
            10,
            10,
            400,
            30,
            font_name="Header 2",
            font_size="18.0",
            bold=True,
            foreground_color=create_color(0, 0, 128),
        )
    )
    create_column_headers(all_children, 50)

    # Add IOC rows for ALL tab
    y_pos = 85
    for ioc in iocs:
        ioc_name = ioc.get("name", "unknown")
        for widget in create_ioc_row(ioc_name, prefix, y_pos, task_name, namespace):
            all_children.append(widget)
        y_pos += row_height

    # Add service rows for ALL tab
    for service_name in services:
        for widget in create_service_row(
            service_name, prefix, y_pos, task_name, namespace
        ):
            all_children.append(widget)
        y_pos += row_height

    # Add tabs for each devgroup
    for devgroup in sorted(all_devgroups):
        tab = ET.SubElement(app_tabs_container, "tab")
        ET.SubElement(tab, "name").text = devgroup.upper()
        children = ET.SubElement(tab, "children")

        # Table header for devgroup tab
        children.append(
            create_label(
                f"{devgroup}TableHeader",
                f"IOC & Service Status & Control - {devgroup.upper()}",
                10,
                10,
                400,
                30,
                font_name="Header 2",
                font_size="18.0",
                bold=True,
                foreground_color=create_color(0, 0, 128),
            )
        )
        create_column_headers(children, 50)

        # Add IOC rows for this devgroup
        y_pos = 85
        if devgroup in iocs_by_devgroup:
            for ioc in iocs_by_devgroup[devgroup]:
                ioc_name = ioc.get("name", "unknown")
                for widget in create_ioc_row(
                    ioc_name, prefix, y_pos, task_name, namespace
                ):
                    children.append(widget)
                y_pos += row_height

        # Add service rows for this devgroup
        if devgroup in services_by_devgroup:
            for service_name in services_by_devgroup[devgroup]:
                for widget in create_service_row(
                    service_name, prefix, y_pos, task_name, namespace
                ):
                    children.append(widget)
                y_pos += row_height

    display.append(app_tabs_widget)

    # Instructions footer
    instructions = ET.Element("widget", type="group", version="3.0.0")
    ET.SubElement(instructions, "name").text = "Instructions"
    ET.SubElement(instructions, "x").text = "10"
    ET.SubElement(instructions, "y").text = str(display_height - 70)
    ET.SubElement(instructions, "width").text = "1380"
    ET.SubElement(instructions, "height").text = "70"
    ET.SubElement(instructions, "style").text = "3"

    inst_bg = ET.SubElement(instructions, "background_color")
    inst_bg.append(create_color(255, 255, 220))

    instructions.append(
        create_label(
            "InstructionsText",
            f"Generated for {len(iocs)} IOCs and {len(services)} services from beamline configuration.\n"
            "Color indicators: Green = Healthy/Synced, Red = Unhealthy/OutOfSync, Yellow = Warning/Progressing\n"
            "Actions: START enables ArgoCD sync, STOP suspends application, RESTART performs hard refresh\n"
            "Archiver monitoring shows EPICS Archiver appliance status and PV connectivity counts",
            10,
            5,
            1360,
            60,
        )
    )

    display.append(instructions)

    # Pretty print and save
    xml_str = ET.tostring(display, encoding="unicode")
    dom = minidom.parseString(xml_str)
    pretty_xml = dom.toprettyxml(indent="  ")

    # Remove extra blank lines
    lines = [line for line in pretty_xml.split("\n") if line.strip()]
    pretty_xml = "\n".join(lines)

    # Write to file
    with open(output_path, "w") as f:
        f.write(pretty_xml)

    print(f"Generated IOC Manager OPI file: {output_path}")
    print(f"  - Prefix: {prefix}")
    print(f"  - IOCs: {len(iocs)}")
    for ioc in iocs:
        ioc_name = ioc.get("name", "unknown")
        app_expected = f"{namespace}-{ioc_name}-ioc" if namespace else f"{ioc_name}-ioc"
        print(f"    * {ioc_name}  =>  ArgoCD app: {app_expected}")
    print(f"  - Services: {len(services)}")
    for service_name in services:
        app_expected = f"{namespace}-{service_name}" if namespace else service_name
        print(f"    * {service_name}  =>  ArgoCD app: {app_expected}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate IOC Manager BOB file from beamline configuration"
    )
    parser.add_argument(
        "--beamline",
        type=str,
        default="tests/sparc-beamline.yaml",
        help="Path to beamline configuration file (default: tests/sparc-beamline.yaml)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="IOCMNG.bob",
        help="Output BOB file path (default: IOCMNG.bob)",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="PV prefix (default: read from beamline config)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to task config file to extract task name (optional)",
    )

    args = parser.parse_args()

    # Check if beamline config exists
    if not Path(args.beamline).exists():
        print(f"Error: Beamline config file not found: {args.beamline}")
        return 1

    # Generate BOB file
    try:
        generate_IOCMNG_bob(args.beamline, args.output, args.prefix, args.config)
        return 0
    except Exception as e:
        print(f"Error generating IOC Manager OPI: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
