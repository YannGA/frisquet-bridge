'Tests for simplified and central-boiler profile logic.'

import pytest

from frisquet_bridge.boiler_profile import (
    SIMPLE_DEFAULT_COMFORT_TEMPERATURE,
    SIMPLE_DEFAULT_FROST_TEMPERATURE,
    SIMPLE_DEFAULT_REDUCED_TEMPERATURE,
    apply_simple_command,
    central_boiler_consigne,
    simple_consigne,
    simple_target_temperature,)
from frisquet_bridge.climate import HVAC_AUTO, HVAC_HEAT, PRESET_ECO
from frisquet_bridge.model import ZoneMode, ZoneState


def test_simple_profile_walks_thermostat_session():
    zone = ZoneState(zone=1, mode=ZoneMode.FROST)
    assert simple_target_temperature(zone) == SIMPLE_DEFAULT_FROST_TEMPERATURE
    zone.mode = ZoneMode.REDUCED
    assert simple_target_temperature(zone) == SIMPLE_DEFAULT_REDUCED_TEMPERATURE
    zone.mode = ZoneMode.COMFORT
    assert simple_target_temperature(zone) == SIMPLE_DEFAULT_COMFORT_TEMPERATURE

    # never keeps AUTO from state, and the existing comfort setpoint survives
    zone = ZoneState(zone=1, mode=ZoneMode.AUTO, comfort_temperature=20.0, reported_ambient=19.0)
    apply_simple_command(zone, hvac_mode=HVAC_HEAT)
    assert (zone.mode, zone.auto_comfort, zone.override) == (ZoneMode.COMFORT, None, False)
    assert simple_target_temperature(zone) == 20.0

    # missing defaults are filled in without overwriting an existing value
    zone = ZoneState(zone=1, mode=ZoneMode.COMFORT, comfort_temperature=21.0)
    apply_simple_command(zone, preset=PRESET_ECO)
    assert (zone.frost_temperature, zone.reduced_temperature, zone.comfort_temperature) == (
        SIMPLE_DEFAULT_FROST_TEMPERATURE,
        SIMPLE_DEFAULT_REDUCED_TEMPERATURE,
        21.0,)

    zone = ZoneState(zone=1, mode=ZoneMode.COMFORT, reduced_temperature=17.0)
    apply_simple_command(zone, preset=PRESET_ECO)
    apply_simple_command(zone, target_temperature=16.5)
    assert (zone.mode, zone.reduced_temperature) == (ZoneMode.REDUCED, 16.5)

    zone = ZoneState(zone=1, mode=ZoneMode.AUTO)
    assert simple_consigne(zone) is None
    zone.reported_ambient = 18.5
    consigne = simple_consigne(zone)
    assert consigne is not None
    assert (consigne.ambient, consigne.setpoint, zone.mode) == (18.5, SIMPLE_DEFAULT_COMFORT_TEMPERATURE, ZoneMode.COMFORT)

    zone = ZoneState(zone=1)
    apply_simple_command(zone, hvac_mode="off")
    apply_simple_command(zone, target_temperature=9.0)
    assert (zone.mode, zone.frost_temperature) == (ZoneMode.FROST, 9.0)
    apply_simple_command(zone, hvac_mode="heat")
    apply_simple_command(zone, preset=PRESET_ECO)
    apply_simple_command(zone, target_temperature=19.0)
    assert (zone.mode, zone.reduced_temperature) == (ZoneMode.REDUCED, 19.0)
    apply_simple_command(zone, preset="comfort")
    apply_simple_command(zone, target_temperature=21.0)
    zone.reported_ambient = 28.0
    consigne = simple_consigne(zone)
    assert consigne is not None
    assert (zone.mode, zone.comfort_temperature, consigne.ambient, consigne.setpoint) == (ZoneMode.COMFORT, 21.0, 28.0, 21.0)


def test_simple_command_rejects_auto_mode():
    with pytest.raises(ValueError, match="off and heat"):
        apply_simple_command(ZoneState(zone=1, mode=ZoneMode.COMFORT), hvac_mode=HVAC_AUTO)


def test_central_boiler_consigne_walks_demand_and_requires_values():
    zone = ZoneState(zone=1, central_setpoint=20.0, central_demand=True, central_demand_on_delta=4.0, central_demand_off_margin=1.0)
    on = central_boiler_consigne(zone)
    zone.central_demand = False
    off = central_boiler_consigne(zone)
    assert on is not None and off is not None
    assert (on.setpoint, on.ambient, off.setpoint, off.ambient, zone.mode) == (20.0, 16.0, 20.0, 21.0, ZoneMode.COMFORT)
    assert central_boiler_consigne(ZoneState(zone=1, central_setpoint=20.0, central_demand=True)) is None
