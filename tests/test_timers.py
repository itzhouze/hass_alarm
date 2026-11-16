"""Home Assistant's timers kept across restarts and offline satellites (timers.py)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.voice_alarms.const import DOMAIN
from custom_components.voice_alarms.timers import (
    RETRY_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
    PersistentTimerManager,
)
from homeassistant.components.assist_satellite import DOMAIN as SAT_DOMAIN
from homeassistant.components.intent import (
    TimerEventType,
    TimerInfo,
    async_register_timer_handler,
)
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.components.intent.timers import StartTimerIntentHandler, TimerManager
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import slot


class Satellite:
    """A satellite's timer handler that records what it is told, or fails like an offline one."""

    def __init__(self) -> None:
        self.events: list[tuple[TimerEventType, str, int, bool]] = []
        self.offline = False

    def __call__(self, event: TimerEventType, timer: TimerInfo) -> None:
        if self.offline:
            raise ConnectionError("Not connected")
        self.events.append((event, timer.id, timer.seconds_left, timer.is_active))

    def kinds(self) -> list[TimerEventType]:
        return [event for event, *_ in self.events]


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Voice Alarms", data={}, options={}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, announce_calls: list[ServiceCall]) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    async def record(call: ServiceCall) -> None:
        announce_calls.append(call)

    hass.services.async_register(SAT_DOMAIN, "announce", record)


@pytest.fixture(autouse=True)
def save_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Save without the delay: the store's delayed write runs on the loop's real clock,
    which async_fire_time_changed does not move (a write pushed later by another one
    would reschedule itself for ever)."""
    monkeypatch.setattr("custom_components.voice_alarms.timers.SAVE_DELAY", 0)


async def _flush(hass: HomeAssistant) -> None:
    """Let the (immediate) save of the store happen."""
    for _ in range(3):
        await asyncio.sleep(0.01)
        await hass.async_block_till_done()


def _saved(hass_storage: dict[str, Any]) -> dict[str, Any]:
    return hass_storage[STORAGE_KEY]["data"]


def _stored_timer(device_id: str, **fields: Any) -> dict[str, Any]:
    data = {
        "id": "01TIMER",
        "name": None,
        "seconds": 300,
        "is_active": True,
        "created_seconds": 300,
        "start_hours": None,
        "start_minutes": 5,
        "start_seconds": None,
        "device_id": device_id,
        "language": "en",
        "area_id": None,
        "area_name": None,
        "floor_id": None,
        "conversation_command": None,
        "conversation_agent_id": None,
    }
    data.update(fields)
    return data


def _store(hass_storage: dict[str, Any], timers=(), finished=()) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"timers": list(timers), "finished": list(finished)},
    }


async def _start(hass: HomeAssistant, device_id: str, **slots: Any) -> intent.IntentResponse:
    return await intent.async_handle(
        hass,
        "test",
        intent.INTENT_START_TIMER,
        {k: slot(v) for k, v in slots.items()},
        device_id=device_id,
        language="en",
    )


async def _intent(hass: HomeAssistant, intent_type: str, device_id: str, **slots: Any):
    return await intent.async_handle(
        hass,
        "test",
        intent_type,
        {k: slot(v) for k, v in slots.items()},
        device_id=device_id,
        language="en",
    )


async def _until(predicate, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_takes_over_with_home_assistants_behaviour(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    manager = hass.data[TIMER_DATA]
    assert isinstance(manager, PersistentTimerManager)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)

    await _start(hass, kitchen, minutes=5, name="pasta")
    assert sat.kinds() == [TimerEventType.STARTED]
    await _flush(hass)
    (saved,) = _saved(hass_storage)["timers"]
    assert saved["name"] == "pasta"
    assert saved["start_minutes"] == 5
    left = (dt_util.parse_datetime(saved["ends_at"]) - dt_util.utcnow()).total_seconds()
    assert 290 < left <= 300

    status = await _intent(hass, intent.INTENT_TIMER_STATUS, kitchen)
    (timer,) = status.speech_slots["timers"]
    assert timer["name"] == "pasta"
    assert 295 <= timer["total_seconds_left"] <= 300

    await _intent(hass, intent.INTENT_PAUSE_TIMER, kitchen)
    await _flush(hass)
    (saved,) = _saved(hass_storage)["timers"]
    assert saved["is_active"] is False
    assert "ends_at" not in saved

    await _intent(hass, intent.INTENT_UNPAUSE_TIMER, kitchen)
    await _intent(hass, intent.INTENT_INCREASE_TIMER, kitchen, minutes=1)
    await _flush(hass)
    (saved,) = _saved(hass_storage)["timers"]
    left = (dt_util.parse_datetime(saved["ends_at"]) - dt_util.utcnow()).total_seconds()
    assert 350 < left <= 360

    await _intent(hass, intent.INTENT_CANCEL_TIMER, kitchen)
    assert sat.kinds()[-1] == TimerEventType.CANCELLED
    await _flush(hass)
    assert _saved(hass_storage) == {"timers": [], "finished": [], "unsent_cancels": []}


async def test_a_timer_rings_and_is_forgotten(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, seconds=1)
    await _until(lambda: TimerEventType.FINISHED in sat.kinds())
    await _flush(hass)
    assert _saved(hass_storage) == {"timers": [], "finished": [], "unsent_cancels": []}
    assert not hass.data[TIMER_DATA].timer_tasks


async def test_timers_run_on_after_a_restart(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    kitchen = satellites["kitchen"]["device_id"]
    _store(
        hass_storage,
        timers=[
            _stored_timer(
                kitchen,
                id="running",
                name="pasta",
                ends_at=(dt_util.utcnow() + timedelta(seconds=1.5)).isoformat(),
            ),
            _stored_timer(kitchen, id="paused", seconds=120, is_active=False),
        ],
    )
    # The satellite registered before the integration was loaded (ESPHome came first).
    assert await async_setup_component(hass, "intent", {})
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _setup(hass, announce_calls)

    manager = hass.data[TIMER_DATA]
    assert manager.timers["running"].name == "pasta"
    assert manager.timers["running"].is_active
    assert manager.timers["running"].seconds_left <= 2
    assert manager.timers["running"].created_seconds == 300
    assert not manager.timers["paused"].is_active
    assert manager.timers["paused"].seconds_left == 120
    # The satellite is told again what runs on it.
    assert sorted((e, i) for e, i, *_ in sat.events) == [
        (TimerEventType.UPDATED, "paused"),
        (TimerEventType.UPDATED, "running"),
    ]

    await _until(lambda: TimerEventType.FINISHED in sat.kinds())
    assert sat.events[-1][1] == "running"
    assert "paused" in manager.timers


async def test_a_timer_that_ended_while_down_rings_within_the_grace(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage, caplog
) -> None:
    kitchen = satellites["kitchen"]["device_id"]
    now = dt_util.utcnow()
    _store(
        hass_storage,
        timers=[
            _stored_timer(kitchen, id="recent", ends_at=(now - timedelta(seconds=60)).isoformat()),
            _stored_timer(kitchen, id="old", ends_at=(now - timedelta(hours=1)).isoformat()),
        ],
    )
    await _setup(hass, announce_calls)
    manager = hass.data[TIMER_DATA]
    assert "old" not in manager.finished
    assert "dropped" in caplog.text
    assert "recent" in manager.finished

    # The satellite connects after Home Assistant has started.
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await hass.async_block_till_done()
    assert [(e, i) for e, i, *_ in sat.events] == [(TimerEventType.FINISHED, "recent")]
    assert not manager.finished
    await _flush(hass)
    assert _saved(hass_storage)["finished"] == []


async def test_finished_while_offline_rings_when_back(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, seconds=1, name="tea")
    sat.offline = True
    manager = hass.data[TIMER_DATA]
    await _until(lambda: manager.finished)
    # Kept on disk too, should Home Assistant restart meanwhile.
    await _flush(hass)
    (saved,) = _saved(hass_storage)["finished"]
    assert saved["name"] == "tea"

    # It comes back: its satellite entity becomes available again.
    sat.offline = False
    entity = satellites["kitchen"]["satellite"]
    hass.states.async_set(entity, STATE_UNAVAILABLE)
    hass.states.async_set(entity, "idle")
    await hass.async_block_till_done()
    assert sat.kinds()[-1] == TimerEventType.FINISHED
    assert not manager.finished


async def test_finished_while_offline_is_retried(
    hass: HomeAssistant, entry, satellites, announce_calls
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, seconds=1)
    sat.offline = True
    manager = hass.data[TIMER_DATA]
    await _until(lambda: manager.finished)
    sat.offline = False
    async_fire_time_changed(hass, dt_util.utcnow() + RETRY_INTERVAL + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert sat.kinds()[-1] == TimerEventType.FINISHED
    assert not manager.finished


async def test_cancel_while_offline_is_told_later(
    hass: HomeAssistant, entry, satellites, announce_calls
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, minutes=5)
    sat.offline = True
    # Cancelled from elsewhere while the satellite is away: not an error.
    response = await _intent(hass, intent.INTENT_CANCEL_TIMER, kitchen)
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    assert not hass.data[TIMER_DATA].timers

    sat.offline = False
    entity = satellites["kitchen"]["satellite"]
    hass.states.async_set(entity, STATE_UNAVAILABLE)
    hass.states.async_set(entity, "idle")
    await hass.async_block_till_done()
    assert sat.kinds() == [TimerEventType.STARTED, TimerEventType.CANCELLED]


async def test_unload_hands_back_and_reload_takes_over(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, minutes=1, name="eggs")
    (timer_id,) = hass.data[TIMER_DATA].timers

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    plain = hass.data[TIMER_DATA]
    assert type(plain) is TimerManager
    assert plain.timers[timer_id].name == "eggs"
    assert timer_id in plain.timer_tasks
    assert plain.is_timer_device(kitchen)
    handler = hass.data[intent.DATA_KEY][intent.INTENT_START_TIMER]
    assert type(handler) is StartTimerIntentHandler

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = hass.data[TIMER_DATA]
    assert isinstance(manager, PersistentTimerManager)
    assert 55 <= manager.timers[timer_id].seconds_left <= 60
    assert not plain.timers
    assert not plain.timer_tasks
    await _intent(hass, intent.INTENT_CANCEL_TIMER, kitchen, name="eggs")
    assert sat.kinds()[-1] == TimerEventType.CANCELLED


async def test_removing_the_integration_deletes_saved_timers(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    async_register_timer_handler(hass, kitchen, Satellite())
    await _start(hass, kitchen, minutes=5)
    await _flush(hass)
    assert STORAGE_KEY in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage
    # The timer itself runs on with Home Assistant's own manager.
    assert type(hass.data[TIMER_DATA]) is TimerManager
    assert hass.data[TIMER_DATA].timers


async def test_unreadable_saved_timer_is_skipped(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage, caplog
) -> None:
    kitchen = satellites["kitchen"]["device_id"]
    good = _stored_timer(
        kitchen, id="good", ends_at=(dt_util.utcnow() + timedelta(minutes=5)).isoformat()
    )
    _store(hass_storage, timers=[{"id": "bad"}, good])
    await _setup(hass, announce_calls)
    assert list(hass.data[TIMER_DATA].timers) == ["good"]
    assert "cannot be read" in caplog.text


async def test_a_timer_cancelled_while_unloaded_stays_cancelled(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    async_register_timer_handler(hass, kitchen, Satellite())
    await _start(hass, kitchen, minutes=5, name="eggs")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _intent(hass, intent.INTENT_CANCEL_TIMER, kitchen, name="eggs")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.data[TIMER_DATA].timers


async def test_unregister_after_a_reload_still_works(
    hass: HomeAssistant, entry, satellites, announce_calls
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    unregister = async_register_timer_handler(hass, kitchen, Satellite())
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    unregister()
    assert not hass.data[TIMER_DATA].is_timer_device(kitchen)


async def test_unload_after_start_logs_no_error(
    hass: HomeAssistant, entry, satellites, announce_calls, caplog
) -> None:
    hass.set_state(CoreState.not_running)
    await _setup(hass, announce_calls)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text


async def test_a_quiet_handler_of_an_unavailable_satellite_does_not_count(
    hass: HomeAssistant, entry, satellites, announce_calls
) -> None:
    """Wyoming's handler returns without a word when its satellite is not connected."""
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, seconds=1)
    entity = satellites["kitchen"]["satellite"]
    hass.states.async_set(entity, STATE_UNAVAILABLE)
    manager = hass.data[TIMER_DATA]
    await _until(lambda: manager.finished)
    assert TimerEventType.FINISHED not in sat.kinds()
    hass.states.async_set(entity, "idle")
    await hass.async_block_till_done()
    assert sat.kinds()[-1] == TimerEventType.FINISHED
    assert not manager.finished


async def test_an_untold_cancellation_survives_a_restart(
    hass: HomeAssistant, entry, satellites, announce_calls, hass_storage
) -> None:
    await _setup(hass, announce_calls)
    kitchen = satellites["kitchen"]["device_id"]
    sat = Satellite()
    async_register_timer_handler(hass, kitchen, sat)
    await _start(hass, kitchen, minutes=5)
    sat.offline = True
    await _intent(hass, intent.INTENT_CANCEL_TIMER, kitchen)
    await _flush(hass)
    (saved,) = _saved(hass_storage)["unsent_cancels"]
    assert saved["device_id"] == kitchen
