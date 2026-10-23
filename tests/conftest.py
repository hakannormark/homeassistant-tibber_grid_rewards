"""pytest fixtures."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations defined in the test dir."""
    yield


@pytest.fixture(autouse=True)
def fake_daily_cost_store():
    """Replace the daily cost store, so tests never write to disk."""
    with patch("custom_components.tibber_grid_reward.sensor.Store") as store_cls:
        store = store_cls.return_value
        store.async_load = AsyncMock(return_value=None)
        store.async_save = AsyncMock()
        yield store
