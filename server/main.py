"""Face authentication server.

Receives face images from the ESP32 camera over MQTT, recognises the person,
toggles their login/logout state, sends the door command to the second ESP32,
and serves a live dashboard at http://127.0.0.1:5055.

Usage:
    python main.py               # load the saved face database
    python main.py --rebuild-db  # rebuild it from the people/ folder first
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from ai_model import FaceAI
from dashboard import run_dashboard


# MQTT settings (the broker runs on this same laptop)
BROKER_HOST = "localhost"
BROKER_PORT = 1883
CLIENT_ID = "face_auth_laptop_server"
QOS = 0

IMAGE_TOPIC = "faceauth/esp32_1/image"
REJECT_TOPIC = "faceauth/esp32_1/reject"
DOOR_TOPIC = "faceauth/esp32_2/door"

REJECT_PAYLOAD = b"0"

# Local files (relative to this script, so it can be run from any folder)
BASE_DIR = Path(__file__).resolve().parent
PEOPLE_DIR = BASE_DIR / "people"
FACE_DATABASE_FILE = BASE_DIR / "face_database.npy"
HISTORY_FILE = BASE_DIR / "history.json"

# Dashboard files
STATIC_DIR = BASE_DIR / "static"
LATEST_ATTEMPT_IMAGE = STATIC_DIR / "latest_attempt.jpg"

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5055  # 5000 is often taken on Windows


class FaceAuthServer:
    def __init__(self, face_ai: FaceAI) -> None:
        self.face_ai = face_ai
        self.lock = threading.Lock()

        self.history = self._load_history()
        self.active_users: dict[str, dict[str, Any]] = {}
        self.login_counts: dict[str, int] = {}
        self.last_event = "Server started"
        self.latest_attempt: dict[str, Any] | None = None

        self._restore_state_from_history()

    def handle_image(self, image_bytes: bytes, mqtt_client: mqtt.Client) -> None:
        """
        Called whenever ESP32 1 sends an image over MQTT.
        Recognises the face, publishes the result, and updates the dashboard state.
        """
        STATIC_DIR.mkdir(parents=True, exist_ok=True)

        try:
            LATEST_ATTEMPT_IMAGE.write_bytes(image_bytes)
        except OSError:
            pass

        result = self.face_ai.recognize_bytes(image_bytes)
        now_ts = time.time()
        now_text = current_time_text()

        if not result["authenticated"]:
            # Tell ESP32 1 the face was rejected.
            mqtt_client.publish(REJECT_TOPIC, REJECT_PAYLOAD, qos=QOS)

            with self.lock:
                self.last_event = f"Rejected face: {result.get('reason', '-')}"
                self.latest_attempt = {
                    "visible": True,
                    "authenticated": False,
                    "status_text": "User Rejected",
                    "status_class": "rejected",
                    "name": None,
                    "person_id": None,
                    "action": None,
                    "timestamp": now_text,
                    "attempt_ts": now_ts,
                    "image_url": f"/static/latest_attempt.jpg?v={int(now_ts)}",
                }

            return

        person = result["person"]
        confidence = float(result["confidence"])

        action = self._toggle_login_logout(person, confidence)
        door_action = "login" if action == "logged in" else "logout"
        door_payload = self._make_door_payload(person, door_action)

        # Door command for ESP32 2: open,<person_id>,login  or  open,<person_id>,logout
        mqtt_client.publish(DOOR_TOPIC, door_payload, qos=QOS)

        with self.lock:
            self.last_event = (
                f"{person['name']} {door_action}; "
                f"sent to ESP32 2: {door_payload.decode()}"
            )
            self.latest_attempt = {
                "visible": True,
                "authenticated": True,
                "status_text": "User Authenticated",
                "status_class": "authenticated",
                "name": str(person["name"]),
                "person_id": str(person["person_id"]),
                "action": door_action,
                "timestamp": now_text,
                "attempt_ts": now_ts,
                "image_url": f"/static/latest_attempt.jpg?v={int(now_ts)}",
            }

    @staticmethod
    def _make_door_payload(person: dict[str, Any], action: str) -> bytes:
        person_id = str(person["person_id"]).strip()
        return f"open,{person_id},{action}".encode("utf-8")

    def get_dashboard_state(self) -> dict[str, Any]:
        """
        Called by dashboard.py /api/state.
        Returns JSON-safe state for the web dashboard.
        """
        now_ts = time.time()

        with self.lock:
            active_rows = []

            for row in self.active_users.values():
                entry_ts = float(row.get("entry_ts", now_ts))
                active_rows.append(
                    {
                        "name": row.get("name", "-"),
                        "person_id": row.get("person_id", "-"),
                        "entry_time": row.get("entry_time", "-"),
                        "current_duration": format_duration(int(now_ts - entry_ts)),
                    }
                )

            history_rows = []

            for row in self.history:
                status = row.get("status", "-")
                duration = row.get("duration", "-")

                if status == "active":
                    entry_ts = float(row.get("entry_ts", now_ts))
                    duration = format_duration(int(now_ts - entry_ts))

                history_rows.append(
                    {
                        "name": row.get("name", "-"),
                        "person_id": row.get("person_id", "-"),
                        # history.json stores entry/exit; the dashboard shows login/logout.
                        "login_time": row.get("entry_time", "-"),
                        "logout_time": row.get("exit_time") or "-",
                        "duration": duration,
                        "login_count": int(row.get("login_count", 0)),
                        "status": status,
                    }
                )

            latest_attempt = self.latest_attempt

            # Clear "Latest Attempt" after 10 seconds.
            if latest_attempt is not None:
                attempt_ts = float(latest_attempt.get("attempt_ts", 0))
                if now_ts - attempt_ts > 10:
                    latest_attempt = None
                    self.latest_attempt = None

            return {
                "active_users": active_rows,
                "history": history_rows,
                "login_history": history_rows,
                "latest_attempt": latest_attempt,
                "last_event": self.last_event,
                "server_time": current_time_text(),
            }

    def _toggle_login_logout(self, person: dict[str, Any], confidence: float) -> str:
        person_id = str(person["person_id"])
        name = str(person["name"])
        now_ts = time.time()
        now_text = current_time_text()

        with self.lock:
            if person_id in self.active_users:
                active_record = self.active_users.pop(person_id)
                history_index = int(active_record["history_index"])
                history_record = self.history[history_index]
                duration_seconds = int(now_ts - float(history_record["entry_ts"]))

                history_record["exit_time"] = now_text
                history_record["exit_ts"] = now_ts
                history_record["duration_seconds"] = duration_seconds
                history_record["duration"] = format_duration(duration_seconds)
                history_record["status"] = "completed"

                self._save_history()
                return "logged out"

            login_count = self.login_counts.get(person_id, 0) + 1
            self.login_counts[person_id] = login_count

            history_record = {
                "person_id": person_id,
                "name": name,
                "entry_time": now_text,
                "entry_ts": now_ts,
                "exit_time": None,
                "exit_ts": None,
                "duration": "active",
                "duration_seconds": None,
                "login_count": login_count,
                "confidence": round(confidence, 4),
                "status": "active",
            }

            self.history.append(history_record)
            history_index = len(self.history) - 1

            self.active_users[person_id] = {
                "person_id": person_id,
                "name": name,
                "entry_time": now_text,
                "entry_ts": now_ts,
                "history_index": history_index,
            }

            self._save_history()
            return "logged in"

    def _load_history(self) -> list[dict[str, Any]]:
        if not HISTORY_FILE.exists():
            return []

        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except json.JSONDecodeError:
            return []

    def _save_history(self) -> None:
        HISTORY_FILE.write_text(
            json.dumps(self.history, indent=2),
            encoding="utf-8",
        )

    def _restore_state_from_history(self) -> None:
        for index, row in enumerate(self.history):
            person_id = str(row.get("person_id", ""))

            if not person_id:
                continue

            self.login_counts[person_id] = max(
                self.login_counts.get(person_id, 0),
                int(row.get("login_count", 0)),
            )

            if row.get("status") == "active":
                self.active_users[person_id] = {
                    "person_id": person_id,
                    "name": row.get("name", person_id),
                    "entry_time": row.get("entry_time", "-"),
                    "entry_ts": row.get("entry_ts", time.time()),
                    "history_index": index,
                }


def create_mqtt_client() -> mqtt.Client:
    # Works with both paho-mqtt 2.x and 1.x.
    try:
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
        )
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=CLIENT_ID)


def start_mqtt(server: FaceAuthServer) -> None:
    client = create_mqtt_client()

    def on_connect(
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        client.subscribe(IMAGE_TOPIC, qos=QOS)

    def on_message(
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        if message.topic == IMAGE_TOPIC:
            server.handle_image(message.payload, client)

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_forever()


def start_mqtt_background(server: FaceAuthServer) -> None:
    mqtt_thread = threading.Thread(
        target=start_mqtt,
        args=(server,),
        daemon=True,
    )
    mqtt_thread.start()


def current_time_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Face authentication server with a local web dashboard"
    )
    parser.add_argument(
        "--rebuild-db",
        action="store_true",
        help="Rebuild face_database.npy from the people/ folder before starting",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    face_ai = FaceAI(
        people_dir=PEOPLE_DIR,
        database_path=FACE_DATABASE_FILE,
    )
    face_ai.load_or_build_database(rebuild=args.rebuild_db)

    server = FaceAuthServer(face_ai)

    start_mqtt_background(server)
    run_dashboard(server, host=DASHBOARD_HOST, port=DASHBOARD_PORT)


if __name__ == "__main__":
    main()
