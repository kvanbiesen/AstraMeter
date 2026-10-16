"""TEST PATCH: Venus E Mini (VNSEM) with and without a battery power sensor.

A mini reports phase "0" and a 0/1 status instead of watts. Without a sensor
it steers itself on 1/N of the grid; with one it is steered like any other
battery.
"""

from __future__ import annotations

import asyncio
import configparser

import pytest
from aiohttp import web

from astrameter.config.config_loader import create_battery_source
from astrameter.config.ini_config import IniAppConfig
from astrameter.ct002.balancer import BalancerConfig
from astrameter.ct002.battery_sensors import BatteryPowerSensors, parse_sources
from astrameter.ct002.ct002 import CT002
from astrameter.ct002.protocol import build_payload

VENUS = "24215edb1931"
MINI1 = "ccc837b39dbe"
MINI2 = "aabbccddeeff"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr: tuple) -> None:
        self.sent.append(data)


def _ct(grid: float, **kwargs) -> CT002:
    ct = CT002(
        ct_type="HME-3",
        ct_mac="02b2508b1d8e",
        active_control=True,
        dedupe_time_window=0.0,
        # Steer on the reading itself, so the split is what the test sees.
        balancer=BalancerConfig(grid_predict_trust=0.0),
        **kwargs,
    )

    async def before_send(*_args, **_kwargs):
        return [grid, 0.0, 0.0]

    ct.before_send = before_send
    return ct


def _poll(ct: CT002, fields: list[str], ip: str) -> list[str]:
    transport = _Transport()
    asyncio.run(ct._handle_request(build_payload(fields), (ip, 22222), transport))
    return transport.sent[-1][2:-2].decode().split("|")


def _venus(ct: CT002, power: int) -> list[str]:
    return _poll(
        ct, ["HMG-50", VENUS, "HME-3", "02b2508b1d8e", "A", str(power)], "1.1.1.1"
    )


def _mini(ct: CT002, mac: str = MINI1, status: int = 1) -> list[str]:
    return _poll(
        ct, ["VNSEM-0", mac, "HME-3", "000000000000", "0", str(status)], "1.1.1.2"
    )


def _grid(fields: list[str]) -> int:
    return int(fields[5])


def _x_dchrg(fields: list[str]) -> int:
    return int(fields[20])


def _sensors(ct: CT002, readings: dict[str, float]) -> None:
    """Feed battery-sensor readings (discharge positive) without any I/O."""
    sensors = ct._power_sensors
    assert sensors is not None
    now = sensors._clock()
    for mac, power in readings.items():
        sensors._readings[mac] = (power, now)


# --- only minis, no sensors: exactly like a real CT003 -----------------------


def test_minis_alone_share_via_status_sum() -> None:
    ct = _ct(800.0)
    _mini(ct, MINI1)
    fields = _mini(ct, MINI2)
    assert _grid(fields) == 800  # raw grid, no clamp or pacing
    assert _x_dchrg(fields) == 2  # two minis at work -> each takes 1/2
    assert len(fields) - 1 == 28


def test_idle_minis_count_nothing() -> None:
    ct = _ct(800.0)
    _mini(ct, MINI1, status=0)
    fields = _mini(ct, MINI2, status=1)
    assert _x_dchrg(fields) == 1


# --- minis without sensors next to a Venus: 1/N each --------------------------


def test_blind_minis_next_to_venus_take_one_nth() -> None:
    ct = _ct(900.0)
    _venus(ct, 300)
    _mini(ct, MINI1)
    fields = _mini(ct, MINI2)
    assert _x_dchrg(fields) == 3  # Venus + two minis
    _venus(ct, 300)
    # The balancer only spreads the Venus' own third.
    assert ct._balancer._diag_fair_share == pytest.approx(300.0)


def test_blind_mini_never_pushed_against_venus() -> None:
    ct = _ct(-500.0)  # export while the Venus discharges
    _venus(ct, 869)
    fields = _mini(ct)
    assert _grid(fields) == 0  # the mini is not told to charge
    _venus(ct, 869)
    assert ct._balancer._diag_fair_share == pytest.approx(-500.0)  # Venus takes all


def test_blind_mini_paced_next_to_venus() -> None:
    ct = _ct(5000.0)
    _venus(ct, 300)
    fields = _mini(ct)
    assert _x_dchrg(fields) == 2
    assert _grid(fields) == 600  # MINI_MAX_STEP_W per sharing battery


# --- minis with sensors: steered like a (big) Venus ---------------------------


def test_sensor_mini_is_steered_like_a_venus() -> None:
    ct = _ct(600.0, battery_power_sensors=f"{MINI1}=sensor.mini@B")
    _sensors(ct, {MINI1: 250.0})
    fields = _mini(ct)
    consumer = ct._consumers[MINI1]
    assert consumer.phase == "B"
    assert consumer.power == 250
    assert _x_dchrg(fields) == 1  # applies its own target in full
    assert int(fields[6]) != 0  # its target sits on phase B


def test_mixed_sensor_and_blind_minis() -> None:
    ct = _ct(900.0, battery_power_sensors=f"{MINI1}=sensor.mini")
    _sensors(ct, {MINI1: 200.0})
    _venus(ct, 200)
    _mini(ct, MINI1)  # steered through its sensor
    blind = _mini(ct, MINI2)  # no sensor
    assert _x_dchrg(blind) == 3  # 1/3 for the blind mini
    _venus(ct, 200)
    # Venus and the sensor mini split the remaining two thirds.
    assert ct._balancer._diag_fair_share == pytest.approx(300.0)


def test_sensor_mini_falls_back_when_stale() -> None:
    ct = _ct(400.0, battery_power_sensors=f"{MINI1}=sensor.mini")
    sensors = ct._power_sensors
    assert sensors is not None
    sensors._readings[MINI1] = (250.0, sensors._clock() - 60)  # old reading
    fields = _mini(ct)
    assert ct._consumers[MINI1].phase == "0"  # back to the blind behaviour
    assert _x_dchrg(fields) == 1


# --- configuration -------------------------------------------------------------


def test_parse_sources() -> None:
    sources = parse_sources(
        "CC:C8:37:B3:9D:BE=-sensor.mini@b, aabbccddeeff=BATTERY_PLUG, "
        "112233445566=x@Q, broken"
    )
    assert set(sources) == {"ccc837b39dbe", "aabbccddeeff"}
    assert sources["ccc837b39dbe"].inverted
    assert sources["ccc837b39dbe"].phase == "B"
    assert sources["ccc837b39dbe"].is_entity
    assert not sources["aabbccddeeff"].is_entity
    assert sources["aabbccddeeff"].phase == "A"


def _config(text: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.optionxform = str  # type: ignore[assignment,method-assign]
    config.read_string(text)
    return config


def test_battery_source_section() -> None:
    config = _config(
        "[BATTERY_PLUG]\nSOURCE = JSON_HTTP\n"
        "URL = http://192.168.1.50/api/v1/data\nJSON_PATHS = $.active_power_w\n"
    )
    meter = create_battery_source("BATTERY_PLUG", config)
    assert type(meter).__name__ == "JsonHttpPowermeter"
    assert IniAppConfig(config).battery_source("battery_plug") is not None


def test_battery_source_refuses_grid_section_names() -> None:
    config = _config("[SHELLY_MINI]\nSOURCE = SHELLY\nIP = 1.2.3.4\n")
    assert create_battery_source("SHELLY_MINI", config) is None


def test_battery_source_unknown_type() -> None:
    config = _config("[BATTERY_X]\nSOURCE = NOPE\n")
    assert create_battery_source("BATTERY_X", config) is None


def test_section_source_reads_through_its_meter() -> None:
    """End to end: a JSON_HTTP section feeding a mini's power."""

    async def scenario() -> tuple[str, int]:
        app = web.Application()

        async def data(_request: web.Request) -> web.Response:
            return web.json_response({"active_power_w": -285.0})

        app.router.add_get("/api/v1/data", data)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        config = _config(
            f"[BATTERY_PLUG]\nSOURCE = JSON_HTTP\n"
            f"URL = http://127.0.0.1:{port}/api/v1/data\n"
            f"JSON_PATHS = $.active_power_w\n"
        )
        sensors = BatteryPowerSensors(parse_sources(f"{MINI1}=BATTERY_PLUG@C"))
        sensors.set_source_factory(IniAppConfig(config).battery_source)
        await sensors.start()
        for _ in range(30):
            if sensors.power_for(MINI1) is not None:
                break
            await asyncio.sleep(0.1)
        power = sensors.power_for(MINI1)
        await sensors.stop()
        await runner.cleanup()
        return sensors.phase_for(MINI1), power  # type: ignore[return-value]

    phase, power = asyncio.run(scenario())
    assert phase == "C"
    assert power == 285  # plug reads -285 (feeding in) -> discharging 285 W
