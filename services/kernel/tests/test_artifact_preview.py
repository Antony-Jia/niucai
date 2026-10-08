import base64

from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.domain.schemas import TaskCreate
from niucai.storage.db import Artifact


def test_artifact_preview_access_and_bounds(setup):
    db, settings, manager, _ = setup
    task = manager.create(TaskCreate(title="Preview", goal="Inspect image"))
    settings.workspace.mkdir(parents=True)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    files = [
        ("ok.png", "image/png", png, 200),
        ("fake.png", "image/png", b"<html>fake</html>", 415),
        ("unsafe.svg", "image/svg+xml", b"<svg/>", 415),
        ("large.png", "image/png", png + b"x" * (2 * 1024 * 1024), 413),
        ("../outside.png", "image/png", png, 404),
        ("missing.png", "image/png", None, 404),
    ]
    ids = []
    with db.sessions.begin() as session:
        for path, media_type, content, expected in files:
            if content is not None:
                (settings.workspace / path).write_bytes(content)
            artifact = Artifact(task_id=task.id, path=path, media_type=media_type)
            session.add(artifact)
            session.flush()
            ids.append((artifact.id, expected))
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    with TestClient(create_app(settings, db)) as client:
        for aid, expected in ids:
            url = f"/api/artifacts/{aid}/preview"
            assert client.get(url).status_code == 401
            response = client.get(url, headers=auth)
            assert response.status_code == expected
            if expected == 200:
                assert base64.b64decode(response.json()["image"].split(",")[1]) == png
                assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/artifacts/missing/preview", headers=auth).status_code == 404
