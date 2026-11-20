"""
CONTROL del inversor (ESCRITURA). Fija los 6 tramos TOU para forzar la carga en valle.

SEGURIDAD: nada se envía al inversor sin confirmación explícita.
  - build_tou(...) solo construye el payload (no toca nada).
  - apply_tou(..., confirm=True) es lo único que escribe, y la CLI exige además teclear "SI".
"""

# Plantilla de los 6 tramos (coincide con tu "Work Modo2" del Deye).
# time = hora de inicio del tramo. power = W. soc = % objetivo. grid = cargar de red en ese tramo.
DEFAULT_SLOTS = [
    {"time": "00:00", "soc": 20, "grid": False},
    {"time": "01:00", "soc": 50, "grid": True},   # valle: reserva/carga
    {"time": "08:00", "soc": 20, "grid": False},
    {"time": "13:00", "soc": 20, "grid": False},
    {"time": "17:00", "soc": 20, "grid": False},
    {"time": "21:00", "soc": 20, "grid": False},
]


def build_tou(target_soc=50, power=6000, slots=None):
    """Devuelve la lista timeUseSettingItems para /v1.0/order/sys/tou/update.
    Pone el SOC objetivo y la carga de red en el tramo de valle (el que tiene grid=True)."""
    slots = slots or DEFAULT_SLOTS
    items = []
    for s in slots:
        items.append({
            "time": s["time"],
            "power": int(power),
            "soc": int(target_soc if s.get("grid") else s.get("soc", 20)),
            "enableGridCharge": bool(s.get("grid", False)),
            "enableGeneration": True,
            "enableSell": True,
            "voltage": 0,
        })
    return items


def apply_tou(client, device_sn, items, confirm=False):
    """Envía los tramos TOU al inversor. Solo escribe si confirm=True."""
    if not confirm:
        return {"dry_run": True, "deviceSn": device_sn, "timeUseSettingItems": items}
    resp = client._order_tou_update(device_sn, items)  # noqa: SLF001 (uso intencionado)
    return {"dry_run": False, "orderId": resp.get("orderId"), "response": resp}


def enable_grid_charge(client, device_sn, on=True, confirm=False):
    """Habilita/deshabilita el modo de carga de red (order/battery/modeControl)."""
    action = "on" if on else "off"
    if not confirm:
        return {"dry_run": True, "action": action, "deviceSn": device_sn}
    resp = client._order_battery_mode(device_sn, action, "GRID_CHARGE")  # noqa: SLF001
    return {"dry_run": False, "orderId": resp.get("orderId"), "response": resp}
