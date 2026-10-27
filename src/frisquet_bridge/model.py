"""Internal data model - the single source of truth for boiler/zone state.

These dataclasses are populated by the protocol layer and observed by adapters
(e.g. the optional MQTT/HA adapter). The model has no I/O dependencies.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# Sentinel the boiler uses for "outside temperature not available".
OUTSIDE_TEMP_ABSENT = 129.0
SONDE_MIN_TEMPERATURE = -30.0
SONDE_MAX_TEMPERATURE = 80.0


class DhwMode(StrEnum):
    'Domestic hot water (DHW) mode. Values are the raw protocol bytes.'

    MAX = "Max"
    ECO = "Eco"
    ECO_SCHEDULE = "Eco Schedule"
    ECO_PLUS = "Eco+"
    ECO_PLUS_SCHEDULE = "Eco+ Schedule"
    STOP = "Stop"

    @property
    def byte(self) -> int: return _DHW_MODE_TO_BYTE[self]

    @classmethod
    def from_byte(cls, raw: int) -> DhwMode | None: return _DHW_BYTE_TO_MODE.get(raw & 0x7E)

    @classmethod
    def parse(cls, value: str) -> DhwMode:
        normalized = _normalize_label(value)
        mode = _DHW_LABELS.get(normalized)
        if mode is None:
            choices = ", ".join(m.value for m in cls)
            raise ValueError(f"unknown DHW mode {value!r}; expected one of: {choices}")
        return mode


_DHW_MODE_TO_BYTE = {DhwMode.MAX: 0, DhwMode.ECO: 8, DhwMode.ECO_SCHEDULE: 16, DhwMode.ECO_PLUS: 24, DhwMode.ECO_PLUS_SCHEDULE: 32, DhwMode.STOP: 40}
_DHW_BYTE_TO_MODE = {v: k for k, v in _DHW_MODE_TO_BYTE.items()}

# Modes exposed in the Home Assistant DHW selector. The Eco+ variants only
# exist on some boiler models (e.g. those with a dedicated DHW tank), so the
# selector advertises only the four modes the standard app shows. The full
# enum above is still used to *decode* whatever the boiler reports.
DHW_SELECTABLE_MODES = (DhwMode.MAX, DhwMode.ECO, DhwMode.ECO_SCHEDULE, DhwMode.STOP)


class ZoneSource(StrEnum):
    "Configured owner of a zone's room state."

    CONNECT = "connect"
    SATELLITE = "satellite"
    VIRTUAL = "virtual"


class ZoneMode(StrEnum):
    'Heating mode for a zone.'

    AUTO = "Auto"
    COMFORT = "Comfort"
    REDUCED = "Reduced"
    FROST = "Frost protection"

    @property
    def byte(self) -> int: return _ZONE_MODE_TO_BYTE[self]

    @classmethod
    def from_byte(cls, raw: int) -> ZoneMode | None: return _ZONE_BYTE_TO_MODE.get(raw)

    @classmethod
    def parse(cls, value: str) -> ZoneMode:
        normalized = _normalize_label(value)
        mode = _ZONE_LABELS.get(normalized)
        if mode is None:
            choices = ", ".join(m.value for m in cls)
            raise ValueError(f"unknown zone mode {value!r}; expected one of: {choices}")
        return mode


_ZONE_MODE_TO_BYTE = {ZoneMode.AUTO: 5, ZoneMode.COMFORT: 6, ZoneMode.REDUCED: 7, ZoneMode.FROST: 8}
_ZONE_BYTE_TO_MODE = {v: k for k, v in _ZONE_MODE_TO_BYTE.items()}


def _normalize_label(value: str) -> str: return value.strip().casefold().replace("é", "e").replace("_", " ").replace("-", " ")


_DHW_LABELS = {_normalize_label(mode.value): mode for mode in DhwMode}
_DHW_LABELS.update({'eco horaires': DhwMode.ECO_SCHEDULE, 'eco schedule': DhwMode.ECO_SCHEDULE, 'eco+ horaires': DhwMode.ECO_PLUS_SCHEDULE,
    'eco+ schedule': DhwMode.ECO_PLUS_SCHEDULE})
_ZONE_LABELS = {_normalize_label(mode.value): mode for mode in ZoneMode}
_ZONE_LABELS.update({'reduit': ZoneMode.REDUCED, 'eco': ZoneMode.REDUCED, 'hors gel': ZoneMode.FROST, 'frost': ZoneMode.FROST})


class BoilerStatus(StrEnum):
    STANDBY = "Standby"
    RUNNING = "Running"
    HEATING_OFF = "Heating off"

    @classmethod
    def from_byte(cls, status_byte: int) -> BoilerStatus:
        if status_byte & 0b0000_0100: return cls.HEATING_OFF
        if status_byte & 0b0000_1000: return cls.RUNNING
        return cls.STANDBY


SCHEDULE_DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


@dataclass(frozen=True)
class ZoneSchedule:
    days: dict[str, bytes]

    @classmethod
    def decode(cls, payload: bytes) -> ZoneSchedule:
        if len(payload) < 42: raise ValueError(f"zone schedule payload too short: {len(payload)} bytes")
        return cls(days={day: payload[offset:offset + 6] for offset, day in zip(range(0, 42, 6), SCHEDULE_DAYS, strict=True)})

    def compact_hex(self) -> str: return " ".join(f"{day[:3]}={self.days[day].hex()}" for day in SCHEDULE_DAYS)


@dataclass
class BoilerState:
    dhw_temperature: float | None = None
    dhw_instant_temperature: float | None = None
    cdc_temperature: float | None = None
    cdc_safety_temperature: float | None = None
    flue_temperature: float | None = None
    outside_temperature: float | None = None
    dhw_power: float | None = None
    heating_power: float | None = None
    pressure: float | None = None
    dhw_consumption: int | None = None
    heating_consumption: int | None = None
    daily_dhw_consumption: int | None = None
    daily_heating_consumption: int | None = None
    daily_consumption_read_at: str | None = None # boiler clock when the previous-day counters were read
    dhw_mode: DhwMode | None = None
    dhw_frame_bits: int = 0
    status: BoilerStatus | None = None
    fault: bool = False


@dataclass
class ZoneState:
    zone: int
    source: ZoneSource = ZoneSource.SATELLITE
    flow_temperature: float | None = None
    flow_setpoint_temperature: float | None = None
    ambient_temperature: float | None = None
    setpoint_temperature: float | None = None
    # Room temperature we report when acting as a virtual satellite for this
    # zone (fed from Home Assistant). Distinct from ``ambient_temperature``,
    # which is what the boiler/satellite reports back to us.
    reported_ambient: float | None = None
    mode: ZoneMode | None = None
    mode_options: int | None = None
    auto_comfort: bool | None = None
    override: bool = False
    comfort_temperature: float | None = None
    reduced_temperature: float | None = None
    frost_temperature: float | None = None
    boost: bool = False
    schedule: ZoneSchedule | None = None
    central_setpoint: float | None = None
    central_demand: bool | None = None
    central_demand_on_delta: float | None = None
    central_demand_off_margin: float | None = None
    flow_demand: bool | None = None
    flow_target: float | None = None


@dataclass
class SondeState:
    outside_temperature: float | None = None


def sonde_temperature(temperature: float) -> float:
    "Round to the sonde's 0.1 °C resolution and clamp to the range it can encode"
    return max(SONDE_MIN_TEMPERATURE, min(SONDE_MAX_TEMPERATURE, round(temperature * 10) / 10))


# HA's history from 17 to 26 October 2026 shows each day's new counter value by 00:54 boiler time. 03:00 leaves room for missed reads.
DAILY_COUNTERS_SETTLED_HOUR = 3


def daily_consumption_reset(read_at: str | None) -> str | None:
    "Start of the day the previous-day counters cover, from the boiler time they were read at. None before the counters have surely rolled over"
    if read_at is None: return None
    seen = datetime.fromisoformat(read_at)
    if seen.hour < DAILY_COUNTERS_SETTLED_HOUR: return None
    return (datetime(seen.year, seen.month, seen.day) - timedelta(days=1)).astimezone().isoformat()


@dataclass
class RfHealth:
    'Runtime RF outcome counters and freshness for one logical operation.'

    freshness_seconds: float
    zone: int | None = None
    attempts: int = 0
    successes: int = 0
    nacks: int = 0
    timeouts: int = 0
    transport_failures: int = 0
    other_failures: int = 0
    consecutive_failures: int = 0
    last_rssi: int | None = None
    last_success: str | None = None
    _last_success_monotonic: float | None = None
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    @property
    def is_fresh(self) -> bool:
        return self._last_success_monotonic is not None and self.clock() - self._last_success_monotonic <= self.freshness_seconds

    @property
    def state(self) -> str:
        if self._last_success_monotonic is None: return "unavailable"
        if not self.is_fresh: return "stale"
        if self.consecutive_failures: return "degraded"
        return "healthy"

    def record_success(self, *, rssi: int | None = None) -> None:
        self.attempts += 1
        self.successes += 1
        self.consecutive_failures = 0
        self._last_success_monotonic = self.clock()
        self.last_success = datetime.now(UTC).isoformat()
        if rssi is not None: self.last_rssi = rssi

    def record_nack(self) -> None: self._record_failure("nacks")

    def record_timeout(self) -> None: self._record_failure("timeouts")

    def record_transport_failure(self) -> None: self._record_failure("transport_failures")

    def record_other_failure(self) -> None: self._record_failure("other_failures")

    def _record_failure(self, counter: str) -> None:
        self.attempts += 1
        setattr(self, counter, getattr(self, counter) + 1)
        self.consecutive_failures += 1


@dataclass
class BoilerData:
    'Aggregate state for the whole installation.'

    boiler: BoilerState = field(default_factory=BoilerState)
    zones: dict[int, ZoneState] = field(default_factory=lambda: {n: ZoneState(zone=n) for n in (1, 2, 3)})
    sonde: SondeState = field(default_factory=SondeState)
    rf_operations: dict[str, RfHealth] = field(default_factory=dict)
    last_seen_date: str | None = None

    def rf_health(self, operation: str, *, freshness_seconds: float, zone: int | None = None) -> RfHealth:
        health = self.rf_operations.get(operation)
        if health is None:
            health = RfHealth(freshness_seconds=freshness_seconds, zone=zone)
            self.rf_operations[operation] = health
        return health
