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
from flask import Flask, render_template
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

clients = {"operator": set(), "phone": set()}

state = {
    "waypoints": [],        # [{"lat":.., "lng":..}, ...]
    "phone_gps": None,      # {"lat","lng","heading","acc"}
    "robot_status": None,   # последний статус от ESP32, пересланный телефоном
}


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

            # ---- Оператор -> Телефон ----
            if mtype == "set_waypoints" and role == "operator":
                state["waypoints"] = msg.get("points", [])
                broadcast("phone", {"type": "waypoints", "points": state["waypoints"]})

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
