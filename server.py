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
    "pending_route": None,  # маршрут, ещё не подтверждённый оператором
    "phone_gps": None,      # {"lat","lng","heading","acc"}
    "robot_status": None,   # последний статус от ESP32, пересланный телефоном
}

# Ключ бери на openrouteservice.org (Dashboard -> API Key) и задавай через
# переменную окружения ORS_API_KEY в настройках сервиса на Render — не хардкодь
# его в коде, чтобы не светился в публичном GitHub-репозитории.
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"


def get_walking_route(coords):
    """coords: список (lat, lng), первая точка — текущее положение робота.
    Возвращает (points, distance_m, duration_s) или (None, None, None) при ошибке."""
    if not ORS_API_KEY:
        return None, None, None
    body = {"coordinates": [[lng, lat] for lat, lng in coords]}
    headers = {"Authorization": ORS_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.post(ORS_URL, json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        feature = data["features"][0]
        geometry = feature["geometry"]["coordinates"]  # [[lng,lat], ...]
        summary = feature["properties"]["summary"]
        points = [{"lat": lat, "lng": lng} for lng, lat in geometry]
        return points, summary.get("distance"), summary.get("duration")
    except Exception as e:
        print("ORS error:", e)
        return None, None, None


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
                continue

            if role is None:
                continue

            # ---- Оператор -> Построение маршрута (превью, ещё не едем) ----
            if mtype == "set_waypoints" and role == "operator":
                dest_points = msg.get("points", [])
                if not state["phone_gps"]:
                    ws.send(json.dumps({
                        "type": "route_error",
                        "message": "Нет GPS телефона — сначала дождись, пока телефон выйдет на связь.",
                    }))
                    continue

                start = (state["phone_gps"]["lat"], state["phone_gps"]["lng"])
                coords = [start] + [(p["lat"], p["lng"]) for p in dest_points]
                route_points, distance_m, duration_s = get_walking_route(coords)

                if route_points is None:
                    # ORS недоступен/не настроен ключ — едем по прямой линии между точками,
                    # как раньше, но явно предупреждаем оператора.
                    route_points = dest_points
                    ws.send(json.dumps({
                        "type": "route_error",
                        "message": "Не удалось построить маршрут по тротуарам (проверь ORS_API_KEY на сервере) — показан путь по прямой.",
                    }))

                state["pending_route"] = route_points
                broadcast("operator", {
                    "type": "route_preview",
                    "points": route_points,
                    "distance_m": distance_m,
                    "duration_s": duration_s,
                })

            elif mtype == "confirm_route" and role == "operator":
                if state["pending_route"]:
                    state["waypoints"] = state["pending_route"]
                    state["pending_route"] = None
                    broadcast("phone", {"type": "waypoints", "points": state["waypoints"]})
                    broadcast("operator", {"type": "route_confirmed"})

            elif mtype == "nav_control" and role == "operator":
                broadcast("phone", {"type": "nav_control", "cmd": msg.get("cmd")})

            elif mtype == "gimbal" and role == "operator":
                broadcast("phone", {"type": "gimbal", **{k: v for k, v in msg.items() if k != "type"}})

            # ---- Телефон -> Оператор ----
            elif mtype == "phone_gps" and role == "phone":
                state["phone_gps"] = {
                    "lat": msg["lat"], "lng": msg["lng"],
                    "heading": msg.get("heading"), "acc": msg.get("acc"),
                }
                broadcast("operator", {"type": "phone_gps", **state["phone_gps"]})

            elif mtype == "video_frame" and role == "phone":
                broadcast("operator", msg)

            elif mtype in ("robot_status", "nav_progress", "nav_done", "sensors") and role == "phone":
                if mtype == "robot_status":
                    state["robot_status"] = msg
                broadcast("operator", msg)

    finally:
        if role:
            clients[role].discard(ws)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
