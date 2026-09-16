"""Entrega de alertas: fichero, Telegram y correo.

**WhatsApp queda fuera, y no por poco.** Verificado el 2026-09-02: la Cloud API
exige un número de teléfono dedicado que nunca haya estado en WhatsApp normal
—y que deja de funcionar en la app al asociarlo—, una cuenta de Meta Business
con verificación documental pasado cierto volumen, y pago por mensaje: el tier
gratuito de 1.000 conversaciones mensuales desapareció en el modelo de 2026, y
desde octubre de 2026 hasta los mensajes de servicio se cobran. Las librerías
no oficiales que automatizan WhatsApp Web violan los términos del servicio y
exponen el número a un bloqueo.

Telegram y correo cubren lo mismo sin ninguna de esas ataduras.

## Principio de diseño

**Un canal que falla no puede tumbar el ciclo.** Una alerta que no se entrega
es un problema; una excepción que aborta el análisis nocturno es peor. Cada
canal captura sus propios errores, los registra y devuelve si tuvo éxito, de
modo que un token caducado de Telegram no impide que el correo salga ni que el
informe quede en disco.

El canal de fichero no necesita configuración y siempre está: es la red de
seguridad para cuando los otros dos fallan.
"""

from __future__ import annotations

import smtplib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import httpx

from core.obs.logging import get_logger, registrar_secreto

log = get_logger(__name__)

TIMEOUT = 20.0

#: Telegram rechaza mensajes por encima de este tamaño.
LIMITE_TELEGRAM = 4096

#: Pausa entre mensajes de una misma tanda. Telegram limita a unos 30 por
#: segundo por bot y corta con 429 al pasarse; un informe partido en cinco
#: trozos entra de sobra, pero la pausa evita que veinte valores no entren.
PAUSA_TELEGRAM = 0.35


@dataclass(frozen=True)
class Mensaje:
    """Un contenido y sus formas, no tres contenidos distintos.

    Nació porque el informe diario dejó de ser una línea de texto. Telegram
    quiere trozos cortos que se lean en un móvil; el correo quiere HTML con
    jerarquía; el fichero de disco quiere texto plano que se pueda abrir con
    cualquier cosa dentro de diez años. La alternativa —tres funciones de envío
    y tres cuerpos— garantizaba que un día dijeran cosas distintas.

    `texto` es obligatorio y es el que siempre funciona: si un canal no sabe
    hacer nada mejor, manda eso y entrega igual.
    """

    asunto: str
    texto: str
    html: str | None = None
    #: El mismo contenido ya partido y formateado para Telegram, en su HTML.
    trozos: tuple[str, ...] = field(default_factory=tuple)
    #: Imágenes que el HTML referencia por `cid:`. Van adjuntas y no como
    #: `data:` porque Gmail bloquea las URI de datos en `<img>`, que es
    #: exactamente el caso para el que existen.
    imagenes: tuple[tuple[str, bytes], ...] = field(default_factory=tuple)


@dataclass
class Envio:
    canal: str
    ok: bool
    detalle: str = ""

    def __str__(self) -> str:
        return f"[{'OK' if self.ok else 'FALLO'}] {self.canal}" + (
            f": {self.detalle}" if self.detalle else ""
        )


class Canal(ABC):
    nombre: str

    @abstractmethod
    def _entregar(self, asunto: str, cuerpo: str) -> str: ...

    def enviar(self, asunto: str, cuerpo: str) -> Envio:
        """Nunca lanza. Un canal caído no puede abortar el ciclo."""
        try:
            detalle = self._entregar(asunto, cuerpo)
            return Envio(self.nombre, True, detalle)
        except Exception as e:  # noqa: BLE001 — cualquier fallo se degrada, no propaga
            log.warning(
                "canal de entrega fallido",
                extra={"canal": self.nombre, "error": type(e).__name__},
            )
            return Envio(self.nombre, False, f"{type(e).__name__}: {e}")

    def enviar_mensaje(self, m: Mensaje) -> Envio:
        """Entrega un `Mensaje` con la mejor forma que este canal sepa dar.

        El comportamiento por defecto es el texto plano, de modo que un canal
        que no sepa nada de HTML ni de trozos sigue entregando. Los que saben
        más, lo sobrescriben.
        """
        return self.enviar(m.asunto, m.texto)


@dataclass
class CanalFichero(Canal):
    """Siempre disponible y sin configuración. Es la red de seguridad."""

    directorio: Path
    nombre: str = "fichero"

    def _entregar(self, asunto: str, cuerpo: str) -> str:
        self.directorio.mkdir(parents=True, exist_ok=True)
        marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        destino = self.directorio / f"{marca}-alerta.txt"
        destino.write_text(f"{asunto}\n{'=' * len(asunto)}\n\n{cuerpo}\n", encoding="utf-8")
        return str(destino)

    def enviar_mensaje(self, m: Mensaje) -> Envio:
        """El texto siempre, y el HTML al lado si viene.

        Guardar los dos cuesta unos kilobytes y permite abrir en el navegador
        exactamente lo que salió por correo, que es la forma barata de
        comprobar el informe cuando el correo no llega.
        """
        envio = self.enviar(m.asunto, m.texto)
        if envio.ok and m.html:
            try:
                base = Path(envio.detalle)
                gemelo = base.with_suffix(".html")
                html = m.html
                # `cid:` solo existe dentro de un correo. En disco se escriben
                # los PNG al lado y se apunta a ellos, para que el gemelo se
                # pueda abrir en el navegador y se vea lo mismo que se envió.
                for cid, datos in m.imagenes:
                    fichero = f"{base.stem}-{cid}.png"
                    (base.parent / fichero).write_bytes(datos)
                    html = html.replace(f"cid:{cid}", fichero)
                gemelo.write_text(html, encoding="utf-8")
                extra = f" + {len(m.imagenes)} img" if m.imagenes else ""
                return Envio(self.nombre, True,
                             f"{envio.detalle} + {gemelo.name}{extra}")
            except OSError as e:  # el texto ya está en disco: esto no lo invalida
                log.warning("informe HTML no escrito", extra={"error": str(e)})
        return envio


@dataclass
class CanalTelegram(Canal):
    """Bot de Telegram. Gratis, sin verificación y sin límite relevante.

    Para configurarlo: hablar con @BotFather, crear un bot, copiar el token, y
    obtener el chat_id escribiendo al bot y consultando `getUpdates`.
    """

    token: str
    chat_id: str
    nombre: str = "telegram"

    def __post_init__(self) -> None:
        registrar_secreto(self.token)

    def _entregar(self, asunto: str, cuerpo: str) -> str:
        texto = f"*{asunto}*\n\n```\n{cuerpo}\n```"
        if len(texto) > LIMITE_TELEGRAM:
            # Recortar por el final conserva lo más urgente: las alertas van
            # ordenadas por fecha, así que las primeras son las inminentes.
            sobra = len(texto) - LIMITE_TELEGRAM + 40
            cuerpo = cuerpo[: max(0, len(cuerpo) - sobra)] + "\n[...recortado]"
            texto = f"*{asunto}*\n\n```\n{cuerpo}\n```"

        return self._mandar(texto, "Markdown")

    def _mandar(self, texto: str, modo: str) -> str:
        r = httpx.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": texto, "parse_mode": modo,
                  "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        return f"enviado a {self.chat_id}"

    def enviar_mensaje(self, m: Mensaje) -> Envio:
        """Un informe largo se manda en varios mensajes, no recortado.

        El camino viejo metía el cuerpo entero dentro de un bloque ``` y lo
        cortaba por el final si no cabía: en un informe de análisis eso es
        tirar las secciones finales —riesgos, qué ha cambiado, conclusión— que
        son justamente las que no se pueden deducir mirando una cotización.

        El modo es HTML y no Markdown a propósito: el Markdown antiguo de
        Telegram revienta con un guion bajo o un corchete sueltos en el nombre
        de una empresa, y el mensaje entero se queda sin enviar con un 400.
        """
        if not m.trozos:
            return self.enviar(m.asunto, m.texto)
        try:
            for i, trozo in enumerate(m.trozos):
                if len(trozo) > LIMITE_TELEGRAM:
                    raise RuntimeError(
                        f"trozo {i + 1} de {len(m.trozos)} con {len(trozo)} caracteres: "
                        "el informe no se ha partido bien"
                    )
                self._mandar(trozo, "HTML")
                if i + 1 < len(m.trozos):
                    time.sleep(PAUSA_TELEGRAM)
        except Exception as e:  # noqa: BLE001 — igual que `enviar`: nunca propaga
            log.warning("canal de entrega fallido",
                        extra={"canal": self.nombre, "error": type(e).__name__})
            return Envio(self.nombre, False, f"{type(e).__name__}: {e}")
        return Envio(self.nombre, True,
                     f"{len(m.trozos)} mensaje(s) a {self.chat_id}")


@dataclass
class CanalCorreo(Canal):
    """SMTP con TLS. Con Gmail hace falta una contraseña de aplicación."""

    servidor: str
    puerto: int
    usuario: str
    contrasena: str
    destinatario: str
    nombre: str = "correo"

    def __post_init__(self) -> None:
        registrar_secreto(self.contrasena)

    def _entregar(self, asunto: str, cuerpo: str, html: str | None = None,
                  imagenes: tuple[tuple[str, bytes], ...] = ()) -> str:
        mensaje = EmailMessage()
        mensaje["Subject"] = asunto
        mensaje["From"] = self.usuario
        mensaje["To"] = self.destinatario
        # El texto plano va SIEMPRE como cuerpo principal y el HTML como
        # alternativa: un cliente que no renderice HTML, o un lector que lo
        # tenga desactivado, recibe el informe entero igual.
        mensaje.set_content(cuerpo)
        if html:
            mensaje.add_alternative(html, subtype="html")
            # Las imágenes cuelgan de la PARTE HTML, no del mensaje: si se
            # adjuntan al nivel de arriba, el cliente las enseña como ficheros
            # sueltos al final en vez de resolverlas por su `cid:`.
            parte = mensaje.get_payload()[-1]
            for cid, datos in imagenes:
                parte.add_related(datos, maintype="image", subtype="png",
                                  cid=f"<{cid}>", filename=f"{cid}.png")

        with smtplib.SMTP(self.servidor, self.puerto, timeout=TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(self.usuario, self.contrasena)
            smtp.send_message(mensaje)
        return f"enviado a {self.destinatario}"

    def enviar_mensaje(self, m: Mensaje) -> Envio:
        try:
            return Envio(self.nombre, True,
                         self._entregar(m.asunto, m.texto, m.html, m.imagenes))
        except Exception as e:  # noqa: BLE001 — un canal caído no aborta el ciclo
            log.warning("canal de entrega fallido",
                        extra={"canal": self.nombre, "error": type(e).__name__})
            return Envio(self.nombre, False, f"{type(e).__name__}: {e}")


def desde_configuracion(ajustes, directorio_informes: Path) -> list[Canal]:
    """Arma los canales que estén configurados.

    El de fichero entra siempre. Los otros dos solo si tienen credenciales, de
    forma que el sistema funciona sin configurar nada y mejora al configurarlo.
    """
    canales: list[Canal] = [CanalFichero(directorio_informes)]

    token = getattr(ajustes, "telegram_bot_token", None)
    chat = getattr(ajustes, "telegram_chat_id", None)
    if token and chat:
        canales.append(
            CanalTelegram(
                token=token.get_secret_value() if hasattr(token, "get_secret_value") else token,
                chat_id=chat,
            )
        )

    for campo in ("smtp_servidor", "smtp_usuario", "smtp_contrasena", "correo_destino"):
        if not getattr(ajustes, campo, None):
            break
    else:
        clave = ajustes.smtp_contrasena
        canales.append(
            CanalCorreo(
                servidor=ajustes.smtp_servidor,
                puerto=getattr(ajustes, "smtp_puerto", 587),
                usuario=ajustes.smtp_usuario,
                contrasena=clave.get_secret_value() if hasattr(clave, "get_secret_value") else clave,
                destinatario=ajustes.correo_destino,
            )
        )

    return canales


def difundir(canales: list[Canal], asunto: str, cuerpo: str) -> list[Envio]:
    """Envía por todos los canales. Devuelve el resultado de cada uno."""
    envios = [c.enviar(asunto, cuerpo) for c in canales]
    if not any(e.ok for e in envios):
        log.error("ningún canal entregó la alerta", extra={"asunto": asunto})
    return envios


def difundir_mensaje(canales: list[Canal], m: Mensaje) -> list[Envio]:
    """Lo mismo, pero dejando que cada canal use la forma que sepa dar."""
    envios = [c.enviar_mensaje(m) for c in canales]
    if not any(e.ok for e in envios):
        log.error("ningún canal entregó el informe", extra={"asunto": m.asunto})
    return envios
