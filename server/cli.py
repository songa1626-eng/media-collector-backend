"""Command-line launcher for the backend-only HTTP service."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from config import ConfigLoader
from server.app import run_server


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="MediaCollector backend API")
    parser.add_argument("--config", default="config.yml", help="YAML configuration path")
    parser.add_argument("--host", default="127.0.0.1", help="listen host")
    parser.add_argument("--port", type=int, default=8000, help="listen port")
    args = parser.parse_args()

    config = ConfigLoader(args.config if Path(args.config).exists() else None)
    try:
        asyncio.run(run_server(config, host=args.host, port=args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
