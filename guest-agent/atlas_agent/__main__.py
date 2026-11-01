"""python -m atlas_agent --channel <virtio device | unix socket> [--workdir /work] [--input-dir /work/in] [--display :99]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
import sys

from .agent import Agent, open_channel


async def amain(a: argparse.Namespace) -> None:
    is_sock = os.path.exists(a.channel) and hasattr(stat, "S_ISSOCK") and stat.S_ISSOCK(os.stat(a.channel).st_mode)
    while True:
        ch = await open_channel(a.channel)
        try:
            await Agent(ch, a.workdir, a.input_dir, a.display).serve()
        finally:
            ch.close()
        if is_sock:  # dev mode: host went away, we're done
            return
        await asyncio.sleep(1)  # device mode: host reconnects (e.g. after revert); keep serving


def main() -> None:
    p = argparse.ArgumentParser(prog="atlas_agent")
    p.add_argument("--channel", required=True, help="virtio char device path, or a Unix socket path (dev mode, agent connects)")
    p.add_argument("--workdir", default="/work")
    p.add_argument("--input-dir", default=None, help="default: <workdir>/in")
    p.add_argument("--display", default=None, help="X display, e.g. :99")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
