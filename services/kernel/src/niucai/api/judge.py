"""裁判（Bot 协作 C3）：裁决列表、纠正、decision 记录与“原则”标记。"""

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from niucai.control import judge
from niucai.storage.db import Memory


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    correction: str = Field(min_length=1, max_length=8000)


class DecisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principle: bool | None = None
    scope: Literal["global", "goal", "bot"] | None = None


def judge_router(db, settings, auth):
    router = APIRouter()

    @router.get("/api/judge/runs", dependencies=auth)
    def runs(
        goal_id: str | None = None,
        verdict: Literal["decided", "returned", "held"] | None = None,
        limit: int = Query(100, ge=1, le=200),
    ):
        with db.sessions() as s:
            return {**judge.list_runs(s, goal_id, verdict, limit), "enabled": settings.judge_enabled}

    @router.post("/api/judge/runs/{run_id}/correct", dependencies=auth, status_code=201)
    def correct(run_id: str, request: Correction):
        with db.sessions.begin() as s:
            return judge.decision_view(judge.correct_run(s, run_id, request.correction))

    @router.get("/api/judge/decisions", dependencies=auth)
    def decisions(goal_id: str | None = None, limit: int = Query(100, ge=1, le=200)):
        with db.sessions() as s:
            rows = s.scalars(
                select(Memory)
                .where(Memory.kind == "decision")
                .order_by(Memory.created_at.desc())
                .limit(limit if not goal_id else 500)
            )
            views = [judge.decision_view(m) for m in rows]
            if goal_id:  # 某个 Goal 的视图：全局记录 + 该 Goal 的记录
                views = [v for v in views if v["scope"] == "global" or v["goal_id"] == goal_id][:limit]
            return {"decisions": views}

    @router.patch("/api/judge/decisions/{memory_id}", dependencies=auth)
    def update_decision(memory_id: str, request: DecisionUpdate):
        with db.sessions.begin() as s:
            changes = request.model_dump(exclude_unset=True)
            return judge.decision_view(judge.update_decision(s, memory_id, **changes))

    return router
