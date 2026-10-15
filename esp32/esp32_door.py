# ESP32 2 - door node (MicroPython)
# Job: listen for door commands from the server over MQTT, spin the door motor
# open and back, and turn the user's office LED on (login) or off (logout).
#
# IMPORTANT:
# 1. Change WIFI_SSID, WIFI_PASSWORD, and MQTT_BROKER_IP.
# 2. MQTT_BROKER_IP must be your laptop IP address, not "localhost".
# 3. Install/use umqtt.simple on the ESP32 MicroPython firmware.

import network
import time
import machine
from umqtt.simple import MQTTClient

# ============================================================
# WiFi settings
# ============================================================
WIFI_SSID = "your_wifi_name"
WIFI_PASSWORD = "your_wifi_password"

# ============================================================
# MQTT settings
# ============================================================
MQTT_BROKER_IP = "192.168.1.100"  # your laptop IP
MQTT_PORT = 1883
CLIENT_ID = "esp32_door_motor_node"
DOOR_TOPIC = b"faceauth/esp32_2/door"

# ============================================================
# Motor driver pins (H-bridge): IN1 = GPIO 5, IN2 = GPIO 6, ENA/PWM = GPIO 7
# Change these only if your wiring is different.
# ============================================================
MOTOR_IN1_PIN = 5
MOTOR_IN2_PIN = 6
MOTOR_ENA_PIN = 7

MOTOR_PWM_FREQ = 30000
MOTOR_DUTY = 43690        # speed: 0 to 65535
HALF_TURN_TIME = 0.8      # seconds for 180 degrees; calibrate this value
RETURN_PAUSE_TIME = 0.3   # small wait before spinning back

motor_in1 = machine.Pin(MOTOR_IN1_PIN, machine.Pin.OUT)
motor_in2 = machine.Pin(MOTOR_IN2_PIN, machine.Pin.OUT)
motor_pwm = machine.PWM(machine.Pin(MOTOR_ENA_PIN))
motor_pwm.freq(MOTOR_PWM_FREQ)

# ============================================================
# LED pins: one LED per user ID (must match person_id in people/<name>/info.json).
# Do not reuse the motor pins 5, 6, 7.
# ============================================================
LED_PINS = {
    "01": 15,
    "02": 16,
    "03": 17,
}
LED_ACTIVE_HIGH = True
leds = {}

# ============================================================
# Motor / door logic
# ============================================================
def stop_motor():
    motor_in1.value(0)
    motor_in2.value(0)
    motor_pwm.duty_u16(0)


def motor_forward(seconds):
    motor_pwm.duty_u16(MOTOR_DUTY)
    motor_in1.value(1)
    motor_in2.value(0)
    time.sleep(seconds)
    stop_motor()


def motor_reverse(seconds):
    motor_pwm.duty_u16(MOTOR_DUTY)
    motor_in1.value(0)
    motor_in2.value(1)
    time.sleep(seconds)
    stop_motor()


def door_open_then_close():
    """
    Opens the door by spinning about 180 degrees,
    then spins back about 180 degrees to return/close.

    Because this is a normal DC motor, the 180-degree movement is controlled
    by time. Adjust HALF_TURN_TIME until the movement is exactly 180 degrees.
    """
    print("Door opening: 180 degrees forward")
    motor_forward(HALF_TURN_TIME)

    time.sleep(RETURN_PAUSE_TIME)

    print("Door returning: 180 degrees back")
    motor_reverse(HALF_TURN_TIME)

    print("Door cycle done")


def door_open_only():
    """Optional: use if your MQTT sends a separate open command."""
    print("Door open only: 180 degrees forward")
    motor_forward(HALF_TURN_TIME)


def door_close_only():
    """Optional: use if your MQTT sends a separate close command."""
    print("Door close only: 180 degrees reverse")
    motor_reverse(HALF_TURN_TIME)


# Start stopped for safety
stop_motor()

# ============================================================
# LED logic
# ============================================================
def setup_leds():
    for person_id, pin_number in LED_PINS.items():
        pin = machine.Pin(pin_number, machine.Pin.OUT)
        leds[person_id] = pin
        set_led(person_id, False)
    print("LEDs ready:", LED_PINS)


def set_led(person_id, state):
    pin = leds.get(person_id)

    if pin is None:
        print("No LED configured for user ID:", person_id)
        return

    if LED_ACTIVE_HIGH:
        pin.value(1 if state else 0)
    else:
        pin.value(0 if state else 1)

    print("LED", person_id, "ON" if state else "OFF")


# ============================================================
# Network / MQTT logic
# ============================================================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)

        timeout_seconds = 25
        while not wlan.isconnected() and timeout_seconds > 0:
            print("Waiting for WiFi...")
            time.sleep(1)
            timeout_seconds -= 1

    if wlan.isconnected():
        print("WiFi connected:", wlan.ifconfig())
        return True

    print("WiFi connection failed")
    return False


def handle_door_message(message):
    """
    Supported MQTT payloads:

      open,01,login       -> door opens 180, returns 180, LED 01 ON
      open,01,logout      -> door opens 180, returns 180, LED 01 OFF

    Also supported if your server sends simple commands:

      open                -> door opens 180, returns 180
      close               -> door closes/reverses 180 only
      open,01             -> door opens 180, returns 180
      close,01            -> door closes/reverses 180 only
    """
    try:
        text = message.decode().strip()
    except Exception:
        print("Invalid message bytes:", message)
        return

    print("Door message:", text)

    parts = [part.strip() for part in text.split(",")]
    command = parts[0].lower() if len(parts) >= 1 else ""
    person_id = parts[1] if len(parts) >= 2 else None
    action = parts[2].lower() if len(parts) >= 3 else None

    if command == "open":
        # Spin ~180 degrees, then spin back.
        door_open_then_close()

    elif command == "close":
        # Optional: only reverse ~180 degrees.
        door_close_only()

    else:
        print("Ignored command:", command)
        return

    # LED on at login, off at logout.
    if person_id is not None and action is not None:
        if action == "login":
            set_led(person_id, True)
        elif action == "logout":
            set_led(person_id, False)
        else:
            print("Invalid action:", action)


def on_mqtt_message(topic, message):
    print("MQTT received:", topic, message)

    if topic == DOOR_TOPIC:
        handle_door_message(message)
    else:
        print("Ignored topic:", topic)


def connect_mqtt():
    client = MQTTClient(
        client_id=CLIENT_ID,
        server=MQTT_BROKER_IP,
        port=MQTT_PORT,
        keepalive=60,
    )

    client.set_callback(on_mqtt_message)
    client.connect()
    client.subscribe(DOOR_TOPIC)

    print("MQTT connected to:", MQTT_BROKER_IP)
    print("Subscribed to:", DOOR_TOPIC)

    return client


def connect_mqtt_with_retry():
    while True:
        try:
            return connect_mqtt()
        except OSError as error:
            print("MQTT connection failed:", error)
            print("Retrying in 5 seconds...")
            time.sleep(5)


def main():
    setup_leds()

    if not connect_wifi():
        return

    client = connect_mqtt_with_retry()

    while True:
        try:
            client.check_msg()
            time.sleep(0.1)

        except OSError as error:
            print("MQTT error:", error)
            print("Reconnecting MQTT...")

            try:
                client.disconnect()
            except Exception:
                pass

            stop_motor()
            client = connect_mqtt_with_retry()


if __name__ == "__main__":
    main()
