#!/usr/bin/env python3
"""
IOC Status Task - Monitors ArgoCD applications for IOC status and control.

This task periodically checks the status of ArgoCD applications in the namespace,
creates PVs for each devgroup showing IOC lists, and provides status/control PVs
for each IOC (sync status, health status, timestamps, and START/STOP/RESTART controls).
"""

import cothread
import time
import threading
import os
from urllib3.exceptions import MaxRetryError, ProxyError
from typing import Any, Dict, List, Optional
from datetime import datetime
import time
from urllib.parse import urlparse
from task_base import TaskBase
from softioc import builder

try:
    from kubernetes import client, config as k8s_config
    from kubernetes.client.rest import ApiException

    KUBERNETES_AVAILABLE = True
except ImportError:
    KUBERNETES_AVAILABLE = False
    print("Warning: kubernetes library not available. IocStatusTask will not function.")


class IocmngTask(TaskBase):
    """
    Task that monitors ArgoCD applications for IOC status.

    Features:
    - Periodically polls ArgoCD applications in the namespace
    - Creates PV waveforms for each devgroup listing IOC names
    - For each IOC, creates status PVs:
      - Sync status (Synced, OutOfSync, Unknown)
      - Health status (Healthy, Progressing, Degraded, Missing, Unknown)
      - Application status (Running, Suspended, etc.)
      - Last sync timestamp
      - Last health change timestamp
    - For each IOC, creates control PVs:
      - START: Sync the application
      - STOP: Suspend the application
      - RESTART: Hard restart (delete and recreate)

    Configuration Parameters:
    - update_rate: Update frequency in Hz (default: 0.05)
    - argocd_namespace: ArgoCD namespace (default: "argocd")
    - kubeconfig_path: Path to kubeconfig file (for out-of-cluster usage)
    - kube_context: Specific kubeconfig context to use
    - api_server: Kubernetes API server endpoint (for custom endpoints)
    """

    def __init__(
        self,
        name: str,
        parameters: Dict[str, Any],
        pv_definitions: Dict[str, Any],
        beamline_config: Dict[str, Any],
        ophyd_devices: Dict[str, object] = None,
        prefix: str = None,
    ):
        """Initialize the IOC status task."""
        super().__init__(
            name, parameters, pv_definitions, beamline_config, ophyd_devices, prefix
        )

        # Kubernetes API client
        self.api = None
        self.k8s_namespace = None

        # IOC tracking
        self.devgroups = {}  # devgroup -> list of IOC names
        self.ioc_status = {}  # ioc_name -> status dict
        self.ioc_pvs = {}  # ioc_name -> dict of PV objects
        self.ioc_to_app_name = {}  # ioc_name -> ArgoCD application name mapping

        # Service tracking (symmetric to IOCs)
        self.service_devgroups = {}  # devgroup -> list of service names
        self.service_status = {}  # service_name -> status dict
        self.service_pvs = {}  # service_name -> dict of PV objects
        self.service_to_app_name = {}  # service_name -> ArgoCD application name mapping

        # Status tracking for change detection
        self.last_health_status = {}  # ioc_name -> health status
        self.last_health_change_time = {}  # ioc_name -> timestamp
        self.last_service_health_status = {}  # service_name -> health status
        self.last_service_health_change_time = {}  # service_name -> timestamp

        # Control action queue
        self.control_queue = []
        self.service_control_queue = []
        self.control_lock = threading.Lock()
        self.service_control_lock = threading.Lock()

        # Kubernetes network/proxy helpers
        self._k8s_proxy_disabled = False
        self._k8s_saved_proxy_env = {}
        self._k8s_last_proxy_error_time = 0

        # EPICS Archiver monitoring
        self.archiver_url = None
        self.archiver_pvs = {}  # pv_name -> archiver status
        self.archiver_connectivity = {}  # pv_name -> connectivity status
        # Archiver restart control
        self.archiver_threshold_restart = None
        self.archiver_wait_restart_min = None
        self.archiver_last_restart_time = None
        self.archiver_app_name = None

    def initialize(self):
        """Initialize the IOC status monitoring task."""
        self.logger.info("Initializing IOC status task")

        # Check if kubernetes is available
        if not KUBERNETES_AVAILABLE:
            self.logger.error("Kubernetes library not available. Task cannot function.")
            self.set_status("ERROR")
            self.set_message("Kubernetes library not available")
            return

        # Disable proxy environment variables early to prevent urllib3 from using them
        try:
            self._disable_k8s_proxy_env()
        except Exception:
            pass

        # Get configuration parameters
        self.update_rate = self.parameters.get(
            "update_rate", 0.05
        )  # Hz (default: every 2 seconds)
        self.argocd_namespace = self.parameters.get("argocd_namespace", "argocd")

        # EPICS Archiver configuration
        self.archiver_url = self.parameters.get(
            "archiver_url"
        )  # Base URL for archiver REST API
        self.archiver_appliance = self.parameters.get(
            "archiver_appliance", "default"
        )  # Appliance name
        self.archiver_threshold_restart = self.parameters.get(
            "archiver_threshold_restart"
        )  # If connected < threshold AND disconnected > threshold -> restart
        self.archiver_wait_restart_min = self.parameters.get(
            "archiver_wait_restart_min"
        )  # Minutes to wait before allowing another restart
        # Optional explicit app name; else derive from URL host
        self.archiver_app_name = self.parameters.get("archiver_app_name")
        if not self.archiver_app_name and self.archiver_url:
            try:
                host = urlparse(self.archiver_url).hostname
                if host:
                    # Use first host label as application name (e.g. sparc-archiver)
                    self.archiver_app_name = host.split(".")[0]
            except Exception:
                pass

        # Kubernetes connection parameters for development/out-of-cluster usage
        self.kubeconfig_path = self.parameters.get(
            "kubeconfig_path"
        )  # Path to kubeconfig file
        self.kube_context = self.parameters.get(
            "kube_context"
        )  # Specific context to use
        self.api_server = self.parameters.get("api_server")  # API server endpoint

        # Get namespace from beamline config
        self.k8s_namespace = self.beamline_config.get("namespace", "default")

        # Get devgroups and IOCs from beamline config
        self._parse_beamline_config()

        # Initialize Kubernetes client
        try:
            # Try in-cluster config first
            k8s_config.load_incluster_config()
            self.logger.info("Loaded in-cluster Kubernetes configuration")
        except Exception as e:
            self.logger.warning(f"Could not load in-cluster config: {e}")
            try:
                # Fall back to kubeconfig with custom parameters
                if self.kubeconfig_path:
                    self.logger.info(f"Loading kubeconfig from: {self.kubeconfig_path}")
                    k8s_config.load_kube_config(
                        config_file=self.kubeconfig_path, context=self.kube_context
                    )
                else:
                    k8s_config.load_kube_config(context=self.kube_context)

                # Override API server if specified
                if self.api_server:
                    # Create custom configuration
                    configuration = client.Configuration()
                    configuration.host = self.api_server
                    # Copy other settings from loaded config
                    loaded_config = k8s_config.load_kube_config(
                        config_file=self.kubeconfig_path, context=self.kube_context
                    )
                    if loaded_config:
                        configuration.api_key = loaded_config.api_key
                        configuration.ssl_ca_cert = loaded_config.ssl_ca_cert
                        configuration.cert_file = loaded_config.cert_file
                        configuration.key_file = loaded_config.key_file
                    client.Configuration.set_default(configuration)
                    self.logger.info(f"Using custom API server: {self.api_server}")

                self.logger.info("Loaded Kubernetes configuration from kubeconfig")
            except Exception as e2:
                self.logger.error(f"Could not load Kubernetes configuration: {e2}")
                self.set_status("ERROR")
                self.set_message("Failed to load Kubernetes config")
                return

        self.api = client.CustomObjectsApi()

        # Create PVs for devgroups, IOCs, and services
        self._create_pvs()

        self.logger.info(
            f"Monitoring {len(self.ioc_status)} IOCs in {len(self.devgroups)} devgroups and {len(self.service_status)} services in {len(self.service_devgroups)} service devgroups"
        )
        self.logger.info(f"Update rate: {self.update_rate} Hz")
        self.logger.info(f"ArgoCD namespace: {self.argocd_namespace}")
        self.logger.info(f"K8s namespace: {self.k8s_namespace}")
        if self.archiver_url:
            self.logger.info(f"Archiver URL: {self.archiver_url}")
            self.logger.info(f"Archiver appliance: {self.archiver_appliance}")

        # Set status to RUN when initialization is successful
        self.set_status("RUN")
        self.set_message("Initialized and monitoring")

    def _disable_k8s_proxy_env(self):
        """Temporarily remove proxy environment variables to allow direct Kubernetes API access."""
        try:
            if getattr(self, "_k8s_proxy_disabled", False):
                return
            proxy_vars = [
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "http_proxy",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
            ]
            for pv in proxy_vars:
                if pv in os.environ:
                    self._k8s_saved_proxy_env[pv] = os.environ.pop(pv)
            self._k8s_proxy_disabled = True
            self.logger.info(
                "Disabled proxy environment variables for Kubernetes API access"
            )
        except Exception as e:
            self.logger.debug(f"Failed to modify proxy env vars: {e}")

    def _restore_k8s_proxy_env(self):
        """Restore proxy environment variables previously removed."""
        try:
            if not getattr(self, "_k8s_proxy_disabled", False):
                return
            for k, v in (self._k8s_saved_proxy_env or {}).items():
                os.environ[k] = v
            self._k8s_saved_proxy_env = {}
            self._k8s_proxy_disabled = False
            self.logger.info(
                "Restored proxy environment variables after Kubernetes access"
            )
        except Exception as e:
            self.logger.debug(f"Failed to restore proxy env vars: {e}")

    def _parse_beamline_config(self):
        """Parse beamline configuration to extract devgroups, IOCs, and services."""
        # Parse IOCs first
        self._parse_iocs_config()

        # Parse services
        self._parse_services_config()

    def _parse_iocs_config(self):
        """Parse IOCs from beamline configuration."""
        # Look for IOCs in common locations. Support both formats:
        # 1) Top-level mapping: beamline_config['iocs'] -> {ioc_name: {...}}
        # 2) Epics configuration list: beamline_config['epicsConfiguration']['iocs'] -> [{name: ..., ...}, ...]
        iocs_config = self.beamline_config.get("iocs")
        if iocs_config is None:
            epics_conf = self.beamline_config.get("epicsConfiguration", {})
            if isinstance(epics_conf, dict):
                iocs_config = epics_conf.get("iocs")

        if not iocs_config:
            self.logger.warning(
                "No 'iocs' section found in beamline configuration (checked 'iocs' and 'epicsConfiguration.iocs')"
            )
            return

        # Normalize to an iterable of (ioc_name, ioc_data) pairs
        items = None
        if isinstance(iocs_config, dict):
            items = list(iocs_config.items())
        elif isinstance(iocs_config, list):
            items = []
            for entry in iocs_config:
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if not name:
                        self.logger.debug(f"Skipping IOC entry without name: {entry}")
                        continue
                    items.append((name, entry))
                elif isinstance(entry, str):
                    items.append((entry, {}))
                else:
                    self.logger.debug(
                        f"Skipping unsupported IOC entry type {type(entry)}: {entry}"
                    )
        else:
            self.logger.warning("Unsupported 'iocs' format in beamline configuration")
            return

        # Apply iocDefaults: merge template defaults into each IOC's data
        ioc_defaults = self.beamline_config.get("iocDefaults") or {}
        if ioc_defaults and items:
            merged_items = []
            for ioc_name, ioc_data in items:
                if isinstance(ioc_data, dict):
                    tmpl = ioc_data.get("template") or ioc_data.get("devtype") or ""
                    tmpl_defaults = ioc_defaults.get(tmpl)
                    if tmpl_defaults:
                        ioc_data = {**tmpl_defaults, **ioc_data}
                merged_items.append((ioc_name, ioc_data))
            items = merged_items

        # Group IOCs by devgroup and initialize status
        for ioc_name, ioc_data in items:
            if isinstance(ioc_data, dict):
                devgroup = ioc_data.get("devgroup", "default")
            else:
                devgroup = "default"

            if devgroup not in self.devgroups:
                self.devgroups[devgroup] = []

            self.devgroups[devgroup].append(ioc_name)

            # Build ArgoCD application name: <namespace>-<iocname>-ioc
            argocd_app_name = f"{self.k8s_namespace}-{ioc_name}-ioc"
            self.ioc_to_app_name[ioc_name] = argocd_app_name

            # Initialize IOC status
            self.ioc_status[ioc_name] = {
                "app_status": "Unknown",
                "sync_status": "Unknown",
                "health_status": "Unknown",
                "last_sync_time": "Never",
                "last_health_change": "Never",
                "devgroup": devgroup,
                "argocd_app_name": argocd_app_name,
            }

        self.logger.info(
            f"Found {len(self.devgroups)} IOC devgroups with {len(self.ioc_status)} IOCs total"
        )
        self.logger.debug(f"IOC to ArgoCD app mapping: {self.ioc_to_app_name}")

    def _parse_services_config(self):
        """Parse services from beamline configuration."""
        # Look for services in epicsConfiguration.services
        epics_conf = self.beamline_config.get("epicsConfiguration", {})
        if not isinstance(epics_conf, dict):
            self.logger.warning(
                "No 'epicsConfiguration' section found in beamline configuration"
            )
            return

        services_config = epics_conf.get("services")
        if not services_config:
            self.logger.warning("No 'services' section found in beamline configuration")
            return

        if not isinstance(services_config, dict):
            self.logger.warning(
                "Unsupported 'services' format in beamline configuration"
            )
            return

        # Group services by devgroup and initialize status
        for service_name, service_data in services_config.items():
            if not isinstance(service_data, dict):
                self.logger.debug(f"Skipping service {service_name}: not a dict")
                continue

            # Services don't have explicit devgroups like IOCs, so we'll use a default
            # or derive from service type
            devgroup = service_data.get("devgroup", "services")

            if devgroup not in self.service_devgroups:
                self.service_devgroups[devgroup] = []

            self.service_devgroups[devgroup].append(service_name)

            # Build ArgoCD application name: <namespace>-<servicename>
            argocd_app_name = f"{self.k8s_namespace}-{service_name}-service"
            self.service_to_app_name[service_name] = argocd_app_name

            # Initialize service status
            self.service_status[service_name] = {
                "app_status": "Unknown",
                "sync_status": "Unknown",
                "health_status": "Unknown",
                "last_sync_time": "Never",
                "last_health_change": "Never",
                "devgroup": devgroup,
                "argocd_app_name": argocd_app_name,
            }

        self.logger.info(
            f"Found {len(self.service_devgroups)} service devgroups with {len(self.service_status)} services total"
        )
        self.logger.debug(f"Service to ArgoCD app mapping: {self.service_to_app_name}")

    def _create_pvs(self):
        """Create PVs for devgroups, IOCs, and services using softioc builder.

        This implementation extends the generic PVs created by TaskBase
        (STATUS, MESSAGE, ENABLE, CYCLE_COUNT, plus any declared inputs/outputs)
        with IOC/service-specific records and summary counters.
        """
        # First create the common/base PVs from TaskBase so STATUS, MESSAGE,
        # ENABLE and CYCLE_COUNT exist and are handled consistently.
        try:
            super()._create_pvs()
        except Exception as e:
            # Log but continue creating the task-specific PVs
            self.logger.debug(f"Failed to create base task PVs: {e}", exc_info=True)

        # Ensure device name prefix is set for subsequent PV creation
        builder.SetDeviceName(self.pv_prefix)

        # Summary counters PVs (total / healthy / progressing / other) for IOCs
        try:
            self.pvs["TOTAL_IOCS"] = builder.longIn(
                "TOTAL_IOCS", initial_value=len(self.ioc_status)
            )
            self.pvs["HEALTHY_COUNT"] = builder.longIn("HEALTHY_COUNT", initial_value=0)
            self.pvs["PROGRESSING_COUNT"] = builder.longIn(
                "PROGRESSING_COUNT", initial_value=0
            )
            self.pvs["OTHER_COUNT"] = builder.longIn("OTHER_COUNT", initial_value=0)
        except Exception:
            self.logger.debug("Failed to create IOC summary count PVs", exc_info=True)

        # Summary counters PVs for services
        try:
            self.pvs["TOTAL_SERVICES"] = builder.longIn(
                "TOTAL_SERVICES", initial_value=len(self.service_status)
            )
            self.pvs["SERVICES_HEALTHY_COUNT"] = builder.longIn(
                "SERVICES_HEALTHY_COUNT", initial_value=0
            )
            self.pvs["SERVICES_PROGRESSING_COUNT"] = builder.longIn(
                "SERVICES_PROGRESSING_COUNT", initial_value=0
            )
            self.pvs["SERVICES_OTHER_COUNT"] = builder.longIn(
                "SERVICES_OTHER_COUNT", initial_value=0
            )
        except Exception:
            self.logger.debug(
                "Failed to create service summary count PVs", exc_info=True
            )

        # Create waveform PVs for IOC devgroups
        for devgroup, ioc_list in self.devgroups.items():
            pv_name = f"DEVGROUP_{devgroup.upper()}_IOCS"
            ioc_list_str = ",".join(ioc_list)
            max_len = len(ioc_list_str)
            self.logger.debug(
                f"Creating IOC devgroup PV: {pv_name} with {len(ioc_list)} IOCs max_len={max_len}"
            )
            pv = builder.WaveformIn(pv_name, initial_value=ioc_list, length=max_len)
            self.pvs[pv_name] = pv
            self.logger.info(
                f"Created IOC devgroup PV: {pv_name} with {len(ioc_list)} IOCs"
            )

        # Create waveform PVs for service devgroups
        for devgroup, service_list in self.service_devgroups.items():
            pv_name = f"SERVICE_DEVGROUP_{devgroup.upper()}_SERVICES"
            service_list_str = ",".join(service_list)
            max_len = len(service_list_str)
            self.logger.debug(
                f"Creating service devgroup PV: {pv_name} with {len(service_list)} services max_len={max_len}"
            )
            pv = builder.WaveformIn(pv_name, initial_value=service_list, length=max_len)
            self.pvs[pv_name] = pv
            self.logger.info(
                f"Created service devgroup PV: {pv_name} with {len(service_list)} services"
            )

        # Create status and control PVs for each IOC
        for ioc_name in self.ioc_status.keys():
            try:
                self._create_ioc_specific_pvs(ioc_name)
            except Exception as e:
                self.logger.error(
                    f"Failed to create PVs for IOC '{ioc_name}': {e}",
                    exc_info=True
                )

        # Create status and control PVs for each service
        for service_name in self.service_status.keys():
            try:
                self._create_service_specific_pvs(service_name)
            except Exception as e:
                self.logger.error(
                    f"Failed to create PVs for service '{service_name}': {e}",
                    exc_info=True
                )

        # Create archiver monitoring PVs if archiver is configured
        if self.archiver_url:
            self._create_archiver_pvs()

    def _create_ioc_specific_pvs(self, ioc_name: str):
        """Create status and control PVs for a specific IOC."""
        ioc_prefix = ioc_name.upper().replace("-", "_")

        # EPICS record name limit is 60 characters (some implementations allow 61-63)
        # We need to account for: PREFIX:TASK_NAME:IOC_PREFIX_PV_SUFFIX
        # Reserve space for the longest suffix and separators
        max_record_length = 60
        prefix_overhead = len(self.pv_prefix) + 1  # +1 for separator
        longest_suffix = len("_LAST_HEALTH")  # Longest PV suffix
        max_ioc_prefix_len = max_record_length - prefix_overhead - longest_suffix

        # Truncate IOC prefix if necessary
        if len(ioc_prefix) > max_ioc_prefix_len:
            original_prefix = ioc_prefix
            ioc_prefix = ioc_prefix[:max_ioc_prefix_len]
            self.logger.warning(
                f"IOC prefix '{original_prefix}' truncated to '{ioc_prefix}' "
                f"(max {max_ioc_prefix_len} chars) to fit EPICS 60-char record name limit"
            )

        ioc_pv_dict = {}

        # Status PVs (readonly from IOC perspective)
        # Application status
        ioc_pv_dict["APP_STATUS"] = builder.stringIn(
            f"{ioc_prefix}_APP_STATUS", initial_value="Unknown"
        )

        # Sync status (mbbi: Synced=0, OutOfSync=1, Unknown=2, Error=3)
        ioc_pv_dict["SYNC_STATUS"] = builder.mbbIn(
            f"{ioc_prefix}_SYNC_STATUS",
            initial_value=2,
            ZRST="Synced",
            ONST="OutOfSync",
            TWST="Unknown",
            THST="Error",
        )

        # Health status (mbbi: Healthy=0, Progressing=1, Degraded=2, Missing=3, Unknown=4, Warning=5, Error=6)
        ioc_pv_dict["HEALTH_STAT"] = builder.mbbIn(
            f"{ioc_prefix}_HEALTH_STAT",
            initial_value=4,
            ZRST="Healthy",
            ONST="Progressing",
            TWST="Degraded",
            THST="Missing",
            FRST="Unknown",
            FVST="Warning",
            SXST="Error",
        )

        # Timestamps
        ioc_pv_dict["LAST_SYNC"] = builder.stringIn(
            f"{ioc_prefix}_LAST_SYNC", initial_value="Never"
        )

        ioc_pv_dict["LAST_HEALTH"] = builder.stringIn(
            f"{ioc_prefix}_LAST_HEALTH", initial_value="Never"
        )

        # Control PVs (writable buttons)
        ioc_pv_dict["START"] = builder.boolOut(
            f"{ioc_prefix}_START",
            initial_value=0,
            on_update=lambda value, ioc=ioc_name: self._on_control_action(
                ioc, "START", value
            ),
        )

        ioc_pv_dict["STOP"] = builder.boolOut(
            f"{ioc_prefix}_STOP",
            initial_value=0,
            on_update=lambda value, ioc=ioc_name: self._on_control_action(
                ioc, "STOP", value
            ),
        )

        ioc_pv_dict["RESTART"] = builder.boolOut(
            f"{ioc_prefix}_RESTART",
            initial_value=0,
            on_update=lambda value, ioc=ioc_name: self._on_control_action(
                ioc, "RESTART", value
            ),
        )

        self.ioc_pvs[ioc_name] = ioc_pv_dict

        self.logger.debug(
            f"Created PVs for IOC: {ioc_name} "
            f"(prefix: {ioc_prefix}, {len(ioc_pv_dict)} PVs)"
        )

    def _create_service_specific_pvs(self, service_name: str):
        """Create status and control PVs for a specific service."""
        service_prefix = service_name.upper().replace("-", "_")

        # EPICS record name limit is 60 characters
        max_record_length = 60
        prefix_overhead = len(self.pv_prefix) + 1
        longest_suffix = len("_LAST_HEALTH")
        max_service_prefix_len = max_record_length - prefix_overhead - longest_suffix

        # Truncate service prefix if necessary
        if len(service_prefix) > max_service_prefix_len:
            original_prefix = service_prefix
            service_prefix = service_prefix[:max_service_prefix_len]
            self.logger.warning(
                f"Service prefix '{original_prefix}' truncated to '{service_prefix}' "
                f"(max {max_service_prefix_len} chars) to fit EPICS 60-char record name limit"
            )

        service_pv_dict = {}

        # Status PVs (readonly from service perspective)
        service_pv_dict["APP_STATUS"] = builder.stringIn(
            f"{service_prefix}_APP_STATUS", initial_value="Unknown"
        )

        # Sync status (mbbi: Synced=0, OutOfSync=1, Unknown=2, Error=3)
        service_pv_dict["SYNC_STATUS"] = builder.mbbIn(
            f"{service_prefix}_SYNC_STATUS",
            initial_value=2,
            ZRST="Synced",
            ONST="OutOfSync",
            TWST="Unknown",
            THST="Error",
        )

        # Health status (mbbi: Healthy=0, Progressing=1, Degraded=2, Missing=3, Unknown=4, Warning=5, Error=6)
        service_pv_dict["HEALTH_STAT"] = builder.mbbIn(
            f"{service_prefix}_HEALTH_STAT",
            initial_value=4,
            ZRST="Healthy",
            ONST="Progressing",
            TWST="Degraded",
            THST="Missing",
            FRST="Unknown",
            FVST="Warning",
            SXST="Error",
        )

        # Timestamps
        service_pv_dict["LAST_SYNC"] = builder.stringIn(
            f"{service_prefix}_LAST_SYNC", initial_value="Never"
        )

        service_pv_dict["LAST_HEALTH"] = builder.stringIn(
            f"{service_prefix}_LAST_HEALTH", initial_value="Never"
        )

        # Control PVs (writable buttons)
        service_pv_dict["START"] = builder.boolOut(
            f"{service_prefix}_START",
            initial_value=0,
            on_update=lambda value, service=service_name: self._on_service_control_action(
                service, "START", value
            ),
        )

        service_pv_dict["STOP"] = builder.boolOut(
            f"{service_prefix}_STOP",
            initial_value=0,
            on_update=lambda value, service=service_name: self._on_service_control_action(
                service, "STOP", value
            ),
        )

        service_pv_dict["RESTART"] = builder.boolOut(
            f"{service_prefix}_RESTART",
            initial_value=0,
            on_update=lambda value, service=service_name: self._on_service_control_action(
                service, "RESTART", value
            ),
        )

        self.service_pvs[service_name] = service_pv_dict

        self.logger.debug(
            f"Created PVs for service: {service_name} "
            f"(prefix: {service_prefix}, {len(service_pv_dict)} PVs)"
        )

    def _create_archiver_pvs(self):
        """Create PVs for EPICS Archiver monitoring."""
        try:
            # Archiver status PVs
            self.pvs["ARCHIVER_STATUS"] = builder.stringIn(
                "ARCHIVER_STATUS", initial_value="Unknown"
            )
            self.pvs["ARCHIVER_TOTAL_PVS"] = builder.longIn(
                "ARCHIVER_TOTAL_PVS", initial_value=0
            )
            self.pvs["ARCHIVER_CONNECTED_PVS"] = builder.longIn(
                "ARCHIVER_CONNECTED_PVS", initial_value=0
            )
            self.pvs["ARCHIVER_DISCONNECTED_PVS"] = builder.longIn(
                "ARCHIVER_DISCONNECTED_PVS", initial_value=0
            )
            self.logger.info("Created archiver monitoring PVs")
        except Exception as e:
            self.logger.debug(f"Failed to create archiver PVs: {e}", exc_info=True)

    def _on_control_action(self, ioc_name: str, action: str, value: Any):
        """Handle control button presses."""
        try:
            pressed = bool(value)
        except Exception:
            pressed = False

        if not pressed:
            return

        # Reset the button immediately
        button_pv = self.ioc_pvs[ioc_name].get(action)
        if button_pv:
            try:
                button_pv.set(0)
            except Exception:
                pass

        # Queue the action for processing
        with self.control_lock:
            self.control_queue.append((ioc_name, action))

        self.logger.info(f"Queued {action} action for IOC: {ioc_name}")

    def _on_service_control_action(self, service_name: str, action: str, value: Any):
        """Handle service control button presses."""
        try:
            pressed = bool(value)
        except Exception:
            pressed = False

        if not pressed:
            return

        # Reset the button immediately
        button_pv = self.service_pvs[service_name].get(action)
        if button_pv:
            try:
                button_pv.set(0)
            except Exception:
                pass

        # Queue the action for processing
        with self.control_lock:
            self.service_control_queue.append((service_name, action))

        self.logger.info(f"Queued {action} action for service: {service_name}")

    def run(self):
        """Main task execution loop."""
        self.logger.info("Starting IOC status monitoring loop")

        while self.running:
            # Only process if task is enabled
            try:
                enabled = self.pvs["ENABLE"].get()
            except KeyError:
                enabled = True  # Default to enabled if PV not found
                self.logger.debug("ENABLE PV not found, defaulting to enabled")

            if enabled:
                self._process_cycle()
                self.step_cycle()
            else:
                self.logger.debug("Task disabled, skipping cycle")

            # Sleep based on update rate
            cothread.Sleep(1.0 / self.update_rate)

    def _process_cycle(self):
        """Process one monitoring cycle."""
        try:
            # Update status for all IOCs and services
            self._update_all_ioc_status()
            self._update_all_service_status()

            # Update archiver status if configured
            if self.archiver_url:
                self._update_archiver_status()

            # Process any queued control actions
            self._process_control_queue()
            self._process_service_control_queue()

            # Update IOC summary PVs
            total_iocs = len(self.ioc_status)
            healthy_count = sum(
                1 for s in self.ioc_status.values() if s["health_status"] == "Healthy"
            )
            progressing_count = sum(
                1
                for s in self.ioc_status.values()
                if s["health_status"] == "Progressing"
            )
            other_count = total_iocs - healthy_count - progressing_count

            # Update service summary PVs
            total_services = len(self.service_status)
            services_healthy_count = sum(
                1
                for s in self.service_status.values()
                if s["health_status"] == "Healthy"
            )
            services_progressing_count = sum(
                1
                for s in self.service_status.values()
                if s["health_status"] == "Progressing"
            )
            services_other_count = (
                total_services - services_healthy_count - services_progressing_count
            )

            # Update IOC summary PVs if present
            try:
                if "TOTAL_IOCS" in self.pvs:
                    self.pvs["TOTAL_IOCS"].set(int(total_iocs))
                if "HEALTHY_COUNT" in self.pvs:
                    self.pvs["HEALTHY_COUNT"].set(int(healthy_count))
                if "PROGRESSING_COUNT" in self.pvs:
                    self.pvs["PROGRESSING_COUNT"].set(int(progressing_count))
                if "OTHER_COUNT" in self.pvs:
                    self.pvs["OTHER_COUNT"].set(int(other_count))
            except Exception:
                self.logger.debug(
                    "Failed to update IOC summary count PVs", exc_info=True
                )

            # Update service summary PVs if present
            try:
                if "TOTAL_SERVICES" in self.pvs:
                    self.pvs["TOTAL_SERVICES"].set(int(total_services))
                if "SERVICES_HEALTHY_COUNT" in self.pvs:
                    self.pvs["SERVICES_HEALTHY_COUNT"].set(int(services_healthy_count))
                if "SERVICES_PROGRESSING_COUNT" in self.pvs:
                    self.pvs["SERVICES_PROGRESSING_COUNT"].set(
                        int(services_progressing_count)
                    )
                if "SERVICES_OTHER_COUNT" in self.pvs:
                    self.pvs["SERVICES_OTHER_COUNT"].set(int(services_other_count))
            except Exception:
                self.logger.debug(
                    "Failed to update service summary count PVs", exc_info=True
                )

            self.set_message(
                f"Monitoring {total_iocs} IOCs ({healthy_count} healthy) and {total_services} services ({services_healthy_count} healthy)"
            )

        except Exception as e:
            self.logger.error(f"Error in processing cycle: {e}", exc_info=True)
            self.set_status("ERROR")
            self.set_message(f"Error: {str(e)}")

    def _update_all_ioc_status(self):
        """Update status for all IOCs by querying ArgoCD applications."""
        if not self.api:
            return

        # List all applications in the ArgoCD namespace
        try:
            apps = self.api.list_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
            )

            app_items = apps.get("items", [])

            # Create a map of application names to app data
            app_map = {}
            for app in app_items:
                app_name = app["metadata"]["name"]
                app_map[app_name] = app

            # Update status for each tracked IOC
            for ioc_name in self.ioc_status.keys():
                # Get the ArgoCD application name for this IOC
                argocd_app_name = self.ioc_to_app_name.get(ioc_name, ioc_name)
                self._update_ioc_status(ioc_name, app_map.get(argocd_app_name))

        except ApiException as e:
            self.logger.error(f"Error listing ArgoCD applications: {e}")
        except Exception as e:
            self.logger.error(
                f"Unexpected error updating IOC status: {e}", exc_info=True
            )

    def _update_service_status(self, service_name, app):
        """Update status for a single service based on ArgoCD application data."""
        if service_name not in self.service_status:
            return

        # Check if PVs exist for this service
        if service_name not in self.service_pvs:
            self.logger.warning(
                f"No PVs found for service '{service_name}'. Skipping status update. "
                f"Service may have been added after PV creation or PV creation failed."
            )
            return

        try:
            if app is None:
                # Application not found
                self.service_status[service_name]["app_status"] = "NOT_FOUND"
                self.service_status[service_name]["sync_status"] = "Unknown"
                self.service_status[service_name]["health_status"] = "Missing"
                self.service_pvs[service_name]["APP_STATUS"].set("NOT_FOUND")
                self.service_pvs[service_name]["SYNC_STATUS"].set(3)  # Red
                self.service_pvs[service_name]["HEALTH_STAT"].set(3)  # Red
                return

            # Extract status information
            status = app.get("status", {})

            # Operation phase (Running, Suspended, etc.)
            operation_state = status.get("operationState", {})
            phase = operation_state.get("phase", "Unknown")
            self.service_status[service_name]["app_status"] = phase

            # Sync status
            sync = status.get("sync", {})
            sync_status = sync.get("status", "Unknown")
            self.service_status[service_name]["sync_status"] = sync_status

            # Last sync time
            sync_result = status.get("operationState", {}).get("finishedAt")
            if sync_result:
                try:
                    # Parse and format timestamp
                    dt = datetime.fromisoformat(sync_result.replace("Z", "+00:00"))
                    self.service_status[service_name]["last_sync_time"] = dt.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except Exception:
                    self.service_status[service_name]["last_sync_time"] = sync_result

            # Health status
            health = status.get("health", {})
            health_status = health.get("status", "Unknown")

            # Detect health status change
            previous_health = self.last_service_health_status.get(
                service_name, "Unknown"
            )
            if health_status != previous_health:
                self.last_service_health_status[service_name] = health_status
                self.last_service_health_change_time[service_name] = (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
                self.service_status[service_name]["last_health_change"] = (
                    self.last_service_health_change_time[service_name]
                )
                self.logger.info(
                    f"Service {service_name} health changed: {previous_health} -> {health_status}"
                )

            self.service_status[service_name]["health_status"] = health_status

            # Map ArgoCD sync status to our numeric values
            sync_status_map = {
                "Synced": 0,  # Green
                "OutOfSync": 1,  # Yellow
                "Unknown": 2,  # Orange
            }
            sync_value = sync_status_map.get(sync_status, 3)  # Red for other statuses

            # Map ArgoCD health status to our numeric values
            health_status_map = {
                "Healthy": 0,  # Green
                "Progressing": 1,  # Yellow
                "Suspended": 5,  # Yellow
                "Degraded": 2,  # Red
                "Missing": 3,  # Red
                "Unknown": 4,  # Red
            }
            health_value = health_status_map.get(
                health_status, 4
            )  # Red for other statuses

            # Determine overall service status
            if sync_value == 0 and health_value == 0:
                overall_status = "HEALTHY"
            elif health_value == 1:
                overall_status = "PROGRESSING"
            else:
                overall_status = "OTHER"

            # Update PVs
            self.service_status[service_name]["app_status"] = overall_status
            self.service_pvs[service_name]["APP_STATUS"].set(overall_status)
            self.service_pvs[service_name]["SYNC_STATUS"].set(sync_value)
            self.service_pvs[service_name]["HEALTH_STAT"].set(health_value)

            # Update timestamp PVs
            try:
                self.service_pvs[service_name]["LAST_SYNC"].set(
                    self.service_status[service_name]["last_sync_time"]
                )
            except Exception as e:
                self.logger.debug(f"Error setting LAST_SYNC for {service_name}: {e}")

            try:
                self.service_pvs[service_name]["LAST_HEALTH"].set(
                    self.service_status[service_name]["last_health_change"]
                )
            except Exception as e:
                self.logger.debug(f"Error setting LAST_HEALTH for {service_name}: {e}")

        except Exception as e:
            self.logger.error(
                f"Error updating service status for {service_name}: {e}", exc_info=True
            )
            self.service_status[service_name]["app_status"] = "ERROR"
            self.service_status[service_name]["sync_status"] = "Unknown"
            self.service_status[service_name]["health_status"] = "Error"
            
            # Only update PVs if they exist
            if service_name in self.service_pvs:
                try:
                    self.service_pvs[service_name]["APP_STATUS"].set("ERROR")
                    self.service_pvs[service_name]["SYNC_STATUS"].set(3)  # Red
                    self.service_pvs[service_name]["HEALTH_STAT"].set(3)  # Red
                except Exception as pv_error:
                    self.logger.debug(
                        f"Failed to update error status PVs for {service_name}: {pv_error}"
                    )

    def _update_all_service_status(self):
        """Update status for all services by querying ArgoCD applications."""
        if not self.api:
            return

        # List all applications in the ArgoCD namespace
        try:
            apps = self.api.list_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
            )

            app_items = apps.get("items", [])

            # Create a map of application names to app data
            app_map = {}
            for app in app_items:
                app_name = app["metadata"]["name"]
                app_map[app_name] = app

            # Update status for each tracked service
            for service_name in self.service_status.keys():
                # Get the ArgoCD application name for this service
                argocd_app_name = self.service_to_app_name.get(
                    service_name, service_name
                )
                self._update_service_status(service_name, app_map.get(argocd_app_name))

        except ApiException as e:
            self.logger.error(f"Error listing ArgoCD applications: {e}")
        except Exception as e:
            self.logger.error(
                f"Unexpected error updating service status: {e}", exc_info=True
            )

    def _update_ioc_status(self, ioc_name: str, app_data: Optional[Dict]):
        """Update status for a single IOC."""
        if not app_data:
            # Application not found
            self.ioc_status[ioc_name]["app_status"] = "Missing"
            self.ioc_status[ioc_name]["sync_status"] = "Unknown"
            self.ioc_status[ioc_name]["health_status"] = "Missing"

            self._update_ioc_pvs(ioc_name)
            return

        # Extract status information
        status = app_data.get("status", {})

        # Operation phase (Running, Suspended, etc.)
        operation_state = status.get("operationState", {})
        phase = operation_state.get("phase", "Unknown")
        self.ioc_status[ioc_name]["app_status"] = phase

        # Sync status
        sync = status.get("sync", {})
        sync_status = sync.get("status", "Unknown")
        self.ioc_status[ioc_name]["sync_status"] = sync_status

        # Last sync time
        sync_result = status.get("operationState", {}).get("finishedAt")
        if sync_result:
            try:
                # Parse and format timestamp
                dt = datetime.fromisoformat(sync_result.replace("Z", "+00:00"))
                self.ioc_status[ioc_name]["last_sync_time"] = dt.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                self.ioc_status[ioc_name]["last_sync_time"] = sync_result

        # Health status
        health = status.get("health", {})
        health_status = health.get("status", "Unknown")

        # Detect health status change
        previous_health = self.last_health_status.get(ioc_name, "Unknown")
        if health_status != previous_health:
            self.last_health_status[ioc_name] = health_status
            self.last_health_change_time[ioc_name] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self.ioc_status[ioc_name]["last_health_change"] = (
                self.last_health_change_time[ioc_name]
            )
            self.logger.info(
                f"IOC {ioc_name} health changed: {previous_health} -> {health_status}"
            )

        self.ioc_status[ioc_name]["health_status"] = health_status

        # Update PVs
        self._update_ioc_pvs(ioc_name)

    def _update_ioc_pvs(self, ioc_name: str):
        """Update PV values for a specific IOC."""
        if ioc_name not in self.ioc_pvs:
            return

        status = self.ioc_status[ioc_name]
        pvs = self.ioc_pvs[ioc_name]

        # Update application status
        try:
            pvs["APP_STATUS"].set(status["app_status"])
        except Exception as e:
            self.logger.debug(f"Error setting APP_STATUS for {ioc_name}: {e}")

        # Update sync status
        sync_map = {"Synced": 0, "OutOfSync": 1, "Unknown": 2}
        sync_val = sync_map.get(status["sync_status"], 3)  # 3 = Error
        try:
            pvs["SYNC_STATUS"].set(sync_val)
        except Exception as e:
            self.logger.debug(f"Error setting SYNC_STATUS for {ioc_name}: {e}")

        # Update health status
        health_map = {
            "Healthy": 0,
            "Progressing": 1,
            "Degraded": 2,
            "Missing": 3,
            "Unknown": 4,
            "Warning": 5,
            "Error": 6,
        }
        health_val = health_map.get(status["health_status"], 4)  # 4 = Unknown

        # Map to warning/error if needed based on health status
        if status["health_status"] == "Progressing":
            health_val = 1  # ONST = Progressing
        elif status["health_status"] not in (
            "Healthy",
            "Progressing",
            "Degraded",
            "Missing",
            "Unknown",
        ):
            # For any other status, consider it a warning
            health_val = 5  # FVST = Warning

        try:
            pvs["HEALTH_STAT"].set(health_val)
        except Exception as e:
            self.logger.debug(f"Error setting HEALTH_STAT for {ioc_name}: {e}")

        # Update timestamps
        try:
            pvs["LAST_SYNC"].set(status["last_sync_time"])
        except Exception as e:
            self.logger.debug(f"Error setting LAST_SYNC for {ioc_name}: {e}")

        try:
            pvs["LAST_HEALTH"].set(status["last_health_change"])
        except Exception as e:
            self.logger.debug(f"Error setting LAST_HEALTH for {ioc_name}: {e}")

    def _process_control_queue(self):
        """Process queued control actions."""
        with self.control_lock:
            queue_copy = self.control_queue[:]
            self.control_queue.clear()

        for ioc_name, action in queue_copy:
            self.logger.info(f"Processing {action} for IOC: {ioc_name}")

            try:
                if action == "START":
                    self._start_ioc(ioc_name)
                elif action == "STOP":
                    self._stop_ioc(ioc_name)
                elif action == "RESTART":
                    self._restart_ioc(ioc_name)

                # Wait 1 second and update status after the action
                time.sleep(1)
                self._process_cycle()

            except Exception as e:
                self.logger.error(
                    f"Error executing {action} for {ioc_name}: {e}", exc_info=True
                )

    def _process_service_control_queue(self):
        """Process pending service control actions."""
        if not self.api:
            return

        with self.service_control_lock:
            queue_copy = self.service_control_queue[:]
            self.service_control_queue.clear()

        for service_name, action in queue_copy:
            self.logger.info(
                f"Processing service control action: {action} for {service_name}"
            )

            try:
                # Get the ArgoCD application name for this service
                argocd_app_name = self.service_to_app_name.get(
                    service_name, service_name
                )

                if action == "START":
                    # For services, START means sync the application
                    self._sync_argocd_application(argocd_app_name)
                elif action == "STOP":
                    # For services, STOP means delete the application
                    self._delete_argocd_application(argocd_app_name)
                elif action == "RESTART":
                    # For services, RESTART means delete and sync to redeploy
                    self._restart_argocd_application(argocd_app_name)
                else:
                    self.logger.warning(
                        f"Unknown service control action: {action} for {service_name}"
                    )

                # Wait 1 second and update status after the action
                time.sleep(1)
                self._process_cycle()

            except Exception as e:
                self.logger.error(
                    f"Error processing service control action for {service_name}: {e}",
                    exc_info=True,
                )

    def _start_ioc(self, ioc_name: str):
        """Start (sync) an IOC application."""
        try:
            # Get the ArgoCD application name for this IOC
            argocd_app_name = self.ioc_to_app_name.get(ioc_name, ioc_name)

            # Trigger a sync operation
            body = {
                "operation": {
                    "initiatedBy": {"username": "beamline-controller"},
                    "sync": {"revision": "HEAD", "prune": True},
                }
            }

            self.api.patch_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
                name=argocd_app_name,
                body=body,
            )
            self.logger.info(
                f"Started (synced) IOC: {ioc_name} (ArgoCD app: {argocd_app_name})"
            )
        except ApiException as e:
            self.logger.error(f"Error starting IOC {ioc_name}: {e}")

    def _stop_ioc(self, ioc_name: str):
        """Stop (delete) an IOC application."""
        try:
            # Get the ArgoCD application name for this IOC
            argocd_app_name = self.ioc_to_app_name.get(ioc_name, ioc_name)

            # Delete the application
            self.api.delete_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
                name=argocd_app_name,
            )

            self.logger.info(
                f"Stopped (deleted) IOC: {ioc_name} (ArgoCD app: {argocd_app_name})"
            )
        except ApiException as e:
            self.logger.error(f"Error stopping IOC {ioc_name}: {e}")

    def _restart_ioc(self, ioc_name: str):
        """Restart an IOC application (delete and sync to redeploy)."""
        try:
            # Get the ArgoCD application name for this IOC
            argocd_app_name = self.ioc_to_app_name.get(ioc_name, ioc_name)

            # First delete the application
            try:
                self.api.delete_namespaced_custom_object(
                    group="argoproj.io",
                    version="v1alpha1",
                    namespace=self.argocd_namespace,
                    plural="applications",
                    name=argocd_app_name,
                )
                self.logger.info(
                    f"Deleted IOC application for restart: {argocd_app_name}"
                )
            except ApiException as e:
                self.logger.warning(
                    f"Could not delete IOC application {argocd_app_name} for restart: {e}"
                )

            # Then sync to redeploy
            sync_body = {
                "operation": {
                    "initiatedBy": {"username": "beamline-controller"},
                    "sync": {"revision": "HEAD", "prune": True},
                }
            }

            self.api.patch_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
                name=argocd_app_name,
                body=sync_body,
            )

            self.logger.info(
                f"Restarted IOC: {ioc_name} (ArgoCD app: {argocd_app_name})"
            )
        except ApiException as e:
            self.logger.error(f"Error restarting IOC {ioc_name}: {e}")

    def _sync_argocd_application(self, argocd_app_name: str):
        """Sync an ArgoCD application."""
        try:
            body = {
                "operation": {
                    "initiatedBy": {"username": "beamline-controller"},
                    "sync": {"revision": "HEAD", "prune": True},
                }
            }

            self.api.patch_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
                name=argocd_app_name,
                body=body,
            )
            self.logger.info(f"Synced ArgoCD application: {argocd_app_name}")
        except ApiException as e:
            self.logger.error(
                f"Error syncing ArgoCD application {argocd_app_name}: {e}"
            )

    def _delete_argocd_application(self, argocd_app_name: str):
        """Delete an ArgoCD application."""
        try:
            self.api.delete_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
                name=argocd_app_name,
            )
            self.logger.info(f"Deleted ArgoCD application: {argocd_app_name}")
        except ApiException as e:
            self.logger.error(
                f"Error deleting ArgoCD application {argocd_app_name}: {e}"
            )

    def _restart_argocd_application(self, argocd_app_name: str):
        """Restart an ArgoCD application (delete and sync to redeploy)."""
        try:
            # First delete the application
            try:
                self.api.delete_namespaced_custom_object(
                    group="argoproj.io",
                    version="v1alpha1",
                    namespace=self.argocd_namespace,
                    plural="applications",
                    name=argocd_app_name,
                )
                self.logger.info(
                    f"Deleted ArgoCD application for restart: {argocd_app_name}"
                )
            except ApiException as e:
                self.logger.warning(
                    f"Could not delete ArgoCD application {argocd_app_name} for restart: {e}"
                )

            # Then sync to redeploy
            sync_body = {
                "operation": {
                    "initiatedBy": {"username": "beamline-controller"},
                    "sync": {"revision": "HEAD", "prune": True},
                }
            }

            self.api.patch_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.argocd_namespace,
                plural="applications",
                name=argocd_app_name,
                body=sync_body,
            )

            self.logger.info(f"Restarted ArgoCD application: {argocd_app_name}")
        except ApiException as e:
            self.logger.error(
                f"Error restarting ArgoCD application {argocd_app_name}: {e}"
            )

    def _update_archiver_status(self):
        """Update EPICS Archiver status via REST API."""
        if not self.archiver_url:
            return

        try:
            import requests
        except ImportError:
            requests = None
            self.logger.warning(
                "requests library not available. Archiver monitoring will be disabled."
            )

        if requests is None:
            self.logger.warning(
                "Skipping archiver status update - requests not available"
            )
            return
        mgmt_url = f"{self.archiver_url}/mgmt/bpl/getApplianceMetrics"
        try:
            # Get appliance metrics from archiver
           
            params = {}
            if self.archiver_appliance != "default":
                params["appliance"] = self.archiver_appliance

            response = requests.get(mgmt_url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                metrics = data[0]
                total_pvs = int(metrics.get("pvCount", 0))
                connected_count = int(metrics.get("connectedPVCount", 0))
                disconnected_count = int(metrics.get("disconnectedPVCount", 0))
            else:
                total_pvs = 0
                connected_count = 0
                disconnected_count = 0

            # Update PVs
            try:
                if "ARCHIVER_STATUS" in self.pvs:
                    self.pvs["ARCHIVER_STATUS"].set("Connected")
                if "ARCHIVER_TOTAL_PVS" in self.pvs:
                    self.pvs["ARCHIVER_TOTAL_PVS"].set(int(total_pvs))
                if "ARCHIVER_CONNECTED_PVS" in self.pvs:
                    self.pvs["ARCHIVER_CONNECTED_PVS"].set(int(connected_count))
                if "ARCHIVER_DISCONNECTED_PVS" in self.pvs:
                    self.pvs["ARCHIVER_DISCONNECTED_PVS"].set(int(disconnected_count))
            except Exception as e:
                self.logger.debug(f"Error updating archiver PVs: {e}")

            self.logger.debug(
                f"Archiver status: {total_pvs} total PVs, {connected_count} connected, {disconnected_count} disconnected"
            )

            # Automatic restart logic
            try:
                if (
                    self.archiver_threshold_restart is not None
                    and self.archiver_wait_restart_min is not None
                    and self.archiver_app_name
                ):
                    # Only attempt restart if outside the cooldown window
                    now = time.time()
                    can_restart = (
                        self.archiver_last_restart_time is None
                        or (
                            now - self.archiver_last_restart_time
                            >= self.archiver_wait_restart_min * 60
                        )
                    )
                    if (
                        can_restart
                        and connected_count < self.archiver_threshold_restart
                        and disconnected_count > self.archiver_threshold_restart
                    ):
                        self.logger.warning(
                            "Archiver unhealthy (connected %d < %d, disconnected %d > %d). Restarting application '%s'.",
                            connected_count,
                            self.archiver_threshold_restart,
                            disconnected_count,
                            self.archiver_threshold_restart,
                            self.archiver_app_name,
                        )
                        # Update status PV before restart
                        try:
                            if "ARCHIVER_STATUS" in self.pvs:
                                self.pvs["ARCHIVER_STATUS"].set("Restarting")
                        except Exception:
                            pass
                        self.set_message(
                            f"Restarting archiver '{self.archiver_app_name}' due to low connectivity"
                        )
                        # Perform restart (delete & recreate application)
                        try:
                            self._restart_argocd_application(self.archiver_app_name)
                        except Exception as e:
                            self.logger.error(
                                f"Failed to restart archiver application {self.archiver_app_name}: {e}"
                            )
                        else:
                            self.archiver_last_restart_time = now
                            self.logger.info(
                                f"Archiver restart initiated for {self.archiver_app_name}; cooldown {self.archiver_wait_restart_min} min"
                            )
                    elif not can_restart and self.archiver_last_restart_time is not None:
                        remaining = (
                            self.archiver_wait_restart_min * 60
                            - (now - self.archiver_last_restart_time)
                        )
                        if remaining > 0:
                            self.logger.debug(
                                f"Archiver restart cooldown active ({int(remaining)}s remaining)"
                            )
            except Exception as e:
                self.logger.debug(
                    f"Archiver auto-restart logic encountered an error: {e}",
                    exc_info=True,
                )

        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Error accessing archiver API {mgmt_url}: {e}")
            try:
                if "ARCHIVER_STATUS" in self.pvs:
                    self.pvs["ARCHIVER_STATUS"].set("Disconnected")
            except Exception:
                pass
        except Exception as e:
            self.logger.error(
                f"Unexpected error updating archiver status: {e}", exc_info=True
            )
            try:
                if "ARCHIVER_STATUS" in self.pvs:
                    self.pvs["ARCHIVER_STATUS"].set("Error")
            except Exception:
                pass

    def cleanup(self):
        """Cleanup when task stops."""
        self.logger.info("Cleaning up IOC status task")
        # Restore proxy environment variables if they were disabled
        try:
            self._restore_k8s_proxy_env()
        except Exception:
            pass
        self.set_status("END")
        self.set_message("Stopped")

    def handle_pv_write(self, pv_name: str, value: Any):
        """
        Handle writes to specific PVs.

        Args:
            pv_name: Name of the PV that was written
            value: New value
        """
        # Control actions are handled via _on_control_action callbacks
        pass
