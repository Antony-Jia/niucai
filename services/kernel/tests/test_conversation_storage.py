from hashlib import sha256

import pytest

pytest.importorskip("fcntl")

from test_agent_sessions import Computer, Model, state

from niucai.agents.pi_adapter import PiDurableRuntime
from niucai.control.conversations import ConversationManager
from niucai.worker import Worker


async def test_missing_shared_session_volume_blocks_next_round_without_model_call(setup):
    db, settings, _, _ = setup
    inbox = ConversationManager(db)
    conversation, _, first = inbox.send(None, "first")
    model = Model({"content": "saved"})
    worker = Worker(db, settings, PiDurableRuntime(model), Computer())
    await worker.run_once()
    assert state(db, first.id).status == "COMPLETED"
    storage = settings.pi_storage / sha256(conversation.id.encode()).hexdigest()
    (storage / "main.jsonl").unlink()
    _, _, second = inbox.send(conversation.id, "next")
    await worker.run_once()
    assert state(db, second.id).status == "FAILED"
    assert "Pi session storage missing" in state(db, second.id).checkpoint["detail"]
    assert len(model.messages) == 1
