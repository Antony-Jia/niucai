from hashlib import sha256

from fastapi.encoders import jsonable_encoder

from niucai.control.tasks import Conflict
from niucai.storage.db import ToolReceipt


def receipt(session, task, method, params):
    if not params.get("toolTaskId"):
        return None, None
    key = f"{task.id}:{method}:{sha256(str(params['toolTaskId']).encode()).hexdigest()}"
    existing = session.get(ToolReceipt, key)
    if existing and existing.payload != params:
        raise Conflict("tool replay payload changed")
    return key, existing.result if existing else None


def save_receipt(session, key, task, params, result):
    if key:
        session.add(ToolReceipt(id=key, task_id=task.id, payload=params, result=jsonable_encoder(result)))
