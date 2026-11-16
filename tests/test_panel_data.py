"""What the panel is given: timers, satellite status, the log and diagnostics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.voice_alarms.const import DOMAIN, INTENT_SET, MAX_EVENT_LOG
from homeassistant.components.intent import async_register_timer_handler
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .conftest import slot

TZ = ZoneInfo("Europe/Zurich")
START = datetime(2026, 9, 7, 10, 0, tzinfo=TZ)
MANIFEST = Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json"


@pytest.fixture
async def ready(hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_integration, manager):
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to(START)
    return manager


async def _start_timer(hass: HomeAssistant, device_id: str, **slots) -> None:
    await intent.async_handle(
        hass,
        "test",
        intent.INTENT_START_TIMER,
        {k: slot(v) for k, v in slots.items()},
        device_id=device_id,
        language="en",
    )


def _handler(*_args) -> None:
    return None


async def test_snapshot_has_timers_status_and_diagnostics(
    hass: HomeAssistant, ready, satellites
) -> None:
    kitchen = satellites["kitchen"]
    async_register_timer_handler(hass, kitchen["device_id"], _handler)
    await _start_timer(hass, kitchen["device_id"], minutes=5, name="pasta")
    await _start_timer(hass, kitchen["device_id"], minutes=90)

    snap = ready.snapshot()

    running = {t["name"]: t for t in snap["timers"]["running"]}
    assert set(running) == {"pasta", ""}
    pasta = running["pasta"]
    assert pasta["total"] == 300
    assert 295 <= pasta["seconds_left"] <= 300
    assert pasta["is_active"]
    assert dt_ends(pasta) == START + timedelta(seconds=pasta["seconds_left"])
    assert pasta["device_id"] == kitchen["device_id"]
    assert pasta["satellite_known"]
    assert snap["timers"]["pending"] == []

    status = {t["entity_id"]: t for t in snap["targets"]}
    here, there = status[kitchen["satellite"]], status[satellites["bedroom"]["satellite"]]
    assert here["available"]
    assert here["state"] == "idle"
    assert here["timers_supported"]
    assert here["player_volume"] == 0.4
    assert not there["timers_supported"]

    debug = snap["debug"]
    assert debug["version"] == json.loads(MANIFEST.read_text())["version"]
    assert debug["time_zone"] == "Europe/Zurich"
    assert debug["timers_persistent"]
    assert debug["alarms_total"] == 0


async def test_an_unavailable_satellite_is_flagged(hass: HomeAssistant, ready, satellites) -> None:
    hass.states.async_set(satellites["bedroom"]["satellite"], STATE_UNAVAILABLE)
    status = {t["entity_id"]: t for t in ready.snapshot()["targets"]}
    assert not status[satellites["bedroom"]["satellite"]]["available"]
    assert status[satellites["kitchen"]["satellite"]]["available"]


async def test_the_log_tells_what_happened(
    hass: HomeAssistant, ready, satellites, freezer: FrozenDateTimeFactory, announce_calls
) -> None:
    kitchen = satellites["kitchen"]
    async_register_timer_handler(hass, kitchen["device_id"], _handler)
    await _start_timer(hass, kitchen["device_id"], minutes=5, name="pasta")
    await intent.async_handle(
        hass,
        "test",
        intent.INTENT_CANCEL_TIMER,
        {"name": slot("pasta")},
        device_id=kitchen["device_id"],
        language="en",
    )
    await intent.async_handle(
        hass,
        "test",
        INTENT_SET,
        {"time": slot("10:01"), "name": slot("tea")},
        device_id=kitchen["device_id"],
        language="en",
    )
    freezer.move_to(START + timedelta(minutes=1, seconds=1))
    async_fire_time_changed(hass, START + timedelta(minutes=1, seconds=1))
    await hass.async_block_till_done()

    events = ready.snapshot()["events"]  # newest first
    kinds = [e["kind"] for e in events]
    assert "timer_started" in kinds
    assert "timer_cancelled" in kinds
    assert "rings" in kinds
    first = next(e for e in events if e["kind"] == "timer_started")
    assert first["label"] == "pasta"
    assert first["seconds"] == 300
    assert first["where"].startswith("Voice PE Kitchen")
    ring = next(e for e in events if e["kind"] == "rings")
    assert ring["label"] == "tea"
    assert ring["where"].startswith("Voice PE Kitchen")
    assert kinds.index("rings") < kinds.index("timer_cancelled") < kinds.index("timer_started")


async def test_the_log_is_bounded(hass: HomeAssistant, ready) -> None:
    for i in range(MAX_EVENT_LOG + 25):
        ready.log_event("rings", label=str(i))
    events = ready.snapshot()["events"]
    assert len(events) == MAX_EVENT_LOG
    assert events[0]["label"] == str(MAX_EVENT_LOG + 24)


async def test_timers_over_the_websocket(
    hass: HomeAssistant, ready, satellites, hass_ws_client
) -> None:
    kitchen = satellites["kitchen"]["device_id"]
    async_register_timer_handler(hass, kitchen, _handler)
    await _start_timer(hass, kitchen, minutes=5, name="pasta")
    (timer_id,) = hass.data[TIMER_DATA].timers
    client = await hass_ws_client(hass)

    async def call(action: str, **extra):
        await client.send_json_auto_id(
            {"type": f"{DOMAIN}/timer", "timer_id": timer_id, "action": action, **extra}
        )
        return await client.receive_json()

    assert (await call("pause"))["success"]
    assert not hass.data[TIMER_DATA].timers[timer_id].is_active
    assert (await call("resume"))["success"]
    assert hass.data[TIMER_DATA].timers[timer_id].is_active
    assert (await call("add", seconds=60))["success"]
    assert 355 <= hass.data[TIMER_DATA].timers[timer_id].seconds_left <= 360
    assert (await call("cancel"))["success"]
    assert not hass.data[TIMER_DATA].timers
    gone = await call("cancel")
    assert not gone["success"]
    assert gone["error"]["code"] == "not_found"


async def test_the_websocket_pushes_timer_changes(
    hass: HomeAssistant, ready, satellites, hass_ws_client
) -> None:
    kitchen = satellites["kitchen"]["device_id"]
    async_register_timer_handler(hass, kitchen, _handler)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/subscribe"})
    assert (await client.receive_json())["success"]
    await client.receive_json()  # the first snapshot

    await _start_timer(hass, kitchen, minutes=2, name="eggs")
    seen = []
    for _ in range(6):
        message = await client.receive_json()
        seen.append(message["event"])
        if any(t["name"] == "eggs" for t in message["event"]["timers"]["running"]):
            break
    assert any(t["name"] == "eggs" for t in seen[-1]["timers"]["running"])


def dt_ends(timer: dict) -> datetime:
    return datetime.fromisoformat(timer["ends_at"]).astimezone(TZ)
