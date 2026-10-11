'Tests for internal data model enums and defaults.'

from frisquet_bridge.model import BoilerData, BoilerStatus, DhwMode, RfHealth, ZoneMode


def test_enum_byte_mappings_and_labels():
    # from_byte masks the 0x7E mode bits, so 0x88 (frame bit 0x80 set) is ECO.
    assert DhwMode.from_byte(0x88) == DhwMode.ECO
    assert DhwMode.from_byte(0xFF) is None
    assert DhwMode.MAX.byte == 0x00 and DhwMode.STOP.byte == 0x28
    assert DhwMode.parse("Eco Horaires") == DhwMode.ECO_SCHEDULE

    assert ZoneMode.from_byte(0x06) == ZoneMode.COMFORT
    assert ZoneMode.AUTO.byte == 0x05
    assert ZoneMode.from_byte(0x99) is None
    assert ZoneMode.parse("Réduit") == ZoneMode.REDUCED
    assert ZoneMode.parse("Hors Gel") == ZoneMode.FROST

    assert BoilerStatus.from_byte(0x08) == BoilerStatus.RUNNING
    assert BoilerStatus.from_byte(0x04) == BoilerStatus.HEATING_OFF
    assert BoilerStatus.from_byte(0x00) == BoilerStatus.STANDBY


def test_rf_health_counters_and_freshness():
    now = 100.0
    health = RfHealth(freshness_seconds=30.0, clock=lambda: now)
    health.record_success(rssi=-57)
    assert (health.attempts, health.successes, health.consecutive_failures, health.last_rssi) == (1, 1, 0, -57)
    assert health.is_fresh is True

    now = 131.0
    assert health.is_fresh is False
    health.record_nack()
    health.record_timeout()
    health.record_transport_failure()
    assert (health.nacks, health.timeouts, health.transport_failures) == (1, 1, 1)
    assert health.consecutive_failures == 3

    health.record_success(rssi=-49)
    assert health.consecutive_failures == 0 and health.last_rssi == -49

    data = BoilerData()
    sensors = data.rf_health("connect_sensors", freshness_seconds=60.0)
    clock = data.rf_health("connect_clock", freshness_seconds=7200.0)
    sensors.record_timeout()
    clock.record_success(rssi=-65)
    assert (sensors.timeouts, sensors.successes) == (1, 0)
    assert (clock.successes, clock.timeouts) == (1, 0)
