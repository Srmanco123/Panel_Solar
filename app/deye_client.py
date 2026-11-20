"""
Cliente de la DeyeCloud OpenAPI (https://eu1-developer.deyecloud.com).

Endpoints verificados contra el swagger oficial (/v2/api-docs) el 19/11/2026:
  POST /v1.0/account/token            -> obtener token (appId en query, body con sha256 de la pass)
  POST /v1.0/station/list             -> lista de plantas (id, lat, lon, capacidad...)
  POST /v1.0/station/detail           -> detalle de una planta
  POST /v1.0/station/history          -> histórico agregado (día/mes/año): generationValue, consumptionValue...
  POST /v1.0/station/history/power    -> histórico fino por timestamp (generationPower, consumptionPower, SOC...)
  POST /v1.0/station/latest           -> datos en vivo de la planta
  POST /v1.0/device/list              -> inversores / dataloggers
  POST /v1.0/device/measurePoints     -> puntos de medida de un dispositivo
  POST /v1.0/device/history           -> histórico por puntos de medida
  POST /v1.0/config/tou               -> leer configuración TOU actual
  POST /v1.0/order/sys/tou/update     -> ESCRIBIR los 6 tramos TOU (forzar carga) [requiere confirmación]
  POST /v1.0/order/battery/modeControl-> habilitar/deshabilitar modo de carga [requiere confirmación]
  GET  /v1.0/order/{orderId}          -> resultado de un comando de control

Nota: todas las escrituras (order/*) están aisladas en control.py y nunca se ejecutan
sin confirmación explícita.
"""
import hashlib
import json
import time
import os
import requests

CODE_OK = "1000000"


class DeyeError(RuntimeError):
    pass


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DeyeClient:
    def __init__(self, config: dict, token_cache_path: str = None):
        self.app_id = config["appId"]
        self.app_secret = config["appSecret"]
        self.email = config.get("email")
        self.country_code = str(config.get("countryCode", "")) or None
        self.base = config.get("baseUrl", "https://eu1-developer.deyecloud.com").rstrip("/")
        # contraseña: se admite en claro (se hashea) o ya hasheada
        pw = config.get("password")
        self.password_sha256 = config.get("password_sha256") or (sha256_hex(pw) if pw else None)
        self.token_cache_path = token_cache_path or os.path.join(os.path.dirname(__file__), "token_cache.json")
        self._token = None
        self._token_exp = 0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "deye-solar/1.0"})

    # ---------- autenticación ----------
    def _load_cached_token(self):
        try:
            with open(self.token_cache_path, encoding="utf-8") as fh:
                c = json.load(fh)
            if c.get("email") == self.email and c.get("exp", 0) > time.time() + 300:
                self._token = c["token"]
                self._token_exp = c["exp"]
                return True
        except (OSError, ValueError, KeyError):
            pass
        return False

    def _save_cached_token(self):
        try:
            with open(self.token_cache_path, "w", encoding="utf-8") as fh:
                json.dump({"email": self.email, "token": self._token, "exp": self._token_exp}, fh)
        except OSError:
            pass

    def authenticate(self, force: bool = False):
        if not force and self._token and self._token_exp > time.time() + 300:
            return self._token
        if not force and self._load_cached_token():
            return self._token
        if not self.password_sha256:
            raise DeyeError("Falta 'password' o 'password_sha256' en la configuración.")
        body = {"appSecret": self.app_secret, "email": self.email, "password": self.password_sha256}
        if self.country_code:
            body["countryCode"] = self.country_code
        url = f"{self.base}/v1.0/account/token?appId={self.app_id}"
        r = self.session.post(url, json=body, timeout=30)
        self._raise_for_api(r, "account/token")
        data = r.json()
        tok = data.get("accessToken")
        if not tok:
            raise DeyeError(f"Sin accessToken en la respuesta: {data}")
        # accessToken ya suele venir como 'Bearer eyJ...'; normalizamos
        self._token = tok if tok.lower().startswith("bearer ") else "Bearer " + tok
        try:
            self._token_exp = time.time() + float(data.get("expiresIn", 5183999))
        except (TypeError, ValueError):
            self._token_exp = time.time() + 3600
        self._save_cached_token()
        return self._token

    def _auth_header(self):
        return {"authorization": self.authenticate()}

    # ---------- utilidades de red ----------
    def _post(self, path: str, body: dict):
        url = f"{self.base}{path}"
        r = self.session.post(url, json=body, headers=self._auth_header(), timeout=60)
        if r.status_code == 401:
            # token caducado: reintenta una vez
            self.authenticate(force=True)
            r = self.session.post(url, json=body, headers=self._auth_header(), timeout=60)
        self._raise_for_api(r, path)
        return r.json()

    def _get(self, path: str):
        url = f"{self.base}{path}"
        r = self.session.get(url, headers=self._auth_header(), timeout=60)
        self._raise_for_api(r, path)
        return r.json()

    @staticmethod
    def _raise_for_api(r, what):
        if r.status_code >= 400:
            raise DeyeError(f"{what}: HTTP {r.status_code} - {r.text[:300]}")
        try:
            data = r.json()
        except ValueError:
            raise DeyeError(f"{what}: respuesta no JSON - {r.text[:200]}")
        if data.get("success") is False or (data.get("code") and str(data["code"]) != CODE_OK):
            raise DeyeError(f"{what}: code={data.get('code')} msg={data.get('msg')}")

    # ---------- lectura ----------
    def station_list(self, page=1, size=100):
        return self._post("/v1.0/station/list", {"page": page, "size": size}).get("stationList", [])

    def resolve_station(self, station_id=None):
        """Devuelve (station_id, lat, lon) usando config o la primera planta."""
        stations = self.station_list()
        if not stations:
            raise DeyeError("La cuenta no tiene plantas.")
        if station_id:
            st = next((s for s in stations if str(s.get("id")) == str(station_id)), None)
            if not st:
                raise DeyeError(f"No existe la planta {station_id}. Disponibles: {[s.get('id') for s in stations]}")
        else:
            st = stations[0]
        return st.get("id"), st.get("locationLat"), st.get("locationLng")

    def station_detail(self, station_id):
        return self._post("/v1.0/station/detail", {"stationId": int(station_id)}).get("station", {})

    def station_latest(self, station_id):
        return self._post("/v1.0/station/latest", {"stationId": int(station_id)})

    def station_history(self, station_id, start_at, end_at, granularity):
        """Histórico agregado. granularity: 1=día, 2=mes, 3=año (verifícalo con 'probe').
        start_at/end_at: fechas 'YYYY-MM-DD' (o el formato que acepte tu cuenta)."""
        body = {"stationId": int(station_id), "startAt": str(start_at), "endAt": str(end_at),
                "granularity": int(granularity)}
        return self._post("/v1.0/station/history", body).get("stationDataItems", [])

    def station_history_power(self, station_id, start_ts, end_ts):
        """Histórico fino (intradía) por timestamp unix en segundos (10 dígitos)."""
        body = {"stationId": int(station_id), "startTimestamp": int(start_ts), "endTimestamp": int(end_ts)}
        return self._post("/v1.0/station/history/power", body).get("stationDataItems", [])

    def device_list(self, page=1, size=100):
        # Solo cuentas 'business'. Usuarios personales: usar station_devices().
        return self._post("/v1.0/device/list", {"page": page, "size": size}).get("deviceList", [])

    def station_devices(self, station_ids, page=1, size=100):
        """Dispositivos de una o varias plantas (válido para usuario personal)."""
        if not isinstance(station_ids, (list, tuple)):
            station_ids = [station_ids]
        body = {"page": page, "size": size, "stationIds": [int(s) for s in station_ids]}
        return self._post("/v1.0/station/device", body).get("deviceListItems", [])

    def resolve_inverter_sn(self, station_id, device_sn=None):
        if device_sn:
            return device_sn
        devices = self.station_devices(station_id)
        inv = next((d for d in devices if d.get("deviceType") == "INVERTER"), None)
        if not inv:
            raise DeyeError(f"No se encontró inversor en la planta {station_id}. "
                            f"Dispositivos: {[(d.get('deviceSn'), d.get('deviceType')) for d in devices]}")
        return inv.get("deviceSn")

    def device_measure_points(self, device_sn, device_type="INVERTER"):
        body = {"deviceSn": device_sn, "deviceType": device_type}
        return self._post("/v1.0/device/measurePoints", body).get("measurePoints", [])

    def device_history(self, device_sn, start_at, end_at, granularity, measure_points=None, device_type="INVERTER"):
        body = {"deviceSn": device_sn, "deviceType": device_type, "startAt": str(start_at),
                "endAt": str(end_at), "granularity": int(granularity)}
        if measure_points:
            body["measurePoints"] = measure_points
        return self._post("/v1.0/device/history", body).get("dataList", [])

    def config_tou(self, device_sn):
        return self._post("/v1.0/config/tou", {"deviceSn": device_sn})

    def order_result(self, order_id):
        return self._get(f"/v1.0/order/{order_id}")

    # ---------- escritura (CONTROL) ----------
    # Se exponen pero control.py es quien las usa, siempre tras confirmación explícita.
    def _order_tou_update(self, device_sn, time_use_items):
        return self._post("/v1.0/order/sys/tou/update",
                          {"deviceSn": device_sn, "timeUseSettingItems": time_use_items})

    def _order_battery_mode(self, device_sn, action, battery_mode_type="GRID_CHARGE"):
        return self._post("/v1.0/order/battery/modeControl",
                          {"deviceSn": device_sn, "action": action, "batteryModeType": battery_mode_type})


def load_config(path="config.json"):
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    if not os.path.exists(path):
        raise DeyeError(f"No existe {path}. Copia config.example.json a config.json y rellénalo.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
