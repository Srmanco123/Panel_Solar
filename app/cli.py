"""
App Deye + clima + regresión. Interfaz de línea de comandos.

Uso rápido:
  python cli.py test-auth                 # comprueba credenciales y lista plantas/inversores
  python cli.py probe                     # muestra una respuesta de histórico (para verificar formato)
  python cli.py fetch-history --days 90    # descarga histórico Deye + clima -> data/history.csv
  python cli.py train                      # entrena el modelo -> models/model.joblib
  python cli.py predict --day 1            # predice mañana y recomienda carga de valle
  python cli.py live                       # datos en vivo de la planta
  python cli.py apply-charge --target 60   # DRY-RUN del TOU; añade --yes para enviarlo de verdad
"""
import argparse
import datetime as dt
import json
import os

import deye_client
import weather
import dataset
import model as modelmod
import battery
import control

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
MODEL_PATH = os.path.join(HERE, "models", "model.joblib")
CSV_PATH = os.path.join(DATA_DIR, "history.csv")


def _client(args):
    cfg = deye_client.load_config(args.config)
    return deye_client.DeyeClient(cfg), cfg


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(HERE, "models"), exist_ok=True)


# ---------------- comandos ----------------
def cmd_test_auth(args):
    client, cfg = _client(args)
    client.authenticate()
    print("OK: autenticado en DeyeCloud.")
    stations = client.station_list()
    print(f"\nPlantas ({len(stations)}):")
    for s in stations:
        print(f"  id={s.get('id')}  {s.get('name')}  lat={s.get('locationLat')} lon={s.get('locationLng')}"
              f"  {s.get('installedCapacity')} kWp  SOC={s.get('batterySOC')}%")
    ids = [s.get("id") for s in stations]
    devices = client.station_devices(ids) if ids else []
    print(f"\nDispositivos ({len(devices)}):")
    for d in devices:
        print(f"  sn={d.get('deviceSn')}  tipo={d.get('deviceType')}  planta={d.get('stationId')}  online={d.get('connectStatus')}")


def cmd_probe(args):
    client, cfg = _client(args)
    sid, lat, lon = client.resolve_station(cfg.get("stationId"))
    print(f"Planta {sid} (lat={lat}, lon={lon}). Probando station/history last ~7 días...")
    end = dt.date.today()
    start = end - dt.timedelta(days=7)
    for g in (1, 2, 3):
        try:
            items = client.station_history(sid, start.isoformat(), end.isoformat(), g)
            print(f"\n--- granularity={g}: {len(items)} items. Ejemplo:")
            print(json.dumps(items[0] if items else {}, indent=2, ensure_ascii=False)[:800])
        except Exception as e:
            print(f"  granularity={g}: error {e}")


def cmd_fetch_history(args):
    _ensure_dirs()
    client, cfg = _client(args)
    sid, lat, lon = client.resolve_station(cfg.get("stationId"))
    end = dt.date.today() - dt.timedelta(days=1)
    start = (dt.date.fromisoformat(args.start) if args.start else end - dt.timedelta(days=args.days))
    end = dt.date.fromisoformat(args.end) if args.end else end
    print(f"Planta {sid} lat={lat} lon={lon}. Histórico {start}..{end} (granularity={args.granularity})")
    gen = dataset.fetch_daily_history(client, sid, start.isoformat(), end.isoformat(), args.granularity)
    print(f"  Deye: {len(gen)} días con generación.")
    wx = weather.history_daily(lat, lon, start.isoformat(), end.isoformat())
    print(f"  Clima: {len(wx)} días.")
    df = dataset.build_frame(gen, wx)
    df.to_csv(CSV_PATH, index=False)
    print(f"  -> guardado {CSV_PATH} ({len(df)} filas emparejadas)")
    print(df[["date", "generation_kwh", "consumption_kwh", "shortwave_radiation_sum"]].tail(10).to_string(index=False))


def cmd_train(args):
    import pandas as pd
    _ensure_dirs()
    if not os.path.exists(CSV_PATH):
        print("No hay data/history.csv. Ejecuta primero: python cli.py fetch-history --days 90")
        return
    df = pd.read_csv(CSV_PATH)
    feats = dataset.usable_features(df)
    lat = None
    try:   # la latitud hace falta para la variable física (radiación en el plano inclinado)
        client, cfg = _client(args)
        _, lat, _ = client.resolve_station(cfg.get("stationId"))
        lat = float(lat)
    except Exception as e:
        print(f"  (aviso: sin latitud, entreno sin variable física: {e})")
    print(f"Entrenando con {len(df)} días y features: {feats}")
    bundle = modelmod.train(df, feats, lat=lat)
    modelmod.save(bundle, MODEL_PATH)
    if bundle.get("n_excluded"):
        print(f"\nExcluidos {bundle['n_excluded']} días anteriores a {bundle['cutoff']} (sin vertido a red: producción recortada).")
    if bundle.get("tilt") is not None:
        print(f"Inclinación efectiva ajustada a tus datos: {bundle['tilt']:.0f}°")
    print(f"\nModelo elegido: {bundle['model_name']}  (n={bundle['n_samples']})  ·  validación walk-forward (pasado -> futuro)")
    fmt = lambda v, f: ("  n/a" if v is None else format(v, f))
    for name, m in bundle["metrics"].items():
        w, k = m["walk_forward"], m["bloques"]
        print(f"  {name:8s}  walk-forward R2={fmt(w['r2'], '.3f')} MAE={fmt(w['mae'], '.2f')}   |   bloques-semana R2={fmt(k['r2'], '.3f')} MAE={fmt(k['mae'], '.2f')} kWh")
    print(f"Banda 80% de la predicción: {bundle['resid_q'][0]:+.1f} / {bundle['resid_q'][1]:+.1f} kWh")
    print("\nPeso de cada variable:")
    for f, w in bundle["importance"].items():
        print(f"  {f:28s} {w*100:5.1f}%")
    print(f"\n-> guardado {MODEL_PATH}")


def _consumption_estimate(cfg):
    import pandas as pd
    if os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH)
        if "consumption_kwh" in df and df["consumption_kwh"].notna().any():
            return float(df["consumption_kwh"].dropna().tail(30).mean())
    return float(cfg["instalacion"].get("consumo_diario_kwh_estimado", 10))


def cmd_predict(args):
    client, cfg = _client(args)
    if not os.path.exists(MODEL_PATH):
        print("No hay modelo entrenado. Ejecuta: python cli.py train")
        return
    bundle = modelmod.load(MODEL_PATH)
    sid, lat, lon = client.resolve_station(cfg.get("stationId"))
    fdays = max(3, args.day + 1)
    wx = weather.forecast_daily(lat, lon, forecast_days=fdays)
    if args.day >= len(wx):
        print("La previsión no llega tan lejos.")
        return
    row = dataset.feature_row(wx[args.day])
    gen_pred = float(modelmod.predict(bundle, [row])[0])
    gen_pred = max(0.0, gen_pred)
    cons = _consumption_estimate(cfg)
    # SOC actual como aproximación del SOC al entrar el valle
    try:
        soc0 = float(client.station_latest(sid).get("batterySOC") or cfg["instalacion"]["soc_reserva_valle"])
    except Exception:
        soc0 = float(cfg["instalacion"]["soc_reserva_valle"])
    rec = battery.decide_charge(gen_pred, cons, soc0, cfg["instalacion"])

    label = {0: "HOY", 1: "MAÑANA"}.get(args.day, f"+{args.day} días")
    print(f"\n=== Predicción {label} ({row['date']}) ===")
    print(f"  Radiación prevista: {row.get('shortwave_radiation_sum')} MJ/m²   Tmax: {row.get('temperature_2m_max')}°C")
    print(f"  Generación prevista: {gen_pred:.1f} kWh   (modelo {bundle['model_name']}, "
          f"R2={bundle['metrics'][bundle['model_name']]['r2']:.2f})")
    print(f"  Consumo estimado:    {cons:.1f} kWh   SOC actual: {soc0:.0f}%")
    print(f"\n  [{rec['status'].upper()}] {rec['title']}")
    print(f"  {rec['detail']}")
    if rec["grid_charge_kwh"] > 0.05:
        print(f"  -> Carga en valle hasta ~{rec['target_soc']}%: {rec['grid_charge_kwh']:.1f} kWh "
              f"≈ {rec['eur_valle']:.2f} € (ahorro vs punta: {rec['ahorro_vs_punta']:.2f} €)")
        print(f"  Para aplicarlo al inversor:  python cli.py apply-charge --target {rec['target_soc']}")
    else:
        print("  -> No hace falta forzar carga de red.")


def cmd_live(args):
    client, cfg = _client(args)
    sid, lat, lon = client.resolve_station(cfg.get("stationId"))
    d = client.station_latest(sid)
    print(json.dumps({k: d.get(k) for k in
          ["generationPower", "consumptionPower", "gridPower", "purchasePower",
           "batteryPower", "batterySOC", "chargePower", "dischargePower", "lastUpdateTime"]},
          indent=2, ensure_ascii=False))


def cmd_apply_charge(args):
    client, cfg = _client(args)
    sid, _, _ = client.resolve_station(cfg.get("stationId"))
    device_sn = cfg.get("deviceSn") or client.resolve_inverter_sn(sid)
    items = control.build_tou(target_soc=args.target)
    print(f"Inversor {device_sn}. TOU a enviar (objetivo valle {args.target}%):")
    print(json.dumps(items, indent=2, ensure_ascii=False))
    if not args.yes:
        print("\nDRY-RUN. No se ha enviado nada. Revisa los tramos y, si es correcto, repite con --yes.")
        return
    # confirmación interactiva adicional (seguridad)
    try:
        ans = input('\nESTO ESCRIBE EN TU INVERSOR. Escribe "SI" para confirmar: ').strip()
    except EOFError:
        ans = ""
    if ans != "SI":
        print("Cancelado.")
        return
    res = control.apply_tou(client, device_sn, items, confirm=True)
    print("Enviado. orderId:", res.get("orderId"))
    if res.get("orderId"):
        print("Resultado:", json.dumps(client.order_result(res["orderId"]), ensure_ascii=False)[:300])


def cmd_dashboard(args):
    import dashboard
    _ensure_dirs()
    client, cfg = _client(args)
    if not os.path.exists(MODEL_PATH):
        print("Falta el modelo. Ejecuta: python cli.py train")
        return
    out = os.path.join(HERE, "dashboard.html")
    path, metrics = dashboard.build(cfg, client, CSV_PATH, MODEL_PATH, out, forecast_days=args.days)
    print(f"Dashboard generado: {path}")
    print(f"  {metrics['days']} días · gen media {metrics['gen_mean']} kWh · R²={metrics['r2']} · "
          f"autosuficiencia {metrics['self_suff']}% · predicción mañana {metrics['pred_tomorrow']} kWh")


def main():
    p = argparse.ArgumentParser(description="Deye + clima + regresión (predicción de carga en valle)")
    p.add_argument("--config", default="config.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test-auth").set_defaults(func=cmd_test_auth)
    sub.add_parser("probe").set_defaults(func=cmd_probe)

    fh = sub.add_parser("fetch-history")
    fh.add_argument("--days", type=int, default=90)
    fh.add_argument("--start"); fh.add_argument("--end")
    fh.add_argument("--granularity", type=int, default=2, help="2=diario (kWh, para entrenar), 1=intradía")
    fh.set_defaults(func=cmd_fetch_history)

    sub.add_parser("train").set_defaults(func=cmd_train)

    pr = sub.add_parser("predict")
    pr.add_argument("--day", type=int, default=1, help="0=hoy, 1=mañana, 2=pasado...")
    pr.set_defaults(func=cmd_predict)

    sub.add_parser("live").set_defaults(func=cmd_live)

    db = sub.add_parser("dashboard")
    db.add_argument("--days", type=int, default=7, help="días de predicción a mostrar")
    db.set_defaults(func=cmd_dashboard)

    ac = sub.add_parser("apply-charge")
    ac.add_argument("--target", type=int, default=60, help="SOC objetivo en el valle (%)")
    ac.add_argument("--yes", action="store_true", help="enviar de verdad (si no, dry-run)")
    ac.set_defaults(func=cmd_apply_charge)

    args = p.parse_args()
    try:
        args.func(args)
    except deye_client.DeyeError as e:
        print("ERROR:", e)


if __name__ == "__main__":
    main()
