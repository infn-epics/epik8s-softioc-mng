"""Tests for the REST API endpoints.

Covers every route with both happy-path and error cases.
Uses a local bare git repo instead of a real remote — no network needed.
"""

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from iocmng.api.app import create_app


# ---------------------------------------------------------------------------
# Helpers — build a tiny local git repo that the loader can clone
# ---------------------------------------------------------------------------

TASK_CODE = textwrap.dedent("""\
    from iocmng import TaskBase

    class SimpleTask(TaskBase):
        def initialize(self):
            pass
        def execute(self):
            pass
        def cleanup(self):
            pass
""")

JOB_CODE = textwrap.dedent("""\
    from iocmng import JobBase
    from iocmng.base.job import JobResult

    class SimpleJob(JobBase):
        def initialize(self):
            pass
        def execute(self):
            return JobResult(success=True, data={}, message="ok")
""")

TASK_CONFIG = {
    "parameters": {"mode": "continuous", "interval": 0.1, "threshold": 50.0},
    "prefix": "MY_TASK",
    "pvs": {"outputs": {"VALUE": {"type": "float", "value": 0.0}}},
}

JOB_CONFIG = {
    "prefix": "MY_JOB",
    "parameters": {"mode": "job"},
}


def _make_git_repo(tmp_path: Path, subdir: str, python_code: str, config: dict) -> str:
    """Create a bare git repo containing a plugin at *subdir* and return its file URL."""
    # Working tree
    work = tmp_path / "work"
    work.mkdir()
    src = work / subdir
    src.mkdir(parents=True)
    (src / "plugin.py").write_text(python_code)
    (src / "config.yaml").write_text(yaml.dump(config))

    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=work,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work,
                   check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True,
                   capture_output=True)

    # Bare repo the loader will clone from
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)],
                   check=True, capture_output=True)
    return f"file://{bare}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task_repo(tmp_path):
    return _make_git_repo(tmp_path, "task", TASK_CODE, TASK_CONFIG)


@pytest.fixture
def job_repo(tmp_path):
    return _make_git_repo(tmp_path, "job", JOB_CODE, JOB_CONFIG)


@pytest.fixture
def client(tmp_path):
    app = create_app(plugins_dir=str(tmp_path / "plugins"))
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert "version" in d
        assert "tasks_count" in d
        assert "jobs_count" in d


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class TestDevices:
    def test_devices_empty(self, client):
        r = client.get("/api/v1/devices")
        assert r.status_code == 200
        d = r.json()
        assert d["available_count"] == 0
        assert d["created_count"] == 0

    def test_ioc_defaults_merging(self, tmp_path):
        """Devices inherit devgroup/devtype from iocDefaults[template]."""
        beamline = {
            "iocDefaults": {
                "motor": {
                    "devgroup": "mot",
                    "devtype": "technosoft-asyn",
                    "template": "motor",
                    "opi": "tml/TML_Main.bob",
                },
                "hazemeyer": {
                    "devgroup": "mag",
                    "devtype": "haz-ser",
                },
            },
            "epicsConfiguration": {
                "iocs": [
                    {
                        "name": "tml-ch1",
                        "template": "motor",
                        "iocprefix": "SPARC:MOT:TML",
                        "devices": [
                            {"name": "GUNFLG01", "axid": 1},
                            {"name": "AC1FLG01", "axid": 2},
                        ],
                    },
                    {
                        "name": "haz-ch1",
                        "template": "hazemeyer",
                        "iocprefix": "SPARC:MAG:HZ",
                        "devices": [
                            {"name": "SOL01", "id": "1"},
                        ],
                    },
                    {
                        "name": "vitara",
                        "template": "motor",
                        "devtype": "newport",
                        "devgroup": "rf",
                        "iocprefix": "SPARC:MOT",
                        "iocroot": "VITARA01",
                        "devices": [
                            {"name": "m0", "axid": 0},
                        ],
                    },
                ],
            },
        }
        from iocmng.core.controller import IocMngController
        ctrl = IocMngController(beamline_config=beamline, disable_ophyd=False,
                                plugins_dir=tmp_path / "plugins")
        idx = ctrl._device_index

        # Motor IOC inherits devgroup=mot, devtype=technosoft-asyn from defaults
        assert "GUNFLG01" in idx
        assert idx["GUNFLG01"]["devgroup"] == "mot"
        assert idx["GUNFLG01"]["devtype"] == "technosoft-asyn"
        assert idx["AC1FLG01"]["devgroup"] == "mot"

        # Hazemeyer IOC inherits from defaults
        assert "SOL01" in idx
        assert idx["SOL01"]["devgroup"] == "mag"
        assert idx["SOL01"]["devtype"] == "haz-ser"

        # Vitara overrides devtype/devgroup from IOC-specific config
        assert "m0" in idx
        assert idx["m0"]["devgroup"] == "rf"
        assert idx["m0"]["devtype"] == "newport"
        assert idx["m0"]["prefix"] == "SPARC:MOT:VITARA01:m0"

        # Merged config for motor IOC should have opi from defaults
        assert idx["GUNFLG01"]["config"].get("opi") == "tml/TML_Main.bob"


# ---------------------------------------------------------------------------
# Unified /api/v1/plugins
# ---------------------------------------------------------------------------

class TestPluginsEndpoint:
    def test_list_empty(self, client):
        r = client.get("/api/v1/plugins")
        assert r.status_code == 200
        assert r.json() == {"plugins": [], "count": 0}

    def test_list_filter_type(self, client):
        r = client.get("/api/v1/plugins?type=task")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_add_task_plugin(self, client, task_repo, tmp_path):
        r = client.post("/api/v1/plugins", json={
            "name": "my-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True, d["message"]
        assert "my-task" in d["message"]

    def test_add_task_stages_only_requested_path(self, tmp_path, task_repo):
        app = create_app(plugins_dir=str(tmp_path / "plugins"))
        client = TestClient(app)
        r = client.post("/api/v1/plugins", json={
            "name": "staged-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        assert r.status_code == 200
        plugin_root = tmp_path / "plugins" / "staged-task"
        assert (plugin_root / "plugin.py").exists()
        assert (plugin_root / "config.yaml").exists()
        assert not (plugin_root / "task").exists()

    def test_add_task_then_list(self, client, task_repo):
        client.post("/api/v1/plugins", json={
            "name": "listed-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.get("/api/v1/plugins")
        assert r.json()["count"] == 1
        assert r.json()["plugins"][0]["name"] == "listed-task"

    def test_add_task_then_filter(self, client, task_repo):
        client.post("/api/v1/plugins", json={
            "name": "filter-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        assert client.get("/api/v1/plugins?type=task").json()["count"] == 1
        assert client.get("/api/v1/plugins?type=job").json()["count"] == 0

    def test_get_plugin(self, client, task_repo):
        client.post("/api/v1/plugins", json={
            "name": "get-me",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.get("/api/v1/plugins/get-me")
        assert r.status_code == 200
        assert r.json()["name"] == "get-me"

    def test_get_nonexistent_plugin(self, client):
        assert client.get("/api/v1/plugins/ghost").status_code == 404

    def test_list_plugins_includes_discovered_on_disk_plugin(self, tmp_path, task_repo):
        app = create_app(plugins_dir=str(tmp_path / "plugins"))
        client = TestClient(app)

        from iocmng.core.loader import PluginLoader

        loader = PluginLoader(tmp_path / "plugins")
        ok, _ = loader.clone("disk-task", task_repo, branch="main", path="task")
        assert ok

        r = client.get("/api/v1/plugins")
        assert r.status_code == 200
        plugins = r.json()["plugins"]
        plugin = next((item for item in plugins if item["name"] == "disk-task"), None)
        assert plugin is not None
        assert plugin["status"] == "available"
        assert plugin["plugin_type"] == "task"
        assert plugin["start_parameters"]["mode"] == "continuous"

    def test_get_discovered_plugin(self, tmp_path, task_repo):
        app = create_app(plugins_dir=str(tmp_path / "plugins"))
        client = TestClient(app)

        from iocmng.core.loader import PluginLoader

        loader = PluginLoader(tmp_path / "plugins")
        ok, _ = loader.clone("disk-task", task_repo, branch="main", path="task")
        assert ok

        r = client.get("/api/v1/plugins/disk-task")
        assert r.status_code == 200
        assert r.json()["status"] == "available"

    def test_add_duplicate_rejected(self, client, task_repo):
        body = {"name": "dup", "git_url": task_repo, "branch": "main", "path": "task"}
        client.post("/api/v1/plugins", json=body)
        r2 = client.post("/api/v1/plugins", json=body)
        assert r2.status_code == 200
        assert r2.json()["ok"] is False
        assert "already exists" in r2.json()["message"]

    def test_delete_plugin(self, client, task_repo):
        client.post("/api/v1/plugins", json={
            "name": "del-me",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.delete("/api/v1/plugins/del-me")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert client.get("/api/v1/plugins").json()["count"] == 0

    def test_delete_nonexistent(self, client):
        assert client.delete("/api/v1/plugins/ghost").status_code == 404

    def test_restart_plugin(self, client, task_repo):
        client.post("/api/v1/plugins", json={
            "name": "restart-me",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.post("/api/v1/plugins/restart-me/restart")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_restart_nonexistent(self, client):
        assert client.post("/api/v1/plugins/ghost/restart").status_code == 404

    def test_invalid_name_rejected(self, client, task_repo):
        r = client.post("/api/v1/plugins", json={
            "name": "bad name!",
            "git_url": task_repo,
        })
        assert r.status_code == 422

    def test_parameters_override(self, client, task_repo):
        r = client.post("/api/v1/plugins", json={
            "name": "param-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
            "parameters": {"threshold": 99.0},
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_add_job_plugin(self, client, job_repo):
        r = client.post("/api/v1/plugins", json={
            "name": "my-job",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_run_job_via_unified(self, client, job_repo):
        client.post("/api/v1/plugins", json={
            "name": "runnable-job",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        r = client.post("/api/v1/plugins/runnable-job/run")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_run_task_as_job_rejected(self, client, task_repo):
        client.post("/api/v1/plugins", json={
            "name": "not-a-job",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.post("/api/v1/plugins/not-a-job/run")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# /api/v1/tasks (type-scoped alias)
# ---------------------------------------------------------------------------

class TestTasksAlias:
    def test_list_tasks_empty(self, client):
        r = client.get("/api/v1/tasks")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_get_task_startup_info(self, client, task_repo):
        r_add = client.post("/api/v1/tasks", json={
            "name": "startup-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
            "parameters": {"threshold": 77.0},
            "auto_start": True,
            "auto_start_on_boot": True,
            "autostart_order": 5,
        })
        assert r_add.status_code == 200
        assert r_add.json()["ok"] is True

        r = client.get("/api/v1/tasks/startup-task/startup")
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "startup-task"
        assert d["plugin_type"] == "task"
        assert d["auto_start"] is True
        assert d["auto_start_on_boot"] is True
        assert d["autostart_order"] == 5
        assert d["pv_prefix"] == "BEAMLINE:DEFAULT:MY_TASK"
        assert d["plugin_prefix"] == "MY_TASK"
        assert d["mode"] == "continuous"
        assert d["start_parameters"]["threshold"] == 77.0
        assert "ENABLE" in d["base_control_pvs"]
        assert "STATUS" in d["base_control_pvs"]
        assert "VALUE" in d["additional_output_pvs"]
        assert "VALUE" in d["built_pvs"]
        assert "outputs" in d["pv_definitions"]

    def test_get_task_startup_info_missing(self, client):
        r = client.get("/api/v1/tasks/missing/startup")
        assert r.status_code == 404

    def test_add_and_list(self, client, task_repo):
        client.post("/api/v1/tasks", json={
            "name": "t1",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.get("/api/v1/tasks")
        assert r.json()["count"] == 1
        plugin = r.json()["plugins"][0]
        assert "start_parameters" in plugin
        assert "pv_definitions" in plugin
        assert "built_pvs" in plugin
        assert "base_control_pvs" in plugin

    def test_add_job_via_tasks_rejected(self, client, job_repo):
        r = client.post("/api/v1/tasks", json={
            "name": "wrong-type",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "not a task" in r.json()["message"].lower() or "job" in r.json()["message"].lower()

    def test_get_task(self, client, task_repo):
        client.post("/api/v1/tasks", json={
            "name": "get-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.get("/api/v1/tasks/get-task")
        assert r.status_code == 200

    def test_delete_task(self, client, task_repo):
        client.post("/api/v1/tasks", json={
            "name": "del-task",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        r = client.delete("/api/v1/tasks/del-task")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_delete_nonexistent_task(self, client):
        assert client.delete("/api/v1/tasks/ghost").status_code == 404


# ---------------------------------------------------------------------------
# /api/v1/jobs (type-scoped alias)
# ---------------------------------------------------------------------------

class TestJobsAlias:
    def test_list_jobs_empty(self, client):
        assert client.get("/api/v1/jobs").json()["count"] == 0

    def test_add_and_list(self, client, job_repo):
        client.post("/api/v1/jobs", json={
            "name": "j1",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        assert client.get("/api/v1/jobs").json()["count"] == 1

    def test_add_task_via_jobs_rejected(self, client, task_repo):
        r = client.post("/api/v1/jobs", json={
            "name": "wrong-type",
            "git_url": task_repo,
            "branch": "main",
            "path": "task",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_run_job(self, client, job_repo):
        client.post("/api/v1/jobs", json={
            "name": "run-j",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        r = client.post("/api/v1/jobs/run-j/run")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_run_nonexistent_job(self, client):
        assert client.post("/api/v1/jobs/ghost/run").status_code == 404

    def test_delete_job(self, client, job_repo):
        client.post("/api/v1/jobs", json={
            "name": "del-job",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        r = client.delete("/api/v1/jobs/del-job")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_delete_nonexistent_job(self, client):
        assert client.delete("/api/v1/jobs/ghost").status_code == 404

    def test_get_job(self, client, job_repo):
        client.post("/api/v1/jobs", json={
            "name": "get-job",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        assert client.get("/api/v1/jobs/get-job").status_code == 200

    def test_get_nonexistent_job(self, client):
        assert client.get("/api/v1/jobs/ghost").status_code == 404

    def test_get_job_startup_info(self, client, job_repo):
        r_add = client.post("/api/v1/jobs", json={
            "name": "startup-job",
            "git_url": job_repo,
            "branch": "main",
            "path": "job",
        })
        assert r_add.status_code == 200
        assert r_add.json()["ok"] is True

        r = client.get("/api/v1/jobs/startup-job/startup")
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "startup-job"
        assert d["plugin_type"] == "job"
        assert d["pv_prefix"] == "BEAMLINE:DEFAULT:MY_JOB"
        assert d["plugin_prefix"] == "MY_JOB"
        assert d["mode"] is None
        assert "STATUS" in d["base_control_pvs"]
        assert "MESSAGE" in d["base_control_pvs"]

    def test_get_job_startup_info_missing(self, client):
        r = client.get("/api/v1/jobs/missing/startup")
        assert r.status_code == 404

