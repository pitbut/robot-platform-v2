"""
Robot Delivery Platform — relay server
=======================================
ESP32 сюда больше НЕ подключается — телефон-мозг общается с ней напрямую
по локальному Wi-Fi (телефон сам раздаёт точку доступа). Этот сервер нужен
только для связи оператор (клиент, далеко от робота) <-> телефон-мозг
(видео, GPS, статус, маршрут) через интернет.

  operator — браузер оператора: карта, выставление точек маршрута,
              просмотр видео с телефона, статус робота.
  phone    — телефон, закреплённый на роботе: шлёт GPS + компас (heading) + видео,
             сам же локально управляет ESP32 (без Render).
"""
# ВАЖНО: gevent должен пропатчить стандартную библиотеку (в т.ч. ssl) ДО того,
# как её импортирует requests/urllib3 — иначе HTTPS-запросы (например, к ORS)
# падают с "maximum recursion depth exceeded". Поэтому это первые две строки
# в файле, раньше всех остальных импортов.
from gevent import monkey
monkey.patch_all()

import json
import os
import requests
from flask import Flask, render_template
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

clients = {"operator": set(), "phone": set()}

state = {
    "waypoints": [],        # [{"lat":.., "lng":..}, ...] — подтверждённый маршрут
    "milestones": [],       # [{"index","address","lat","lng"}, ...] — адреса точек оператора
    "pending_route": None,  # маршрут, ещё не подтверждённый оператором
    "pending_milestones": [],
    "phone_gps": None,      # {"lat","lng","heading","acc"}
    "robot_status": None,   # последний статус от ESP32, пересланный телефоном
    "nav_running": False,   # едет ли робот сейчас — чтобы восстановить при обновлении страницы
}

# Ключ бери на openrouteservice.org (Dashboard -> API Key) и задавай через
# переменную окружения ORS_API_KEY в настройках сервиса на Render — не хардкодь
# его в коде, чтобы не светился в публичном GitHub-репозитории.
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"
ORS_GEOCODE_URL = "https://api.openrouteservice.org/geocode/reverse"


def nearest_route_index(route_points, target_lat, target_lng):
    """Индекс точки маршрута, ближайшей к (target_lat, target_lng) — чтобы знать,
    на каком шаге детального маршрута находится каждая исходная точка оператора."""
    best_i, best_d = 0, float("inf")
    for i, p in enumerate(route_points):
        d = (p["lat"] - target_lat) ** 2 + (p["lng"] - target_lng) ** 2
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def get_walking_route(coords):
    """coords: список (lat, lng), первая точка — текущее положение робота.
    Возвращает (points, distance_m, duration_s) или (None, None, None) при ошибке."""
    if not ORS_API_KEY:
        return None, None, None
    body = {"coordinates": [[lng, lat] for lat, lng in coords]}
    headers = {"Authorization": ORS_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.post(ORS_URL, json=body, headers=headers, timeout=10)
        if not resp.ok:
            print(f"ORS error: HTTP {resp.status_code} — {resp.text[:500]}")
            return None, None, None
        data = resp.json()
        feature = data["features"][0]
        geometry = feature["geometry"]["coordinates"]  # [[lng,lat], ...]
        summary = feature["properties"]["summary"]
        points = [{"lat": lat, "lng": lng} for lng, lat in geometry]
        return points, summary.get("distance"), summary.get("duration")
    except Exception as e:
        print("ORS error (exception):", repr(e))
        return None, None, None


def reverse_geocode(lat, lng):
    """Координаты -> человекочитаемый адрес поблизости, или None если не удалось."""
    if not ORS_API_KEY:
        return None
    try:
        resp = requests.get(ORS_GEOCODE_URL, params={
            "api_key": ORS_API_KEY,
            "point.lon": lng,
            "point.lat": lat,
            "size": 1,
        }, timeout=8)
        if not resp.ok:
            print(f"Geocode error: HTTP {resp.status_code} — {resp.text[:300]}")
            return None
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return None
        return features[0]["properties"].get("label")
    except Exception as e:
        print("Geocode error (exception):", repr(e))
        return None


def broadcast(role, payload, exclude=None):
    dead = []
    for ws in clients[role]:
        if ws is exclude:
            continue
        try:
            ws.send(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients[role].discard(ws)


@app.route("/")
def operator_page():
    return render_template("map.html")


@app.route("/phone")
def phone_page():
    return render_template("phone.html")


@sock.route("/ws")
def ws_endpoint(ws):
    role = None
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue

            mtype = msg.get("type")

            if mtype == "hello":
                role = msg.get("role")
                if role not in clients:
                    role = None
                    continue
                clients[role].add(ws)
                if role == "operator":
                    if state["robot_status"]:
                        ws.send(json.dumps(state["robot_status"]))
                    if state["phone_gps"]:
                        ws.send(json.dumps({"type": "phone_gps", **state["phone_gps"]}))
                    if state["waypoints"]:
                        ws.send(json.dumps({
                            "type": "route_restore",
                            "points": state["waypoints"],
                            "milestones": state["milestones"],
                            "nav_running": state["nav_running"],
                        }))
                continue

            if role is None:
                continue

            # ---- Координаты -> адрес (общее для оператора и телефона, отвечаем только спросившему) ----
            if mtype == "reverse_geocode_request":
                lat = msg.get("lat")
                lng = msg.get("lng")
                address = reverse_geocode(lat, lng) if lat is not None and lng is not None else None
                reply = {"type": "reverse_geocode_result", "lat": lat, "lng": lng, "address": address}
                if "index" in msg:
                    reply["index"] = msg["index"]
                ws.send(json.dumps(reply))
                continue

            # ---- Оператор -> Построение маршрута (превью, ещё не едем) ----
            if mtype == "set_waypoints" and role == "operator":
                dest_points = msg.get("points", [])
                if not state["phone_gps"]:
                    ws.send(json.dumps({
                        "type": "route_preview",
                        "points": dest_points,
                        "distance_m": None,
                        "duration_s": None,
                        "error": "Нет GPS телефона — сначала дождись, пока телефон выйдет на связь.",
                    }))
                    continue

                start = (state["phone_gps"]["lat"], state["phone_gps"]["lng"])
                coords = [start] + [(p["lat"], p["lng"]) for p in dest_points]
                route_points, distance_m, duration_s = get_walking_route(coords)

                error_msg = None
                if route_points is None:
                    # ORS недоступен/не настроен ключ — едем по прямой линии между точками,
                    # как раньше, но явно предупреждаем оператора (в ТОМ ЖЕ сообщении,
                    # чтобы предупреждение не затёрлось следующим broadcast'ом).
                    route_points = dest_points
                    error_msg = "Не удалось построить маршрут по тротуарам (см. логи сервера на Render) — показан путь по прямой."

                state["pending_route"] = route_points

                milestones = []
                for p in dest_points:
                    addr = reverse_geocode(p["lat"], p["lng"]) or f"{p['lat']:.5f}, {p['lng']:.5f}"
                    idx = nearest_route_index(route_points, p["lat"], p["lng"])
                    milestones.append({"index": idx, "address": addr, "lat": p["lat"], "lng": p["lng"]})
                state["pending_milestones"] = milestones

                broadcast("operator", {
                    "type": "route_preview",
                    "points": route_points,
                    "distance_m": distance_m,
                    "duration_s": duration_s,
                    "error": error_msg,
                    "milestones": milestones,
                })

            elif mtype == "confirm_route" and role == "operator":
                if state["pending_route"]:
                    state["waypoints"] = state["pending_route"]
                    state["milestones"] = state.get("pending_milestones", [])
                    state["pending_route"] = None
                    broadcast("phone", {
                        "type": "waypoints",
                        "points": state["waypoints"],
                        "milestones": state["milestones"],
                    })
                    broadcast("operator", {"type": "route_confirmed"})

            elif mtype == "nav_control" and role == "operator":
                state["nav_running"] = msg.get("cmd") == "start"
                broadcast("phone", {"type": "nav_control", "cmd": msg.get("cmd")})

            elif mtype == "gimbal" and role == "operator":
                broadcast("phone", {"type": "gimbal", **{k: v for k, v in msg.items() if k != "type"}})

            elif mtype == "listen_control" and role == "operator":
                broadcast("phone", {"type": "listen_control", "enabled": msg.get("enabled", False)})

            elif mtype == "talk_control" and role == "operator":
                broadcast("phone", {"type": "talk_control", "enabled": msg.get("enabled", False)})

            elif mtype == "operator_audio_chunk" and role == "operator":
                broadcast("phone", msg)

            # ---- Телефон -> Оператор ----
            elif mtype == "phone_gps" and role == "phone":
                state["phone_gps"] = {
                    "lat": msg["lat"], "lng": msg["lng"],
                    "heading": msg.get("heading"), "acc": msg.get("acc"),
                }
                broadcast("operator", {"type": "phone_gps", **state["phone_gps"]})

            elif mtype == "video_frame" and role == "phone":
                broadcast("operator", msg)

            elif mtype == "audio_frame" and role == "phone":
                broadcast("operator", msg)

            elif mtype in ("robot_status", "nav_progress", "nav_done", "sensors") and role == "phone":
                if mtype == "robot_status":
                    state["robot_status"] = msg
                if mtype == "nav_done":
                    state["nav_running"] = False
                broadcast("operator", msg)

    finally:
        if role:
            clients[role].discard(ws)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
