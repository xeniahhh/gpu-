from pathlib import Path

from dotenv import load_dotenv

from gateway.api import create_app
from gateway.runtime import GatewayRuntime


ROOT = Path(__file__).resolve().parents[4]
load_dotenv(ROOT / ".env")
app = create_app(GatewayRuntime.from_environment())

WEB_DIST = ROOT / "apps" / "web" / "dist"
if WEB_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")

__all__ = ["app"]
