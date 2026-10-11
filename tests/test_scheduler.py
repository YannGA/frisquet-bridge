"""Tests for periodic service scheduling."""

import asyncio

import pytest

from frisquet_bridge.model import BoilerData
from frisquet_bridge.scheduler import PollScheduler


class FakeSondeOps:
    def __init__(self, *, fail_attempts=frozenset()):
        self.temperatures = []
        self.fail_attempts = fail_attempts
        self._second_write = asyncio.Event()

    async def write_outside_temperature(self, data, temperature):
        self.temperatures.append(temperature)
        attempt = len(self.temperatures)
        if attempt >= 2:
            self._second_write.set()
        if attempt in self.fail_attempts:
            raise RuntimeError("RF write failed")
        data.sonde.outside_temperature = temperature

    async def wait_for_second_write(self):
        await asyncio.wait_for(self._second_write.wait(), timeout=1.0)


class FakeBoilerOps:
    def __init__(self, *, fail_satellite_info=False):
        self.sensor_reads = 0
        self.satellite_info_reads = 0
        self.fail_satellite_info = fail_satellite_info
        self._second_sensor_read = asyncio.Event()
        self._second_satellite_info_read = asyncio.Event()

    async def read_sensors(self, data):
        self.sensor_reads += 1
        if self.sensor_reads >= 2:
            self._second_sensor_read.set()

    async def read_satellite_info(self, data, *, timeout=5.0, retries=3):
        self.satellite_info_reads += 1
        if self.satellite_info_reads >= 2:
            self._second_satellite_info_read.set()
        if self.fail_satellite_info:
            raise RuntimeError("no A029 response")

    async def read_consumption(self, data): pass

    async def read_daily_consumption(self, data): pass

    async def read_dhw_mode(self, data): pass

    async def read_clock(self, data): pass

    async def wait_for_second_sensor_read(self):
        await asyncio.wait_for(self._second_sensor_read.wait(), timeout=1.0)

    async def wait_for_second_satellite_info_read(self):
        await asyncio.wait_for(self._second_satellite_info_read.wait(), timeout=1.0)


async def test_scheduler_polls_sensors_and_satellite_info_on_their_own_intervals():
    published = 0

    async def publish():
        nonlocal published
        published += 1

    # no physical satellite zone: one bootstrap satellite-info read, sensors keep polling
    data, ops = BoilerData(), FakeBoilerOps()
    sched = PollScheduler(ops, data, sensor_interval=0.01, poll_satellite_info=False, on_update=publish)
    task = asyncio.create_task(sched.run())
    try:
        await ops.wait_for_second_sensor_read()
    finally:
        sched.stop()
        await task
    assert ops.sensor_reads >= 2 and ops.satellite_info_reads == 1 and published > 0

    # physical satellite zone: satellite info repeats on its own interval, sensors do not
    data, ops = BoilerData(), FakeBoilerOps()
    sched = PollScheduler(ops, data, sensor_interval=1.0, poll_satellite_info=True, satellite_info_interval=0.01)
    task = asyncio.create_task(sched.run())
    try:
        await ops.wait_for_second_satellite_info_read()
    finally:
        sched.stop()
        await task
    assert ops.satellite_info_reads >= 2 and ops.sensor_reads == 1

    # a failing satellite-info read does not stop sensor polling
    data, ops = BoilerData(), FakeBoilerOps(fail_satellite_info=True)
    sched = PollScheduler(ops, data, sensor_interval=0.01, poll_satellite_info=False)
    task = asyncio.create_task(sched.run())
    try:
        await ops.wait_for_second_sensor_read()
    finally:
        sched.stop()
        await task
    assert ops.satellite_info_reads == 1 and ops.sensor_reads >= 2


async def test_outside_temperature_change_inside_window_is_deferred_not_dropped():
    data, sonde_ops = BoilerData(), FakeSondeOps()
    sched = PollScheduler(
        None, data, poll_connect=False, sonde_ops=sonde_ops, push_outside_temperature=True,
        outside_temperature_interval=5.0, outside_temperature_retry_interval=5.0,
        outside_temperature_min_change_interval=0.2,
    )

    assert await sched.send_outside_temperature_now(12.3) is True
    assert sonde_ops.temperatures == [12.3]

    assert await sched.send_outside_temperature_now(13.4) is False
    assert sonde_ops.temperatures == [12.3] and data.sonde.outside_temperature == 13.4

    task = asyncio.create_task(sched.run())
    try:
        await sonde_ops.wait_for_second_write()
    finally:
        sched.stop()
        await task
    assert sonde_ops.temperatures[:2] == [12.3, 13.4]


async def test_outside_temperature_repushes_keepalive_and_skips_unknown_value():
    data = BoilerData()
    data.sonde.outside_temperature = 12.3
    sonde_ops = FakeSondeOps()
    sched = PollScheduler(
        None, data, poll_connect=False, sonde_ops=sonde_ops, push_outside_temperature=True,
        outside_temperature_interval=0.01,
    )
    task = asyncio.create_task(sched.run())
    try:
        await sonde_ops.wait_for_second_write()
    finally:
        sched.stop()
        await task
    assert sonde_ops.temperatures[:2] == [12.3, 12.3]

    data, sonde_ops = BoilerData(), FakeSondeOps()
    sched = PollScheduler(
        None, data, poll_connect=False, sonde_ops=sonde_ops, push_outside_temperature=True,
        outside_temperature_interval=0.01,
    )
    task = asyncio.create_task(sched.run())
    try:
        await asyncio.sleep(0.02)
    finally:
        sched.stop()
        await task
    assert sonde_ops.temperatures == []


async def test_failed_outside_temperature_write_is_kept_and_retried_on_short_interval():
    data, sonde_ops = BoilerData(), FakeSondeOps(fail_attempts={1})
    sched = PollScheduler(
        None, data, poll_connect=False, sonde_ops=sonde_ops, push_outside_temperature=True,
        outside_temperature_interval=1.0, outside_temperature_retry_interval=0.01,
    )

    task = asyncio.create_task(sched.run())
    try:
        await asyncio.sleep(0)
        assert await sched.send_outside_temperature_now(12.34) is False
        assert sonde_ops.temperatures == [12.3] and data.sonde.outside_temperature == 12.3
        await sonde_ops.wait_for_second_write()
    finally:
        sched.stop()
        await task
    assert sonde_ops.temperatures[:2] == [12.3, 12.3]


async def test_successful_outside_temperature_write_is_not_repeated_until_keepalive():
    data, sonde_ops = BoilerData(), FakeSondeOps()
    sched = PollScheduler(
        None, data, poll_connect=False, sonde_ops=sonde_ops, push_outside_temperature=True,
        outside_temperature_interval=0.05, outside_temperature_retry_interval=1.0,
    )

    task = asyncio.create_task(sched.run())
    try:
        await asyncio.sleep(0)
        assert await sched.send_outside_temperature_now(12.34) is True
        assert sonde_ops.temperatures == [12.3]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sonde_ops._second_write.wait(), timeout=0.01)
        await sonde_ops.wait_for_second_write()
    finally:
        sched.stop()
        await task
    assert sonde_ops.temperatures[:2] == [12.3, 12.3]

    # a state-publication failure after a successful write does not trigger a retry
    data, sonde_ops = BoilerData(), FakeSondeOps()

    async def fail_publish():
        raise RuntimeError("MQTT unavailable")

    sched = PollScheduler(
        None, data, poll_connect=False, sonde_ops=sonde_ops, push_outside_temperature=True,
        outside_temperature_interval=1.0, outside_temperature_retry_interval=0.01, on_update=fail_publish,
    )
    assert await sched.send_outside_temperature_now(12.3) is True
    task = asyncio.create_task(sched.run())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sonde_ops._second_write.wait(), timeout=0.03)
    finally:
        sched.stop()
        await task
    assert sonde_ops.temperatures == [12.3]
