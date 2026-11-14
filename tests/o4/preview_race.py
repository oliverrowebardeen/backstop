"""Offline reference-client preview with a deterministic successful race."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from server.app import ServerSettings, create_app
from tests.o4.fakes import run_question

_logs = TemporaryDirectory(prefix="o4-race-preview-")
app = create_app(settings=replace(ServerSettings.from_config(), log_dir=Path(_logs.name)),
                 runner=run_question)
