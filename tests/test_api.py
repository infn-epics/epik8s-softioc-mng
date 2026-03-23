"""Tests for the REST API endpoints."""

import pytest
from fastapi.testclient import TestClient

from iocmng.api.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(plugins_dir=str(tmp_path / "plugins"))
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestTaskEndpoints:
    def test_list_tasks_empty(self, client):
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["plugins"] == []

    def test_add_task_invalid_name(self, client):
        resp = client.post(
            "/api/v1/tasks",
            json={"name": "invalid name!", "git_url": "https://example.com/repo.git"},
        )
        assert resp.status_code == 422  # Pydantic validation error

    def test_add_task_with_path(self, client):
        resp = client.post(
            "/api/v1/tasks",
            json={
                "name": "test_path_task",
                "git_url": "https://example.com/repo.git",
                "path": "src/my_task",
            },
        )
        # Clone will fail since the URL is fake, but we validate the request is accepted
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False  # git clone fails

    def test_remove_nonexistent_task(self, client):
        resp = client.delete("/api/v1/tasks/nonexistent")
        assert resp.status_code == 404


class TestJobEndpoints:
    def test_list_jobs_empty(self, client):
        resp = client.get("/api/v1/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    def test_remove_nonexistent_job(self, client):
        resp = client.delete("/api/v1/jobs/nonexistent")
        assert resp.status_code == 404

    def test_run_nonexistent_job(self, client):
        resp = client.post("/api/v1/jobs/nonexistent/run")
        assert resp.status_code == 404
