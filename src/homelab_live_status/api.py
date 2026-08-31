"""Stateless status API: serves the collector's snapshot from a mounted file.

PLAN.md §2 API surface — the API never talks to Traefik/Proxmox/etc.; it
reads the last snapshot written by the collector CronJob, fresh per request
(ConfigMap volume updates propagate without a pod restart). Deployed twice
from the same image per §5: the internal instance mounts the full snapshot,
the public instance mounts the redacted one — the difference is entirely in
which file is mounted.

    SNAPSHOT_FILE  path to the mounted snapshot JSON (default: /data/snapshot.json)
    CORS_ORIGINS   comma-separated allowed origins (default: none). Set only on
                   the public instance so the status page on another domain
                   (e.g. a GitHub Pages site) can fetch the API.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from homelab_live_status.models import Event, Snapshot

DEFAULT_SNAPSHOT_FILE = "/data/snapshot.json"

app = FastAPI(
    title="Homelab Live-Status API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
)

if origins := [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET"],
        allow_headers=[],
    )


def snapshot_path() -> Path:
    """Resolve the mounted snapshot path (env-overridable for tests)."""
    return Path(os.environ.get("SNAPSHOT_FILE", DEFAULT_SNAPSHOT_FILE))


def load_snapshot() -> Snapshot:
    """Read and validate the current snapshot; 503 when unavailable."""
    path = snapshot_path()
    try:
        return Snapshot.model_validate_json(path.read_text())
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail=f"snapshot not available yet ({path} missing)",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=503, detail="snapshot unreadable (corrupt or in-flight write)"
        ) from None


@app.get("/api/health")
def health(response: Response) -> dict[str, str]:
    """Liveness probe; never cached."""
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}


@app.get("/api/v1/status", response_model=Snapshot)
def status() -> Snapshot:
    """The full current snapshot (minus the event feed, which has its own route)."""
    snapshot = load_snapshot()
    return snapshot.model_copy(update={"events": []})


@app.get("/api/v1/events", response_model=list[Event])
def events() -> list[Event]:
    """The rolling change feed, most recent first."""
    return load_snapshot().events
