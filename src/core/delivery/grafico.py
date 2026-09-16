"""Gráfico de línea en PNG, con la biblioteca estándar y nada más.

## Por qué no matplotlib

Porque para una línea son 40 MB de dependencias en un repositorio que declara
nueve y que genera su cuaderno en HTML sin framework. Un PNG es una cabecera,
un bloque `zlib` y tres CRC: `zlib` y `struct` ya están en la biblioteca
estándar, y esto cabe en un fichero.

## Por qué el PNG no lleva ni un número

Dibujar texto sin una librería de fuentes obliga a empotrar un mapa de bits por
carácter, y eso es donde este fichero dejaría de caber en una cabeza. No hace
falta: el correo ya lleva las cifras en HTML, que además se pueden seleccionar,
leer con un lector de pantalla y buscar. **La imagen aporta la forma; el texto,
los valores.** Repetir los números dentro del PNG sería empeorar las dos cosas.

## Por qué se dibuja al doble y se reduce

Sin suavizado, una línea inclinada en 640 px sale como una escalera. Dibujar al
doble y promediar cada cuadro de 2x2 da el suavizado por el camino corto, sin
tocar el trazado. Cuesta cuatro veces la memoria de un mapa de bits de 1.280 x
440, que son 1,7 MB: irrelevante para un proceso que ya tiene la base abierta.

## Y por qué no va en el cuaderno

El cuaderno dibuja en SVG en el navegador, que es mejor: escala, se lee en
cualquier tamaño y pesa menos. Esto existe **solo** porque el correo no puede
hacer eso — Gmail borra el SVG en línea— y un adjunto PNG referenciado por
`cid:` sí se ve. No es una segunda forma de dibujar lo mismo; es la única que
funciona en un cliente de correo.
"""

from __future__ import annotations

import struct
import zlib

#: Se dibuja a este factor y se reduce promediando. 2 basta: a 3 la mejora ya
#: no se ve y el mapa de bits se pone en 4 MB.
SUPER = 2

Color = tuple[int, int, int]


class Lienzo:
    """Mapa de bits RGB con lo justo para pintar una serie."""

    def __init__(self, ancho: int, alto: int, fondo: Color) -> None:
        self.w, self.h = ancho, alto
        self.px = bytearray(bytes(fondo) * (ancho * alto))

    def punto(self, x: int, y: int, c: Color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def linea(self, x0: int, y0: int, x1: int, y1: int, c: Color,
              grosor: int = 1) -> None:
        """Bresenham. El grosor se hace engordando en perpendicular al eje
        dominante: en una serie temporal la pendiente rara vez es vertical, así
        que engordar en vertical es suficiente y evita el caso degenerado."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            for k in range(-(grosor // 2), grosor // 2 + 1):
                self.punto(x0, y0 + k, c)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def rayas(self, y: int, c: Color, patron: int = 6) -> None:
        """Horizontal discontinua, para la línea de referencia."""
        for x in range(0, self.w, patron):
            for k in range(patron // 2):
                self.punto(x + k, y, c)

    def disco(self, cx: int, cy: int, r: int, c: Color) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self.punto(x, y, c)

    def reducir(self, factor: int) -> "Lienzo":
        if factor == 1:
            return self
        w, h = self.w // factor, self.h // factor
        fuera = Lienzo(w, h, (0, 0, 0))
        n = factor * factor
        for y in range(h):
            for x in range(w):
                r = g = b = 0
                for dy in range(factor):
                    fila = ((y * factor + dy) * self.w + x * factor) * 3
                    for dx in range(factor):
                        i = fila + dx * 3
                        r += self.px[i]; g += self.px[i + 1]; b += self.px[i + 2]
                j = (y * w + x) * 3
                fuera.px[j:j + 3] = bytes((r // n, g // n, b // n))
        return fuera

    def png(self) -> bytes:
        # Cada scanline va precedida por su byte de filtro; 0 es «sin filtro»,
        # que para una imagen de pocos colores comprime de sobra.
        crudo = bytearray()
        for y in range(self.h):
            crudo.append(0)
            crudo += self.px[y * self.w * 3:(y + 1) * self.w * 3]

        def chunk(tipo: bytes, datos: bytes) -> bytes:
            return (struct.pack(">I", len(datos)) + tipo + datos
                    + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(crudo), 9))
                + chunk(b"IEND", b""))


#: Paleta del correo, que es de tema claro: los clientes no respetan
#: `prefers-color-scheme` de forma fiable y una imagen no se adapta sola.
FONDO = (255, 255, 255)
REJILLA = (232, 234, 237)
REFERENCIA = (154, 160, 166)
SUBE = (26, 127, 75)
BAJA = (179, 38, 30)


def serie(valores: list[float], ancho: int = 640, alto: int = 220,
          referencia: float | None = None, marcas: list[int] | None = None,
          color: Color | None = None) -> bytes | None:
    """Una serie y su línea de referencia, en PNG.

    `referencia` es la línea contra la que se lee todo —el primer valor, o el
    capital inicial— y entra SIEMPRE en la escala: sin ella, una serie que solo
    baja se dibujaría plana en el aire, sin decir respecto a qué baja.

    Devuelve `None` con menos de dos puntos, que es el caso en el que no hay
    recta que trazar. El correo se las arregla sin imagen; lo que no puede es
    enseñar una imagen que miente.
    """
    if not valores or len(valores) < 2:
        return None

    s = SUPER
    W, H = ancho * s, alto * s
    pad = 8 * s
    lz = Lienzo(W, H, FONDO)

    base = referencia if referencia is not None else valores[0]
    lo, hi = min(min(valores), base), max(max(valores), base)
    if hi - lo <= 0:
        hi, lo = hi + 1, lo - 1
    margen = (hi - lo) * 0.10
    lo -= margen; hi += margen

    def Y(v: float) -> int:
        return int(pad + (hi - v) / (hi - lo) * (H - 2 * pad))

    def X(i: int) -> int:
        return int(pad + i / (len(valores) - 1) * (W - 2 * pad))

    for k in range(1, 4):
        y = int(pad + k / 4 * (H - 2 * pad))
        lz.linea(pad, y, W - pad, y, REJILLA, grosor=s)

    lz.rayas(Y(base), REFERENCIA, patron=10 * s)

    c = color or (SUBE if valores[-1] >= base else BAJA)
    for i in range(len(valores) - 1):
        lz.linea(X(i), Y(valores[i]), X(i + 1), Y(valores[i + 1]), c, grosor=2 * s)

    # Las marcas son índices de la serie: en el informe, los días con compras
    # de insiders. Se dibujan encima de la línea, nunca desplazadas.
    for i in (marcas or []):
        if 0 <= i < len(valores):
            lz.disco(X(i), Y(valores[i]), 3 * s, (176, 106, 18))

    lz.disco(X(len(valores) - 1), Y(valores[-1]), 4 * s, c)
    return lz.reducir(s).png()
