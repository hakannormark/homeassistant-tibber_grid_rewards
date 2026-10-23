from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.tibber_grid_reward.adjusted_purchase import (
    ChargingSpotTracker,
    adjusted_purchase,
    session_price,
)
from custom_components.tibber_grid_reward.charging_session import (
    ChargingSessionCoordinator,
)

CAR = "car-a"
TODAY = "custom_components.tibber_grid_reward.adjusted_purchase._local_today"


@pytest.fixture
def coordinator():
    hass = MagicMock(spec=HomeAssistant)
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "entry"
    return ChargingSessionCoordinator(hass, MagicMock(), "home", [CAR], entry)


@pytest.fixture
def tracker():
    return ChargingSpotTracker(
        MagicMock(spec=HomeAssistant), {CAR: "sensor.added"}, "sensor.price"
    )


def _session(energy: float, cost: float) -> dict:
    return {
        CAR: {
            "chargingStatus": "disconnected",
            "summary": {"energyConsumed": energy, "totalCost": cost},
        }
    }


def test_tracker_prices_each_increase_at_the_spot_price_of_the_moment(tracker):
    with patch(TODAY, return_value="2026-10-23"):
        tracker.add(CAR, "sensor.added", 0.0, 1.0)
        tracker.add(CAR, "sensor.added", 2.0, 1.0)
        tracker.add(CAR, "sensor.added", 3.0, 0.5)

        assert tracker.energy(CAR) == 3.0
        assert tracker.spot_price(CAR) == pytest.approx((2.0 * 1.0 + 1.0 * 0.5) / 3.0)


def test_tracker_counts_a_reset_counter_as_a_new_session(tracker):
    with patch(TODAY, return_value="2026-10-23"):
        tracker.add(CAR, "sensor.added", 20.0, 1.0)
        tracker.add(CAR, "sensor.added", 1.5, 1.0)

        assert tracker.energy(CAR) == 1.5


def test_tracker_ignores_the_first_value_and_values_without_a_price(tracker):
    with patch(TODAY, return_value="2026-10-23"):
        tracker.add(CAR, "sensor.added", 5.0, 1.0)
        tracker.add(CAR, "sensor.added", 6.0, None)

        assert tracker.energy(CAR) == 0.0
        assert tracker.spot_price(CAR) is None


def test_tracker_starts_over_at_midnight(tracker):
    with patch(TODAY, return_value="2026-10-23"):
        tracker.add(CAR, "sensor.added", 0.0, 1.0)
        tracker.add(CAR, "sensor.added", 2.0, 1.0)
    with patch(TODAY, return_value="2026-10-24"):
        assert tracker.energy(CAR) == 0.0
        tracker.add(CAR, "sensor.added", 3.0, 0.5)
        assert tracker.energy(CAR) == 1.0


def test_session_price_is_cost_per_kwh_after_grid_rewards(coordinator):
    coordinator.data = _session(4.0, -0.4)

    assert session_price(coordinator, CAR) == pytest.approx(-0.1)


def test_adjusted_purchase_swaps_the_cars_spot_cost_for_the_session_cost(
    coordinator, tracker
):
    """10 kWh charged at 1.00 kr spot, billed at -0.10 kr: 11 kr comes off."""
    coordinator.data = _session(10.0, -1.0)
    with patch(TODAY, return_value="2026-10-23"):
        tracker.add(CAR, "sensor.added", 0.0, 1.0)
        tracker.add(CAR, "sensor.added", 10.0, 1.0)

        assert adjusted_purchase(67.0, coordinator, tracker) == pytest.approx(56.0)


def test_adjusted_purchase_uses_tibbers_energy_when_the_car_missed_the_end(
    coordinator, tracker
):
    """The car reported 8 kWh, but Tibber's counted sessions today were 10 kWh."""
    coordinator.data = _session(10.0, -1.0)
    coordinator.daily_cost.update(_session(1.0, 0.0), "2026-10-23")
    coordinator.daily_cost.update(_session(10.0, -1.0), "2026-10-23")
    coordinator.daily_cost.update(_session(10.0, -1.0), "2026-10-23")
    with patch(TODAY, return_value="2026-10-23"):
        tracker.add(CAR, "sensor.added", 0.0, 1.0)
        tracker.add(CAR, "sensor.added", 8.0, 1.0)

        assert adjusted_purchase(67.0, coordinator, tracker) == pytest.approx(56.0)


def test_adjusted_purchase_is_the_home_cost_without_charging_today(
    coordinator, tracker
):
    coordinator.data = _session(10.0, -1.0)

    assert adjusted_purchase(67.0, coordinator, tracker) == 67.0
