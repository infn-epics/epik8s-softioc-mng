"""Pydantic models for the REST API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AddPluginRequest(BaseModel):
    """Request body for adding a task or job."""

    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$",
                      description="Unique plugin name (alphanumeric, hyphens, underscores)")
    git_url: str = Field(..., min_length=1, description="Git repository URL (https)")
    path: str = Field("", description="Sub-path inside the git repo where the plugin Python file and config.yaml live (e.g. 'src/my_task')")
    pat: Optional[str] = Field(None, description="Personal Access Token for private repos")
    branch: str = Field("main", description="Branch or tag to clone")
    auto_start: bool = Field(True, description="Start task immediately after loading")
    auto_start_on_boot: bool = Field(False, description="Persist plugin for IOCMNG startup autoload")
    autostart_order: Optional[int] = Field(None, description="Autostart order on IOCMNG boot (lower starts first)")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Extra parameters merged into config.yaml parameters")


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
    pv_definitions: Dict[str, Any]
    base_control_pvs: List[str]
    additional_input_pvs: List[str]
    additional_output_pvs: List[str]
    built_pvs: List[str]
