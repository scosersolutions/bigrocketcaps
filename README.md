# Big Rocket Caps

Laboratorio cuantitativo de acciones estadounidenses. Responde una sola
pregunta, una y otra vez:

> ¿Esta idea de mercado tiene ventaja real, o es ruido bien contado?

El sesgo del sistema es hacia el **no**. Todo está montado para que una
hipótesis falsa muera cuanto antes, aunque el precio sea que casi ninguna
sobreviva. Ninguna ha sobrevivido todavía, y eso es un resultado, no un
pendiente.

Es el proyecto hermano de [MoonRocket](https://github.com/scosersolutions/MoonRocket),
que se queda como está. Aquí van las líneas nuevas.

## Qué NO es

Ni un robot de trading, ni un asesor, ni una fuente de señales. Es el andamio
de medición que tendría que existir antes de cualquiera de esas tres cosas.

## De dónde viene, y qué trae puesto

`src/core/` es una copia congelada del núcleo de MoonRocket: motor de backtest,
cartera, juez, modelo de costes, cadena de registros y esquema de datos. Se
copió en vez de compartirse como paquete por una razón medida: extraerlo
conservando la historia de git y repartir 667 tests entre dos repositorios son
30-55 horas de trabajo antes de que exista un solo dato nuevo aquí. Se paga el
duplicado, que es visible, en vez de meses de fontanería, que no lo son.

Viene con los controles que MoonRocket fue necesitando, ya dentro:

| | |
|---|---|
| **Holdout con puerta** | `hipotesis/holdouts.toml` declara los periodos reservados y `validar()` mira el **rango real** de las velas. En MoonRocket el recorte vivía en cada script, y el que no lo tenía juzgaba igual: 8.016 velas de 43.080 se juzgaron dentro del holdout sin que nada avisara |
| **Costes que no se inventan** | El juez se niega a juzgar con un spread supuesto. El de 2,0 bps se queda corto en 5 de 7 activos, y con stops del 0,4-0,6 % aprueba lo que el coste real refuta |
| **Cadena de un solo escritor** | Los registros van encadenados por hash y solo los escribe quien debe; dos máquinas anotando la misma cadena la bifurcan sin remedio |
| **Contador de contrastes** | El Deflated Sharpe se alimenta del presupuesto declarado **antes** de medir, no de un número puesto a mano |

## Universo

Large y mid caps estadounidenses, con las puertas abiertas a más mercados y sin
construirlos todavía.

La elección no es un gusto: se midió el efecto de las compras de insiders por
decil de volumen en dólares sobre 34.352 señales de 2010 a 2026. En los dos
deciles más grandes —124 y 338 millones de dólares diarios— el exceso es
**negativo**. Por debajo de 50.000 $/día es −1,68 % con t = −6,41. Lo único que
sobrevive a winsorizar, a corregir por solapamiento y a la corrección por haber
mirado diez deciles es la banda de ~8,8 M $/día.

Eso **no autoriza nada**: mirar cuarenta celdas y quedarse con la bonita es
seleccionar la subrejilla ganadora. Es una restricción de dónde explorar, no
una hipótesis con evidencia. El detalle está en
[`efecto-por-tamano.md`](https://github.com/scosersolutions/MoonRocket/blob/main/docs/research/efecto-por-tamano.md).

## Datos

Todo gratuito, y lo que no se puede obtener gratis se queda fuera y se dice:

| Dato | Fuente | Estado |
|---|---|---|
| Presentaciones, Form 4, XBRL, 8-K | EDGAR (10 req/s, descargas masivas diarias) | Núcleo |
| Acciones en circulación | XBRL `frames` — 4.611 a 7.016 empresas por trimestre, 2012-2026 | Núcleo, es el universo |
| Precios diarios | Yahoo + Stooq, con verificación cruzada | Solo en local; sus términos prohíben redistribuirlos |
| Constituyentes históricos de índices | — | **Fuera**: no hay fuente gratuita fiable. Se sustituyen por deciles de capitalización reconstruida |
| Revisiones de analistas, guidance, intradía, préstamo | — | **Fuera** |

## Estado: parado el 2026-09-18

**Cinco hipótesis selladas, una medida y refutada, cero supervivientes. El
proyecto se para aquí**, y esta sección existe para que quien lo abra dentro de
seis meses no tenga que reconstruir por qué.

### Lo que se probó

| | predice | veredicto |
|---|---|---|
| **B1** ampliación de capital → presión bajista | corto | **REFUTADA**. +0,201 % de exceso, o sea al revés. El azar lo iguala el 30 % de las veces y cambia de signo entre 2018-2021 y 2022-2024 |
| **B2** entrada de activista | largo | Sellada, **sin medir válidamente** |
| **B3** prima de anuncio de resultados | largo | Sellada, **sin medir válidamente** |
| **B4** quiebra → venta forzada | corto | Sellada, **sin medir válidamente** |
| **B5** registro de emisión → dilución | corto | Sellada, **sin medir válidamente** |

B2..B5 se midieron una vez y el resultado **se descartó**: el juez cruza los
eventos contra `sector_empresa`, que solo cubre las 1.780 empresas que
necesitaba B1, así que se perdía entre el 57 % y el 63 % de los eventos en el
JOIN. Eso mide «empresas que además hicieron una ampliación de capital», que no
es lo que ninguno de esos TOML declara. No son resultados flojos: son una
medición de otra cosa, y por eso no se conservan.

Para medirlas de verdad hay que completar `sector_empresa` (ver
`brc.datos.sector`, que ya avisaba de esto) y volver a correr `juzgar.py` sin
tocar un solo pre-registro.

**Aviso para quien retome esto**: los números de esa medición nula se llegaron
a ver. B3 pasaba cinco de seis criterios y fallaba el del azar. Quien repita la
medición ya no parte de ciego, y eso tiene que pesar en cuánto se crea el
resultado.

### Por qué se para

No por la maquinaria, que funciona y está probada. Por dónde apunta.

Estudios de eventos sobre expedientes públicos de EDGAR, a resolución diaria,
contra la mediana del universo, es terreno minado desde los años ochenta por
académicos con mejores datos y por fondos con incentivos de miles de millones.
La probabilidad de que quede ahí una ventaja explotable con datos gratis y a
resolución de día es muy baja, y esa premisa no se puso a prueba antes de
construir encima de ella.

### Lo que queda vivo si algún día se retoma

Nada de esto sabe que está mirando acciones US, así que se reapunta a otro
mercado sin tocarlo:

- `brc.estudio.eventos` — estudio de eventos con las tres trampas tapadas.
- `brc.estudio.azar` — la puerta que refutó a B1 y a B3. Se ganó el sueldo:
  B3 tenía `t = +5,19`, que convence a cualquiera, y el sorteo dijo que
  carteras aleatorias del mismo tamaño lo igualan el 28 % de las veces.
- `brc.estudio.horquilla` — Corwin-Schultz y Abdi-Ranaldo, con el test de sesgo
  de Tremacoldi-Rossi e Irwin. Dice que al menos el 48 % de lo que estima es
  sesgo demostrado.
- `brc.datos.particion` — la valla exploración/validación, de disco.
- `scripts/juzgar.py` — juez que LEE el pre-registro sellado, así que no puede
  medir algo distinto de lo declarado.

### Lo que está apagado

Los dos workflows (`Horquilla US`, `Veredictos`) no tienen cron: solo se lanzan
a mano. Sus automatizaciones en el Centro de Control quedan **pausadas**, para
que nadie las dispare por costumbre y para que el vigilante no las cuente como
caídas.
