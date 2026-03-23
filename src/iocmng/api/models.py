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


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    tasks_count: int
    jobs_count: int
