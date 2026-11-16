"""Assist timers that survive a restart of Home Assistant and a satellite going offline.

Home Assistant keeps the timers of its timer intents ("set a timer for 5 minutes") in
memory only (``homeassistant.components.intent.timers.TimerManager``): a restart loses
every running timer, and a timer that ends while its satellite is not connected is
gone too (ESPHome's handler raises, the timer was already removed). This module puts a
subclass of that manager in its place (``hass.data[TIMER_DATA]``; every user of it
looks it up there on each call), so that Home Assistant's own sentences, responses,
LLM tools and the satellites' timer handling all stay as they are, and adds:

- every change is saved; after a restart the timers run on from their saved end
  (wall clock), paused ones stay paused;
- a timer that ended while Home Assistant was down, or whose satellite could not be
  told, is delivered once it can be: when the satellite's handler registers or its
  entity becomes available again, and every few seconds meanwhile, for as long as the
  missed-alarm grace allows; after that it is dropped with a warning;
- a satellite that was offline is told the state of its timers again when it is back.

Taking over is undone on unload: the timers go back to the manager that was there.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.assist_satellite import DOMAIN as SATELLITE_DOMAIN
from homeassistant.components.intent import TimerEventType, TimerInfo
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.components.intent.timers import TimerHandler, TimerManager
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.timers"
STORAGE_VERSION = 1
# A timer is saved this long after a change: a crash right after setting one is the
# only thing it does not cover; Home Assistant's own stop writes it at once.
SAVE_DELAY = 1.0
# How often a finished timer that could not be delivered is tried again.
RETRY_INTERVAL = timedelta(seconds=5)


def _exact_left(timer: TimerInfo, now_ns: int) -> float:
    """Seconds left on an active timer, without the rounding of ``seconds_left``."""
    if not timer.is_active:
        return float(timer.seconds)
    return max(0.0, timer.seconds - (now_ns - timer.updated_at) / 1e9)


@dataclass
class _Finished:
    """A timer that ended but whose satellite (or command) has not had it yet."""

    timer: TimerInfo
    at: datetime  # when it ended, UTC


class PersistentTimerManager(TimerManager):
    """Home Assistant's timer manager, saved to disk and delivered reliably."""

    def __init__(
        self,
        hass: HomeAssistant,
        previous: TimerManager,
        grace: Callable[[], timedelta],
    ) -> None:
        super().__init__(hass)
        self.previous = previous
        # The same dict object: handlers registered with the previous manager stay
        # registered, and the unregister callbacks they hold pop from it.
        self.handlers = previous.handlers
        self._grace = grace
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.finished: dict[str, _Finished] = {}
        # Cancellations a satellite could not be told about, sent when it is back.
        self._unsent_cancels: dict[str, dict[str, TimerInfo]] = {}
        self._unsub: list[CALLBACK_TYPE] = []
        self._started_unsub: CALLBACK_TYPE | None = None
        self._retry_unsub: CALLBACK_TYPE | None = None
        self._active = False

    # ------------------------------------------------------------ lifecycle

    async def async_take_over(self) -> None:
        """Load saved timers, adopt the running ones and replace the previous manager."""
        stored = await self._store.async_load() or {}
        now_ns = time.monotonic_ns()
        now = dt_util.utcnow()

        # Timers already running (set before this integration was loaded, or handed
        # back by an unload for a reload) keep their TimerInfo and go on ticking.
        for timer_id, timer in list(self.previous.timers.items()):
            if task := self.previous.timer_tasks.pop(timer_id, None):
                task.cancel()
            self.previous.timers.pop(timer_id)
            self.timers[timer_id] = timer
            if timer.is_active:
                self._schedule(timer, _exact_left(timer, now_ns))

        for data in stored.get("timers", []):
            if data.get("id") in self.timers:
                continue
            try:
                self._restore(data, now, now_ns)
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception("Dropping a saved timer that cannot be read: %s", data)
        for data in stored.get("finished", []):
            try:
                timer = self._timer_from_dict(data, 0, now_ns)
                timer.finish()
                at = dt_util.parse_datetime(data["finished_at"])
                if at is None:
                    raise ValueError("finished_at")
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception("Dropping a saved finished timer that cannot be read: %s", data)
                continue
            self.finished.setdefault(timer.id, _Finished(timer, dt_util.as_utc(at)))
        for data in stored.get("unsent_cancels", []):
            try:
                timer = self._timer_from_dict(data, 0, now_ns)
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception("Dropping a saved cancellation that cannot be read: %s", data)
                continue
            timer.cancel()
            if timer.device_id:
                self._unsent_cancels.setdefault(timer.device_id, {})[timer.id] = timer

        self.hass.data[TIMER_DATA] = self
        self._active = True
        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed))
        if not self.hass.is_running:
            self._started_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._started
            )
        for device_id in {t.device_id for t in self.timers.values() if t.device_id}:
            self._resync(device_id)
        self._deliver_all()
        self._save()
        _LOGGER.debug(
            "Timers taken over: %d running or paused, %d to deliver",
            len(self.timers),
            len(self.finished),
        )

    async def async_hand_back(self) -> None:
        """Give the timers back to the previous manager (the integration unloads).

        The running and paused timers are Home Assistant's again: whatever happens to
        them meanwhile (cancelled, rung) is the truth when this integration takes over
        once more, so they are not kept in the store, which would bring a timer
        cancelled meanwhile back. What only this integration knows stays saved:
        finished timers not delivered yet and cancellations not told yet.
        The hand-over itself has no await in it, so no timer ends half-way.
        """
        if not self._active:
            return
        self._active = False
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._started_unsub is not None:
            self._started_unsub()
            self._started_unsub = None
        self._stop_retry()
        now_ns = time.monotonic_ns()
        for timer_id, timer in list(self.timers.items()):
            if task := self.timer_tasks.pop(timer_id, None):
                task.cancel()
            self.previous.timers[timer_id] = timer
            if timer.is_active:
                self.previous.timer_tasks[timer_id] = self.hass.async_create_background_task(
                    self.previous._wait_for_timer(  # noqa: SLF001
                        timer_id, _exact_left(timer, now_ns), timer.updated_at
                    ),
                    name=f"Timer {timer_id}",
                )
        self.timers.clear()
        if self.hass.data.get(TIMER_DATA) is self:
            self.hass.data[TIMER_DATA] = self.previous
        await self._store.async_save(self._data())

    # ------------------------------------------------------- timer manager

    def start_timer(self, *args: Any, **kwargs: Any) -> str:
        """Start a timer and save it."""
        timer_id = super().start_timer(*args, **kwargs)
        self._save()
        return timer_id

    def cancel_timer(self, timer_id: str) -> None:
        """Cancel a timer and save."""
        super().cancel_timer(timer_id)
        self._save()

    def add_time(self, timer_id: str, seconds: int) -> None:
        """Add (or with a negative number remove) time and save."""
        super().add_time(timer_id, seconds)
        self._save()

    def pause_timer(self, timer_id: str) -> None:
        """Pause a timer and save."""
        super().pause_timer(timer_id)
        self._save()

    def unpause_timer(self, timer_id: str) -> None:
        """Unpause a timer and save."""
        super().unpause_timer(timer_id)
        self._save()

    def register_handler(self, device_id: str, handler: TimerHandler) -> Callable[[], None]:
        """Register a satellite and tell it the timers it has here."""
        unregister = super().register_handler(device_id, handler)
        # After the satellite's own setup has finished (this runs inside it).
        self.hass.loop.call_soon(self._resync, device_id)
        return unregister

    def _timer_finished(self, timer_id: str) -> None:
        """A timer ran out: deliver it, or keep it until it can be."""
        timer = self.timers.pop(timer_id, None)
        self.timer_tasks.pop(timer_id, None)
        if timer is None:
            return
        timer.finish()
        self.finished[timer_id] = _Finished(timer, dt_util.utcnow())
        _LOGGER.debug(
            "Timer finished: id=%s, name=%s, device_id=%s", timer_id, timer.name, timer.device_id
        )
        self._deliver(timer_id)
        self._save()

    # ------------------------------------------------------------- delivery

    def _reachable(self, device_id: str) -> bool:
        """False while every assist satellite entity of the device is unavailable.

        Handlers do not all fail when their satellite is gone: Wyoming's returns
        quietly without a client, so its answer alone cannot be trusted.
        """
        entities = [
            entry.entity_id
            for entry in er.async_entries_for_device(er.async_get(self.hass), device_id)
            if entry.domain == SATELLITE_DOMAIN
        ]
        if not entities:
            return True  # nothing to tell by: trust the handler
        return any(
            (state := self.hass.states.get(entity_id)) is not None
            and state.state != STATE_UNAVAILABLE
            for entity_id in entities
        )

    def _notify(self, device_id: str | None, event: TimerEventType, timer: TimerInfo) -> bool:
        """Call a satellite's timer handler; False if it is missing, offline or failed."""
        if device_id is None or (handler := self.handlers.get(device_id)) is None:
            return False
        if not self._reachable(device_id):
            return False
        try:
            handler(event, timer)
        except Exception as err:  # noqa: BLE001 - any satellite failure, e.g. not connected
            _LOGGER.debug("Timer %s not delivered to %s: %s", event, device_id, err)
            return False
        return True

    @callback
    def _deliver(self, timer_id: str) -> None:
        """Deliver one finished timer if possible, drop it once it is too late."""
        if (pending := self.finished.get(timer_id)) is None:
            return
        timer = pending.timer
        late = dt_util.utcnow() - pending.at
        if late > self._grace():
            del self.finished[timer_id]
            _LOGGER.warning(
                "Timer '%s' ended %s ago and could not be rung on its satellite in time; dropped",
                timer.name or _length(timer),
                _ago(late),
            )
            self._save()
            return
        if timer.conversation_command:
            if not self.hass.is_running:
                return  # the conversation agents are not loaded yet
            del self.finished[timer_id]
            self.hass.async_create_background_task(
                self._async_run_conversation_command(timer), "timer assist command"
            )
        elif self._notify(timer.device_id, TimerEventType.FINISHED, timer):
            del self.finished[timer_id]
            if late > RETRY_INTERVAL:
                _LOGGER.info("Timer '%s' rung %s late", timer.name or _length(timer), _ago(late))
        else:
            self._start_retry()
            return
        self._save()

    @callback
    def _deliver_all(self, device_id: str | None = None) -> None:
        for timer_id, pending in list(self.finished.items()):
            if device_id is None or pending.timer.device_id == device_id:
                self._deliver(timer_id)
        if not self.finished:
            self._stop_retry()
        else:
            self._start_retry()

    @callback
    def _resync(self, device_id: str) -> None:
        """Tell a satellite (again) what runs on it and what it missed."""
        if not self._active or device_id not in self.handlers:
            return
        for timer in list(self.timers.values()):
            if timer.device_id == device_id and not timer.conversation_command:
                self._notify(device_id, TimerEventType.UPDATED, timer)
        for timer_id, timer in list(self._unsent_cancels.get(device_id, {}).items()):
            if self._notify(device_id, TimerEventType.CANCELLED, timer):
                self._unsent_cancels[device_id].pop(timer_id, None)
                self._save()
        if not self._unsent_cancels.get(device_id):
            self._unsent_cancels.pop(device_id, None)
        self._deliver_all(device_id)

    @callback
    def _start_retry(self) -> None:
        if self._retry_unsub is None and self._active:
            self._retry_unsub = async_track_time_interval(
                self.hass, self._retry, RETRY_INTERVAL, name="voice_alarms timer delivery"
            )

    @callback
    def _stop_retry(self) -> None:
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

    @callback
    def _retry(self, _now: datetime) -> None:
        self._deliver_all()

    @callback
    def _started(self, _event: Event) -> None:
        self._started_unsub = None  # fired: removing it now would be an error
        self._deliver_all()

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        """A satellite entity came back: its device gets its timers again."""
        entity_id = event.data["entity_id"]
        if not entity_id.startswith(f"{SATELLITE_DOMAIN}."):
            return
        new = event.data["new_state"]
        old = event.data["old_state"]
        if new is None or new.state == STATE_UNAVAILABLE:
            return
        if old is not None and old.state != STATE_UNAVAILABLE:
            return
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is not None and entry.device_id:
            self._resync(entry.device_id)

    # ------------------------------------------------------- handler safety

    def _call_handler(self, device_id: str, event: TimerEventType, timer: TimerInfo) -> None:
        """What the base class's notifications go through (see ``handlers`` below)."""
        if not self._notify(device_id, event, timer) and device_id in self.handlers:
            if event is TimerEventType.CANCELLED:
                self._unsent_cancels.setdefault(device_id, {})[timer.id] = timer
                self._save()
            # STARTED/UPDATED: the state is sent again when the satellite is back.

    # ----------------------------------------------------------- persistence

    def _schedule(self, timer: TimerInfo, seconds: float) -> None:
        self.timer_tasks[timer.id] = self.hass.async_create_background_task(
            self._wait_for_timer(timer.id, seconds, timer.updated_at),  # type: ignore[arg-type]
            name=f"Timer {timer.id}",
        )

    def _timer_from_dict(self, data: dict[str, Any], seconds: int, now_ns: int) -> TimerInfo:
        timer = TimerInfo(
            id=str(data["id"]),
            name=data.get("name"),
            seconds=seconds,
            device_id=data.get("device_id"),
            start_hours=data.get("start_hours"),
            start_minutes=data.get("start_minutes"),
            start_seconds=data.get("start_seconds"),
            created_at=now_ns,
            updated_at=now_ns,
            language=str(data["language"]),
            is_active=bool(data.get("is_active", True)),
            area_id=data.get("area_id"),
            area_name=data.get("area_name"),
            floor_id=data.get("floor_id"),
            conversation_command=data.get("conversation_command"),
            conversation_agent_id=data.get("conversation_agent_id"),
        )
        timer._created_seconds = max(int(data.get("created_seconds") or 0), seconds)  # noqa: SLF001
        return timer

    def _restore(self, data: dict[str, Any], now: datetime, now_ns: int) -> None:
        if not data.get("is_active", True):
            timer = self._timer_from_dict(data, int(data["seconds"]), now_ns)
            self.timers[timer.id] = timer
            return
        ends = dt_util.parse_datetime(data["ends_at"])
        if ends is None:
            raise ValueError("ends_at")
        left = (dt_util.as_utc(ends) - now).total_seconds()
        if left <= 0:
            timer = self._timer_from_dict(data, 0, now_ns)
            timer.finish()
            self.finished[timer.id] = _Finished(timer, dt_util.as_utc(ends))
            return
        # Whole seconds on the timer (HA's model), the fraction already "run".
        whole = math.ceil(left)
        timer = self._timer_from_dict(data, whole, now_ns - int((whole - left) * 1e9))
        self.timers[timer.id] = timer
        self._schedule(timer, left)

    @callback
    def _save(self) -> None:
        if self._active:
            self._store.async_delay_save(self._data, SAVE_DELAY)

    @callback
    def _data(self) -> dict[str, Any]:
        now_ns = time.monotonic_ns()
        now = dt_util.utcnow()
        timers = []
        for timer in self.timers.values():
            item = _timer_to_dict(timer)
            if timer.is_active:
                item["ends_at"] = (now + timedelta(seconds=_exact_left(timer, now_ns))).isoformat()
            timers.append(item)
        finished = [
            {**_timer_to_dict(p.timer), "finished_at": p.at.isoformat()}
            for p in self.finished.values()
        ]
        unsent = [
            _timer_to_dict(timer)
            for cancels in self._unsent_cancels.values()
            for timer in cancels.values()
        ]
        return {"timers": timers, "finished": finished, "unsent_cancels": unsent}


class _SafeHandlers(dict[str, TimerHandler]):
    """The handlers dict, whose lookups by the base class cannot raise into it.

    ``TimerManager`` calls ``self.handlers[device_id](event, timer)`` right after
    changing a timer; a satellite that is offline raises there, which would turn a
    timer that was set or cancelled into a failed intent.  Item lookups hand out a
    wrapper that reports to the manager instead; ``get`` gives the real handler.
    """

    manager: PersistentTimerManager | None = None

    def __getitem__(self, device_id: str) -> TimerHandler:
        handler = super().__getitem__(device_id)
        manager = self.manager
        if manager is None:
            return handler

        def call(event: TimerEventType, timer: TimerInfo) -> None:
            manager._call_handler(device_id, event, timer)  # noqa: SLF001

        return call


def _timer_to_dict(timer: TimerInfo) -> dict[str, Any]:
    return {
        "id": timer.id,
        "name": timer.name,
        "seconds": timer.seconds,
        "is_active": timer.is_active,
        "created_seconds": timer.created_seconds,
        "start_hours": timer.start_hours,
        "start_minutes": timer.start_minutes,
        "start_seconds": timer.start_seconds,
        "device_id": timer.device_id,
        "language": timer.language,
        "area_id": timer.area_id,
        "area_name": timer.area_name,
        "floor_id": timer.floor_id,
        "conversation_command": timer.conversation_command,
        "conversation_agent_id": timer.conversation_agent_id,
    }


def _length(timer: TimerInfo) -> str:
    hours, rest = divmod(timer.created_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _ago(delta: timedelta) -> str:
    return f"{int(delta.total_seconds())} s"


async def async_take_over_timers(
    hass: HomeAssistant, grace: Callable[[], timedelta]
) -> PersistentTimerManager:
    """Put a persistent timer manager in place of Home Assistant's."""
    previous = hass.data[TIMER_DATA]
    if isinstance(previous, PersistentTimerManager):  # left behind by a failed unload
        await previous.async_hand_back()
        previous = previous.previous
    if isinstance(previous.handlers, _SafeHandlers):
        # A reload: the same dict again, which unregister callbacks handed out by the
        # manager before pop from.
        safe = previous.handlers
    else:
        safe = _SafeHandlers(previous.handlers)
        # Handlers registered from now on (with either manager) land in the safe
        # dict; unregister callbacks of earlier ones look ``self.handlers`` up.
        previous.handlers = safe
    manager = PersistentTimerManager(hass, previous, grace)
    safe.manager = manager
    await manager.async_take_over()
    return manager


async def async_hand_back_timers(manager: PersistentTimerManager) -> None:
    """Undo ``async_take_over_timers``."""
    await manager.async_hand_back()
    if isinstance(manager.handlers, _SafeHandlers):
        manager.handlers.manager = None


async def async_remove_timer_store(hass: HomeAssistant) -> None:
    """Delete the saved timers (the integration is removed)."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()
