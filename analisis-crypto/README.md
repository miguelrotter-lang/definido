# Squeeze semanal Bitget (Bollinger + MACD)

Escáner de los futuros perpetuos USDT-M de Bitget en velas **semanales**. Busca
monedas con las Bollinger comprimidas (squeeze) que empiezan a romper al alza y las
ordena por probabilidad de ruptura y de continuación alcista.

## Archivos

| Archivo | Qué hace |
|---|---|
| `bitget_squeeze_scanner.py` | Descarga velas semanales de Bitget, calcula indicadores y puntúa. Solo librería estándar de Python. |
| `generar_informe.py` | Convierte `resultados.json` en `informe.html` (ranking + fichas con gráfico). |
| `informe_template.html` | Plantilla del informe. |
| `resultados.json` | Última ejecución (lo escribe GitHub Actions). |

## Cómo ejecutarlo

En GitHub: pestaña **Actions → Escáner squeeze semanal Bitget → Run workflow**.
El resultado se guarda en `resultados.json` en esta rama.

En tu PC:

```bash
python3 bitget_squeeze_scanner.py --min-vol 1e6 --top 80
python3 generar_informe.py
```

## Indicadores

- Bollinger 20, 2σ. Ancho de banda = (sup − inf) / media.
- Squeeze TTM: Bollinger dentro del canal de Keltner (media 20 ± 1,5 × rango verdadero medio).
- MACD 12, 26, 9.
- Resistencia: máximo de las 26 semanas anteriores.

## Puntuación

**Ruptura (0–100):** compresión del ancho de banda frente a 2 años, squeeze TTM
activo o recién liberado, %B, distancia o rotura del máximo de 26 semanas, bandas
abriéndose con el precio sobre la media y volumen.

**Continuación (0–100):** MACD sobre señal, histograma positivo y creciendo,
MACD sobre cero o cruzándolo, precio sobre la media de 20 con pendiente positiva,
mínimos crecientes, fuerza de la vela semanal y volumen. Penaliza si el precio
está muy estirado sobre la media.

**Total** = media de ambas. Es un orden de calidad técnica, no una probabilidad
calibrada. La vela semanal en curso no está cerrada: las rupturas se confirman al
cierre semanal. No es asesoramiento financiero.
