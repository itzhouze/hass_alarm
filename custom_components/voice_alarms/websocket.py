"""Websocket API used by the Voice Alarms panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.intent.const import TIMER_DATA
from homeassistant.components.intent.timers import TimerNotFoundError
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    DOMAIN,
    SIGNAL_ALARMS_UPDATED,
    SIGNAL_CONFIG_UPDATED,
    SIGNAL_LOG_UPDATED,
    SIGNAL_RINGING_UPDATED,
    SIGNAL_TARGETS_UPDATED,
    SIGNAL_TIMERS_UPDATED,
)
from .manager import AlarmManager
from .timers import PersistentTimerManager

_UNIT = vol.All(vol.Coerce(float), vol.Range(min=0, max=1))


@callback
def async_register_websocket(hass: HomeAssistant, get_manager) -> None:
    """Register websocket commands.

    ``get_manager`` returns the current AlarmManager (or None when unloaded) so
    that the commands survive config entry reloads.
    """

    @websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
    @callback
    def subscribe(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
    ) -> None:
        manager: AlarmManager | None = get_manager()
        if manager is None:
            connection.send_error(msg["id"], "not_loaded", "Voice Alarms is not loaded")
            return

        @callback
        def push() -> None:
            current = get_manager()
            if current is None:
                return
            connection.send_message(websocket_api.event_message(msg["id"], current.snapshot()))

        unsubs = [
            async_dispatcher_connect(hass, SIGNAL_ALARMS_UPDATED, push),
            async_dispatcher_connect(hass, SIGNAL_RINGING_UPDATED, push),
            async_dispatcher_connect(hass, SIGNAL_TARGETS_UPDATED, push),
            async_dispatcher_connect(hass, SIGNAL_CONFIG_UPDATED, push),
            async_dispatcher_connect(hass, SIGNAL_TIMERS_UPDATED, push),
            async_dispatcher_connect(hass, SIGNAL_LOG_UPDATED, push),
        ]

        @callback
        def unsubscribe() -> None:
            for unsub in unsubs:
                unsub()

        connection.subscriptions[msg["id"]] = unsubscribe
        connection.send_result(msg["id"])
        push()

    @websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/snapshot"})
    @callback
    def snapshot(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
    ) -> None:
        manager: AlarmManager | None = get_manager()
        if manager is None:
            connection.send_error(msg["id"], "not_loaded", "Voice Alarms is not loaded")
            return
        connection.send_result(msg["id"], manager.snapshot())

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/set_ramp",
            vol.Required("seconds"): vol.All(vol.Coerce(float), vol.Range(min=0, max=300)),
            vol.Required("start"): _UNIT,
            vol.Required("curve"): vol.All([_UNIT], vol.Length(min=4, max=4)),
        }
    )
    @callback
    def set_ramp(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
    ) -> None:
        """Save the volume ramp edited in the panel; returns the new config."""
        manager: AlarmManager | None = get_manager()
        if manager is None:
            connection.send_error(msg["id"], "not_loaded", "Voice Alarms is not loaded")
            return
        x1, y1, x2, y2 = msg["curve"]
        manager.async_set_ramp(seconds=msg["seconds"], start=msg["start"], curve=(x1, y1, x2, y2))
        connection.send_result(msg["id"], manager.config_dict())

    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/timer",
            vol.Required("timer_id"): str,
            vol.Required("action"): vol.In(["cancel", "pause", "resume", "add"]),
            vol.Optional("seconds", default=60): vol.All(
                vol.Coerce(int), vol.Range(min=-86400, max=86400)
            ),
        }
    )
    @callback
    def timer(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
    ) -> None:
        """Cancel, pause, resume or change the time of one of Assist's timers."""
        timers = hass.data.get(TIMER_DATA)
        if not isinstance(timers, PersistentTimerManager):
            connection.send_error(msg["id"], "not_loaded", "Voice Alarms is not loaded")
            return
        timer_id = msg["timer_id"]
        try:
            match msg["action"]:
                case "cancel":
                    timers.cancel_timer(timer_id)
                case "pause":
                    timers.pause_timer(timer_id)
                case "resume":
                    timers.unpause_timer(timer_id)
                case "add":
                    timers.add_time(timer_id, msg["seconds"])
        except TimerNotFoundError:
            connection.send_error(msg["id"], "not_found", "That timer no longer exists")
            return
        connection.send_result(msg["id"])

    websocket_api.async_register_command(hass, subscribe)
    websocket_api.async_register_command(hass, timer)
    websocket_api.async_register_command(hass, snapshot)
    websocket_api.async_register_command(hass, set_ramp)
