"""节点配对与拓扑：Desktop 生成一次性配对码，Runner 兑换后获得独立节点令牌。"""

import secrets
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select

from niucai.control.remote import RemoteManager, scheduling_lock
from niucai.control.tasks import Conflict
from niucai.storage.db import PairingCode, RemoteNode, emit, now

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I/O/0/1，便于口述与手抄
CODE_TTL = timedelta(minutes=10)


def normalize(code):
    return "".join(ch for ch in code.upper() if ch.isalnum())


def code_hash(code):
    return sha256(normalize(code).encode()).hexdigest()


class PairingManager:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.remote = RemoteManager(db, settings)

    def create(self, name, resource_type="unspecified"):
        self.remote.enabled()
        raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
        expires_at = now() + CODE_TTL
        with self.db.sessions.begin() as s:
            s.add(
                PairingCode(
                    code_hash=code_hash(raw),
                    name=name,
                    resource_type=resource_type,
                    expires_at=expires_at,
                )
            )
            emit(s, "remote.pairing.created", name=name)
        return {"code": f"{raw[:4]}-{raw[4:]}", "expires_at": expires_at, "name": name}

    def redeem(self, code, platform="", runner_version=""):
        """一次性兑换；失败统一返回同一错误，不泄露码是否存在。"""
        self.remote.enabled()
        token = secrets.token_urlsafe(32)
        with self.db.sessions.begin() as s:
            scheduling_lock(s)
            pairing = s.scalar(
                select(PairingCode).where(PairingCode.code_hash == code_hash(code)).with_for_update()
            )
            if pairing is None or pairing.used_at is not None or pairing.expires_at <= now():
                raise Conflict("配对码无效或已过期")
            node = RemoteNode(
                name=pairing.name,
                resource_type=pairing.resource_type,
                token_hash=sha256(token.encode()).hexdigest(),
                connection={},
                platform=platform,
                runner_version=runner_version,
                paired_at=now(),
            )
            s.add(node)
            s.flush()
            pairing.used_at, pairing.node_id = now(), node.id
            emit(s, "remote.node.paired", node_id=node.id)
            from niucai.control.instance import PROTOCOL_VERSION, instance

            return {
                "node_id": node.id,
                "token": token,
                "name": node.name,
                "kernel_instance_id": instance(s).instance_id,
                "protocol_version": PROTOCOL_VERSION,
            }

    def topology(self):
        from niucai.control.instance import instance

        with self.db.sessions() as s:
            ident = instance(s).instance_id
        nodes = self.remote.nodes()
        return {
            "kernel": {"instance_id": ident, "remote_enabled": bool(self.settings.remote_pi_enabled)},
            "nodes": [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "resource_type": n["resource_type"],
                    "platform": n["platform"],
                    "runner_version": n["runner_version"],
                    "status": n["status"] if n["enabled"] else "DISABLED",
                    "enabled": bool(n["enabled"]),
                    "slots": n["capacity"],
                    "busy": 1 if n.get("active_job") else 0,
                    "engines": n["engines"],
                    "preview_url": n["preview_url"],
                    "paired_at": n["paired_at"],
                    "heartbeat_at": n["heartbeat_at"],
                    # P2 起 Bot 挂在节点下；当前阶段始终为空列表
                    "bots": [],
                }
                for n in nodes
            ],
        }
