"""LangGraph state in the Kernel database, fenced by the current worker lease.

Store complete checkpoints and pending writes, including nested namespaces. Retain
parent history (needed by DeltaChannel); no pickle or process-local persistence.
"""

import asyncio

from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple
from sqlalchemy import select

from niucai.actions.gateway import ActionGateway
from niucai.control.tasks import require
from niucai.storage.db import HarnessCheckpoint, HarnessWrite, Task


class DatabaseSaver(BaseCheckpointSaver):
    def __init__(self, db, task_id, token):
        super().__init__()
        self.db, self.task_id, self.token = db, task_id, token

    def fence(self, session):
        ActionGateway.check_task(require(session, Task, self.task_id, lock=True), self.token)

    @staticmethod
    def key(config):
        c = config["configurable"]
        return c["thread_id"], c.get("checkpoint_ns", ""), c.get("checkpoint_id")

    @staticmethod
    def config(thread, namespace, ident):
        return {"configurable": {"thread_id": thread, "checkpoint_ns": namespace, "checkpoint_id": ident}}

    def tuple(self, session, row):
        writes = session.scalars(
            select(HarnessWrite)
            .where(
                HarnessWrite.thread_id == row.thread_id,
                HarnessWrite.namespace == row.namespace,
                HarnessWrite.checkpoint_id == row.checkpoint_id,
            )
            .order_by(HarnessWrite.task_id, HarnessWrite.idx)
        ).all()
        return CheckpointTuple(
            self.config(row.thread_id, row.namespace, row.checkpoint_id),
            self.serde.loads_typed((row.payload_type, row.payload)),
            self.serde.loads_typed((row.metadata_type, row.metadata_payload)),
            self.config(row.thread_id, row.namespace, row.parent_id) if row.parent_id else None,
            [(w.task_id, w.channel, self.serde.loads_typed((w.payload_type, w.payload))) for w in writes],
        )

    def get_tuple(self, config):
        thread, ns, ident = self.key(config)
        with self.db.sessions() as s:
            query = select(HarnessCheckpoint).where(
                HarnessCheckpoint.thread_id == thread, HarnessCheckpoint.namespace == ns
            )
            if ident:
                query = query.where(HarnessCheckpoint.checkpoint_id == ident)
            row = s.scalar(query.order_by(HarnessCheckpoint.checkpoint_id.desc()).limit(1))
            return self.tuple(s, row) if row else None

    def put(self, config, checkpoint, metadata, new_versions):
        thread, ns, parent = self.key(config)
        kind, payload = self.serde.dumps_typed(checkpoint)
        mkind, mpayload = self.serde.dumps_typed(metadata)
        with self.db.sessions.begin() as s:
            self.fence(s)
            s.merge(
                HarnessCheckpoint(
                    thread_id=thread,
                    namespace=ns,
                    checkpoint_id=checkpoint["id"],
                    parent_id=parent,
                    payload_type=kind,
                    payload=payload,
                    metadata_type=mkind,
                    metadata_payload=mpayload,
                )
            )
        return self.config(thread, ns, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        thread, ns, ident = self.key(config)
        with self.db.sessions.begin() as s:
            self.fence(s)
            for i, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, i)
                existing = s.get(HarnessWrite, (thread, ns, ident, task_id, idx))
                if existing and idx >= 0:
                    continue
                kind, payload = self.serde.dumps_typed(value)
                s.merge(
                    HarnessWrite(
                        thread_id=thread,
                        namespace=ns,
                        checkpoint_id=ident,
                        task_id=task_id,
                        idx=idx,
                        channel=channel,
                        payload_type=kind,
                        payload=payload,
                    )
                )

    def list(self, config, *, filter=None, before=None, limit=None):
        with self.db.sessions() as s:
            query = select(HarnessCheckpoint)
            if config:
                thread, ns, _ = self.key(config)
                query = query.where(HarnessCheckpoint.thread_id == thread, HarnessCheckpoint.namespace == ns)
            if before:
                query = query.where(HarnessCheckpoint.checkpoint_id < self.key(before)[2])
            rows = s.scalars(query.order_by(HarnessCheckpoint.checkpoint_id.desc())).all()
            result = [self.tuple(s, row) for row in rows]
        matches = [r for r in result if not filter or all(r.metadata.get(k) == v for k, v in filter.items())]
        yield from matches if limit is None else matches[:limit]

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        rows = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for row in rows:
            yield row
