"""Latest charging session per vehicle, as shown in Tibber's smart charging timeline."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import TibberAPI, TibberAuthError
from .const import DOMAIN
from .query_blocks import GraphQLQueryBlock, GraphQLQueryComposer

_LOGGER = logging.getLogger(__name__)

# Poll often while a vehicle is plugged in or was unplugged recently, since the
# session summary is only final after unplugging. Otherwise poll rarely.
ACTIVE_INTERVAL = timedelta(minutes=5)
IDLE_INTERVAL = timedelta(hours=2)
RECENTLY_CONNECTED = timedelta(hours=1)

# Each failure doubles the wait. A wrong login waits longest before retrying.
ERROR_BACKOFF = timedelta(minutes=15)
AUTH_BACKOFF = timedelta(hours=1)
MAX_BACKOFF = timedelta(hours=6)

DISCONNECTED = "disconnected"
SUMMARY_FIELDS = (
    "energyConsumed",
    "totalCost",
    "gridRewards",
    "total",
    "averagePrice",
)

DAILY_COST_STORAGE_VERSION = 1


class ChargingSessionBlock(GraphQLQueryBlock):
    """Query block for the latest charging session of each vehicle.

    Every vehicle gets its own alias (v0, v1, ...), so one request covers all
    of them.
    """

    name = "charging_sessions"
    root_field = "me"

    def __init__(self, vehicle_ids: list[str]) -> None:
        """Initialize the block for the given vehicle ids."""
        self.vehicle_ids = vehicle_ids

    def get_variable_definitions(self) -> dict[str, str]:
        """Return one vehicle id variable per vehicle."""
        return {f"$vehicleId{i}": "String!" for i in range(len(self.vehicle_ids))}

    def get_variables(
        self, now: datetime, home_id: str, device_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Return the vehicle ids as variables."""
        return {f"vehicleId{i}": vid for i, vid in enumerate(self.vehicle_ids)}

    def get_query_fragment(self) -> str:
        """Return one aliased vehicle selection per vehicle."""
        summary = "\n".join(f"      {field}" for field in SUMMARY_FIELDS)
        return "\n".join(
            f"""v{i}: vehicle(id: $vehicleId{i}) {{
  chargingStatus
  charging {{
    summary {{
{summary}
    }}
  }}
}}"""
            for i in range(len(self.vehicle_ids))
        )

    def parse_response(self, root_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return the charging status and latest summary keyed by vehicle id."""
        snapshots: dict[str, dict[str, Any]] = {}
        for i, vehicle_id in enumerate(self.vehicle_ids):
            vehicle = root_data.get(f"v{i}") or {}
            charging = vehicle.get("charging") or {}
            snapshots[vehicle_id] = {
                "chargingStatus": vehicle.get("chargingStatus"),
                "summary": charging.get("summary"),
            }
        return snapshots


def claimed_by_earlier_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, vehicle_id: str
) -> bool:
    """Return True if an entry created before this one already lists the vehicle.

    Two homes can list the same vehicle. Only the earlier entry shows its session
    sensors and polls the vehicle, so the vehicle is fetched once. Entry ids are
    ULIDs, so sorting them orders the entries by creation time.
    """
    return any(
        other.entry_id < config_entry.entry_id
        and any(
            device.get("id") == vehicle_id
            for device in other.data.get("flex_devices", [])
        )
        for other in hass.config_entries.async_entries(DOMAIN)
    )


def _local_today() -> str:
    """Return today's date in the local time zone, as an ISO string."""
    return dt_util.as_local(dt_util.utcnow()).date().isoformat()


class DailyChargingCost:
    """Add up each vehicle's finished charging sessions for the current day.

    Tibber reports only the latest session, so a session is counted once its
    summary stays the same on two polls in a row while the vehicle is
    unplugged. The first summary seen for a vehicle is a baseline and is not
    counted, so an old session never lands in today's total.
    """

    def __init__(self, store: Store | None = None) -> None:
        """Initialize the accumulator, optionally persisted with a store."""
        self._store = store
        self._date: str | None = None
        self._totals: dict[str, float] = {}
        self._last_key: dict[str, list[float]] = {}
        self._candidate: dict[str, list[float]] = {}

    async def async_load(self) -> None:
        """Restore the day's totals, so a restart does not reset them."""
        if self._store is None:
            return
        data = await self._store.async_load() or {}
        self._date = data.get("date")
        self._totals = data.get("totals", {})
        self._last_key = data.get("last_key", {})

    async def async_save(self) -> None:
        """Persist the day's totals."""
        if self._store is None:
            return
        await self._store.async_save(
            {"date": self._date, "totals": self._totals, "last_key": self._last_key}
        )

    def update(self, snapshots: dict[str, dict[str, Any]], today: str) -> None:
        """Count the sessions that finished since the last update."""
        if today != self._date:
            self._date = today
            self._totals = {}

        for vehicle_id, snapshot in snapshots.items():
            summary = snapshot.get("summary")
            if not summary:
                continue
            key = [summary["energyConsumed"], summary["totalCost"]]
            if vehicle_id not in self._last_key:
                self._last_key[vehicle_id] = key
                continue
            if (
                snapshot["chargingStatus"] != DISCONNECTED
                or key == self._last_key[vehicle_id]
            ):
                self._candidate.pop(vehicle_id, None)
                continue
            if key == self._candidate.get(vehicle_id):
                self._totals[vehicle_id] = (
                    self._totals.get(vehicle_id, 0.0) + summary["totalCost"]
                )
                self._last_key[vehicle_id] = key
                self._candidate.pop(vehicle_id, None)
            else:
                self._candidate[vehicle_id] = key

    def total(self, vehicle_id: str) -> float | None:
        """Return today's net cost for a vehicle, or None before its first summary."""
        if vehicle_id not in self._last_key:
            return None
        return round(self._totals.get(vehicle_id, 0.0), 2)


def _backoff(base: timedelta, failures: int) -> timedelta:
    """Return the wait after the given number of consecutive failures."""
    return min(base * 2 ** min(failures - 1, 8), MAX_BACKOFF)


class ChargingSessionCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetch the latest charging session for each vehicle of one home.

    Data is keyed by vehicle id. The poll interval follows the plug state, and
    failures back off so a bad login is not retried often.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: TibberAPI,
        home_id: str,
        vehicle_ids: list[str],
        config_entry: Any,
        store: Store | None = None,
    ) -> None:
        """Initialize the charging session coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"Tibber Charging Sessions {home_id}",
            update_interval=ACTIVE_INTERVAL,
        )
        self.api = api
        self.home_id = home_id
        self.vehicle_ids = vehicle_ids
        self.daily_cost = DailyChargingCost(store)
        self._composer = GraphQLQueryComposer(
            blocks=[ChargingSessionBlock(vehicle_ids)],
            operation_name="GetChargingSessions",
            root_field="me",
        )
        self._last_connected: dict[str, datetime] = {}
        self._failures = 0

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch the latest sessions and pick the next poll interval."""
        try:
            raw = await self.api.execute_query_blocks(self._composer, self.home_id)
        except TibberAuthError as err:
            self._failures += 1
            self.update_interval = _backoff(AUTH_BACKOFF, self._failures)
            raise UpdateFailed(f"Tibber login failed: {err}") from err
        except Exception as err:
            self._failures += 1
            self.update_interval = _backoff(ERROR_BACKOFF, self._failures)
            raise UpdateFailed(f"Error fetching charging sessions: {err}") from err

        self._failures = 0
        snapshots: dict[str, dict[str, Any]] = raw.get("charging_sessions", {})
        self.update_interval = self._interval_for(snapshots)
        self.daily_cost.update(snapshots, _local_today())
        await self.daily_cost.async_save()
        return snapshots

    def _interval_for(self, snapshots: dict[str, dict[str, Any]]) -> timedelta:
        """Poll often while any vehicle is plugged in or was unplugged recently."""
        now = dt_util.utcnow()
        for vehicle_id, snapshot in snapshots.items():
            if snapshot["chargingStatus"] not in (None, DISCONNECTED):
                self._last_connected[vehicle_id] = now

        if any(
            now - seen < RECENTLY_CONNECTED for seen in self._last_connected.values()
        ):
            return ACTIVE_INTERVAL
        return IDLE_INTERVAL
