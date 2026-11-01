import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from atlas_agent.agent import Agent, open_channel
from fakehost import FakeHost


@pytest.fixture
def sandbox():
    d = Path(tempfile.mkdtemp(prefix="atl"))  # short path: AF_UNIX limit is ~108 bytes
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def host(sandbox):
    h = FakeHost(str(sandbox / "a.sock"))
    await h.start()
    ch = await open_channel(h.path)
    agent = Agent(ch, workdir=sandbox / "work", input_dir=sandbox / "work" / "in", heartbeat_interval=0.2)
    h.agent = agent
    task = asyncio.create_task(agent.serve())
    yield h
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    ch.close()
    await h.stop()
