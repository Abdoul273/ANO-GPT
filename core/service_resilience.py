"""Protections légères des fournisseurs réseau en lecture seule.

Le circuit est partagé entre appels voix/MCP, par service et non par outil.
Les erreurs de configuration ne déclenchent ni relance ni ouverture du circuit.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class ServiceUnavailable(RuntimeError):
    """Fournisseur en refroidissement ; essayer le fournisseur de secours."""


class ServiceCircuit:
    def __init__(self, failures: int = 3, cooldown_s: float = 300.0):
        self.failures = failures
        self.cooldown_s = cooldown_s
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def available(self, service: str) -> bool:
        with self._lock:
            count, until = self._state.get(service, (0, 0.0))
            if until and time.monotonic() >= until:
                self._state.pop(service, None)
                return True
            return count < self.failures or not until

    def success(self, service: str) -> None:
        with self._lock:
            self._state.pop(service, None)

    def failure(self, service: str) -> None:
        with self._lock:
            count, until = self._state.get(service, (0, 0.0))
            if until and time.monotonic() >= until:
                count = 0
            count += 1
            self._state[service] = (
                count, time.monotonic() + self.cooldown_s if count >= self.failures else 0.0,
            )


SERVICES = ServiceCircuit()


def read_with_retry(
    service: str,
    call: Callable[[], T],
    *,
    transient: Callable[[Exception], bool],
    attempts: int = 2,
    initial_delay_s: float = 0.25,
    circuit: ServiceCircuit = SERVICES,
) -> T:
    """Relance une lecture transitoire, puis laisse l'appelant utiliser son repli.

    Une série de tentatives ratées compte pour un échec de service. Les écritures
    et opérations facturables ne doivent jamais passer par cette fonction.
    """
    if not circuit.available(service):
        raise ServiceUnavailable(f"{service} temporairement indisponible")
    for attempt in range(attempts):
        try:
            result = call()
        except Exception as exc:
            if not transient(exc):
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is None:
                    status = getattr(exc, "code", None)
                if status in (401, 403):
                    circuit.failure(service)
                raise
            if attempt + 1 >= attempts:
                circuit.failure(service)
                raise
            time.sleep(initial_delay_s * (2 ** attempt))
        else:
            circuit.success(service)
            return result
    raise AssertionError("attempts doit être positif")
