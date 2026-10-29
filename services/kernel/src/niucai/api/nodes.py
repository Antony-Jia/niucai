from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from niucai.control.pairing import PairingManager


def nodes_router(db, settings, auth):
    router = APIRouter()
    manager = PairingManager(db, settings)

    class PairingCreate(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
        name: str = Field(min_length=1, max_length=200)
        resource_type: Literal["local_docker", "remote_vps", "windows_local", "unspecified"] = "unspecified"

    class PairingRedeem(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
        code: str = Field(min_length=8, max_length=20)
        platform: Literal["linux", "windows", "macos"] = "linux"
        runner_version: str = Field(default="", max_length=50)

    @router.post("/api/pairing", dependencies=auth, status_code=201)
    def create_pairing(request: PairingCreate, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return manager.create(request.name, request.resource_type)

    # 无 Bearer：配对码本身即一次性凭据，兑换后才发放节点令牌。
    @router.post("/api/pairing/redeem", status_code=201)
    def redeem_pairing(request: PairingRedeem, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return manager.redeem(request.code, request.platform, request.runner_version)

    @router.get("/api/topology", dependencies=auth)
    def topology():
        return manager.topology()

    return router
