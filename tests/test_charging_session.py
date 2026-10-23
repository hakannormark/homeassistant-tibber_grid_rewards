from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.tibber_grid_reward.charging_session import (
    ACTIVE_INTERVAL,
    AUTH_BACKOFF,
    ERROR_BACKOFF,
    IDLE_INTERVAL,
    MAX_BACKOFF,
    ChargingSessionBlock,
    ChargingSessionCoordinator,
    _backoff,
    claimed_by_earlier_entry,
)
from custom_components.tibber_grid_reward.client import (
    TibberAuthError,
    TibberConnectionError,
)
from custom_components.tibber_grid_reward.const import DOMAIN
from custom_components.tibber_grid_reward.query_blocks import GraphQLQueryComposer
from custom_components.tibber_grid_reward.sensor import (
    CHARGING_SESSION_SENSORS,
    FLEX_DEVICE_SENSORS,
    ChargingSessionSensor,
    FlexDeviceSensor,
    async_setup_entry,
)

VEHICLE_A = "vehicle-a"
VEHICLE_B = "vehicle-b"

SUMMARY = {
    "energyConsumed": 3.28,
    "totalCost": -0.25,
    "gridRewards": 0.41,
    "total": 2.09,
    "averagePrice": -0.08,
}

# Shape of the "me" object returned for two vehicles: A is unplugged with a
# finished session, B is charging and has no session summary yet.
ROOT_DATA = {
    "v0": {"chargingStatus": "disconnected", "charging": {"summary": SUMMARY}},
    "v1": {"chargingStatus": "charging", "charging": None},
}


@pytest.fixture
def mock_hass():
    """Mock HomeAssistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {DOMAIN: {}}
    return hass


@pytest.fixture
def mock_api():
    """Mock Tibber API."""
    return MagicMock()


@pytest.fixture
def mock_config_entry():
    """Mock ConfigEntry instance."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.data = {"home_id": "home1", "flex_devices": []}
    return entry


@pytest.fixture
def coordinator(mock_hass, mock_api, mock_config_entry):
    """Coordinator for two vehicles in one home."""
    return ChargingSessionCoordinator(
        mock_hass, mock_api, "home1", [VEHICLE_A, VEHICLE_B], mock_config_entry
    )


def _snapshots(status_a: str, status_b: str = "disconnected") -> dict:
    return {
        VEHICLE_A: {"chargingStatus": status_a, "summary": None},
        VEHICLE_B: {"chargingStatus": status_b, "summary": None},
    }


def test_block_has_one_alias_per_vehicle():
    """The query fetches every vehicle in a single request."""
    composer = GraphQLQueryComposer(
        blocks=[ChargingSessionBlock([VEHICLE_A, VEHICLE_B])],
        operation_name="GetChargingSessions",
        root_field="me",
    )

    query = composer.build_query()

    assert (
        "query GetChargingSessions($vehicleId0: String!, $vehicleId1: String!)" in query
    )
    assert "v0: vehicle(id: $vehicleId0)" in query
    assert "v1: vehicle(id: $vehicleId1)" in query
    for field in SUMMARY:
        assert field in query
    assert composer.build_variables(dt_util.utcnow(), "home1") == {
        "vehicleId0": VEHICLE_A,
        "vehicleId1": VEHICLE_B,
    }


def test_parse_maps_aliases_to_vehicle_ids():
    """Each alias is returned under its vehicle id, with null sessions kept."""
    composer = GraphQLQueryComposer(
        blocks=[ChargingSessionBlock([VEHICLE_A, VEHICLE_B])], root_field="me"
    )

    result = composer.parse_response({"data": {"me": ROOT_DATA}})

    assert result == {
        "charging_sessions": {
            VEHICLE_A: {"chargingStatus": "disconnected", "summary": SUMMARY},
            VEHICLE_B: {"chargingStatus": "charging", "summary": None},
        }
    }


def test_parse_tolerates_missing_me():
    """A response without the vehicle data yields empty sessions, not an error."""
    composer = GraphQLQueryComposer(
        blocks=[ChargingSessionBlock([VEHICLE_A])], root_field="me"
    )

    result = composer.parse_response({"data": {"me": None}})

    assert result == {
        "charging_sessions": {
            VEHICLE_A: {"chargingStatus": None, "summary": None},
        }
    }


@pytest.mark.parametrize("status", ["charging", "not_charging"])
def test_polls_often_while_plugged_in(coordinator, status):
    assert coordinator._interval_for(_snapshots(status)) == ACTIVE_INTERVAL


def test_polls_rarely_when_never_plugged_in(coordinator):
    assert coordinator._interval_for(_snapshots("disconnected")) == IDLE_INTERVAL


def test_keeps_polling_for_an_hour_after_unplugging(coordinator):
    start = datetime(2026, 10, 23, 22, 0, tzinfo=dt_util.UTC)
    with patch(
        "custom_components.tibber_grid_reward.charging_session.dt_util.utcnow",
        return_value=start,
    ):
        coordinator._interval_for(_snapshots("charging"))

    with patch(
        "custom_components.tibber_grid_reward.charging_session.dt_util.utcnow",
        return_value=start + timedelta(minutes=50),
    ):
        assert coordinator._interval_for(_snapshots("disconnected")) == ACTIVE_INTERVAL

    with patch(
        "custom_components.tibber_grid_reward.charging_session.dt_util.utcnow",
        return_value=start + timedelta(minutes=70),
    ):
        assert coordinator._interval_for(_snapshots("disconnected")) == IDLE_INTERVAL


def test_backoff_doubles_and_caps():
    assert _backoff(ERROR_BACKOFF, 1) == ERROR_BACKOFF
    assert _backoff(ERROR_BACKOFF, 2) == ERROR_BACKOFF * 2
    assert _backoff(ERROR_BACKOFF, 50) == MAX_BACKOFF


async def test_auth_failure_waits_long_and_keeps_growing(coordinator, mock_api):
    mock_api.execute_query_blocks = AsyncMock(side_effect=TibberAuthError("bad"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == AUTH_BACKOFF

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == AUTH_BACKOFF * 2


async def test_connection_error_backs_off_then_success_resets(coordinator, mock_api):
    mock_api.execute_query_blocks = AsyncMock(side_effect=TibberConnectionError("down"))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == ERROR_BACKOFF

    mock_api.execute_query_blocks = AsyncMock(
        return_value={"charging_sessions": _snapshots("disconnected")}
    )
    data = await coordinator._async_update_data()

    assert coordinator._failures == 0
    assert data == _snapshots("disconnected")
    assert coordinator.update_interval == IDLE_INTERVAL


@pytest.mark.parametrize("description", CHARGING_SESSION_SENSORS)
def test_sensor_reads_its_field(coordinator, description):
    coordinator.data = {
        VEHICLE_A: {"chargingStatus": "disconnected", "summary": SUMMARY}
    }
    sensor = ChargingSessionSensor(
        coordinator,
        "test_entry_id",
        {"id": VEHICLE_A, "name": "Smurfen"},
        description,
    )

    assert sensor.native_value == SUMMARY[description.summary_key]
    assert sensor.unique_id == f"{VEHICLE_A}_{description.key}"
    assert sensor.name == f"Smurfen {description.name}"


@pytest.mark.parametrize("data", [None, {VEHICLE_A: {"summary": None}}])
def test_sensor_is_unknown_without_a_session(coordinator, data):
    coordinator.data = data
    sensor = ChargingSessionSensor(
        coordinator,
        "test_entry_id",
        {"id": VEHICLE_A, "name": "Smurfen"},
        CHARGING_SESSION_SENSORS[0],
    )

    assert sensor.native_value is None


def test_sensor_shares_the_vehicle_device(coordinator):
    device = {"id": VEHICLE_A, "name": "Smurfen", "type": "vehicle"}
    sensor = ChargingSessionSensor(
        coordinator, "test_entry_id", device, CHARGING_SESSION_SENSORS[0]
    )
    flex_sensor = FlexDeviceSensor(
        MagicMock(), "test_entry_id", device, FLEX_DEVICE_SENSORS[0]
    )

    assert sensor.device_info["identifiers"] == flex_sensor.device_info["identifiers"]


async def test_setup_adds_all_session_sensors_per_vehicle(
    mock_hass, mock_api, mock_config_entry
):
    """Each vehicle gets every session sensor, from one coordinator and one request."""
    vehicles = [
        {"id": VEHICLE_A, "type": "vehicle", "name": "Smurfen"},
        {"id": VEHICLE_B, "type": "vehicle", "name": "Teslarossa"},
    ]
    mock_config_entry.data = {"home_id": "home1", "flex_devices": vehicles}
    mock_hass.data[DOMAIN][mock_config_entry.entry_id] = {
        "api": mock_api,
        "public_api": None,
        "flex_devices": vehicles,
        "grid_reward_devices": [],
        "battery_coordinators": {},
        "vehicle_devices": {VEHICLE_A: [], VEHICLE_B: []},
        "daily_tracker": MagicMock(),
        "session_tracker": MagicMock(),
    }
    mock_api.execute_query_blocks = AsyncMock(
        return_value={
            "charging_sessions": {
                VEHICLE_A: {"chargingStatus": "disconnected", "summary": SUMMARY},
                VEHICLE_B: {"chargingStatus": "charging", "summary": None},
            }
        }
    )

    async_add_entities = MagicMock()
    await async_setup_entry(mock_hass, mock_config_entry, async_add_entities)

    added = async_add_entities.call_args[0][0]
    session_sensors = [e for e in added if isinstance(e, ChargingSessionSensor)]
    assert len(session_sensors) == 2 * len(CHARGING_SESSION_SENSORS)
    assert mock_api.execute_query_blocks.await_count == 1


async def test_setup_without_vehicles_skips_session_sensors(
    mock_hass, mock_api, mock_config_entry
):
    mock_hass.data[DOMAIN][mock_config_entry.entry_id] = {
        "api": mock_api,
        "public_api": None,
        "flex_devices": [],
        "grid_reward_devices": [],
        "battery_coordinators": {},
        "vehicle_devices": {},
        "daily_tracker": MagicMock(),
        "session_tracker": MagicMock(),
    }

    async_add_entities = MagicMock()
    await async_setup_entry(mock_hass, mock_config_entry, async_add_entities)

    added = async_add_entities.call_args[0][0]
    assert not any(isinstance(e, ChargingSessionSensor) for e in added)
    mock_api.execute_query_blocks.assert_not_called()


def _entry(entry_id: str, flex_devices: list[dict]) -> MagicMock:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = entry_id
    entry.data = {"home_id": f"home-{entry_id}", "flex_devices": flex_devices}
    return entry


def test_earlier_entry_claims_a_shared_vehicle(mock_hass):
    """A vehicle listed by an earlier entry is claimed by it, not by a later one."""
    shared = {"id": VEHICLE_A, "type": "vehicle", "name": "Smurfen"}
    only_later = {"id": VEHICLE_B, "type": "vehicle", "name": "Teslarossa"}
    earlier = _entry("01A", [shared])
    later = _entry("01B", [shared, only_later])
    mock_hass.config_entries.async_entries.return_value = [earlier, later]

    assert claimed_by_earlier_entry(mock_hass, later, VEHICLE_A)
    assert not claimed_by_earlier_entry(mock_hass, later, VEHICLE_B)
    assert not claimed_by_earlier_entry(mock_hass, earlier, VEHICLE_A)


async def test_setup_skips_vehicles_claimed_by_earlier_entry(
    mock_hass, mock_api, mock_config_entry
):
    """Only the vehicle the earlier entry does not list gets session sensors here."""
    shared = {"id": VEHICLE_A, "type": "vehicle", "name": "Smurfen"}
    only_later = {"id": VEHICLE_B, "type": "vehicle", "name": "Teslarossa"}
    earlier = _entry("01A", [shared])
    mock_config_entry.entry_id = "01B"
    mock_config_entry.data = {
        "home_id": "home1",
        "flex_devices": [shared, only_later],
    }
    mock_hass.config_entries.async_entries.return_value = [earlier, mock_config_entry]
    mock_hass.data[DOMAIN][mock_config_entry.entry_id] = {
        "api": mock_api,
        "public_api": None,
        "flex_devices": [shared, only_later],
        "grid_reward_devices": [],
        "battery_coordinators": {},
        "vehicle_devices": {VEHICLE_A: [], VEHICLE_B: []},
        "daily_tracker": MagicMock(),
        "session_tracker": MagicMock(),
    }
    mock_api.execute_query_blocks = AsyncMock(
        return_value={
            "charging_sessions": {
                VEHICLE_B: {"chargingStatus": "charging", "summary": None},
            }
        }
    )

    async_add_entities = MagicMock()
    await async_setup_entry(mock_hass, mock_config_entry, async_add_entities)

    added = async_add_entities.call_args[0][0]
    session_sensors = [e for e in added if isinstance(e, ChargingSessionSensor)]
    assert len(session_sensors) == len(CHARGING_SESSION_SENSORS)
    for sensor in session_sensors:
        assert sensor.device_info["identifiers"] == {(DOMAIN, VEHICLE_B)}
