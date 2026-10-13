#!/usr/bin/env python3
"""Starts Galaxy AI Engine v7.

    python3 run.py

Binding: HOST and PORT come from the environment. When PORT is set (Render, Hugging Face Spaces, Docker) the server listens
on all interfaces at that port. Otherwise it stays on 127.0.0.1:8000, the safe default on a laptop.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from galaxy.api import build_app  # noqa: E402


def resolve_bind(env) -> tuple:
    port = int(env.get("PORT", "8000"))
    host = env.get("HOST") or ("0.0.0.0" if "PORT" in env else "127.0.0.1")
    return host, port


def main() -> None:
    import uvicorn

    host, port = resolve_bind(os.environ)
    os.environ["HOST"] = host   # build_app() checks this same address: local mode refuses anything but loopback
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
