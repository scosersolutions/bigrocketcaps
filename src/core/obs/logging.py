"""Logging estructurado JSONL con trace id y redacción de secretos.

Todo log lleva trace_id. Ningún log lleva un secreto: la redacción actúa sobre
valores registrados explícitamente y sobre patrones habituales de credencial.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"
_MIN_SECRET_LEN = 8

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_secretos: set[str] = set()

# Patrones de credencial en texto libre. Cada patrón conserva el prefijo (grupo 1)
# y sustituye el valor por REDACTED.
# El orden importa: "Bearer" va primero porque, si no, el patrón genérico
# redactaría la palabra "Bearer" y dejaría el token a la vista.
_PATRONES = (
    re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]{10,})", re.IGNORECASE),
    re.compile(
        r"((?:api[_-]?key|apikey|secret|token|password|passwd|authorization|auth)"
        r"\"?\s*[=:]\s*\"?)([^\s\"',}]{6,})",
        re.IGNORECASE,
    ),
)


def registrar_secreto(valor: str | None) -> None:
    """Marca un valor como secreto para que nunca aparezca en los logs."""
    if valor and len(valor) >= _MIN_SECRET_LEN:
        _secretos.add(valor)


def limpiar_secretos_registrados() -> None:
    """Solo para tests."""
    _secretos.clear()


def redactar(texto: str) -> str:
    # De más largo a más corto: si un secreto es prefijo de otro, reemplazar
    # primero el corto dejaría a la vista el resto del largo. Con una clave
    # pública y su versión extendida registradas, salía "***REDACTED***-larga".
    for secreto in sorted(_secretos, key=len, reverse=True):
        if secreto in texto:
            texto = texto.replace(secreto, REDACTED)
    for patron in _PATRONES:
        texto = patron.sub(rf"\1{REDACTED}", texto)
    return texto


def _redactar_valor(valor: Any) -> Any:
    if isinstance(valor, str):
        return redactar(valor)
    if isinstance(valor, dict):
        return {k: _redactar_valor(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_redactar_valor(v) for v in valor]
    return valor


def nuevo_trace_id() -> str:
    tid = uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    return tid


def set_trace_id(tid: str) -> None:
    _trace_id.set(tid)


def get_trace_id() -> str:
    return _trace_id.get()


class JsonFormatter(logging.Formatter):
    _RESERVADOS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "msg": redactar(record.getMessage()),
        }
        extras = {
            k: _redactar_valor(v) for k, v in record.__dict__.items() if k not in self._RESERVADOS
        }
        if extras:
            payload["ctx"] = extras
        if record.exc_info:
            payload["exc"] = redactar(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(log_dir: Path, level: str = "INFO", *, to_stdout: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = JsonFormatter()

    fichero = logging.FileHandler(log_dir / "core.jsonl", encoding="utf-8")
    fichero.setFormatter(formatter)
    root.addHandler(fichero)

    if to_stdout:
        consola = logging.StreamHandler()
        consola.setFormatter(formatter)
        root.addHandler(consola)


def get_logger(nombre: str) -> logging.Logger:
    return logging.getLogger(nombre)
