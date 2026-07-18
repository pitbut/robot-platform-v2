"""
Robot Delivery Platform — relay server
=======================================
Три роли клиентов подключаются на один WebSocket-эндпоинт /ws
и представляются первым сообщением {"type":"hello","role":"..."}:

  operator  — браузер оператора: карта, выставление точек маршрута,
              просмотр видео с телефона, гимбал (pan/tilt/roll), ручное управление.
  phone     — телефон, закреплённый на роботе: шлёт GPS + компас (heading) + видео.
  robot     — ESP32: получает маршрут и GPS/heading телефона, едет, объезжает
              препятствия, шлёт статус/показания датчиков обратно.

Сервер только ретранслирует и хранит последнее состояние, чтобы новый
клиент сразу получил актуальную картину при подключении.
"""
import json
import time
from flask import Flask, render_template
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

clients = {"operator": set(), "phone": set(), "robot": set()}

state = {
    "waypoints": [],        # [{"lat":.., "lng":..}, ...]
    "phone_gps": None,      # {"lat","lng","heading","acc","ts"}
    "robot_status": None,   # последний {"type":"robot_status", ...} от ESP32
    "gimbal": {"pan": 90, "tilt": 90, "roll": 90},
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
                if role == "robot":
                    ws.send(json.dumps({"type": "waypoints", "points": state["waypoints"]}))
                    ws.send(json.dumps({"type": "gimbal", **state["gimbal"]}))
                if role == "operator":
                    if state["robot_status"]:
                        ws.send(json.dumps(state["robot_status"]))
                    if state["phone_gps"]:
                        ws.send(json.dumps({"type": "phone_gps", **state["phone_gps"]}))
                continue

            if role is None:
                continue  # игнорируем всё до hello

            # ---- Оператор -> Робот + Телефон (мозг теперь в телефоне) ----
            if mtype == "set_waypoints" and role == "operator":
                state["waypoints"] = msg.get("points", [])
                broadcast("robot", {"type": "waypoints", "points": state["waypoints"]})
                broadcast("phone", {"type": "waypoints", "points": state["waypoints"]})

            elif mtype == "nav_control" and role == "operator":
                # ESP32 включает/выключает применение phone_drive,
                # телефон включает/выключает свой цикл навигации
                broadcast("robot", {"type": "nav_control", "cmd": msg.get("cmd")})
                broadcast("phone", {"type": "nav_control", "cmd": msg.get("cmd")})

            elif mtype == "gimbal" and role == "operator":
                g = state["gimbal"]
                state["gimbal"] = {
                    "pan": msg.get("pan", g["pan"]),
                    "tilt": msg.get("tilt", g["tilt"]),
                    "roll": msg.get("roll", g["roll"]),
                }
                broadcast("robot", {"type": "gimbal", **state["gimbal"]})

            elif mtype == "manual_drive" and role == "operator":
                broadcast("robot", msg)

            # ---- Телефон -> Робот + Оператор ----
            elif mtype == "phone_gps" and role == "phone":
                state["phone_gps"] = {
                    "lat": msg["lat"], "lng": msg["lng"],
                    "heading": msg.get("heading"), "acc": msg.get("acc"),
                    "ts": time.time(),
                }
                broadcast("robot", {"type": "phone_gps", **state["phone_gps"]})
                broadcast("operator", {"type": "phone_gps", **state["phone_gps"]})

            elif mtype == "video_frame" and role == "phone":
                broadcast("operator", msg)

            # ---- Телефон (мозг) -> Робот: команды на моторы ----
            elif mtype == "phone_drive" and role == "phone":
                broadcast("robot", msg)

            # ---- Робот -> Оператор ----
            elif mtype in ("robot_status", "nav_progress", "nav_done", "sensors") and role == "robot":
                if mtype == "robot_status":
                    state["robot_status"] = msg
                broadcast("operator", msg)

    finally:
        if role:
            clients[role].discard(ws)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
