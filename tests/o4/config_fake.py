"""Settings substitute for testing the server before shared configuration lands."""
from so import config


def install() -> None:
    defaults = {
        "SERVER_UI_DIR": config.ROOT / "ui",
        "SERVER_DEMO_DIR": config.ROOT / "demo",
        "SERVER_FIXTURE_DIR": config.ROOT / "tests/o4/fixtures",
        "SERVER_LOG_DIR": config.ROOT / "out/logs",
        "SERVER_RESULTS_PATH": config.ROOT / "out/results.json",
        "SERVER_FRONTIER_PATH": config.ROOT / "out/frontier.json",
        "SERVER_MAX_ACTIVE_RUNS": 8,
        "SERVER_MAX_QUESTION_CHARS": 2000,
        "SERVER_START_TIMEOUT_SECONDS": 10.0,
        "SERVER_RUN_TIMEOUT_SECONDS": 180.0,
        "SERVER_POLL_SECONDS": .05,
        "SERVER_KEEPALIVE_SECONDS": 10.0,
    }
    for name, value in defaults.items():
        if not hasattr(config, name):
            setattr(config, name, value)
