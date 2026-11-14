"""Run one event-loop worker using the shared deployment configuration."""
import uvicorn

from so import config


if __name__ == "__main__":
    uvicorn.run("server.app:app", host=config.SERVER_HOST, port=config.SERVER_PORT)
