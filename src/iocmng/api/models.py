"""Pydantic models for the REST API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic import model_validator


class AddPluginRequest(BaseModel):
    """Request body for adding a task or job."""

    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$",
                      description="Unique plugin name (alphanumeric, hyphens, underscores)")
    git_url: Optional[str] = Field(None, min_length=1, description="Git repository URL")
    local_path: Optional[str] = Field(None, min_length=1, description="Local filesystem path to a plugin source directory")
    path: str = Field("", description="Sub-path inside the git repo where the plugin Python file and config.yaml live (e.g. 'src/my_task')")
    pat: Optional[str] = Field(None, description="Personal Access Token for private repos")
    branch: str = Field("main", description="Branch or tag to clone")
    auto_start: bool = Field(True, description="Start task immediately after loading")
    auto_start_on_boot: bool = Field(False, description="Persist plugin for IOCMNG startup autoload")
    autostart_order: Optional[int] = Field(None, description="Autostart order on IOCMNG boot (lower starts first)")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Extra parameters merged into config.yaml parameters")

    @model_validator(mode="after")
    def validate_source(self):
        if not self.git_url and not self.local_path:
            raise ValueError("Either git_url or local_path must be provided")
        return self


class RemovePluginRequest(BaseModel):
    """Request body for removing a task or job."""

    name: str = Field(..., min_length=1, max_length=128)


class PluginResponse(BaseModel):
    """Response for plugin operations."""

    ok: bool
    message: str
    validation: Optional[Dict[str, Any]] = None


class PluginInfoResponse(BaseModel):
    """Response containing plugin information."""

    name: str
    git_url: str
    path: str = ""
    plugin_type: str
    class_name: str
    status: str
    running: Optional[bool] = None
    cycle_count: Optional[int] = None


class PluginListResponse(BaseModel):
    """Response for listing plugins."""

    plugins: List[Dict[str, Any]]
    count: int


class JobRunResponse(BaseModel):
    """Response for running a job."""

    ok: bool
    result: Optional[Dict[str, Any]] = None


class RestartResponse(BaseModel):
    """Response for a plugin restart operation."""

    ok: bool
    message: str
    validation: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    tasks_count: int
    jobs_count: int


class PluginStartupInfoResponse(BaseModel):
    """Startup metadata for a loaded task or job."""

    name: str
    plugin_type: str
    auto_start: bool
    auto_start_on_boot: bool
    autostart_order: Optional[int] = None
    pv_prefix: Optional[str] = None
    plugin_prefix: Optional[str] = None
    mode: Optional[str] = None
    start_parameters: Dict[str, Any]
    arguments: Dict[str, Any] = Field(default_factory=dict)
    pv_definitions: Dict[str, Any] = Field(default_factory=dict)
    base_control_pvs: List[str] = Field(default_factory=list)
    additional_input_pvs: List[str] = Field(default_factory=list)
    additional_output_pvs: List[str] = Field(default_factory=list)
    built_pvs: List[str] = Field(default_factory=list)


# ------------------------------------------------------------------
# External PV client models
# ------------------------------------------------------------------


class PvGetRequest(BaseModel):
    """Request body for reading an external PV."""

    pv_name: str = Field(..., min_length=1, description="Full PV name")
    timeout: float = Field(5.0, gt=0, description="Timeout in seconds")


class PvPutRequest(BaseModel):
    """Request body for writing to an external PV."""

    pv_name: str = Field(..., min_length=1, description="Full PV name")
    value: Any = Field(..., description="Value to write")
    timeout: float = Field(5.0, gt=0, description="Timeout in seconds")


class PvMonitorRequest(BaseModel):
    """Request body for starting a PV subscription."""

    pv_name: str = Field(..., min_length=1, description="Full PV name")
    name: Optional[str] = Field(None, description="Friendly key for the subscription (defaults to pv_name)")


class PvValueResponse(BaseModel):
    """Response carrying a PV value."""

    ok: bool
    pv_name: str
    value: Optional[Any] = None
    error: Optional[str] = None


class PvPutResponse(BaseModel):
    """Response for a PV put operation."""

    ok: bool
    pv_name: str
    error: Optional[str] = None


class PvMonitorResponse(BaseModel):
    """Response for monitor start/stop."""

    ok: bool
    key: Optional[str] = None
    message: str


class PvMonitorListResponse(BaseModel):
    """List of active PV monitors."""

    monitors: Dict[str, str]
    count: int


class PvProviderResponse(BaseModel):
    """Current PV client provider info."""

    provider: str
