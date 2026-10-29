from fastapi import APIRouter

from niucai.control.kanban import kanban


def kanban_router(db, settings, auth):
    router = APIRouter()

    @router.get("/api/kanban", dependencies=auth)
    def board():
        return kanban(db, settings)

    return router
