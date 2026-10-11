'Tests for service-level polling decisions.'

import asyncio
from pathlib import Path

import pytest

from frisquet_bridge.config import BridgeConfig, ConnectConfig, DeviceIdentity, ZoneConfig
from frisquet_bridge.model import BoilerData, ZoneSource
from frisquet_bridge.service import BridgeService, _configure_rf_diagnostics, _configure_zone_sources, _has_read_only_satellite_zone
from frisquet_bridge.transport.base import TransportError


def _config(tmp_path: Path, *modes: str) -> BridgeConfig:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    for zone_number, mode in enumerate(modes, 1): setattr(cfg, f"zone{zone_number}", ZoneConfig(mode=mode))
    return cfg


def test_has_read_only_satellite_zone_only_for_physical_satellite(tmp_path: Path):
    cases = [(('virtual_satellite', 'simple_satellite', 'disabled'), (1, 2), False),
        (('central_boiler', 'disabled', 'disabled'), (1,), False), (('virtual_satellite', 'satellite', 'disabled'), (1, 2), True)]
    for modes, zones, expected in cases: assert _has_read_only_satellite_zone(_config(tmp_path, *modes), zones) == expected


def test_configure_zone_sources_maps_zone_modes_to_owners(tmp_path: Path):
    cfg = _config(tmp_path, "disabled", "satellite", "virtual_satellite")
    cfg.connect = ConnectConfig(mode="read", identity=DeviceIdentity(association_id=1, request_id=1))
    data = BoilerData()
    _configure_zone_sources(cfg, data)
    assert (
        data.zones[1].source,
        data.zones[2].source,
        data.zones[3].source,
    ) == (ZoneSource.CONNECT, ZoneSource.SATELLITE, ZoneSource.VIRTUAL)

    cfg = _config(tmp_path, "central_boiler", "disabled", "disabled")
    data = BoilerData()
    _configure_zone_sources(cfg, data)
    assert data.zones[1].source == ZoneSource.VIRTUAL


def test_configure_rf_diagnostics_includes_satellite_info_only_for_physical_zone(tmp_path: Path):
    cfg = _config(tmp_path, "disabled", "virtual_satellite", "disabled")
    cfg.connect = ConnectConfig(mode="full", identity=DeviceIdentity(association_id=1, request_id=1))
    data = BoilerData()
    _configure_rf_diagnostics(cfg, data)
    assert "connect_satellite_info" not in data.rf_operations
    assert "connect_zone1_write" not in data.rf_operations

    cfg = _config(tmp_path, "satellite", "disabled", "disabled")
    cfg.connect = ConnectConfig(mode="read", identity=DeviceIdentity(association_id=1, request_id=1))
    data = BoilerData()
    _configure_rf_diagnostics(cfg, data)
    assert "connect_satellite_info" in data.rf_operations


async def test_shutdown_propagates_transport_failure_but_swallows_offline_publish_failure():
    service = BridgeService.__new__(BridgeService)
    service._stop = asyncio.Event()

    async def fail_transport(): raise TransportError("serial reader failed: disconnected")

    with pytest.raises(TransportError, match="disconnected"): await service._wait_for_stop_or_transport_failure(fail_transport())

    class FailingAdapter:
        async def publish_offline(self, _client): raise RuntimeError("broker disconnected")

    await service._publish_offline_preserving_failure(FailingAdapter(), object(), TransportError("transport died"))
