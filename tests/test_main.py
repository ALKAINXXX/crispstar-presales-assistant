# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: adapted UI and route assertions for the presales entry and personal-route exclusion.
# Source and license: see README.md.

from fastapi.testclient import TestClient

from app.main import app


def test_root_serves_saas_ui() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "澄星售前助手" in response.text
    assert 'lang="zh-CN"' in response.text
    assert 'id="root"' in response.text
    assert 'type="module"' in response.text and '/assets/' in response.text
    assert 'Personal Assistant' not in response.text
    assert "Google Calendar" not in response.text
    assert "Local Time" not in response.text
    assert "Reminders" not in response.text


def test_favicon_is_empty_success() -> None:
    client = TestClient(app)

    response = client.get("/favicon.ico")

    assert response.status_code == 204


def test_personal_routes_are_not_exposed() -> None:
    paths = {route.path for route in app.routes}

    assert "/time" not in paths
    assert "/google-calendar/status" not in paths
    assert "/google-calendar/login" not in paths
    assert "/google-calendar/callback" not in paths
    assert "/reminders" not in paths
    assert "/reminders/due" not in paths
    assert "/reminders/{reminder_id}/fired" not in paths
    assert "/reminders/{reminder_id}" not in paths


def test_business_routes_remain_exposed() -> None:
    paths = {route.path for route in app.routes}

    assert "/" in paths
    assert "/health" in paths
    assert "/chat" in paths
    assert "/sessions" in paths
    assert "/sessions/{session_id}" in paths
