"""Kernel 托管技能库：所有 Bot 可读的说明包，按 Bot 定义的 skills 白名单收窄。"""

from sqlalchemy import select

from niucai.control.bots import allows
from niucai.control.tasks import Missing
from niucai.storage.db import Skill

CATALOG_LIMIT = 50


def visible(identity, name):
    return identity.skills is None or allows(identity.skills, name)


def catalog(session, identity):
    rows = session.scalars(select(Skill).where(Skill.enabled == 1).order_by(Skill.name))
    return [
        {"name": row.name, "description": row.description[:300], "version": row.version}
        for row in rows
        if visible(identity, row.name)
    ][:CATALOG_LIMIT]


def find(session, identity, name):
    row = session.scalar(select(Skill).where(Skill.name == name, Skill.enabled == 1))
    if row is None:
        raise Missing(f"skill {name} not found")
    if not visible(identity, name):
        raise PermissionError(f"Bot configuration denies skill {name}")
    return row


def read(session, settings, identity, name):
    row = find(session, identity, name)
    limit = settings.tool_result_chars
    return {
        "name": row.name,
        "version": row.version,
        "content": row.content[:limit],
        "truncated": len(row.content) > limit,
    }
