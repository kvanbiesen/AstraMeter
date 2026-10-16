"""TEST PATCH: Venus E Mini parity between the Python emulator and ESPHome.

Drives the same mixed fleet -- a Venus, a mini with a battery power sensor and
a mini without one -- through both backends and checks that every response
they send is identical, field for field.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from astrameter.ct002.ct002 import CT002
from astrameter.ct002.protocol import build_payload, parse_request

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
MINI_YAML = HERE / "test.mini.host.yaml"
MINI_BINARY = (
    HERE
    / ".esphome"
    / "build"
    / "ct002-mini-test"
    / ".pioenvs"
    / "ct002-mini-test"
    / "program"
)
UDP_PORT = 12345
CONTROL_PORT = 12346

VENUS = "24215edb1931"
MINI_SENSOR = "ccc837b39dbe"  # has a battery power sensor (phase B)
MINI_BLIND = "aabbccddeeff"

pytestmark = [
    pytest.mark.esphome_e2e,
    pytest.mark.skipif(
        shutil.which("esphome") is None and not MINI_BINARY.exists(),
        reason="esphome CLI not on PATH",
    ),
]


def _venus_poll(power: int) -> list[str]:
    return ["HMG-50", VENUS, "HME-3", "000000000000", "A", str(power)]


def _mini_poll(mac: str, status: int = 1) -> list[str]:
    return ["VNSEM-0", mac, "HME-3", "000000000000", "0", str(status)]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Transport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append(data)


class PythonBackend:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.grid = [0.0, 0.0, 0.0]
        self.ct = CT002(
            ct_type="HME-3",
            active_control=True,
            consumer_ttl=100000,
            clock=self.clock,
            battery_power_sensors=f"{MINI_SENSOR}=sensor.mini_power@B",
        )
        assert self.ct._power_sensors is not None
        self.ct._power_sensors._clock = self.clock

        async def before_send(*_args, **_kwargs):
            return list(self.grid)

        self.ct.before_send = before_send

    def set_grid(self, l1: float) -> None:
        self.grid = [l1, 0.0, 0.0]

    def advance(self, seconds: float) -> None:
        self.clock.now += seconds

    def battery_sensor(self, mac: str, watts: float | None) -> None:
        sensors = self.ct._power_sensors
        assert sensors is not None
        if watts is None:
            sensors._readings.pop(mac, None)
        else:
            sensors._readings[mac] = (-watts, self.clock())

    def poll(self, fields: list[str]) -> list[str]:
        transport = _Transport()
        asyncio.run(
            self.ct._handle_request(
                build_payload(fields), ("127.0.0.1", 40000), transport
            )
        )
        out, err = parse_request(transport.sent[-1])
        assert err is None
        return out


class EsphomeBackend:
    def __init__(self) -> None:
        self._ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ctrl.settimeout(2.0)
        self._cmd("clock_set 1000")

    def _cmd(self, cmd: str) -> None:
        self._ctrl.sendto(cmd.encode(), ("127.0.0.1", CONTROL_PORT))
        reply = self._ctrl.recvfrom(256)[0].decode()
        assert reply.startswith("ok"), f"{cmd!r}: {reply!r}"

    def set_grid(self, l1: float) -> None:
        self._cmd(f"grid {l1} 0 0")

    def advance(self, seconds: float) -> None:
        self._cmd(f"clock_advance {seconds}")

    def battery_sensor(self, mac: str, watts: float | None) -> None:
        self._cmd(f"battery_sensor {mac} {'nan' if watts is None else watts}")

    def poll(self, fields: list[str]) -> list[str]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        try:
            s.sendto(build_payload(fields), ("127.0.0.1", UDP_PORT))
            data = s.recvfrom(512)[0]
        finally:
            s.close()
        out, err = parse_request(data)
        assert err is None
        return out

    def close(self) -> None:
        self._ctrl.close()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


@pytest.fixture
def esphome() -> Iterator[EsphomeBackend]:
    if not MINI_BINARY.exists():
        subprocess.run(
            ["esphome", "compile", str(MINI_YAML)], check=True, cwd=REPO_ROOT
        )
    deadline = time.monotonic() + 5.0
    while _port_in_use(UDP_PORT) and time.monotonic() < deadline:
        time.sleep(0.1)
    proc = subprocess.Popen(
        [str(MINI_BINARY)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    deadline = time.monotonic() + 5.0
    while not _port_in_use(UDP_PORT) and time.monotonic() < deadline:
        time.sleep(0.1)
    backend = EsphomeBackend()
    yield backend
    backend.close()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    proc.wait(timeout=5)


def _scenario(backend) -> list[tuple[str, list[str]]]:
    """One fixed script; returns every response with a label."""
    out: list[tuple[str, list[str]]] = []

    def step(label: str, fields: list[str]) -> None:
        out.append((label, backend.poll(fields)))
        backend.advance(1.0)

    # Mixed fleet importing: Venus, a sensor mini (discharging 200 W) and a
    # blind mini share the grid.
    backend.battery_sensor(MINI_SENSOR, -200.0)
    backend.set_grid(900.0)
    for _ in range(3):
        backend.battery_sensor(MINI_SENSOR, -200.0)
        step("venus", _venus_poll(300))
        step("sensor-mini", _mini_poll(MINI_SENSOR))
        step("blind-mini", _mini_poll(MINI_BLIND))
    # Big load: the blind mini is paced.
    backend.set_grid(5000.0)
    backend.battery_sensor(MINI_SENSOR, -200.0)
    step("blind-mini paced", _mini_poll(MINI_BLIND))
    # Export while every measured battery discharges: the blind mini is held.
    backend.set_grid(-500.0)
    backend.battery_sensor(MINI_SENSOR, -200.0)
    step("venus export", _venus_poll(869))
    backend.battery_sensor(MINI_SENSOR, -200.0)
    step("blind-mini held", _mini_poll(MINI_BLIND))
    # The sensor drops out: that mini falls back to the blind behaviour.
    backend.battery_sensor(MINI_SENSOR, None)
    backend.set_grid(300.0)
    step("sensor-mini fallback", _mini_poll(MINI_SENSOR))
    return out


def test_mini_parity(esphome: EsphomeBackend) -> None:
    python_out = _scenario(PythonBackend())
    esphome_out = _scenario(esphome)
    assert [label for label, _ in python_out] == [label for label, _ in esphome_out]
    for (label, py), (_, esp) in zip(python_out, esphome_out, strict=True):
        assert py == esp, f"{label}: python {py} != esphome {esp}"


def test_mini_behaviour(esphome: EsphomeBackend) -> None:
    responses = dict(_scenario(esphome))
    blind = responses["blind-mini"]
    assert blind[19] == "3"  # x_dchrg: 1/3 next to a Venus and a sensor mini
    assert len(blind) == 28  # CT003
    sensor = responses["sensor-mini"]
    assert sensor[19] == "1"  # steered through its sensor, applies its own target
    assert responses["blind-mini paced"][4] == "900"  # 300 W x 3 sharing
    assert responses["blind-mini held"][4] == "0"  # never told to charge
    assert responses["sensor-mini fallback"][4] == "300"  # raw grid again
