"""Cliente del Centro de Control. COPIA VERBATIM, no editar aqui.

Origen: `scosersolutions/control-center`, `sdk/python/control_center.py`,
copiado el 2026-09-16. Un arreglo va alli primero y luego se vuelve a copiar;
parchearlo aqui crearia dos SDK que se parecen.

## Por que copiado y no instalado

El SDK es un fichero de biblioteca estandar, sin dependencias, escrito para
copiarse. Publicarlo en PyPI para una sola linea de `import` seria montar un
paquete, su version y su release solo para esto. Es el mismo trato que ya
tiene `src/core/`: se paga el duplicado, que se ve, en vez de la fontaneria,
que no se ve.
"""
# Docstring del original:
#   SDK cliente en Python para la API de Control Center.
#
#   Solo biblioteca estandar, sin dependencias externas. Python 3.11+.

import email.utils
import json
import os
import random
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.3
MAX_BACKOFF_SECONDS = 8.0
MAX_ERROR_LENGTH = 4000

#: Sin esto urllib manda `Python-urllib/3.12`, y Cloudflare lo bloquea con
#: un 403 ANTES de que la peticion llegue al Worker. El cliente se quedaba
#: sin saber por que: esa respuesta es HTML, asi que el SDK solo podia
#: decir `invalid_response`. Comprobado el 2026-09-17 contra el despliegue
#: real: con este User-Agent responde 401 -token invalido, o sea que
#: llega-; con el de urllib, 403 sin llegar.
USER_AGENT = "control-center-sdk-python/1 (+https://github.com/scosersolutions/control-center)"


class ControlCenterError(Exception):
    """Error de la API (codigo + mensaje del sobre) o fallo de red tras agotar reintentos."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.status}] {self.code}: {self.message}"


class Paused(Exception):
    """
    La automatizacion esta pausada en el Centro de Control.

    Solo la lanza `run(slug, raise_if_paused=True)`, para quien prefiera el
    idioma `with contextlib.suppress(Paused), cc.run(slug, raise_if_paused=True) as r:`.
    """


class RunHandle:
    """Handle expuesto dentro del bloque `with cc.run(...) as r:`."""

    def __init__(self, run_id: str, client: "ControlCenter", skipped: bool = False) -> None:
        self.run_id = run_id
        self.summary: Optional[str] = None
        self.metrics: dict[str, Any] = {}
        #: True si la automatizacion estaba pausada. El run ya se cerro como
        #: "skipped" y este handle no envia nada mas.
        self.skipped = skipped
        self._client = client

    def event(self, level: str, title: str, body: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Registra un evento (level: info|warn|critical). No hace nada si el run esta saltado."""
        if self.skipped:
            return None
        return self._client.event(level, title, body)

    def result(self, key: str, payload: Any) -> Optional[dict[str, Any]]:
        """Publica un resultado bajo la clave `key`. No hace nada si el run esta saltado."""
        if self.skipped:
            return None
        return self._client.result(key, payload)


class ControlCenter:
    """
    Cliente para la API de Control Center.

    Si no se pasan base_url/token, se leen de las variables de entorno
    CONTROL_CENTER_URL y CONTROL_CENTER_TOKEN como valores por defecto.
    """

    def __init__(self, base_url: str = "", token: str = "", retries: int = DEFAULT_RETRIES) -> None:
        resolved_base_url = base_url or os.environ.get("CONTROL_CENTER_URL", "")
        resolved_token = token or os.environ.get("CONTROL_CENTER_TOKEN", "")
        if not resolved_base_url:
            raise ValueError("Falta base_url (o la variable de entorno CONTROL_CENTER_URL)")
        if not resolved_token:
            raise ValueError("Falta token (o la variable de entorno CONTROL_CENTER_TOKEN)")

        self.base_url = resolved_base_url.rstrip("/")
        self.token = resolved_token
        self.retries = retries

    def self_info(self) -> dict[str, Any]:
        """GET /api/ingest/self: datos de la automatizacion y si esta activa."""
        return self._request("GET", "/api/ingest/self")

    def start_run(self, idempotency_key: Optional[str] = None, started_at: Optional[int] = None) -> dict[str, Any]:
        """POST /api/ingest/runs/start."""
        body: dict[str, Any] = {}
        if idempotency_key is not None:
            body["idempotencyKey"] = idempotency_key
        if started_at is not None:
            body["startedAt"] = started_at
        return self._request("POST", "/api/ingest/runs/start", body)

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: Optional[str] = None,
        error: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """POST /api/ingest/runs/{runId}/finish."""
        body: dict[str, Any] = {"status": status}
        if summary is not None:
            body["summary"] = summary
        if error is not None:
            body["error"] = error
        if metrics is not None:
            body["metrics"] = metrics
        path = f"/api/ingest/runs/{urllib.parse.quote(run_id, safe='')}/finish"
        return self._request("POST", path, body)

    def event(self, level: str, title: str, body: Optional[str] = None) -> dict[str, Any]:
        """POST /api/ingest/events."""
        payload: dict[str, Any] = {"level": level, "title": title}
        if body is not None:
            payload["body"] = body
        return self._request("POST", "/api/ingest/events", payload)

    def result(self, key: str, payload: Any) -> dict[str, Any]:
        """POST /api/ingest/results."""
        return self._request("POST", "/api/ingest/results", {"key": key, "payload": payload})

    def commands(self) -> dict[str, Any]:
        """GET /api/ingest/commands (al leerlos quedan marcados como recogidos)."""
        return self._request("GET", "/api/ingest/commands")

    def ack_commands(self, ids: list[str]) -> dict[str, Any]:
        """POST /api/ingest/commands/ack."""
        return self._request("POST", "/api/ingest/commands/ack", {"ids": ids})

    def run(
        self,
        slug: str,
        idempotency_key: Optional[str] = None,
        raise_if_paused: bool = False,
    ) -> "_RunContext":
        """
        Context manager: `with cc.run("mi-slug") as r: ...`.

        Comprueba que el token corresponde a `slug`, abre el run y lo cierra con
        "ok" o "error" segun el resultado del bloque.

        Si la automatizacion esta pausada, el run se cierra al instante como
        "skipped" y `r.skipped` vale True; `r.event()` y `r.result()` pasan a no
        hacer nada. **El cuerpo del `with` se ejecuta igualmente**: Python no
        permite que `__enter__` lo salte sin recurrir a `sys.settrace`, que
        romperia depuradores y medidores de cobertura del proceso. Tienes tres
        formas de saltarlo de verdad:

            with cc.run("slug") as r:
                if r.skipped:
                    return
                ...

            with contextlib.suppress(Paused), cc.run("slug", raise_if_paused=True) as r:
                ...

            cc.with_run("slug", lambda r: ...)   # equivalente exacto a withRun del SDK TS
        """
        return _RunContext(self, slug, idempotency_key, raise_if_paused)

    def with_run(
        self,
        slug: str,
        fn: "Callable[[RunHandle], Any]",
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Equivalente exacto de `withRun` del SDK TypeScript: si la automatizacion
        esta pausada NO llama a `fn` y devuelve {"skipped": True}. Si `fn` lanza,
        cierra el run como "error" y vuelve a lanzar la excepcion original.
        """
        with self.run(slug, idempotency_key=idempotency_key) as handle:
            if handle.skipped:
                return {"skipped": True, "value": None}
            return {"skipped": False, "value": fn(handle)}

    def _finish_run_safely(
        self,
        run_id: str,
        status: str,
        summary: Optional[str] = None,
        error: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        # El cierre del run se intenta siempre. Si falla (tras agotar reintentos)
        # no debe enmascarar el resultado original del bloque `with`: solo se
        # deja constancia en stderr.
        try:
            self.finish_run(run_id, status, summary=summary, error=error, metrics=metrics)
        except ControlCenterError as finish_error:
            print(f"[control-center] No se pudo finalizar el run {run_id}: {finish_error}", file=sys.stderr)

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data: Optional[bytes] = None
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
        }

        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise ControlCenterError(
                    0,
                    "payload_too_large",
                    f"El payload supera el limite de 64 KB ({len(payload)} bytes)",
                )
            data = payload
            headers["Content-Type"] = "application/json"

        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request) as response:
                    return self._parse_envelope(response.status, response.read())
            except urllib.error.HTTPError as http_error:
                error_body = http_error.read()
                if _is_retryable_status(http_error.code) and attempt < self.retries:
                    retry_after = _parse_retry_after(http_error.headers.get("Retry-After"))
                    time.sleep(retry_after if retry_after is not None else _compute_backoff(attempt))
                    continue
                return self._parse_envelope(http_error.code, error_body)
            except urllib.error.URLError as url_error:
                last_error = url_error
                if attempt == self.retries:
                    break
                time.sleep(_compute_backoff(attempt))
                continue

        message = str(last_error) if last_error is not None else "error desconocido"
        raise ControlCenterError(0, "network_error", f"Fallo de red tras {self.retries + 1} intento(s): {message}")

    def _parse_envelope(self, status: int, raw_body: bytes) -> dict[str, Any]:
        try:
            envelope = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as decode_error:
            raise ControlCenterError(
                status, "invalid_response", "La respuesta de la API no es JSON valido"
            ) from decode_error

        if envelope.get("ok") is True:
            return envelope.get("data") or {}

        error = envelope.get("error") or {}
        raise ControlCenterError(
            status,
            error.get("code", "unknown_error"),
            error.get("message", "Error desconocido"),
        )


class _RunContext:
    """Objeto devuelto por `ControlCenter.run(slug)`; implementa el protocolo `with`."""

    def __init__(
        self,
        client: ControlCenter,
        slug: str,
        idempotency_key: Optional[str] = None,
        raise_if_paused: bool = False,
    ) -> None:
        self._client = client
        self._slug = slug
        self._idempotency_key = idempotency_key
        self._raise_if_paused = raise_if_paused
        self._run_id: Optional[str] = None
        self._skip = False
        self.handle: Optional[RunHandle] = None

    def __enter__(self) -> RunHandle:
        info = self._client.self_info()
        automation = info["automation"]
        if automation["slug"] != self._slug:
            raise ControlCenterError(
                0,
                "slug_mismatch",
                f'El token pertenece a la automatizacion "{automation["slug"]}", no a "{self._slug}" '
                "(token de otra automatizacion?)",
            )

        start = self._client.start_run(idempotency_key=self._idempotency_key)
        self._run_id = start["runId"]

        if not info["enabled"]:
            self._skip = True
            self._client._finish_run_safely(self._run_id, "skipped", summary="Automatizacion pausada")
            self.handle = RunHandle(self._run_id, self._client, skipped=True)
            if self._raise_if_paused:
                raise Paused(f'La automatizacion "{self._slug}" esta pausada')
            return self.handle

        self.handle = RunHandle(self._run_id, self._client)
        return self.handle

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        # El run saltado ya se cerro en __enter__: no hay nada que finalizar.
        if self._skip:
            return False

        if self._run_id is None:
            return False

        if exc_type is None:
            summary = self.handle.summary if self.handle else None
            metrics = (self.handle.metrics or None) if self.handle else None
            self._client._finish_run_safely(self._run_id, "ok", summary=summary, metrics=metrics)
            return False

        trace_text = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        message = f"{exc_val}\n{trace_text}" if trace_text else str(exc_val)
        self._client._finish_run_safely(self._run_id, "error", error=message[:MAX_ERROR_LENGTH])
        return False  # No se silencia: la excepcion original del bloque se vuelve a lanzar.


def _compute_backoff(attempt: int) -> float:
    """
    Full jitter: retardo aleatorio entre 0 y el techo exponencial, para que
    varios procesos que fallan a la vez no reintenten todos en el mismo instante.
    """
    cap = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2**attempt))
    return random.uniform(0, cap)


def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    if not header_value:
        return None
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(header_value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


def _is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


if __name__ == "__main__":
    # Ejemplo minimo de uso. Requiere CONTROL_CENTER_URL y CONTROL_CENTER_TOKEN
    # en el entorno (o pasarlos explicitamente al constructor).
    cc = ControlCenter()

    with cc.run("mi-automatizacion") as r:
        if r.skipped:
            print("Automatizacion pausada: no se hace nada.")
        else:
            r.summary = "Ejecucion de ejemplo"
            r.metrics["procesados"] = 42
            r.event("info", "Arranque", body="Empezando el trabajo de ejemplo")
            r.result("ultimo-resultado", {"ok": True})
