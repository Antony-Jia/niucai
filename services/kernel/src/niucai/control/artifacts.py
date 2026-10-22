"""Immutable, content-addressed transport snapshots. Paths never come from a node."""

import re
from hashlib import sha256

from niucai.storage.db import Artifact, uid


def snapshot(session, settings, task_id, content, media_type="application/octet-stream"):
    if len(media_type) > 100 or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", media_type):
        raise ValueError("invalid artifact media type")
    digest = sha256(content).hexdigest()
    root = settings.workspace.resolve()
    path = root / ".kernel-artifacts" / digest
    if not path.resolve().is_relative_to(root):
        raise ValueError("artifact storage must remain inside workspace")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temp = path.parent / uid()
        temp.write_bytes(content)
        temp.replace(path)
    artifact = Artifact(
        task_id=task_id,
        path=str(path.relative_to(root)),
        media_type=media_type,
        content_hash=digest,
        version=1,
    )
    session.add(artifact)
    session.flush()
    return artifact


def reference(artifact):
    return {
        "id": artifact.id,
        "version": artifact.version,
        "content_hash": artifact.content_hash,
        "media_type": artifact.media_type,
    }
