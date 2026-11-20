"""
Precios de electricidad, TODO configurable. Para compra y para excedentes, 3 modos:
  - "fijo"  : un único €/kWh
  - "tramos": periodos por horas (p. ej. PVPC 2.0TD valle/llano/punta), con precio por tramo
              (un tramo con "findesemana": true se aplica todo el fin de semana)
  - "pvpc"  : precio real por horas desde REE (lo que pagas)        [solo compra]
  - "spot"  : precio real del mercado por horas desde REE           [solo excedente]

Se configura en config.json -> "precios": {"compra": {...}, "excedente": {...}}.
"""
import requests

REE = "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
_cache = {}


def _ree_day(date_iso):
    """(pvpc, spot) dicts {hora 0-23: €/kWh} para ese día, cacheado."""
    if date_iso in _cache:
        return _cache[date_iso]
    params = {"start_date": date_iso + "T00:00", "end_date": date_iso + "T23:59", "time_trunc": "hour"}
    pvpc, spot = {}, {}
    try:
        r = requests.get(REE, params=params, headers={"Accept": "application/json"}, timeout=25)
        r.raise_for_status()
        for s in r.json().get("included", []):
            title = (s.get("attributes", {}).get("title") or "").lower()
            buckets = {}
            for v in s["attributes"].get("values", []):
                buckets.setdefault(int(v["datetime"][11:13]), []).append(v["value"])
            hourly = {h: sum(xs) / len(xs) / 1000.0 for h, xs in buckets.items()}
            if "pvpc" in title:
                pvpc = hourly
            elif "spot" in title:
                spot = hourly
    except Exception:
        pass
    _cache[date_iso] = (pvpc, spot)
    return pvpc, spot


class PriceModel:
    """Resuelve el €/kWh de una hora según el modo configurado."""
    def __init__(self, section, kind):
        self.kind = kind                     # "compra" | "excedente"
        self.mode = (section or {}).get("modo", "fijo")
        self.fijo = float((section or {}).get("fijo", 0.15 if kind == "compra" else 0.06))
        self.tramos = (section or {}).get("tramos", [])

    def hour_price(self, date_iso, hour, weekend):
        if self.mode == "fijo":
            return self.fijo
        if self.mode in ("pvpc", "spot"):
            pvpc, spot = _ree_day(date_iso)
            src = pvpc if self.mode == "pvpc" else spot
            return src.get(hour, self.fijo) if src else self.fijo
        # tramos
        if weekend:
            w = [t for t in self.tramos if t.get("findesemana")]
            if w:
                return float(w[0]["precio"])
        for t in self.tramos:
            for rng in t.get("horas", []):
                if rng[0] <= hour < rng[1]:
                    return float(t["precio"])
        return self.fijo

    def curve(self, date_iso, weekend):
        return [round(self.hour_price(date_iso, h, weekend), 4) for h in range(24)]

    def avg(self, date_iso, weekend):
        c = self.curve(date_iso, weekend)
        return sum(c) / len(c) if c else self.fijo


def build_models(cfg):
    """Devuelve (modelo_compra, modelo_excedente) desde config.json.
    Admite el bloque nuevo 'precios' o, si no está, cae a los precios sueltos de 'instalacion'."""
    pr = cfg.get("precios")
    if not pr:
        inst = cfg.get("instalacion", {})
        pr = {
            "compra": {"modo": "tramos", "tramos": [
                {"nombre": "valle", "horas": [[0, 8]], "findesemana": True,
                 "precio": inst.get("precio_valle_eur_kwh", 0.10)},
                {"nombre": "punta", "horas": [[8, 24]],
                 "precio": inst.get("precio_punta_eur_kwh", 0.21)},
            ]},
            "excedente": {"modo": "fijo", "fijo": inst.get("precio_excedente_eur_kwh", 0.06)},
        }
    return PriceModel(pr.get("compra"), "compra"), PriceModel(pr.get("excedente"), "excedente")


def is_weekend(date_iso):
    import datetime as dt
    return dt.date.fromisoformat(date_iso[:10]).weekday() >= 5
