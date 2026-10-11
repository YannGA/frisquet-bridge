'Tests for zone climate mapping (pure logic, no I/O).'

import pytest

from frisquet_bridge.climate import (
    HVAC_AUTO,
    HVAC_HEAT,
    HVAC_OFF,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateError,
    resolve_zone_intent,
    zone_hvac_action,
    zone_hvac_mode,
    zone_preset,
    zone_target_temperature,)
from frisquet_bridge.model import BoilerState, ZoneMode, ZoneState


def _zone(**kwargs):
    base = ZoneState(zone=1)
    for key, value in kwargs.items(): setattr(base, key, value)
    return base


def test_zone_hvac_mode_and_preset_mapping():
    for mode, expected in (
        (ZoneMode.FROST, HVAC_OFF),
        (ZoneMode.AUTO, HVAC_AUTO),
        (ZoneMode.COMFORT, HVAC_HEAT),
        (ZoneMode.REDUCED, HVAC_HEAT),
    ):
        assert zone_hvac_mode(_zone(mode=mode)) == expected

    assert zone_preset(_zone(mode=ZoneMode.FROST)) == PRESET_NONE
    assert zone_preset(_zone(mode=ZoneMode.COMFORT, boost=False)) == PRESET_COMFORT
    assert zone_preset(_zone(mode=ZoneMode.COMFORT, boost=True)) == PRESET_BOOST
    assert zone_preset(_zone(mode=ZoneMode.REDUCED)) == PRESET_ECO
    # auto with no known setpoint falls back to the derogation flags
    assert zone_preset(_zone(mode=ZoneMode.AUTO, override=False)) == PRESET_NONE
    assert zone_preset(_zone(mode=ZoneMode.AUTO, override=True, auto_comfort=True)) == PRESET_COMFORT
    assert zone_preset(_zone(mode=ZoneMode.AUTO, override=True, auto_comfort=False)) == PRESET_ECO
    # auto without a derogation tracks the boiler-reported program slot
    comfort_slot = _zone(mode=ZoneMode.AUTO, override=False, comfort_temperature=20.0, reduced_temperature=17.0, setpoint_temperature=20.0)
    eco_slot = _zone(mode=ZoneMode.AUTO, override=False, comfort_temperature=20.0, reduced_temperature=17.0, setpoint_temperature=17.0)
    assert zone_preset(comfort_slot) == PRESET_COMFORT
    assert zone_preset(eco_slot) == PRESET_ECO


def test_zone_hvac_action_tracks_setpoint_vs_ambient():
    boiler = BoilerState()
    assert zone_hvac_action(_zone(mode=ZoneMode.FROST), boiler) == "off"
    assert zone_hvac_action(_zone(mode=ZoneMode.AUTO, ambient_temperature=18.0, setpoint_temperature=20.0), boiler) == "heating"
    assert zone_hvac_action(_zone(mode=ZoneMode.AUTO, ambient_temperature=21.0, setpoint_temperature=20.0), boiler) == "idle"


def test_zone_target_temperature_follows_mode_and_boost():
    assert zone_target_temperature(_zone(mode=ZoneMode.COMFORT, comfort_temperature=20.5)) == 20.5
    assert zone_target_temperature(_zone(mode=ZoneMode.COMFORT, boost=True, comfort_temperature=20.0)) == 22.0
    assert zone_target_temperature(_zone(mode=ZoneMode.REDUCED, reduced_temperature=17.0)) == 17.0


def test_resolve_zone_intent_applies_commands():
    intent = resolve_zone_intent(_zone(mode=ZoneMode.AUTO), preset=PRESET_COMFORT)
    assert (intent.mode, intent.override, intent.auto_comfort, intent.boost) == (ZoneMode.AUTO, True, True, False)

    cleared = resolve_zone_intent(_zone(mode=ZoneMode.AUTO, override=True, auto_comfort=True), hvac_mode=HVAC_AUTO, preset=PRESET_NONE)
    assert (cleared.mode, cleared.override) == (ZoneMode.AUTO, False)

    boosted = resolve_zone_intent(_zone(mode=ZoneMode.COMFORT), hvac_mode=HVAC_HEAT, preset=PRESET_BOOST)
    assert (boosted.mode, boosted.boost) == (ZoneMode.COMFORT, True)

    frosted = resolve_zone_intent(_zone(mode=ZoneMode.AUTO), hvac_mode=HVAC_OFF)
    assert (frosted.mode, frosted.boost) == (ZoneMode.FROST, False)

    targeted = resolve_zone_intent(_zone(mode=ZoneMode.COMFORT, comfort_temperature=20.0, reduced_temperature=17.0, frost_temperature=7.0),
        target_temperature=21.5)
    assert targeted.comfort_temperature == 21.5


def test_resolve_boost_outside_comfort_raises():
    with pytest.raises(ClimateError, match="auto"): resolve_zone_intent(_zone(mode=ZoneMode.AUTO), preset=PRESET_BOOST)
