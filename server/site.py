"""Product website routes: the race page at /race and read-only site data."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse

from so import config


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def install_site(app: FastAPI, settings: Any) -> None:
    """GET /race serves the live race view; GET /site-data serves findings, the rescue table and raw frontier flags."""

    @app.get("/race")
    async def race_page() -> FileResponse:
        return FileResponse(settings.ui_dir / "race.html")

    @app.get("/site-data")
    async def site_data() -> dict[str, Any]:
        rescue_text = _read_text(settings.results_path.parent / "rescue_table.json")
        try:
            rescue = json.loads(rescue_text) if rescue_text else None
        except ValueError:
            rescue = None
        frontier_text = _read_text(settings.frontier_path)
        try:
            frontier = json.loads(frontier_text) if frontier_text else None
        except ValueError:
            frontier = None
        # Raw frontier.json keeps the flags /results drops (simulated, label, n_questions).
        return {"findings": _read_text(config.ROOT / "analysis" / "findings.md"),
                "rescue_table": rescue if isinstance(rescue, dict) else None,
                "frontier": frontier if isinstance(frontier, dict) and isinstance(frontier.get("points"), list) else None}
