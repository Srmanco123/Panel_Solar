"""
Decisión de carga en valle a partir de la generación prevista.

Balance de energía diario con tu instalación:
  - Si el sol previsto cubre el consumo y la batería no baja del suelo -> no hace falta cargar de red.
  - Si el día es flojo -> carga en valle lo justo para cubrir el déficit sin comprar en punta,
    respetando la reserva del 50% y sin pasar de la potencia contratada.
"""


def decide_charge(gen_kwh, cons_kwh, soc0_pct, cfg):
    """
    gen_kwh   : generación prevista del día (kWh)  [salida del modelo]
    cons_kwh  : consumo esperado del día (kWh)
    soc0_pct  : estado de la batería al entrar el valle (%)
    cfg       : dict 'instalacion' de config.json
    Devuelve un dict con la recomendación.
    """
    cap = float(cfg.get("bateria_kwh", 10.24))
    floor = float(cfg.get("soc_suelo", 20))
    reserve = float(cfg.get("soc_reserva_valle", 50))
    chg_kw = float(cfg.get("carga_red_kw", 2.0))
    p_valle = float(cfg.get("precio_valle_eur_kwh", 0.10))
    p_punta = float(cfg.get("precio_punta_eur_kwh", 0.21))
    valle = cfg.get("valle", {"inicio": 1, "fin": 8})
    valle_h = max(1, int(valle.get("fin", 8)) - int(valle.get("inicio", 1)))

    deficit = max(0.0, cons_kwh - gen_kwh)          # lo que el sol NO cubre
    # energía que la batería puede aportar hoy por encima del suelo, partiendo de soc0
    usable_from_soc0 = max(0.0, (soc0_pct - floor) / 100.0 * cap)

    # SOC objetivo para cubrir el déficit sin bajar del suelo
    target = floor + (deficit / cap * 100.0)
    target = max(reserve, min(100.0, target))       # nunca por debajo de la reserva

    # energía a cargar de red en el valle para llegar al objetivo
    grid_kwh = max(0.0, (target - soc0_pct) / 100.0 * cap)
    # límite físico: potencia de carga x horas de valle
    grid_cap = chg_kw * valle_h
    grid_kwh = min(grid_kwh, grid_cap)

    eur_valle = grid_kwh * p_valle
    eur_punta = grid_kwh * p_punta

    # ¿sol de sobra? cubre el consumo con margen y la batería aguanta sobre la reserva
    sunny = gen_kwh >= cons_kwh * 0.95 and usable_from_soc0 + max(0.0, gen_kwh - cons_kwh) >= 0

    if sunny:
        status = "ok"
        title = "No hace falta cargar de red"
        detail = (f"Generación prevista {gen_kwh:.1f} kWh cubre el consumo {cons_kwh:.1f} kWh. "
                  f"En el valle mantén solo la reserva del {reserve:.0f}%.")
        target = reserve
        grid_kwh = max(0.0, (reserve - soc0_pct) / 100.0 * cap)
        grid_kwh = min(grid_kwh, grid_cap)
        eur_valle = grid_kwh * p_valle
    elif deficit > usable_from_soc0 * 1.5:
        status = "crit"
        title = "Carga la batería en el valle"
        detail = (f"Día flojo: {gen_kwh:.1f} kWh frente a {cons_kwh:.1f} kWh de consumo. "
                  f"Sin cargar, tirarías de red en horas caras.")
    else:
        status = "warn"
        title = "Conviene cargar algo en el valle"
        detail = (f"El sol no cubre el día (faltan ~{deficit:.1f} kWh). "
                  f"Cargar barato de noche evita comprar en punta.")

    return {
        "status": status,
        "title": title,
        "detail": detail,
        "gen_kwh": round(gen_kwh, 2),
        "cons_kwh": round(cons_kwh, 2),
        "deficit_kwh": round(deficit, 2),
        "target_soc": round(target),
        "grid_charge_kwh": round(grid_kwh, 2),
        "eur_valle": round(eur_valle, 2),
        "eur_punta_equiv": round(eur_punta, 2),
        "ahorro_vs_punta": round(eur_punta - eur_valle, 2),
        "valle": valle,
    }
