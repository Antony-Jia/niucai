"""P6：旧的 Kernel 内执行链路可整体关闭，Goal 与白板不受影响。"""

import pytest
from fastapi.testclient import TestClient

from niucai.api.app import create_app
from niucai.control.collaboration import check_admission
from niucai.control.tasks import Conflict
from niucai.domain.schemas import TaskCreate


def test_legacy_execution_switch_blocks_new_kernel_work(setup):
    db, settings, tasks, _ = setup
    settings.legacy_execution_enabled = False
    settings.remote_pi_enabled = True
    with pytest.raises(Conflict, match="Kernel 内执行已关闭"):
        tasks.create(TaskCreate(title="t", goal="g"))
    settings.multi_bot_enabled = True
    with db.sessions() as s, pytest.raises(Conflict, match="Kernel 内执行已关闭"):
        check_admission(s, settings)
    with db.sessions() as s:
        check_admission(s, settings, new_work=False)  # 已有工作的收尾不受影响
    auth = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    api = TestClient(create_app(settings, db))
    blocked = api.post("/api/chat", json={"content": "hi"}, headers=auth)
    assert blocked.status_code == 409 and "Goal" in blocked.text
    # 白板不经过旧链路。
    created = api.post("/api/cards", json={"title": "c", "goal": "g"}, headers=auth)
    assert created.status_code == 201
    settings.legacy_execution_enabled = True
    assert tasks.create(TaskCreate(title="t", goal="g")).id
