"""Offline browser preview of provider failures in both race columns."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from server.app import ServerSettings, create_app
from tests.o4.fakes import usage_limit_question

_logs = TemporaryDirectory(prefix="o4-failure-preview-")
app = create_app(settings=replace(ServerSettings.from_config(), log_dir=Path(_logs.name)),
                 runner=usage_limit_question)
