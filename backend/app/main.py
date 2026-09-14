from fastapi import FastAPI
from pydantic import BaseModel

from .align import goto_and_align
from .camera import MockCamera
from .mount import MockMount, RaDec
from .platesolve import MockPlateSolver

app = FastAPI(title="astropi")

mount = MockMount()
camera = MockCamera(mount)
solver = MockPlateSolver()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


class GotoRequest(BaseModel):
    ra_deg: float
    dec_deg: float


@app.post("/goto")
def goto(req: GotoRequest) -> dict:
    result = goto_and_align(RaDec(req.ra_deg, req.dec_deg), mount, camera, solver)
    return {
        "converged": result.converged,
        "final_position": {"ra_deg": result.final_position.ra_deg, "dec_deg": result.final_position.dec_deg},
        "iterations": len(result.steps),
        "steps": [{"ra_deg": s.solved.ra_deg, "dec_deg": s.solved.dec_deg, "error_deg": s.error_deg} for s in result.steps],
    }
