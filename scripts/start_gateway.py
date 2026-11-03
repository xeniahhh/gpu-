from __future__ import annotations

import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
for source_root in (
    "packages/fabric-domain/src",
    "packages/fabric-sim/src",
    "packages/event-contracts/src",
    "services/gateway/src",
    "services/planner/src",
    "services/aggregator/src",
    "services/evaluator/src",
):
    sys.path.insert(0, str(ROOT / source_root))

uvicorn.run(
    "gateway.main:app",
    host="127.0.0.1",
    port=8000,
    reload=False,
)
