import asyncio
import copy
import fastapi
import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.WARNING)
logger.setLevel("INFO")

router = fastapi.APIRouter()

_access_info = {
    "admin": {},
    "expert": {},
    "advanced": {},
    "user": {},
    "observer": {},
}

_instrument = "TST"
_endstation = "default"

_delay = 0

app = fastapi.FastAPI()


@app.on_event("startup")
async def startup_event():
    logger.info("Access API Server started successfully.")


def _get_qserver_group_members(*, beamline, endstation):
    return _access_info


@router.get("/instruments/{instrument}/{endstation}/qserver/access")
async def get_access_info(instrument: str, endstation: str):
    # The delay is intended for testing timeouts.
    await asyncio.sleep(_delay)
    if instrument != _instrument:
        raise HTTPException(status_code=406, detail=f"Unknown instrument: {instrument!r}")
    if endstation != _endstation:
        raise HTTPException(status_code=406, detail=f"Unknown endstation: {endstation!r}")
    return _get_qserver_group_members(beamline=instrument, endstation=endstation)


# ================================================================================
#        The following API are intended exclusively for using in unit tests


@router.post("/test/set_info")
async def _set_info(access_info: dict):
    global _access_info
    _access_info = copy.deepcopy(access_info)


@router.post("/test/set_instrument")
async def _set_instrument(instrument: dict):
    global _instrument
    _instrument = copy.deepcopy(instrument)


@router.post("/test/set_endstation")
async def _set_endstation(endstation: dict):
    global _endstation
    _endstation = copy.deepcopy(endstation)


@router.post("/test/set_delay")
async def _set_delay(delay: dict):
    global _delay
    _delay = copy.deepcopy(delay)


def configure_routing():
    app.include_router(router)


configure_routing()
