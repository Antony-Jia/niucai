"""Persistent control-center identity and admission fencing for cold migration."""

from datetime import timedelta

from sqlalchemy import select, text

from niucai.storage.db import KernelInstance, RemoteJob, Task, TaskRun, WorkerHealth, now, uid

PROTOCOL_VERSION = 2
MIGRATION_HEAD = "b4e21024d008"


def instance(session):
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = postgresql if session.bind.dialect.name == "postgresql" else sqlite
    session.execute(
        dialect.insert(KernelInstance)
        .values(id="singleton", instance_id=uid(), admission_enabled=1, created_at=now())
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return session.get(KernelInstance, "singleton")


def worker_heartbeat(db, settings, worker_id):
    with db.sessions.begin() as session:
        row = session.get(WorkerHealth, worker_id)
        if row is None:
            row = WorkerHealth(id=worker_id)
            session.add(row)
        row.heartbeat_at = now()
        from niucai.domain.schemas import Role
        from niucai.models.gateway import ModelRouter

        try:
            profile = ModelRouter(settings.model_config_path).resolve(Role.EXECUTOR)
            configured, model = True, profile.model
        except (OSError, ValueError, KeyError, TypeError):
            configured, model = False, None
        row.details = {
            "slots": settings.worker_slots,
            "runtime": settings.runtime,
            "model_config_exists": settings.model_config_path.is_file(),
            "model_role_resolved": configured,
            "central_model": model,
            "model_credentials_configured": bool(
                settings.openai_api_key.get_secret_value() or settings.openrouter_api_key.get_secret_value()
            ),
        }
        # Admission is database-backed; draining does not kill in-flight execution.
        return bool(instance(session).admission_enabled)


def diagnostics(session, settings):
    ident = instance(session)
    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    workers = list(
        session.scalars(
            select(WorkerHealth).where(
                WorkerHealth.heartbeat_at > now() - timedelta(seconds=max(15, settings.poll_seconds * 5))
            )
        )
    )
    model_ready = any(
        w.details.get("model_role_resolved") and w.details.get("model_credentials_configured")
        for w in workers
    )
    return {
        "kernel_instance_id": ident.instance_id,
        "protocol_version": PROTOCOL_VERSION,
        "admission_enabled": bool(ident.admission_enabled),
        "api": "ready",
        "migration": {"current": revision, "head": MIGRATION_HEAD, "ready": revision == MIGRATION_HEAD},
        "workers": [{"id": w.id, "heartbeat_at": w.heartbeat_at, **w.details} for w in workers],
        "model_configured": model_ready,
        "execution_ready": bool(
            workers and model_ready and revision == MIGRATION_HEAD and ident.admission_enabled
        ),
        "features": {
            "remote_pi": settings.remote_pi_enabled,
            "multi_bot": settings.multi_bot_enabled,
            "yolo": settings.yolo_enabled,
            "mcp": settings.mcp_enabled,
            "shell": settings.allow_shell,
        },
        "host_limitation": (
            "Closing Desktop keeps services running; host sleep, shutdown or Docker stop interrupts them."
        ),
    }


def migration_state(session):
    active = list(
        session.scalars(select(Task.id).where(Task.status.not_in(["COMPLETED", "FAILED", "CANCELLED"])))
    )
    remote = list(
        session.scalars(select(RemoteJob.id).where(RemoteJob.status.in_(["RUNNING", "CANCELLING", "LOST"])))
    )
    local = list(
        session.scalars(
            select(TaskRun.id).where(
                TaskRun.process_exited_at.is_(None), TaskRun.exit_tracking_started_at.is_not(None)
            )
        )
    )
    return {
        "active_tasks": active,
        "unconfirmed_remote_jobs": remote,
        "unconfirmed_local_runs": local,
        "backup_ready": not (active or remote or local),
    }
