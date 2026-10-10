"""Run every protocol scenario on the Python model."""

import asyncio

import pytest

from pemu.bench import ModelBench
from pemu.scenarios import SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_scenario(scenario):
    asyncio.run(scenario(ModelBench()))
