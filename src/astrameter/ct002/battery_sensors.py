"""Measured battery power for batteries that do not report it (TEST PATCH).

Some batteries -- notably the Marstek Venus E Mini (VNSEM) -- report neither a
phase nor their real power over the CT protocol, so the balancer cannot steer
them like the other batteries. Any power source that measures such a battery
(a smart plug in front of it, a Home Assistant sensor, an MQTT topic, ...)
fills that gap, and the battery is then steered like any other.

Configured per battery as::

    BATTERY_POWER_SENSORS = <battery mac>=[-]<source>[@<phase>], ...

``<source>`` is either

* the name of a config section whose ``SOURCE`` key names a power meter type
  (``JSON_HTTP``, ``SHELLY``, ``TASMOTA``, ``MQTT``, ``HOMEASSISTANT``, ...) and
  whose other keys are that meter's usual keys, or
* a Home Assistant entity id (``sensor.mini_power``), read from
  ``BATTERY_SENSORS_HA_URL`` / ``BATTERY_SENSORS_HA_TOKEN`` (or, inside the
  add-on, through the Supervisor).

The source counts power the battery **draws** (charging) as positive, the way
a plug in front of it measures it; prefix it with ``-`` for the opposite sign.
``@A`` / ``@B`` / ``@C`` / ``@D`` (combined) is the phase a battery that reports
none is steered on; it defaults to A.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from astrameter.powermeter.base import Powermeter

logger = logging.getLogger("astrameter")

DEFAULT_PHASE = "A"
VALID_PHASES = ("A", "B", "C", "D")
POLL_INTERVAL_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
# A reading older than this is not used; the battery then falls back to the
# behaviour it would have without a sensor.
MAX_AGE_SECONDS = 5.0
# Failures this soon after start are the source still connecting.
STARTUP_GRACE_SECONDS = 10.0

#: Builds the power meter a config section describes, or ``None``.
SourceFactory = Callable[[str], "Powermeter | None"]


@dataclass(frozen=True)
class BatterySource:
    name: str
    phase: str = DEFAULT_PHASE
    # True when the source counts discharge as positive (``-`` prefix).
    inverted: bool = False

    @property
    def is_entity(self) -> bool:
        return "." in self.name


def parse_sources(spec: str) -> dict[str, BatterySource]:
    """``"aabb..=MINI_PLUG@B, ccdd..=-sensor.x"`` -> ``{mac: BatterySource}``."""
    sources: dict[str, BatterySource] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        mac, sep, target = part.partition("=")
        mac = mac.strip().lower().replace(":", "").replace("-", "")
        name, _, phase = target.partition("@")
        name = name.strip()
        inverted = name.startswith("-")
        name = name.lstrip("-").strip()
        phase = phase.strip().upper() or DEFAULT_PHASE
        if not sep or len(mac) != 12 or not name or phase not in VALID_PHASES:
            logger.error(
                "Ignoring BATTERY_POWER_SENSORS entry %r; expected "
                "<battery mac>=[-]<section or entity id>[@A|B|C|D]",
                part,
            )
            continue
        sources[mac] = BatterySource(name, phase, inverted)
    return sources


def build_ha_entity_meter(entity: str, ha_url: str, ha_token: str) -> Powermeter | None:
    """A Home Assistant reader for a single entity."""
    from astrameter.powermeter.homeassistant import HomeAssistant

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if ha_url:
        parsed = urlparse(ha_url if "://" in ha_url else f"http://{ha_url}")
        https = parsed.scheme == "https"
        host = parsed.hostname or ""
        port = str(parsed.port or (443 if https else 8123))
        prefix = parsed.path.rstrip("/") or None
        token = ha_token or supervisor_token
        if not host or not token:
            logger.error(
                "BATTERY_POWER_SENSORS: BATTERY_SENSORS_HA_URL needs a host and "
                "BATTERY_SENSORS_HA_TOKEN a long-lived access token"
            )
            return None
    elif supervisor_token:
        # Inside the Home Assistant add-on: go through the Supervisor proxy.
        https, host, port, prefix = False, "supervisor", "80", "/core"
        token = ""
    else:
        logger.error(
            "BATTERY_POWER_SENSORS names %s but there is no Home Assistant to "
            "read it from: set BATTERY_SENSORS_HA_URL and BATTERY_SENSORS_HA_TOKEN",
            entity,
        )
        return None

    def token_getter() -> str:
        return token or os.environ.get("SUPERVISOR_TOKEN", "")

    return HomeAssistant(
        host,
        port,
        https,
        token_getter,
        current_power_entity=[entity],
        power_calculate=False,
        power_input_alias=[],
        power_output_alias=[],
        path_prefix=prefix,
    )


class BatteryPowerSensors:
    def __init__(
        self,
        sources: dict[str, BatterySource],
        ha_url: str = "",
        ha_token: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources = sources
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._clock = clock
        self._factory: SourceFactory | None = None
        self._meters: dict[str, Powermeter] = {}
        # mac -> (battery power in watts, discharge positive; reading time)
        self._readings: dict[str, tuple[float, float]] = {}
        self._available: dict[str, bool] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._started_at = 0.0

    @classmethod
    def from_settings(
        cls, spec: str, ha_url: str = "", ha_token: str = ""
    ) -> BatteryPowerSensors | None:
        sources = parse_sources(spec)
        return cls(sources, ha_url, ha_token) if sources else None

    def set_source_factory(self, factory: SourceFactory | None) -> None:
        """How to build a meter from a config section name."""
        self._factory = factory

    def power_for(self, mac: str) -> int | None:
        """Measured battery power (discharge positive), or ``None`` if unknown."""
        reading = self._readings.get(mac.lower())
        if reading is None:
            return None
        power, at = reading
        if self._clock() - at > MAX_AGE_SECONDS:
            return None
        return round(power)

    def phase_for(self, mac: str) -> str:
        """The phase configured for *mac* (A when none was given)."""
        source = self._sources.get(mac.lower())
        return source.phase if source else DEFAULT_PHASE

    def _build(self, source: BatterySource) -> Powermeter | None:
        if not source.is_entity:
            if self._factory is not None:
                meter = self._factory(source.name)
                if meter is not None:
                    return meter
            logger.error(
                "BATTERY_POWER_SENSORS: no usable section [%s] (it needs a "
                "SOURCE key naming a power meter type, e.g. SOURCE = JSON_HTTP)",
                source.name,
            )
            return None
        return build_ha_entity_meter(source.name, self._ha_url, self._ha_token)

    async def start(self) -> None:
        self._started_at = self._clock()
        for mac, source in self._sources.items():
            meter = self._build(source)
            if meter is None:
                continue
            self._meters[mac] = meter
            logger.info(
                "Battery %s: reading its power from %s (phase %s%s)",
                mac,
                source.name,
                source.phase,
                ", inverted" if source.inverted else "",
            )
            try:
                await meter.start()
            except Exception:
                logger.exception("Battery %s: %s failed to start", mac, source.name)
                continue
            self._tasks.append(asyncio.create_task(self._poll(mac, source, meter)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for meter in self._meters.values():
            with contextlib.suppress(Exception):
                await meter.stop()
        self._meters.clear()

    async def _poll(self, mac: str, source: BatterySource, meter: Powermeter) -> None:
        while True:
            try:
                values = await asyncio.wait_for(
                    meter.get_powermeter_watts(), READ_TIMEOUT_SECONDS
                )
                drawn = float(sum(values))
                # The source counts charging as positive; the balancer counts
                # discharge as positive.
                power = drawn if source.inverted else -drawn
                self._readings[mac] = (power, self._clock())
                self._note(mac, source, True)
                logger.debug(
                    "Battery %s: %s reads %s W (battery %s W)",
                    mac,
                    source.name,
                    drawn,
                    power,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note(mac, source, False, f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _note(
        self, mac: str, source: BatterySource, available: bool, reason: str = ""
    ) -> None:
        if self._available.get(mac) == available:
            return
        if (
            not available
            and mac not in self._available
            and self._clock() - self._started_at < STARTUP_GRACE_SECONDS
        ):
            return  # still connecting; not worth a warning yet
        self._available[mac] = available
        if available:
            logger.info("Battery %s: power from %s is live", mac, source.name)
        else:
            logger.warning(
                "Battery %s: %s unusable (%s); using the battery's own report",
                mac,
                source.name,
                reason,
            )
