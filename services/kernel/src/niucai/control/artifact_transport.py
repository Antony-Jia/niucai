"""Chunked binary transport fenced by a live job attempt. No client paths enter storage."""

import re
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select

from niucai.control.artifacts import reference
from niucai.control.remote import RemoteManager, scheduling_lock
from niucai.control.tasks import Conflict, require
from niucai.storage.db import Artifact, ArtifactUpload, RemoteJob, RemoteNode, WorkItem, now

CHUNK_BYTES = 1024 * 1024


def digest_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


class ArtifactTransport:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.remote = RemoteManager(db, settings)

    def root(self):
        path = self.settings.workspace.resolve() / ".kernel-artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def live(self, session, node_id, job_id, attempt_id):
        job = self.remote.owned(session, node_id, job_id, attempt_id)
        if (
            job.status != "RUNNING"
            or not job.lease_until
            or job.lease_until < now()
            or not self.settings.remote_pi_enabled
            or not require(session, RemoteNode, node_id).enabled
            or not self.remote.authorized(session, job, node_id)
        ):
            raise Conflict("artifact transport requires a live authorized attempt")
        if not job.parent_task_id:
            raise PermissionError("artifact transport requires a workflow work item")
        item = session.scalar(select(WorkItem).where(WorkItem.task_id == job.parent_task_id))
        if not item:
            raise PermissionError("artifact transport requires a workflow work item")
        return job

    def start(self, node_id, job_id, attempt_id, filename, size, content_hash, media_type):
        if (
            not re.fullmatch(r"[a-zA-Z0-9._ -]{1,200}", filename)
            or filename in {".", ".."}
            or filename.startswith(".")
        ):
            raise ValueError("filename must be a safe basename")
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("invalid SHA256")
        if not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", media_type) or len(media_type) > 100:
            raise ValueError("invalid media type")
        if not 0 <= size <= self.settings.artifact_max_file_bytes:
            raise ValueError("artifact exceeds file limit")
        with self.db.sessions.begin() as session:
            scheduling_lock(session)
            job = self.live(session, node_id, job_id, attempt_id)
            existing = session.scalar(
                select(ArtifactUpload).where(
                    ArtifactUpload.job_id == job_id,
                    ArtifactUpload.attempt_id == attempt_id,
                    ArtifactUpload.filename == filename,
                )
            )
            if existing:
                if (existing.size, existing.content_hash, existing.media_type) != (
                    size,
                    content_hash,
                    media_type,
                ):
                    raise Conflict("upload filename reused with different content")
                return self.view(existing)
            uploads = list(
                session.scalars(
                    select(ArtifactUpload)
                    .join(RemoteJob)
                    .where(RemoteJob.parent_task_id == job.parent_task_id)
                )
            )
            finalized = {u.artifact_id for u in uploads if u.artifact_id}
            legacy_bytes = 0
            for artifact in session.scalars(select(Artifact).where(Artifact.task_id == job.parent_task_id)):
                if artifact.id in finalized:
                    continue
                path = (self.settings.workspace / artifact.path).resolve()
                if not path.is_relative_to(self.settings.workspace.resolve()) or not path.is_file():
                    raise Conflict("work item artifact storage unavailable")
                legacy_bytes += path.stat().st_size
            if sum(u.size for u in uploads) + legacy_bytes + size > self.settings.artifact_max_item_bytes:
                raise ValueError("artifact exceeds work item quota")
            if sum(u.job_id == job_id and u.attempt_id == attempt_id for u in uploads) >= 16:
                raise ValueError("at most sixteen files per attempt")
            upload = ArtifactUpload(
                job_id=job_id,
                attempt_id=attempt_id,
                filename=filename,
                size=size,
                content_hash=content_hash,
                media_type=media_type,
            )
            session.add(upload)
            session.flush()
            return self.view(upload)

    @staticmethod
    def view(upload):
        return {
            "id": upload.id,
            "received": upload.received,
            "artifact_id": upload.artifact_id,
            "chunk_bytes": CHUNK_BYTES,
        }

    def owned(self, session, node_id, job_id, attempt_id, upload_id):
        job = self.live(session, node_id, job_id, attempt_id)
        upload = require(session, ArtifactUpload, upload_id, lock=True)
        if upload.job_id != job_id or upload.attempt_id != attempt_id:
            raise PermissionError("upload is outside this attempt")
        return job, upload

    def chunk(self, node_id, job_id, attempt_id, upload_id, offset, content):
        if len(content) > CHUNK_BYTES:
            raise ValueError("chunk exceeds 1 MiB")
        with self.db.sessions.begin() as session:
            scheduling_lock(session)
            _, upload = self.owned(session, node_id, job_id, attempt_id, upload_id)
            if offset < 0 or offset + len(content) > upload.size:
                raise ValueError("chunk outside declared size")
            if upload.artifact_id:
                return self.view(upload)
            path = self.root() / (upload.id + ".part")
            if offset < upload.received:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    if offset + len(content) > upload.received or stream.read(len(content)) != content:
                        raise Conflict("replayed chunk changed")
            elif offset != upload.received:
                raise Conflict("chunk offset gap")
            else:
                with path.open("r+b" if path.exists() else "w+b") as stream:
                    # A crash after writing but before committing can safely replay this offset.
                    stream.seek(offset)
                    stream.write(content)
                    stream.truncate(offset + len(content))
                    stream.flush()
                    import os

                    os.fsync(stream.fileno())
                upload.received += len(content)
            return self.view(upload)

    def finalize(self, node_id, job_id, attempt_id, upload_id):
        with self.db.sessions.begin() as session:
            scheduling_lock(session)
            job, upload = self.owned(session, node_id, job_id, attempt_id, upload_id)
            if upload.artifact_id:
                return reference(require(session, Artifact, upload.artifact_id))
            if upload.received != upload.size:
                raise Conflict("upload incomplete")
            part = self.root() / (upload.id + ".part")
            target = self.root() / upload.content_hash
            if upload.size == 0 and not part.exists():
                part.touch()
            # A crash after rename but before DB commit recovers from the immutable target.
            source = part if part.exists() else target
            if not source.is_file() or source.stat().st_size != upload.size:
                raise Conflict("upload file missing or wrong size")
            if digest_file(source) != upload.content_hash:
                raise Conflict("artifact SHA256 mismatch")
            if source != target:
                source.replace(target)
            artifact = Artifact(
                task_id=job.parent_task_id,
                path=str(target.relative_to(self.settings.workspace.resolve())),
                media_type=upload.media_type,
                content_hash=upload.content_hash,
                version=1,
            )
            session.add(artifact)
            session.flush()
            upload.artifact_id = artifact.id
            return reference(artifact)

    def download(self, node_id, job_id, attempt_id, artifact_id):
        with self.db.sessions() as session:
            job = self.live(session, node_id, job_id, attempt_id)
            ref = next((r for r in job.input_artifacts if r["id"] == artifact_id), None)
            if not ref:
                raise PermissionError("artifact is outside approved work package")
            artifact = require(session, Artifact, artifact_id)
            path = (self.settings.workspace / artifact.path).resolve()
            if (
                not path.is_relative_to(self.settings.workspace.resolve())
                or not path.is_file()
                or path.stat().st_size > self.settings.artifact_max_file_bytes
                or digest_file(path) != ref["content_hash"]
            ):
                raise Conflict("artifact unavailable, changed or exceeds limit")
            return path, artifact.media_type

    def cleanup(self):
        with self.db.sessions.begin() as session:
            scheduling_lock(session)
            expired = list(
                session.scalars(
                    select(ArtifactUpload).where(
                        ArtifactUpload.artifact_id.is_(None),
                        ArtifactUpload.created_at
                        < now() - timedelta(seconds=self.settings.artifact_upload_ttl_seconds),
                    )
                )
            )
            for upload in expired:
                (self.root() / (upload.id + ".part")).unlink(missing_ok=True)
                session.delete(upload)
            return len(expired)
