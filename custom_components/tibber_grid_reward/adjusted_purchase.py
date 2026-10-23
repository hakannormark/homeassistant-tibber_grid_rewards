"""Today's electricity purchase, with car charging at Tibber's own session price.

The home's purchase cost values all energy, car charging included, at the
announced spot price. Tibber bills the car's charging at its own price and pays
Grid Rewards on top, so the car's share is swapped for the session figures.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .charging_session import ChargingSessionCoordinator, _local_today

_LOGGER = logging.getLogger(__name__)

ADJUSTED_PURCHASE_STORAGE_VERSION = 1

CONF_HOUSE_COST_ENTITY = "house_cost_entity"
CONF_PRICE_ENTITY = "price_entity"
CONF_ENERGY_ENTITIES = "energy_entities"


def state_as_float(state: Any) -> float | None:
    """Return a state's value as a float, or None if it has no number."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


class ChargingSpotTracker:
    """Track each vehicle's charging today, priced at the announced spot price.

    Each vehicle has a charged-energy counter (such as a car's "energy added").
    Every increase is priced at the spot price at that moment. The counter
    resets when a new session starts, so a drop counts its new value.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        energy_entities: dict[str, str],
        price_entity: str,
        store: Store | None = None,
    ) -> None:
        """Initialize the tracker for vehicle id to energy entity mappings."""
        self.hass = hass
        self._vehicle_by_entity = {
            entity: vehicle for vehicle, entity in energy_entities.items()
        }
        self._price_entity = price_entity
        self._store = store
        self._date: str | None = None
        self._energy: dict[str, float] = {}
        self._spot_cost: dict[str, float] = {}
        self._last_value: dict[str, float] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsub: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        """Restore today's figures and start following the energy counters."""
        if self._store is not None:
            data = await self._store.async_load() or {}
            self._date = data.get("date")
            self._energy = data.get("energy", {})
            self._spot_cost = data.get("spot_cost", {})
            self._last_value = data.get("last_value", {})
        self._unsub = async_track_state_change_event(
            self.hass, list(self._vehicle_by_entity), self._handle_change
        )

    @callback
    def async_stop(self) -> None:
        """Stop following the energy counters."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call the listener whenever today's figures change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _handle_change(self, event: Event) -> None:
        """Price the energy added since the counter's last value."""
        entity_id = event.data["entity_id"]
        value = state_as_float(event.data.get("new_state"))
        if value is None:
            return
        price = state_as_float(self.hass.states.get(self._price_entity))
        self.add(self._vehicle_by_entity[entity_id], entity_id, value, price)
        if self._store is not None:
            self.hass.async_create_task(self.async_save())
        for listener in list(self._listeners):
            listener()

    def add(
        self, vehicle_id: str, entity_id: str, value: float, price: float | None
    ) -> None:
        """Record a new counter value, priced at the given spot price."""
        today = _local_today()
        if today != self._date:
            self._date = today
            self._energy = {}
            self._spot_cost = {}

        last = self._last_value.get(entity_id)
        self._last_value[entity_id] = value
        if last is None or price is None:
            return
        added = value - last if value >= last else value
        if added <= 0:
            return
        self._energy[vehicle_id] = self._energy.get(vehicle_id, 0.0) + added
        self._spot_cost[vehicle_id] = (
            self._spot_cost.get(vehicle_id, 0.0) + added * price
        )

    async def async_save(self) -> None:
        """Persist today's figures."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "date": self._date,
                "energy": self._energy,
                "spot_cost": self._spot_cost,
                "last_value": self._last_value,
            }
        )

    def energy(self, vehicle_id: str) -> float:
        """Return the energy charged today according to the counter, in kWh."""
        if self._date != _local_today():
            return 0.0
        return self._energy.get(vehicle_id, 0.0)

    def spot_price(self, vehicle_id: str) -> float | None:
        """Return the energy-weighted spot price of today's charging."""
        energy = self.energy(vehicle_id)
        if energy <= 0:
            return None
        return self._spot_cost[vehicle_id] / energy


def session_price(
    coordinator: ChargingSessionCoordinator, vehicle_id: str
) -> float | None:
    """Return the latest session's cost per kWh, after Grid Rewards."""
    snapshot = (coordinator.data or {}).get(vehicle_id) or {}
    summary = snapshot.get("summary") or {}
    energy = summary.get("energyConsumed")
    cost = summary.get("totalCost")
    if not energy or cost is None:
        return None
    return cost / energy


def adjusted_purchase(
    house_cost: float,
    coordinator: ChargingSessionCoordinator,
    tracker: ChargingSpotTracker,
) -> float:
    """Return the home's purchase today with each car's charging repriced.

    For every vehicle, today's charged energy is taken off at the spot price it
    was charged at and added back at the session's price after Grid Rewards.
    The car's counter can miss the end of a session, so the energy is the
    larger of the counter and the sessions Tibber reported today.
    """
    total = house_cost
    for vehicle_id in coordinator.vehicle_ids:
        spot = tracker.spot_price(vehicle_id)
        actual = session_price(coordinator, vehicle_id)
        if spot is None or actual is None:
            continue
        energy = max(
            tracker.energy(vehicle_id), coordinator.daily_cost.energy(vehicle_id)
        )
        total += energy * (actual - spot)
    return round(total, 2)
