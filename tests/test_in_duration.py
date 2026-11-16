"""Reminders and alarms "in <duration>": the set tool's in_* slots and HassStartTimer's
delayed commands that ask for a reminder or a wake-up."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.voice_alarms.const import INTENT_SET, KIND_ALARM, KIND_REMINDER
from custom_components.voice_alarms.intents import ReminderTimerIntent, reminder_from_command
from homeassistant.components import conversation
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util

from .conftest import slot

TZ = ZoneInfo("Europe/Zurich")
START = datetime(2026, 9, 7, 10, 0, tzinfo=TZ)  # Monday


@pytest.fixture
async def ready(hass: HomeAssistant, freezer: FrozenDateTimeFactory, setup_integration, manager):
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to(START)
    return manager


async def handle(
    hass: HomeAssistant, intent_type: str, device_id: str | None, language: str = "en", **slots
) -> intent.IntentResponse:
    return await intent.async_handle(
        hass,
        "test",
        intent_type,
        {k: slot(v) for k, v in slots.items()},
        device_id=device_id,
        language=language,
    )


def speech(response: intent.IntentResponse) -> str:
    return response.speech["plain"]["speech"]


def ring_at(manager, response: intent.IntentResponse) -> datetime:
    alarm = manager.get_alarm(response.speech_slots["alarm_id"])
    return alarm.next_pending(dt_util.utcnow()).astimezone(TZ)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("erinnere mich an die Wäsche", (KIND_REMINDER, "die Wäsche")),
        ("Erinner mich daran, die Wäsche hochzuholen.", (KIND_REMINDER, "die Wäsche hochzuholen")),
        (
            "erinnere mich bitte, dass der Kuchen fertig ist",
            (KIND_REMINDER, "der Kuchen fertig ist"),
        ),
        ("bitte erinnere uns an den Müll", (KIND_REMINDER, "den Müll")),
        ("erinnere mich", (KIND_REMINDER, None)),
        ("remind me to call mom", (KIND_REMINDER, "call mom")),
        ("Remind me about the laundry!", (KIND_REMINDER, "the laundry")),
        ("wecke mich", (KIND_ALARM, None)),
        ("weck mich auf", (KIND_ALARM, None)),
        ("wake me up", (KIND_ALARM, None)),
        ("weck mich bitte auf", (KIND_ALARM, None)),
        ("erinnere uns ans Essen", (KIND_REMINDER, "Essen")),
        ("weck mich und mach das Licht an", None),
        ("wake me up and turn on the lights", None),
        ("schalte das Licht aus", None),
        ("turn off the lights", None),
        ("erinnerung abspielen", None),
        ("weckt die Kinder", None),
    ],
)
def test_reminder_from_command(command: str, expected) -> None:
    assert reminder_from_command(command) == expected


async def test_set_in_hours_is_a_one_time_reminder(hass: HomeAssistant, ready, satellites) -> None:
    response = await handle(
        hass,
        INTENT_SET,
        satellites["kitchen"]["device_id"],
        in_hours=20,
        message="fetch the laundry",
    )
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    alarm = ready.get_alarm(response.speech_slots["alarm_id"])
    assert alarm.kind == KIND_REMINDER
    assert alarm.message == "fetch the laundry"
    assert not alarm.is_recurring
    assert ring_at(ready, response) == START + timedelta(hours=20)
    assert speech(response) == "Set: reminder tomorrow at 06:00 (fetch the laundry)."


async def test_set_in_rounds_up_to_the_minute(
    hass: HomeAssistant, ready, satellites, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START + timedelta(seconds=20))
    kitchen = satellites["kitchen"]["device_id"]
    response = await handle(hass, INTENT_SET, kitchen, in_minutes=1, in_seconds=30)
    # 10:00:20 + 1:30 = 10:01:50 -> 10:02, never early
    assert ring_at(ready, response) == START + timedelta(minutes=2)
    assert ready.get_alarm(response.speech_slots["alarm_id"]).kind == KIND_ALARM
    response = await handle(hass, INTENT_SET, kitchen, in_seconds=2)
    assert ring_at(ready, response) == START + timedelta(minutes=1)


@pytest.mark.parametrize(
    ("slots", "error"),
    [
        ({"in_hours": 2, "time": "07:00"}, "either a clock time or how long from now"),
        ({"in_hours": 2, "weekdays": "weekdays"}, "rings once"),
        ({"in_hours": 2, "date": "tomorrow"}, "rings once"),
        ({}, "Say when"),
        ({"in_hours": 9000}, "at most 366 days"),
        ({"in_minutes": -5}, "must not be negative"),
        ({"in_minutes": "soon"}, "is not a number"),
    ],
)
async def test_set_in_errors(hass: HomeAssistant, ready, satellites, slots, error) -> None:
    response = await handle(hass, INTENT_SET, satellites["kitchen"]["device_id"], **slots)
    assert response.response_type == intent.IntentResponseType.ERROR
    assert error in speech(response)
    assert not ready.alarms


async def test_set_in_across_the_night_the_clocks_go_back(
    hass: HomeAssistant, ready, satellites, freezer: FrozenDateTimeFactory
) -> None:
    # 25 October 2026: 03:00 CEST becomes 02:00 CET, so 02:30 comes twice.
    freezer.move_to(datetime(2026, 10, 25, 0, 30, tzinfo=TZ))
    kitchen = satellites["kitchen"]["device_id"]
    first = await handle(hass, INTENT_SET, kitchen, in_hours=2)
    second = await handle(hass, INTENT_SET, kitchen, in_hours=3)
    # Timestamps: Python never calls an ambiguous local time equal to one in another zone.
    first_utc = datetime(2026, 10, 25, 0, 30, tzinfo=dt_util.UTC).timestamp()
    second_utc = datetime(2026, 10, 25, 1, 30, tzinfo=dt_util.UTC).timestamp()
    assert ring_at(ready, first).timestamp() == first_utc
    assert ring_at(ready, second).timestamp() == second_utc
    # ...and still the later one after a restart.
    alarm = ready.get_alarm(second.speech_slots["alarm_id"])
    restored = type(alarm).from_dict(alarm.to_dict())
    assert restored.next_pending(dt_util.utcnow()).timestamp() == second_utc
    assert "fold" not in ready.get_alarm(first.speech_slots["alarm_id"]).to_dict()


async def test_timer_command_to_remind_becomes_a_reminder(
    hass: HomeAssistant, ready, satellites
) -> None:
    response = await handle(
        hass,
        intent.INTENT_START_TIMER,
        satellites["kitchen"]["device_id"],
        language="de",
        hours=20,
        conversation_command="erinnere mich an die Wäsche",
    )
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    assert speech(response) == "Gestellt: Erinnerung morgen um 06:00 (die Wäsche)."
    alarm = ready.get_alarm(response.speech_slots["alarm_id"])
    assert alarm.kind == KIND_REMINDER
    assert alarm.message == "die Wäsche"
    assert alarm.target == satellites["kitchen"]["satellite"]
    assert ring_at(ready, response) == START + timedelta(hours=20)
    assert not hass.data[TIMER_DATA].timers


async def test_timer_command_to_wake_becomes_an_alarm(
    hass: HomeAssistant, ready, satellites
) -> None:
    response = await handle(
        hass,
        intent.INTENT_START_TIMER,
        satellites["bedroom"]["device_id"],
        language="de",
        hours=8,
        minutes=30,
        conversation_command="wecke mich",
    )
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    alarm = ready.get_alarm(response.speech_slots["alarm_id"])
    assert alarm.kind == KIND_ALARM
    assert alarm.target == satellites["bedroom"]["satellite"]
    assert ring_at(ready, response) == START + timedelta(hours=8, minutes=30)


async def test_timer_command_to_remind_without_words_still_speaks(
    hass: HomeAssistant, ready, satellites
) -> None:
    response = await handle(
        hass,
        intent.INTENT_START_TIMER,
        satellites["kitchen"]["device_id"],
        minutes=20,
        conversation_command="remind me",
    )
    alarm = ready.get_alarm(response.speech_slots["alarm_id"])
    assert alarm.kind == KIND_REMINDER
    assert alarm.message is None


async def test_other_timers_stay_home_assistants(hass: HomeAssistant, ready, satellites) -> None:
    kitchen = satellites["kitchen"]["device_id"]
    # A plain timer: Home Assistant's handler (no satellite handler: not supported).
    with pytest.raises(intent.IntentHandleError) as err:
        await handle(hass, intent.INTENT_START_TIMER, kitchen, minutes=5)
    assert err.value.response_key == "no_timer_support"
    # A delayed command that is no reminder: Home Assistant checks it.
    with pytest.raises(intent.IntentHandleError) as err:
        await handle(
            hass,
            intent.INTENT_START_TIMER,
            kitchen,
            minutes=5,
            conversation_command="mache etwas völlig unbekanntes",
        )
    assert err.value.response_key == "no_timer_command"
    assert not ready.alarms
    handler = hass.data[intent.DATA_KEY][intent.INTENT_START_TIMER]
    assert isinstance(handler, ReminderTimerIntent)


async def test_german_sentence_through_home_assistants_own_recognition(
    hass: HomeAssistant, ready, satellites
) -> None:
    """The sentence Home Assistant matches locally as a delayed command, end to end."""
    result = await conversation.async_converse(
        hass,
        "Erinnere mich an die Wäsche in 20 Stunden",
        None,
        Context(),
        language="de",
        device_id=satellites["kitchen"]["device_id"],
    )
    assert result.response.response_type == intent.IntentResponseType.ACTION_DONE
    assert (
        result.response.speech["plain"]["speech"]
        == "Gestellt: Erinnerung morgen um 06:00 (die Wäsche)."
    )
    (alarm,) = ready.alarms.values()
    assert alarm.message == "die Wäsche"
    assert alarm.next_pending(dt_util.utcnow()) == START + timedelta(hours=20)


async def test_rings_in_the_second_of_two_equal_hours(
    hass: HomeAssistant, ready, satellites, freezer: FrozenDateTimeFactory, announce_calls
) -> None:
    """The scheduler's wake-up for the later 02:30 is not the earlier one.

    Woken an hour early, it found nothing due and asked to be woken at that same past
    instant again: a busy loop for the whole hour.
    """
    freezer.move_to(datetime(2026, 10, 24, 23, 0, tzinfo=dt_util.UTC))  # 01:00 CEST
    kitchen = satellites["kitchen"]["device_id"]
    await handle(hass, INTENT_SET, kitchen, in_hours=2, in_minutes=30, message="tea")
    runs = 0
    process = ready._process

    async def counted() -> None:
        nonlocal runs
        runs += 1
        await process()

    ready._process = counted

    async def advance(to: datetime) -> None:
        freezer.move_to(to)
        async_fire_time_changed(hass, to)
        await hass.async_block_till_done()

    await advance(datetime(2026, 10, 25, 0, 31, tzinfo=dt_util.UTC))  # the first 02:31
    for _ in range(100):
        await asyncio.sleep(0)
    assert not announce_calls
    assert runs == 0
    await advance(datetime(2026, 10, 25, 1, 30, 1, tzinfo=dt_util.UTC))  # the second 02:30
    for _ in range(200):
        if announce_calls:
            break
        await asyncio.sleep(0)
    assert announce_calls
