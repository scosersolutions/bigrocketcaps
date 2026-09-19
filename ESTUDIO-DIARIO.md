# Estudio: convertir lo que sigue vivo en un diario de trading

Escrito el 2026-09-19, después de parar la rama de investigación. Solo mira
piezas **vivas**: nada de B1..B5, nada de la medición de horquilla por Tiingo,
nada que dependa de que aparezca una ventaja.

---

## 1. Lo que está vivo, con números

| pieza | estado | evidencia |
|---|---|---|
| `cuaderno-diario` | **corriendo a diario** | 8 ejecuciones, 0 errores, la última hoy 11:43 |
| `sistema-watchdog` | corriendo | 473 ejecuciones, 0 errores |
| Centro de Control | desplegado | panel, alertas, Telegram, tokens con auditoría |
| `brc-horquilla` / `brc-veredictos` | **pausadas** | se apagaron ayer |

Y esto es lo importante: **ya existe un diario de trading, solo que nadie lo
está leyendo.** `cuaderno-diario` publica tres carteras de papel:

| cartera | estado | rentabilidad | cerradas | abiertas | mínimo declarado |
|---|---|---|---|---|---|
| `demo-e7c` | cerrada | **−2,11 %** | 6 | 0 | 30 operaciones |
| `demo-e7c-2` | abierta | **−5,53 %** | 0 | 8 | 30 operaciones |
| `demo-1semana` | abierta | **−8,41 %** | 13 | 8 | 30 operaciones |

Cada operación cerrada ya guarda lo que pedías, con este formato:

```json
{"t":"CWBC","fe":"2026-09-03","f":"2026-09-03","pe":26.1992,"ps":26.47,"pct":1.034,"acc":28}
```

Ticker, fecha de entrada, fecha de salida, precio de entrada, precio de salida
y **`pct`: el porcentaje con el que acabó**. Son 19 operaciones cerradas ya
registradas. El dato lleva ahí desde el primer día; lo que falta es enseñarlo.

---

## 2. El aviso que va antes de cualquier mejora

Las tres carteras llevan `preregistrada: true` y `min_operaciones: 30`.

Eso significa que **tú mismo declaraste, antes de empezar, que hasta las 30
operaciones el resultado no significa nada**. Vas por 19, y las tres pierden.

Con 19 operaciones, un −5 % no distingue entre «la estrategia no funciona» y
«mala racha normal». Usar estos números para decidir algo ahora es exactamente
el error que el pre-registro existe para impedir, y es un error que se comete
solo: cuando el diario esté bonito en pantalla, la tentación de leerlo como
consejo llega sola.

**Consecuencia para el diseño**: todo lo que se construya debe enseñar el
contador `19/30` al lado de cualquier cifra de rendimiento. No como
advertencia legal — como dato de primera línea.

---

## 3. Mejoras usables, por orden de lo que dan dividido por lo que cuestan

### A. Enseñar el histórico de operaciones · coste: una línea de configuración

El dato existe, el panel pinta tablas, solo falta declararlo. Con las
`etiquetas` de columna que se añadieron hoy, sale legible:

| Valor | Entrada | Salida | Precio entrada | Precio salida | Resultado |
|---|---|---|---|---|---|
| CWBC | 2026-09-03 | 2026-09-03 | 26,20 | 26,47 | **+1,03 %** |
| TENX | 2026-09-03 | 2026-09-03 | 1,99 | 1,83 | **−7,99 %** |

**Esto es el diario de trading.** Es lo primero y ya se puede hacer.

### B. Estadísticas del diario · coste: bajo, dentro del Centro de Control

Sobre esas 19 operaciones: aciertos y fallos, media de las ganadoras contra
media de las perdedoras, esperanza por operación, y **cuánto falta para 30**.

Con eso el diario deja de ser una lista y pasa a contestar «¿esto va a algún
sitio?» — que es la única pregunta que un diario tiene que contestar.

Requiere un widget que calcule, no solo que pinte. Es la primera pieza de
código de verdad de esta lista.

### C. El umbral de coste por valor · coste: medio · **es el consejero honesto**

Ya existe y funciona: para cada valor, cuánto hay que ganar por operación para
que entrar y salir no se coma el beneficio. JPM 0,44 %; SOUN 2,29 %.

Hoy cubre 21 valores elegidos a mano que **no son los que tienes en cartera**
(CWBC, TENX, UAMY, NPB, EMPD, FRST, ENHA, ENOV). Apuntarlo a los tickers reales
del diario lo convierte en una pregunta contestable antes de cada entrada:

> «Vas a entrar en TENX. Operarlo cuesta del orden de X % ida y vuelta. Tu
> operación media gana Y %. ¿Sigues?»

Eso es aconsejar sin decirte qué comprar: es ponerte delante tu propio número.

### D. Avisos por Telegram cuando una posición llega a su horizonte · coste: bajo

La infraestructura está entera (eventos, niveles, Telegram, horas de silencio).
Las posiciones llevan `resta` — sesiones que le quedan. Un aviso a falta de una
sesión convierte el panel en algo que te busca a ti.

### E. Botones de vender · **bloqueado, y no solo por lo técnico**

Dos motivos, y el segundo pesa más:

1. **MoonRocket no está en esta máquina.** Solo tengo `control-center`. El
   panel enseña lo que MoonRocket publica; no puede cerrar una posición porque
   la cartera no vive aquí. Un botón que escribiera en la base del panel
   crearía una segunda verdad: el panel diría «vendida» y MoonRocket la
   republicaría abierta en la siguiente ejecución.
2. **Vender a mano rompe el pre-registro.** Las carteras declaran horizonte de
   21 sesiones y cierre por regla. Una venta discrecional convierte un
   experimento pre-registrado en uno discrecional, y sus 19 operaciones dejan
   de ser comparables con las que vengan después.

Si lo quieres igual — y es defendible, es dinero de demostración y aprender a
usar el diario también vale— la forma honesta es: abrir una cartera NUEVA
marcada como discrecional, dejar las tres pre-registradas intactas, y que el
botón encole un comando que ejecute MoonRocket. La tabla `commands` ya existe
para eso; le falta el tipo `cerrar` y que MoonRocket lo consuma.

---

## 4. Dónde está la raya del «consejero»

Lo que se puede construir sin mentir:

- Enseñarte **tus** números: qué operaciones hiciste y cómo acabaron.
- Comparar una operación que te planteas contra **tus reglas declaradas** y
  contra el coste de operar ese valor.
- Avisarte cuando algo se sale de lo que tú mismo escribiste.

Lo que no:

- Decirte qué comprar o vender. Este sistema no lo sabe. Probó una hipótesis
  en serio y la refutó; las otras cuatro ni siquiera se midieron bien. Un panel
  bonito no cambia eso, y el riesgo real es que lo parezca.

La diferencia práctica: un consejero que dice «entrar aquí te cuesta 2,29 % y
tu operación media gana 0,4 %» te está dando un hecho tuyo. Uno que dice
«compra TENX» se lo está inventando.

---

## 5. Lo que yo haría, en este orden

1. **A** — el histórico, hoy. Es lo que pediste y cuesta una configuración.
2. **D** — los avisos, porque reutilizan algo ya construido y pagado.
3. **B** — las estadísticas, cuando haya más de 19 operaciones que resumir.
4. **C** — el umbral por valor, cuando exista una decisión real que tomar.
5. **E** — los botones, solo con cartera discrecional aparte y acceso a
   MoonRocket.

Y nada de esto vuelve a tocar B1..B5 ni la horquilla de Tiingo.

---

## 6. Lo que necesito de ti para pasar de aquí

**Dónde está MoonRocket.** No está en esta máquina, y sin él no puedo tocar ni
el cierre de posiciones (E) ni cambiar lo que publica el cuaderno. Todo lo
demás —A, B, D y la mitad de C— se puede hacer solo desde `control-center`.
