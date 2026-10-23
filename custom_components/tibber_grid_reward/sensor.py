"""Platform for sensor integration."""

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .adjusted_purchase import (
    ADJUSTED_PURCHASE_STORAGE_VERSION,
    CONF_ENERGY_ENTITIES,
    CONF_HOUSE_COST_ENTITY,
    CONF_PRICE_ENTITY,
    ChargingSpotTracker,
    adjusted_purchase,
    state_as_float,
)
from .charging_session import (
    DAILY_COST_STORAGE_VERSION,
    ChargingSessionCoordinator,
    claimed_by_earlier_entry,
)
from .const import DOMAIN
from .coordinator import TibberBatteryDataCoordinator
from .public_client import TibberPublicAPI

_LOGGER = logging.getLogger(__name__)


PRICE_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="current_price",
    name="Current Price",
    device_class=SensorDeviceClass.MONETARY,
)


BATTERY_SAVINGS_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="TODAY",
        name="Savings Today",
        device_class=SensorDeviceClass.MONETARY,
    ),
    SensorEntityDescription(
        key="WEEK",
        name="Savings This Week",
        device_class=SensorDeviceClass.MONETARY,
    ),
    SensorEntityDescription(
        key="MONTH",
        name="Savings This Month",
        device_class=SensorDeviceClass.MONETARY,
    ),
)

BATTERY_ACTIVITY_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="battery_activity_reason",
    name="Activity Reason",
    icon="mdi:home-battery",
)

BATTERY_PLANNED_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="battery_planned_activity",
    name="Next Planned Activity",
    device_class=SensorDeviceClass.TIMESTAMP,
    icon="mdi:calendar-clock",
)

# Tibber reports the reason as a GraphQL type name. Strip the shared prefix and
# expose snake_case so the value reads as a state rather than a class name:
# HomeBatteryChargingForGridRewards -> charging_for_grid_rewards
_REASON_PREFIX = "HomeBattery"


def _reason_to_state(typename: str | None) -> str | None:
    """Turn a reason type name into a snake_case sensor state."""
    if not typename:
        return None
    name = typename.removeprefix(_REASON_PREFIX)
    out = []
    for i, char in enumerate(name):
        if char.isupper() and i:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


# Below this the interval is noise rather than a planned event.
PLANNED_POWER_THRESHOLD_W = 100


GRID_REWARD_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="grid_reward_state",
        name="Grid Reward State",
    ),
    SensorEntityDescription(
        key="grid_reward_reason",
        name="Grid Reward Reason",
    ),
    SensorEntityDescription(
        key="grid_reward_current_month",
        name="Grid Reward Current Month",
        device_class=SensorDeviceClass.MONETARY,
    ),
    SensorEntityDescription(
        key="grid_reward_current_day",
        name="Grid Reward Current Day",
        device_class=SensorDeviceClass.MONETARY,
    ),
    SensorEntityDescription(
        key="last_reward_session",
        name="Last Reward Session",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="current_reward_session",
        name="Current Reward Session",
        device_class=SensorDeviceClass.MONETARY,
    ),
)

FLEX_DEVICE_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="state",
        name="State",
    ),
    SensorEntityDescription(
        key="grid_reward_reason",
        name="Grid Reward Reason",
    ),
    SensorEntityDescription(
        key="connectivity",
        name="Connectivity",
    ),
)

VEHICLE_BATTERY_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="battery_level",
    name="Battery Level",
    device_class=SensorDeviceClass.BATTERY,
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
)

SESSION_CURRENCY = "SEK"


@dataclass(frozen=True, kw_only=True)
class ChargingSessionSensorDescription(SensorEntityDescription):
    """Describe a sensor for one field of the latest charging session."""

    summary_key: str


# Field names follow Tibber's charging summary, which matches the figures in
# the app's smart charging timeline.
CHARGING_SESSION_SENSORS: tuple[ChargingSessionSensorDescription, ...] = (
    ChargingSessionSensorDescription(
        key="charging_session_energy",
        name="Session Energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        summary_key="energyConsumed",
    ),
    ChargingSessionSensorDescription(
        key="charging_session_cost",
        name="Session Cost After Grid Rewards",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=SESSION_CURRENCY,
        summary_key="totalCost",
    ),
    ChargingSessionSensorDescription(
        key="charging_session_grid_rewards",
        name="Session Grid Rewards",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=SESSION_CURRENCY,
        summary_key="gridRewards",
    ),
    ChargingSessionSensorDescription(
        key="charging_session_total_cost",
        name="Session Total Cost Incl. Grid Fees",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=SESSION_CURRENCY,
        summary_key="total",
    ),
    ChargingSessionSensorDescription(
        key="charging_session_average_price",
        name="Session Average Price",
        native_unit_of_measurement=f"{SESSION_CURRENCY}/kWh",
        summary_key="averagePrice",
    ),
)

CHARGING_COST_TODAY_DESCRIPTION = ChargingSessionSensorDescription(
    key="charging_cost_today",
    name="Charging Cost Today (Net)",
    device_class=SensorDeviceClass.MONETARY,
    native_unit_of_measurement=SESSION_CURRENCY,
    summary_key="totalCost",
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the sensor platform."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    api = entry_data["api"]
    public_api = entry_data.get("public_api")
    flex_devices = entry_data["flex_devices"]
    daily_tracker = entry_data["daily_tracker"]
    session_tracker = entry_data["session_tracker"]

    sensors = []
    if public_api:
        sensors.append(
            PriceSensor(
                public_api,
                config_entry.data["home_id"],
                config_entry.entry_id,
                PRICE_SENSOR_DESCRIPTION,
            )
        )

    grid_reward_sensors = []
    for description in GRID_REWARD_SENSORS:
        if description.key == "grid_reward_current_day":
            grid_reward_sensors.append(
                GridRewardCurrentDaySensor(
                    api, config_entry.entry_id, daily_tracker, description
                )
            )
        elif description.key in ("last_reward_session", "current_reward_session"):
            grid_reward_sensors.append(
                RewardSessionSensor(
                    api, config_entry.entry_id, session_tracker, description
                )
            )
        else:
            grid_reward_sensors.append(
                GridRewardSensor(api, config_entry.entry_id, description)
            )

    for device in flex_devices:
        for description in FLEX_DEVICE_SENSORS:
            grid_reward_sensors.append(
                FlexDeviceSensor(api, config_entry.entry_id, device, description)
            )
        if device.get("type") == "battery":
            battery_id = device["id"]
            battery_coordinators = entry_data.setdefault("battery_coordinators", {})
            if battery_id not in battery_coordinators:
                coordinator = TibberBatteryDataCoordinator(
                    hass,
                    api,
                    config_entry.data["home_id"],
                    battery_id,
                    config_entry=config_entry,
                )
                battery_coordinators[battery_id] = coordinator
            else:
                coordinator = battery_coordinators[battery_id]

            await coordinator.async_config_entry_first_refresh()

            sensors.extend(
                BatterySavingsSensor(
                    coordinator, config_entry.entry_id, device, description
                )
                for description in BATTERY_SAVINGS_SENSORS
            )
            sensors.append(
                BatteryActivitySensor(
                    coordinator,
                    config_entry.entry_id,
                    device,
                    BATTERY_ACTIVITY_SENSOR_DESCRIPTION,
                )
            )
            sensors.append(
                BatteryPlannedActivitySensor(
                    coordinator,
                    config_entry.entry_id,
                    device,
                    BATTERY_PLANNED_SENSOR_DESCRIPTION,
                )
            )
        if device.get("type") == "vehicle":
            vehicle_id = device["id"]
            if (
                "vehicle_devices" in entry_data
                and vehicle_id in entry_data["vehicle_devices"]
            ):
                vehicle_devices = entry_data["vehicle_devices"][vehicle_id]
                manager = _VehicleBatterySensorManager(
                    api,
                    config_entry.entry_id,
                    device,
                    vehicle_devices,
                    async_add_entities,
                )
                vehicle_devices.append(manager)

    vehicles = [
        device
        for device in flex_devices
        if device.get("type") == "vehicle"
        and not claimed_by_earlier_entry(hass, config_entry, device["id"])
    ]
    if vehicles:
        charging_coordinator = ChargingSessionCoordinator(
            hass,
            api,
            config_entry.data["home_id"],
            [device["id"] for device in vehicles],
            config_entry,
            store=Store(
                hass,
                DAILY_COST_STORAGE_VERSION,
                f"{DOMAIN}_daily_cost_{config_entry.entry_id}",
            ),
        )
        await charging_coordinator.daily_cost.async_load()
        await charging_coordinator.async_refresh()
        hass.data[DOMAIN][config_entry.entry_id]["charging_session_coordinator"] = (
            charging_coordinator
        )
        sensors.extend(
            ChargingSessionSensor(
                charging_coordinator, config_entry.entry_id, device, description
            )
            for device in vehicles
            for description in CHARGING_SESSION_SENSORS
        )
        sensors.extend(
            ChargingCostTodaySensor(
                charging_coordinator,
                config_entry.entry_id,
                device,
                CHARGING_COST_TODAY_DESCRIPTION,
            )
            for device in vehicles
        )
        sensors.append(
            ChargingCostTodayHomeSensor(
                charging_coordinator, config_entry.entry_id, config_entry.title
            )
        )

        options = config_entry.options
        house_cost_entity = options.get(CONF_HOUSE_COST_ENTITY)
        price_entity = options.get(CONF_PRICE_ENTITY)
        energy_entities = {
            vehicle_id: entity
            for vehicle_id, entity in (options.get(CONF_ENERGY_ENTITIES) or {}).items()
            if entity and vehicle_id in charging_coordinator.vehicle_ids
        }
        if house_cost_entity and price_entity and energy_entities:
            tracker = ChargingSpotTracker(
                hass,
                energy_entities,
                price_entity,
                store=Store(
                    hass,
                    ADJUSTED_PURCHASE_STORAGE_VERSION,
                    f"{DOMAIN}_adjusted_purchase_{config_entry.entry_id}",
                ),
            )
            await tracker.async_start()
            config_entry.async_on_unload(tracker.async_stop)
            sensors.append(
                AdjustedPurchaseTodaySensor(
                    charging_coordinator,
                    tracker,
                    house_cost_entity,
                    config_entry.entry_id,
                    config_entry.title,
                )
            )

    hass.data[DOMAIN][config_entry.entry_id]["grid_reward_devices"].extend(
        grid_reward_sensors
    )
    sensors.extend(grid_reward_sensors)
    async_add_entities(sensors)


class GridRewardSensor(SensorEntity):
    """Base class for Tibber Grid Reward sensors."""

    entity_description: SensorEntityDescription

    def __init__(self, api, entry_id, description: SensorEntityDescription):
        self.entity_description = description
        self._api = api
        self._entry_id = entry_id
        self._attributes = {}
        self._attr_unique_id = f"{self._entry_id}_{description.key}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "Tibber Grid Reward",
            "manufacturer": "Tibber",
        }

    @callback
    def update_data(self, data):
        _LOGGER.debug(
            "Updating grid reward sensor %s with data: %s", self.unique_id, data
        )
        self._attributes = data
        self._attr_native_value = self._get_state(data)
        if self.hass is not None:
            self.async_write_ha_state()

    def _get_state(self, data):
        """Get the state of the sensor."""
        if self.entity_description.key == "grid_reward_state":
            return data.get("state", {}).get("__typename")
        if self.entity_description.key == "grid_reward_reason":
            reasons = data.get("state", {}).get("reasons")
            if reasons:
                return ", ".join(reasons)
            return data.get("state", {}).get("reason")
        if self.entity_description.key == "grid_reward_current_month":
            self._attr_native_unit_of_measurement = data.get("rewardCurrency")
            return data.get("rewardCurrentMonth")
        return None


class GridRewardCurrentDaySensor(GridRewardSensor):
    """Representation of a Grid Reward Current Day Sensor."""

    def __init__(self, api, entry_id, tracker, description: SensorEntityDescription):
        """Initialize the sensor."""
        super().__init__(api, entry_id, description)
        self._tracker = tracker

    def _get_state(self, data):
        """Get the state of the sensor."""
        self._attr_native_unit_of_measurement = data.get("rewardCurrency")
        return round(self._tracker.daily_reward, 2)


class RewardSessionSensor(GridRewardSensor):
    """Representation of a reward session sensor."""

    def __init__(
        self, api, entry_id, session_tracker, description: SensorEntityDescription
    ):
        """Initialize the sensor."""
        super().__init__(api, entry_id, description)
        self._session_tracker = session_tracker

    def _get_state(self, data):
        """Get the state of the sensor."""
        if self.entity_description.key == "last_reward_session":
            last_session = self._session_tracker.last_session
            if last_session:
                self._attr_extra_state_attributes = {
                    "start_time": last_session["start_time"],
                    "end_time": last_session["end_time"],
                    "duration_minutes": last_session["duration_minutes"],
                    "reward": last_session["reward"],
                    "currency": data.get("rewardCurrency"),
                }
                return dt_util.parse_datetime(last_session["end_time"])
            return None
        if self.entity_description.key == "current_reward_session":
            self._attr_native_unit_of_measurement = data.get("rewardCurrency")
            return self._session_tracker.current_session_reward
        return None


class BatterySavingsSensor(
    CoordinatorEntity[TibberBatteryDataCoordinator], SensorEntity
):
    """Savings for one aggregation period of one battery."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: TibberBatteryDataCoordinator,
        entry_id: str,
        device: dict[str, Any],
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._device_id = device["id"]
        self._device_name = device.get("name", self._device_id)
        self._attr_unique_id = f"{self._device_id}_savings_{description.key.lower()}"
        self._attr_name = f"{self._device_name} {description.name}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tibber",
            "via_device": (DOMAIN, self._entry_id),
        }

    @property
    def native_value(self):
        """Return the savings value for this period."""
        if not self.coordinator.data or not self.coordinator.data.savings:
            return None
        item = self.coordinator.data.savings.get(self.entity_description.key)
        if not item:
            return None
        return item.get("value")

    @property
    def native_unit_of_measurement(self):
        """Return currency unit."""
        if not self.coordinator.data or not self.coordinator.data.savings:
            return None
        item = self.coordinator.data.savings.get(self.entity_description.key)
        if not item:
            return None
        return item.get("unit")


class BatteryActivitySensor(
    CoordinatorEntity[TibberBatteryDataCoordinator], SensorEntity
):
    """Why the battery is doing what it is doing right now.

    Tibber labels each activity interval with a reason, which distinguishes
    grid rewards from price arbitrage, solar charging and fuse protection —
    something that cannot be told apart from the inverter side.
    """

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: TibberBatteryDataCoordinator,
        entry_id: str,
        device: dict[str, Any],
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._device_id = device["id"]
        self._device_name = device.get("name", self._device_id)
        self._attr_unique_id = f"{self._device_id}_{description.key}"
        self._attr_name = f"{self._device_name} {description.name}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tibber",
            "via_device": (DOMAIN, self._entry_id),
        }

    def _get_current_interval(self) -> dict[str, Any] | None:
        if not self.coordinator.data or not self.coordinator.data.activity:
            return None
        items = self.coordinator.data.activity
        if not items:
            return None
        return next((i for i in items if not i.get("to")), items[-1])

    @property
    def native_value(self) -> str | None:
        """Return snake_case reason state."""
        current = self._get_current_interval()
        if not current:
            return None
        reason = (current.get("reason") or {}).get("__typename")
        return _reason_to_state(reason)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return raw reason, secondary reason, and start timestamp."""
        current = self._get_current_interval()
        if not current:
            return {}
        reason = (current.get("reason") or {}).get("__typename")
        secondary = (current.get("secondaryReason") or {}).get("__typename")
        return {
            "reason_raw": reason,
            "secondary_reason": _reason_to_state(secondary),
            "since": current.get("from"),
        }


class BatteryPlannedActivitySensor(
    CoordinatorEntity[TibberBatteryDataCoordinator], SensorEntity
):
    """When the battery next plans to charge or discharge.

    The state is the start of the next planned event, so it can be used in
    automations directly. The full quarter-hourly plan is exposed as an
    attribute for charting.
    """

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: TibberBatteryDataCoordinator,
        entry_id: str,
        device: dict[str, Any],
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._device_id = device["id"]
        self._device_name = device.get("name", self._device_id)
        self._attr_unique_id = f"{self._device_id}_{description.key}"
        self._attr_name = f"{self._device_name} {description.name}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tibber",
            "via_device": (DOMAIN, self._entry_id),
        }

    def _get_forecast(self) -> list[dict[str, Any]]:
        if not self.coordinator.data or not self.coordinator.data.planned:
            return []
        return [p for p in self.coordinator.data.planned if p.get("kind") == "FORECAST"]

    def _get_next_event(self) -> dict[str, Any] | None:
        forecast = self._get_forecast()
        return next(
            (
                p
                for p in forecast
                if (p.get("charged") or 0) >= PLANNED_POWER_THRESHOLD_W
                or (p.get("discharged") or 0) >= PLANNED_POWER_THRESHOLD_W
            ),
            None,
        )

    @property
    def native_value(self):
        nxt = self._get_next_event()
        if not nxt or not nxt.get("time"):
            return None
        return dt_util.parse_datetime(nxt["time"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self._get_next_event()
        forecast = self._get_forecast()
        return {
            "next_action": (
                None
                if not nxt
                else "charge"
                if (nxt.get("charged") or 0) >= PLANNED_POWER_THRESHOLD_W
                else "discharge"
            ),
            "next_power_w": (
                None
                if not nxt
                else (nxt.get("charged") or 0) or (nxt.get("discharged") or 0)
            ),
            "forecast": [
                {
                    "time": p.get("time"),
                    "charged": p.get("charged"),
                    "discharged": p.get("discharged"),
                    "state_of_charge": (
                        None
                        if p.get("state_of_charge") is None
                        else round(p["state_of_charge"], 1)
                    ),
                }
                for p in forecast
            ],
        }


class FlexDeviceSensor(SensorEntity):
    """Base class for Flex Device sensors."""

    entity_description: SensorEntityDescription

    def __init__(self, api, entry_id, device, description: SensorEntityDescription):
        self.entity_description = description
        self._api = api
        self._entry_id = entry_id
        self._device_id = device["id"]
        self._device_type = device["type"]
        self._device_name = device.get("name", self._device_id)
        self._attributes = {}
        self._attr_unique_id = f"{self._device_id}_{description.key}"
        self._attr_name = f"{self._device_name} {description.name}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tibber",
            "via_device": (DOMAIN, self._entry_id),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for flex device."""
        if self.entity_description.key in ("state", "grid_reward_reason"):
            state_data = self._attributes.get("state") or {}
            attrs: dict[str, Any] = {}
            if "__typename" in state_data:
                attrs["state"] = state_data["__typename"]
            if "kind" in state_data and state_data["kind"] is not None:
                attrs["kind"] = state_data["kind"]
            if "reason" in state_data and state_data["reason"] is not None:
                attrs["reason"] = state_data["reason"]
            if "reasons" in state_data and state_data["reasons"] is not None:
                attrs["reasons"] = state_data["reasons"]
            return attrs
        return {}

    @callback
    def update_data(self, data):
        _LOGGER.debug(
            "Updating flex device sensor %s with data: %s", self.unique_id, data
        )
        # Each snapshot replaces the previous status, including absent devices.
        self._attributes = {}
        self._attr_native_value = None
        flex_devices = data.get("flexDevices") or []
        device_id_key = "vehicleId" if self._device_type == "vehicle" else "batteryId"
        for device in flex_devices:
            dev_id = (
                device.get(device_id_key)
                or device.get("vehicleId")
                or device.get("batteryId")
            )
            if dev_id == self._device_id:
                self._attributes = device
                self._attr_native_value = self._get_state(device)
                break
        if self.hass is not None:
            self.async_write_ha_state()

    def _get_state(self, data):
        """Get the state of the sensor."""
        if self.entity_description.key == "state":
            return data.get("state", {}).get("__typename")
        if self.entity_description.key == "grid_reward_reason":
            state_data = data.get("state", {})
            typename = state_data.get("__typename")
            if typename == "GridRewardDelivering":
                return state_data.get("reason")
            if typename == "GridRewardUnavailable":
                reasons = state_data.get("reasons")
                if isinstance(reasons, list) and reasons:
                    return ", ".join(reasons)
                return None
            if typename == "GridRewardAvailable":
                return state_data.get("kind")
            return None
        if self.entity_description.key == "connectivity":
            if self._device_type == "vehicle":
                is_plugged_in = data.get("isPluggedIn")
                self._attr_icon = (
                    "mdi:car-electric" if is_plugged_in else "mdi:car-electric-outline"
                )
                return "Plugged In" if is_plugged_in else "Unplugged"
            self._attr_icon = "mdi:battery"
            return "Online"  # Placeholder for battery
        return None


class PriceSensor(SensorEntity):
    """Representation of a Tibber price sensor."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        public_api: TibberPublicAPI,
        home_id: str,
        entry_id: str,
        description: SensorEntityDescription,
    ):
        """Initialize the sensor."""
        self.entity_description = description
        self._public_api = public_api
        self._home_id = home_id
        self._entry_id = entry_id
        self._attr_unique_id = f"{self._entry_id}_{description.key}"
        self._attr_extra_state_attributes = {}

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "Tibber Grid Reward",
            "manufacturer": "Tibber",
        }

    async def async_update(self) -> None:
        """Fetch new state data for the sensor."""
        price_info = await self._public_api.get_price_info(self._home_id)
        if not price_info:
            return

        now = dt_util.now()
        current_hour = now.replace(minute=0, second=0, microsecond=0)

        today_prices_data = price_info.get("today", [])
        tomorrow_prices_data = price_info.get("tomorrow", [])
        all_prices_data = today_prices_data + tomorrow_prices_data

        def is_current_hour(price_dict):
            starts_at_str = price_dict.get("startsAt")
            if not starts_at_str:
                return False
            dt = dt_util.parse_datetime(starts_at_str)
            if not dt:
                return False
            return dt == current_hour

        current_price = next((p for p in all_prices_data if is_current_hour(p)), None)

        if current_price:
            self._attr_native_value = current_price.get("total")
            if "currency" in current_price:
                self._attr_native_unit_of_measurement = current_price["currency"]

        all_prices = [p["total"] for p in all_prices_data if p.get("total") is not None]

        def get_price_rating(price, prices):
            if not prices or price is None:
                return None

            p_count = len(prices)
            if p_count == 0 or len(set(prices)) == 1:
                return "Normal"

            lower_prices = sum(1 for p in prices if p < price)
            percentile = lower_prices / p_count

            if percentile < 0.33:
                return "Low"
            if percentile < 0.66:
                return "Moderate"
            return "High"

        today_prices_total = [p.get("total") for p in today_prices_data]
        tomorrow_prices_total = [p.get("total") for p in tomorrow_prices_data]

        self._attr_extra_state_attributes = {
            "last_update": now.isoformat(),
            "today": ", ".join(map(str, today_prices_total)),
            "today_raw": [
                {
                    "time": p.get("startsAt"),
                    "price": p.get("total"),
                    "rating": get_price_rating(p.get("total"), all_prices),
                }
                for p in today_prices_data
            ],
            "tomorrow": ", ".join(map(str, tomorrow_prices_total))
            if tomorrow_prices_total
            else None,
            "tomorrow_raw": [
                {
                    "time": p.get("startsAt"),
                    "price": p.get("total"),
                    "rating": get_price_rating(p.get("total"), all_prices),
                }
                for p in tomorrow_prices_data
            ]
            if tomorrow_prices_data
            else None,
            "tomorrow_valid": bool(tomorrow_prices_data),
        }


class _VehicleBatterySensorManager:
    """Decides, from the first vehicleState update, whether a
    VehicleBatterySensor should be created for this vehicle at all.

    This sensor only makes sense for "online" (API-connected, e.g. Tesla)
    vehicles, which report real telemetry via battery.level. "Offline"
    vehicles (tracked manually, e.g. Nissan Leaf, Renault 5 E-TECH) instead
    get number.py's settable BatteryLevelEntity for the same physical
    quantity — creating both here would give a user two differently-behaved
    "Battery Level" entities per offline vehicle. So this sensor is only
    created once a vehicle's first update confirms isAlive is True; offline
    vehicles never get it.

    A vehicle's online/offline kind isn't known at platform-setup time — it
    only arrives asynchronously with the first vehicleState update — so,
    mirroring number.py's _BatteryLevelEntityManager, this sits in the same
    per-vehicle `vehicle_devices` dispatch list and removes itself once
    resolved either way.
    """

    def __init__(self, api, entry_id, device, vehicle_devices, async_add_entities):
        self._api = api
        self._entry_id = entry_id
        self._device = device
        self._vehicle_devices = vehicle_devices
        self._async_add_entities = async_add_entities
        self._resolved = False

    @callback
    def update_data(self, data: dict) -> None:
        """Resolve, at most once, whether this vehicle gets the sensor."""
        if self._resolved:
            return

        is_alive = data.get("isAlive")
        if is_alive is None:
            # Kind not yet known from this payload; wait for a later one.
            return

        self._resolved = True
        if self in self._vehicle_devices:
            self._vehicle_devices.remove(self)

        if is_alive is True:
            _LOGGER.debug(
                "Vehicle %s confirmed online; adding its battery level sensor",
                self._device["id"],
            )
            entity = VehicleBatterySensor(self._api, self._entry_id, self._device, data)
            self._vehicle_devices.append(entity)
            self._async_add_entities([entity])
        else:
            _LOGGER.debug(
                "Vehicle %s confirmed offline; battery level sensor is not "
                "applicable (see number.py's BatteryLevelEntity instead)",
                self._device["id"],
            )


class VehicleBatterySensor(SensorEntity):
    """Representation of a vehicle battery level sensor.

    Only ever created by _VehicleBatterySensorManager once a vehicle's
    first update has confirmed isAlive is True, so it applies for as long
    as it exists.
    """

    entity_description = VEHICLE_BATTERY_SENSOR_DESCRIPTION

    def __init__(self, api, entry_id: str, device: dict, data: dict):
        """Initialize the vehicle battery sensor, populated from the data that confirmed it."""
        self._api = api
        self._entry_id = entry_id
        self._device_id = device["id"]
        self._device_name = device.get("name", self._device_id)
        self._attr_unique_id = f"{self._device_id}_battery_level"
        self._attr_name = f"{self._device_name} Battery Level"
        self._apply_data(data)

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tibber",
            "via_device": (DOMAIN, self._entry_id),
        }

    def _apply_data(self, data: dict) -> None:
        """Parse battery.level out of an update, handling it being absent."""
        battery = data.get("battery")
        level = battery.get("level") if isinstance(battery, dict) else None
        if level is None:
            self._attr_native_value = None
            return
        try:
            self._attr_native_value = int(level)
        except (ValueError, TypeError):
            self._attr_native_value = level

    @callback
    def update_data(self, data: dict) -> None:
        """Update entity with vehicle state data."""
        if self.hass is None:
            # async_add_entities() (called by the manager that created us)
            # registers this entity with hass as a background task rather
            # than synchronously, so a vehicleState update can land here
            # before that finishes. __init__ already applied the data that
            # triggered creation; skip until we're actually attached, the
            # next update will catch up.
            _LOGGER.debug(
                "Skipping battery level sensor update for vehicle %s: not yet attached to hass",
                self._device_id,
            )
            return

        _LOGGER.debug(
            "Updating vehicle battery sensor %s with data: %s", self.unique_id, data
        )
        self._apply_data(data)
        self.async_write_ha_state()


class ChargingSessionSensor(
    CoordinatorEntity[ChargingSessionCoordinator], SensorEntity
):
    """Sensor for one field of a vehicle's latest charging session."""

    entity_description: ChargingSessionSensorDescription

    def __init__(
        self,
        coordinator: ChargingSessionCoordinator,
        entry_id: str,
        device: dict,
        description: ChargingSessionSensorDescription,
    ) -> None:
        """Initialize the charging session sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._device_id = device["id"]
        self._device_name = device.get("name", self._device_id)
        self._attr_unique_id = f"{self._device_id}_{description.key}"
        self._attr_name = f"{self._device_name} {description.name}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tibber",
            "via_device": (DOMAIN, self._entry_id),
        }

    @property
    def native_value(self) -> float | None:
        """Return the field from the vehicle's latest charging session."""
        snapshot = (self.coordinator.data or {}).get(self._device_id) or {}
        summary = snapshot.get("summary") or {}
        return summary.get(self.entity_description.summary_key)


class ChargingCostTodaySensor(ChargingSessionSensor):
    """Sensor for a vehicle's net charging cost so far today."""

    @property
    def native_value(self) -> float | None:
        """Return today's net cost, summed over the vehicle's finished sessions."""
        return self.coordinator.daily_cost.total(self._device_id)


class ChargingCostTodayHomeSensor(
    CoordinatorEntity[ChargingSessionCoordinator], SensorEntity
):
    """Net charging cost so far today, summed over the vehicles of one home."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = SESSION_CURRENCY

    def __init__(
        self, coordinator: ChargingSessionCoordinator, entry_id: str, title: str
    ) -> None:
        """Initialize the home total sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_charging_cost_today_total"
        self._attr_name = f"{title} Charging Cost Today (Net)"

    @property
    def native_value(self) -> float | None:
        """Return the sum of today's net cost over this home's vehicles."""
        known: list[float] = []
        for vehicle_id in self.coordinator.vehicle_ids:
            total = self.coordinator.daily_cost.total(vehicle_id)
            if total is not None:
                known.append(total)
        return round(sum(known), 2) if known else None


class AdjustedPurchaseTodaySensor(
    CoordinatorEntity[ChargingSessionCoordinator], SensorEntity
):
    """The home's electricity purchase today, with car charging repriced.

    Starts from the home's purchase cost today and swaps each car's charging,
    valued at the spot price, for the session price after Grid Rewards.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = SESSION_CURRENCY

    def __init__(
        self,
        coordinator: ChargingSessionCoordinator,
        tracker: ChargingSpotTracker,
        house_cost_entity: str,
        entry_id: str,
        title: str,
    ) -> None:
        """Initialize the adjusted purchase sensor."""
        super().__init__(coordinator)
        self._tracker = tracker
        self._house_cost_entity = house_cost_entity
        self._attr_unique_id = f"{entry_id}_adjusted_purchase_today"
        self._attr_name = f"{title} Electricity Purchase Today (Adjusted)"

    async def async_added_to_hass(self) -> None:
        """Also update when the home's cost or the car's charging changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._tracker.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._house_cost_entity],
                self._handle_house_cost,
            )
        )

    @callback
    def _handle_house_cost(self, _event) -> None:
        """Update when the home's purchase cost changes."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the adjusted purchase, or None without the home's cost."""
        house_cost = state_as_float(self.hass.states.get(self._house_cost_entity))
        if house_cost is None:
            return None
        return adjusted_purchase(house_cost, self.coordinator, self._tracker)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show what the adjustment is built from, per vehicle."""
        attributes: dict[str, Any] = {}
        for vehicle_id in self.coordinator.vehicle_ids:
            spot = self._tracker.spot_price(vehicle_id)
            attributes[vehicle_id] = {
                "charged_energy_today": round(self._tracker.energy(vehicle_id), 2),
                "spot_price": round(spot, 4) if spot is not None else None,
            }
        return attributes
