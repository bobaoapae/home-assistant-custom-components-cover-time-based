"""Cover Time based."""
import logging

import voluptuous as vol

import time
from datetime import timedelta

from homeassistant.const import (
    STATE_ON,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_STOP_COVER,
)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverEntityFeature,
    PLATFORM_SCHEMA,
    CoverEntity,
)

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

CONF_DEVICES = 'devices'
CONF_NAME = 'name'
CONF_TRAVELLING_TIME_DOWN = 'travelling_time_down'
CONF_TRAVELLING_TIME_UP = 'travelling_time_up'
DEFAULT_TRAVEL_TIME = 25

CONF_OPEN_SWITCH_ENTITY_ID = 'open_switch_entity_id'
CONF_CLOSE_SWITCH_ENTITY_ID = 'close_switch_entity_id'
CONF_STOP_SWITCH_ENTITY_ID = 'stop_switch_entity_id'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_DEVICES, default={}): vol.Schema(
            {
                cv.string: {
                    vol.Optional(CONF_NAME): cv.string,
                    vol.Optional(CONF_OPEN_SWITCH_ENTITY_ID): cv.string,
                    vol.Optional(CONF_CLOSE_SWITCH_ENTITY_ID): cv.string,
                    vol.Optional(CONF_STOP_SWITCH_ENTITY_ID): cv.string,

                    vol.Optional(CONF_TRAVELLING_TIME_DOWN, default=DEFAULT_TRAVEL_TIME):
                        cv.positive_int,
                    vol.Optional(CONF_TRAVELLING_TIME_UP, default=DEFAULT_TRAVEL_TIME):
                        cv.positive_int,
                }
            }
        ),
    }
)

def devices_from_config(domain_config):
    """Parse configuration and add cover devices."""
    devices = []
    for device_id, config in domain_config[CONF_DEVICES].items():
        name = config.pop(CONF_NAME)
        travel_time_down = config.pop(CONF_TRAVELLING_TIME_DOWN)
        travel_time_up = config.pop(CONF_TRAVELLING_TIME_UP)
        open_switch_entity_id = config.pop(CONF_OPEN_SWITCH_ENTITY_ID)
        close_switch_entity_id = config.pop(CONF_CLOSE_SWITCH_ENTITY_ID)
        stop_switch_entity_id = config.pop(CONF_STOP_SWITCH_ENTITY_ID, None)
        device = CoverTimeBased(device_id, name, travel_time_down, travel_time_up, open_switch_entity_id, close_switch_entity_id, stop_switch_entity_id)
        devices.append(device)
    return devices

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the cover platform."""
    async_add_entities(devices_from_config(config))

class CoverTimeBased(CoverEntity, RestoreEntity):
	
    def __init__(self, device_id, name, travel_time_down, travel_time_up, open_switch_entity_id, close_switch_entity_id, stop_switch_entity_id=None):
        """Initialize the cover."""
        from xknx.devices import TravelCalculator
        self._travel_time_down = travel_time_down
        self._travel_time_up = travel_time_up
        self._open_switch_entity_id = open_switch_entity_id
        self._close_switch_entity_id = close_switch_entity_id
        self._stop_switch_entity_id = stop_switch_entity_id
        self._attr_name = name if name else device_id
        self._attr_unique_id = device_id
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )

        self._unsubscribe_auto_updater = None
        self._last_command_time = 0
        self._command_debounce = 2  # seconds to ignore switch changes after our commands

        self.tc = TravelCalculator(self._travel_time_down, self._travel_time_up)

    async def async_added_to_hass(self):
        """ Only cover's position matters.             """
        """ The rest is calculated from this attribute."""
        old_state = await self.async_get_last_state()
        _LOGGER.debug('async_added_to_hass :: oldState %s', old_state)
        if (
                old_state is not None and
                self.tc is not None and
                old_state.attributes.get(ATTR_CURRENT_POSITION) is not None):
            self.tc.set_position(int(
                old_state.attributes.get(ATTR_CURRENT_POSITION)))

        # Listen for external switch activations (remote control, app, etc.)
        switch_ids = [self._open_switch_entity_id, self._close_switch_entity_id]
        if self._stop_switch_entity_id:
            switch_ids.append(self._stop_switch_entity_id)
        async_track_state_change_event(
            self.hass, switch_ids, self._async_switch_changed
        )

    @callback
    def _async_switch_changed(self, event):
        """Handle switch state changes from external sources."""
        if time.monotonic() - self._last_command_time < self._command_debounce:
            return

        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != STATE_ON:
            return

        entity_id = event.data.get("entity_id")
        _LOGGER.debug('_async_switch_changed :: external activation of %s', entity_id)

        if entity_id == self._open_switch_entity_id:
            self.tc.start_travel_up()
            self.start_auto_updater()
        elif entity_id == self._close_switch_entity_id:
            self.tc.start_travel_down()
            self.start_auto_updater()
        elif entity_id == self._stop_switch_entity_id:
            self._handle_my_button()

        self.async_write_ha_state()

    def _handle_my_button(self):
        """Handle the MY button press"""
        if self.tc.is_traveling():
            _LOGGER.debug('_handle_my_button :: button stops cover')
            self.tc.stop()
            self.stop_auto_updater()

    @property
    def extra_state_attributes(self):
        """Return the device state attributes."""
        attr = {}
        if self._travel_time_down is not None:
            attr[CONF_TRAVELLING_TIME_DOWN] = self._travel_time_down
        if self._travel_time_up is not None:
            attr[CONF_TRAVELLING_TIME_UP] = self._travel_time_up
        return attr

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self.tc.current_position()

    @property
    def is_opening(self):
        """Return if the cover is opening or not."""
        from xknx.devices import TravelStatus
        return self.tc.is_traveling() and \
               self.tc.travel_direction == TravelStatus.DIRECTION_UP

    @property
    def is_closing(self):
        """Return if the cover is closing or not."""
        from xknx.devices import TravelStatus
        return self.tc.is_traveling() and \
               self.tc.travel_direction == TravelStatus.DIRECTION_DOWN

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self.tc.is_closed()

    @property
    def assumed_state(self):
        """Return True because covers can be stopped midway."""
        return True

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        if ATTR_POSITION in kwargs:
            position = kwargs[ATTR_POSITION]
            _LOGGER.debug('async_set_cover_position: %d', position)
            await self.set_position(position)

    async def async_close_cover(self, **kwargs):
        """Turn the device close."""
        _LOGGER.debug('async_close_cover')
        was_traveling = self.tc.is_traveling()
        self.tc.start_travel_down()

        self.start_auto_updater()
        await self._async_handle_command(SERVICE_CLOSE_COVER, was_traveling)

    async def async_open_cover(self, **kwargs):
        """Turn the device open."""
        _LOGGER.debug('async_open_cover')
        was_traveling = self.tc.is_traveling()
        self.tc.start_travel_up()

        self.start_auto_updater()
        await self._async_handle_command(SERVICE_OPEN_COVER, was_traveling)

    async def async_stop_cover(self, **kwargs):
        """Turn the device stop."""
        _LOGGER.debug('async_stop_cover')
        self._handle_my_button()
        await self._async_handle_command(SERVICE_STOP_COVER)

    async def set_position(self, position):
        _LOGGER.debug('set_position')
        """Move cover to a designated position."""
        current_position = self.tc.current_position()
        _LOGGER.debug('set_position :: current_position: %d, new_position: %d',
                      current_position, position)
        command = None
        if position < current_position:
            command = SERVICE_CLOSE_COVER
        elif position > current_position:
            command = SERVICE_OPEN_COVER
        if command is not None:
            was_traveling = self.tc.is_traveling()
            self.start_auto_updater()
            self.tc.start_travel(position)
            _LOGGER.debug('set_position :: command %s', command)
            await self._async_handle_command(command, was_traveling)
        return

    def start_auto_updater(self):
        """Start the autoupdater to update HASS while cover is moving."""
        _LOGGER.debug('start_auto_updater')
        if self._unsubscribe_auto_updater is None:
            _LOGGER.debug('init _unsubscribe_auto_updater')
            interval = timedelta(seconds=0.1)
            self._unsubscribe_auto_updater = async_track_time_interval(
                self.hass, self.auto_updater_hook, interval)

    @callback
    def auto_updater_hook(self, now):
        """Call for the autoupdater."""
        _LOGGER.debug('auto_updater_hook')
        self.async_schedule_update_ha_state()
        if self.position_reached():
            _LOGGER.debug('auto_updater_hook :: position_reached')
            self.stop_auto_updater()
        self.hass.async_create_task(self.auto_stop_if_necessary())

    def stop_auto_updater(self):
        """Stop the autoupdater."""
        _LOGGER.debug('stop_auto_updater')
        if self._unsubscribe_auto_updater is not None:
            self._unsubscribe_auto_updater()
            self._unsubscribe_auto_updater = None

    def position_reached(self):
        """Return if cover has reached its final position."""
        return self.tc.position_reached()

    async def auto_stop_if_necessary(self):
        """Do auto stop if necessary."""
        if self.position_reached():
            current_pos = self.tc.current_position()
            self.tc.stop()
            if current_pos > 0 and current_pos < 100:
                _LOGGER.debug('auto_stop_if_necessary :: intermediate position, sending stop')
                await self._async_handle_command(SERVICE_STOP_COVER)
            else:
                _LOGGER.debug('auto_stop_if_necessary :: end position %d, no stop needed', current_pos)
    
    
    async def _async_handle_command(self, command, was_traveling=False):
        self._last_command_time = time.monotonic()

        if command == "close_cover":
            cmd = "DOWN"
            if was_traveling and self._stop_switch_entity_id:
                await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self._stop_switch_entity_id}, False)
            elif was_traveling:
                await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self._open_switch_entity_id}, False)
            await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self._close_switch_entity_id}, False)

        elif command == "open_cover":
            cmd = "UP"
            if was_traveling and self._stop_switch_entity_id:
                await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self._stop_switch_entity_id}, False)
            elif was_traveling:
                await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self._close_switch_entity_id}, False)
            await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self._open_switch_entity_id}, False)

        elif command == "stop_cover":
            cmd = "STOP"
            if self._stop_switch_entity_id:
                await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self._stop_switch_entity_id}, False)
            else:
                await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self._close_switch_entity_id}, False)
                await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self._open_switch_entity_id}, False)

        _LOGGER.debug('_async_handle_command :: %s', cmd)

        # Update state of entity
        self.async_write_ha_state()
