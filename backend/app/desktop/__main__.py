import logging

import uvicorn

from app.desktop.api import create_app
from app.desktop.config import DesktopConfig


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = DesktopConfig()
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.port, access_log=False))
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
