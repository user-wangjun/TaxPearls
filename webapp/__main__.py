"""python -m webapp 启动 Web 端证据链页面（FR-G01）。

    .venv/Scripts/python.exe -m webapp            # http://127.0.0.1:8000

仅监听 127.0.0.1：本机演示用途，不对局域网暴露（安全基线：宁少勿多）。
"""
from __future__ import annotations

import argparse
import os
from src import settings


def main() -> int:
    ap = argparse.ArgumentParser(description="税海拾珠 · Web 端证据链页面")
    ap.add_argument("--host", default=os.getenv("TAXPEARLS_HOST", "127.0.0.1"), help="监听地址")
    ap.add_argument("--port", type=int, default=int(os.getenv("TAXPEARLS_PORT", "8000")), help="监听端口")
    args = ap.parse_args()

    import uvicorn

    print(f"  税海拾珠 Web 端启动中 → http://{args.host}:{args.port}")
    uvicorn.run("webapp.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
