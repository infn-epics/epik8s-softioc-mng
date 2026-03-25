"""REST API routes for the IOC Manager."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from iocmng.api.models import (
    AddPluginRequest,
    HealthResponse,
    JobRunResponse,
    PluginInfoResponse,
    PluginListResponse,
    PluginResponse,
    RestartResponse,
    TaskStartupInfoResponse,
)
from iocmng.core.controller import IocMngController

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

# Will be set by create_app
_controller: Optional[IocMngController] = None


def set_controller(controller: IocMngController):
    global _controller
    _controller = controller


def _get_controller() -> IocMngController:
    if _controller is None:
        raise HTTPException(status_code=503, detail="Controller not initialized")
    return _controller


# ------------------------------------------------------------------
# Unified plugin management  (type determined by config.yaml / class)
# ------------------------------------------------------------------


@router.post("/plugins", response_model=PluginResponse)
async def add_plugin(req: AddPluginRequest):
    """Add a task or job plugin from a git repository.

    The plugin type (task / job) is determined automatically from the class
    found inside the repo (must derive from TaskBase or JobBase).  The
    ``config.yaml`` at the specified ``path`` controls PV definitions,
    default parameters, and may include a ``type`` field for documentation.
    """
    ctrl = _get_controller()
    ok, msg, validation = ctrl.add_plugin(
        name=req.name,
        git_url=req.git_url,
        pat=req.pat,
        branch=req.branch,
        path=req.path,
        auto_start=req.auto_start,
        auto_start_on_boot=req.auto_start_on_boot,
        autostart_order=req.autostart_order,
        parameters=req.parameters,
    )
    return PluginResponse(ok=ok, message=msg, validation=validation)


@router.delete("/plugins/{name}", response_model=PluginResponse)
async def remove_plugin(name: str):
    """Remove a plugin by name (stops it first if it is a running task)."""
    ctrl = _get_controller()
    ok, msg = ctrl.remove_plugin(name)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return PluginResponse(ok=True, message=msg)


@router.get("/plugins", response_model=PluginListResponse)
async def list_plugins(type: Optional[str] = None):
    """List all loaded plugins, optionally filtered by type ('task' or 'job')."""
    ctrl = _get_controller()
    plugins = ctrl.list_plugins(plugin_type=type)
    return PluginListResponse(plugins=plugins, count=len(plugins))


@router.get("/plugins/{name}")
async def get_plugin(name: str):
    """Get details of a specific plugin."""
    ctrl = _get_controller()
    info = ctrl.get_plugin(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    return info


@router.post("/plugins/{name}/restart", response_model=RestartResponse)
async def restart_plugin(name: str):
    """Re-fetch, validate, and hot-reload a plugin.

    The repository is re-cloned into a temporary directory. If every
    validation step passes the running instance is stopped, the code is
    swapped, and the plugin is restarted — all without removing it from the
    registry.  The original branch and PAT are reused automatically.
    """
    ctrl = _get_controller()
    ok, msg, validation = ctrl.restart_plugin(name)
    if not ok and validation is None:
        raise HTTPException(status_code=404, detail=msg)
    return RestartResponse(ok=ok, message=msg, validation=validation)


@router.post("/plugins/{name}/run", response_model=JobRunResponse)
async def run_plugin(name: str):
    """Execute a loaded job and return its result.

    Returns 400 if the named plugin is a task rather than a job.
    """
    ctrl = _get_controller()
    info = ctrl.get_plugin(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    if info.get("plugin_type") != "job":
        raise HTTPException(status_code=400, detail=f"'{name}' is a task, not a job — use DELETE or restart")
    ok, result = ctrl.run_job(name)
    if "error" in (result or {}):
        raise HTTPException(status_code=404, detail=result["error"])
    return JobRunResponse(ok=ok, result=result)


# ------------------------------------------------------------------
# Tasks (type-scoped convenience aliases)
# ------------------------------------------------------------------


@router.post("/tasks", response_model=PluginResponse)
async def add_task(req: AddPluginRequest):
    """Add a new task from a git repository (type-checked after load)."""
    ctrl = _get_controller()
    ok, msg, validation = ctrl.add_plugin(
        name=req.name,
        git_url=req.git_url,
        pat=req.pat,
        branch=req.branch,
        path=req.path,
        auto_start=req.auto_start,
        auto_start_on_boot=req.auto_start_on_boot,
        autostart_order=req.autostart_order,
        parameters=req.parameters,
    )
    if ok and validation and validation.get("plugin_type") != "task":
        ctrl.remove_plugin(req.name)
        return PluginResponse(
            ok=False,
            message=f"Repository contains a {validation.get('plugin_type')}, not a task. Use /api/v1/jobs instead.",
            validation=validation,
        )
    return PluginResponse(ok=ok, message=msg, validation=validation)


@router.delete("/tasks/{name}", response_model=PluginResponse)
async def remove_task(name: str):
    """Remove a task by its unique name, stopping it if running."""
    ctrl = _get_controller()
    info = ctrl.get_plugin(name)
    if info and info.get("plugin_type") != "task":
        raise HTTPException(status_code=400, detail=f"'{name}' is not a task")
    ok, msg = ctrl.remove_plugin(name)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return PluginResponse(ok=True, message=msg)


@router.get("/tasks", response_model=PluginListResponse)
async def list_tasks():
    """List all loaded tasks."""
    ctrl = _get_controller()
    plugins = ctrl.list_plugins(plugin_type="task")
    return PluginListResponse(plugins=plugins, count=len(plugins))


@router.get("/tasks/{name}")
async def get_task(name: str):
    """Get details of a specific task."""
    ctrl = _get_controller()
    info = ctrl.get_plugin(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")
    return info


@router.get("/tasks/{name}/startup", response_model=TaskStartupInfoResponse)
async def get_task_startup(name: str):
    """Get startup parameters and PV definitions for a loaded task."""
    ctrl = _get_controller()
    startup = ctrl.get_task_startup_info(name)
    if not startup:
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")
    return startup


# ------------------------------------------------------------------
# Jobs (type-scoped convenience aliases)
# ------------------------------------------------------------------


@router.post("/jobs", response_model=PluginResponse)
async def add_job(req: AddPluginRequest):
    """Add a new job from a git repository (type-checked after load)."""
    ctrl = _get_controller()
    ok, msg, validation = ctrl.add_plugin(
        name=req.name,
        git_url=req.git_url,
        pat=req.pat,
        branch=req.branch,
        path=req.path,
        auto_start=False,  # Jobs don't auto-start
        auto_start_on_boot=False,
        autostart_order=None,
        parameters=req.parameters,
    )
    if ok and validation and validation.get("plugin_type") != "job":
        ctrl.remove_plugin(req.name)
        return PluginResponse(
            ok=False,
            message=f"Repository contains a {validation.get('plugin_type')}, not a job. Use /api/v1/tasks instead.",
            validation=validation,
        )
    return PluginResponse(ok=ok, message=msg, validation=validation)


@router.delete("/jobs/{name}", response_model=PluginResponse)
async def remove_job(name: str):
    """Remove a job by its unique name."""
    ctrl = _get_controller()
    info = ctrl.get_plugin(name)
    if info and info.get("plugin_type") != "job":
        raise HTTPException(status_code=400, detail=f"'{name}' is not a job")
    ok, msg = ctrl.remove_plugin(name)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return PluginResponse(ok=True, message=msg)


@router.post("/jobs/{name}/run", response_model=JobRunResponse)
async def run_job(name: str):
    """Execute a loaded job and return its result."""
    ctrl = _get_controller()
    ok, result = ctrl.run_job(name)
    if "error" in (result or {}):
        raise HTTPException(status_code=404, detail=result["error"])
    return JobRunResponse(ok=ok, result=result)


@router.get("/jobs", response_model=PluginListResponse)
async def list_jobs():
    """List all loaded jobs."""
    ctrl = _get_controller()
    plugins = ctrl.list_plugins(plugin_type="job")
    return PluginListResponse(plugins=plugins, count=len(plugins))


@router.get("/jobs/{name}")
async def get_job(name: str):
    """Get details of a specific job."""
    ctrl = _get_controller()
    info = ctrl.get_plugin(name)
    if not info:
        raise HTTPException(status_code=404, detail=f"Job '{name}' not found")
    return info


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    import iocmng

    ctrl = _get_controller()
    tasks = ctrl.list_plugins(plugin_type="task")
    jobs = ctrl.list_plugins(plugin_type="job")
    return HealthResponse(
        status="ok",
        version=iocmng.__version__,
        tasks_count=len(tasks),
        jobs_count=len(jobs),
    )


# ------------------------------------------------------------------
# Devices
# ------------------------------------------------------------------


@router.get("/devices")
async def list_devices():
    """List all devices available in the beamline config (indexed, not yet created)."""
    ctrl = _get_controller()
    available = ctrl.list_available_devices()
    created = list(ctrl.ophyd_devices.keys())
    return {
        "available": available,
        "available_count": len(available),
        "created": created,
        "created_count": len(created),
    }
