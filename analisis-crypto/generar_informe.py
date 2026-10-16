#!/usr/bin/env python3
"""Genera informe.html (página autocontenida) a partir de resultados.json."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
TOP_CARDS = 24


def fmt_pct(v, signed=True):
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def razones(x):
    m = x["metrics"]
    out = []
    p = m["bandwidth_min_8s_percentil"]
    if m["squeeze_ttm_activo"]:
        out.append(f"Squeeze TTM activo desde hace {m['semanas_squeeze']} semanas: Bollinger dentro de Keltner.")
    elif m["squeeze_ttm_disparado"]:
        out.append("El squeeze TTM se acaba de liberar: las bandas salen de Keltner.")
    if p <= 10:
        out.append(f"Ancho de Bollinger en mínimos: percentil {p:.0f} de los últimos 2 años.")
    elif p <= 30:
        out.append(f"Bandas comprimidas: percentil {p:.0f} del ancho en 2 años.")
    if m["bandwidth_min_6m"]:
        out.append("Compresión más estrecha de los últimos 6 meses.")
    if m["sobre_banda_sup_cierre"]:
        out.append("La última vela semanal cerró por encima de la banda superior.")
    elif m["sobre_banda_sup_ahora"]:
        out.append("Cotiza por encima de la banda superior (pendiente del cierre semanal).")
    elif m["pct_b"] >= 0.8:
        out.append(f"Presionando la banda superior (%B {m['pct_b']:.2f}).")
    if m["rompe_resistencia"]:
        out.append(f"Supera el máximo de 26 semanas ({m['resistencia_26s']:.6g}).")
    elif m["dist_resistencia_pct"] > -10:
        out.append(f"A {abs(m['dist_resistencia_pct']):.1f}% del máximo de 26 semanas ({m['resistencia_26s']:.6g}).")
    if m["bandas_expandiendo"] and m["precio_sobre_media20"]:
        out.append("Las bandas empiezan a abrirse con el precio sobre la media de 20.")
    cw = m["cruce_alcista_hace_semanas"]
    if cw is not None and cw <= 3:
        when = "esta semana" if cw == 0 else f"hace {cw} semana{'s' if cw > 1 else ''}"
        out.append(f"Cruce alcista del MACD {when}.")
    elif m["macd_sobre_senal"]:
        out.append("MACD por encima de su señal.")
    if m["histograma_subiendo_semanas"] >= 2:
        out.append(f"Histograma MACD creciendo {m['histograma_subiendo_semanas']} semanas seguidas.")
    if m["macd_sobre_cero"]:
        out.append("MACD por encima de cero: momentum alcista de fondo.")
    if m["vol_ratio"] >= 1.5:
        out.append(f"Volumen {m['vol_ratio']:.1f}× la media de 20 semanas.")
    return out


def riesgos(x):
    m = x["metrics"]
    out = []
    if not m["macd_sobre_cero"]:
        out.append("MACD aún bajo cero: rebote dentro de tendencia bajista hasta que lo cruce.")
    if not m["macd_sobre_senal"]:
        out.append("MACD bajo la señal: sin confirmación de momentum.")
    if m["pct_b"] > 1.15 or m["ext_sobre_media_pct"] > 30:
        out.append(f"Extendido: {m['ext_sobre_media_pct']:.0f}% sobre la media de 20. Retroceso probable antes de seguir.")
    if m["sobre_banda_sup_ahora"] and not m["sobre_banda_sup_cierre"]:
        out.append("La ruptura no vale hasta el cierre semanal (domingo 23:59 UTC).")
    if m["vol_ratio"] < 0.9:
        out.append("Volumen por debajo de la media: ruptura sin respaldo.")
    if x["weeks_history"] < 60:
        out.append(f"Solo {x['weeks_history']} semanas de historial: indicadores menos fiables.")
    if x["vol24h_usdt"] < 5e6:
        out.append("Poca liquidez en el perpetuo: deslizamiento y mechas.")
    if abs(x["funding"]) >= 0.0005:
        out.append(f"Funding {x['funding'] * 100:.3f}%: posicionamiento cargado.")
    return out


def main():
    src = HERE / (sys.argv[1] if len(sys.argv) > 1 else "resultados.json")
    data = json.loads(src.read_text())
    for i, x in enumerate(data["ranking"], 1):
        x["rank"] = i
        x["razones"] = razones(x)
        x["riesgos"] = riesgos(x)
        if i > TOP_CARDS:
            x.pop("series", None)
    tpl = (HERE / "informe_template.html").read_text()
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out = HERE / "informe.html"
    out.write_text(html)
    print(f"Escrito {out} ({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
