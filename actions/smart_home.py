"""smart_home — MQTT publish/subscribe for local home automation.

Talks plain MQTT (the protocol Home Assistant, zigbee2mqtt, Tasmota and
ESPHome bridges all speak) — no cloud, no vendor SDK, no paid tier.
paho-mqtt is the only dependency and it is OPTIONAL: without it the tool
still loads and says exactly how to install it.

Actions:
  pub    topic + payload (+ retain, qos)   → publish one message
  sub    topic (+ timeout)                 → collect messages for N seconds
  status                                    → is the broker reachable?
  help                                      → topic conventions cheat-sheet

Privacy mode: only LOOPBACK / private-network brokers may be reached
(MQTT on the LAN is not the cloud; an internet broker is — blocked).
"""

from __future__ import annotations

import json
import time

try:                                     # optional — CI and minimal installs
    import paho.mqtt.client as _mqtt
except Exception:                        # pragma: no cover
    _mqtt = None                         # type: ignore[assignment]

from core import privacy as _privacy

# ── config ────────────────────────────────────────────────────────────


def _settings() -> dict:
    host, port, user, password = "localhost", 1883, None, None
    try:
        from memory.config_manager import load_api_keys
        keys = load_api_keys() or {}
        host = str(keys.get("mqtt_host") or host)
        port = int(keys.get("mqtt_port") or port)
        user = keys.get("mqtt_user") or None
        password = keys.get("mqtt_password") or None
    except Exception:
        pass
    return {"host": host, "port": port, "user": user, "password": password}


def _is_local_host(host: str) -> bool:
    """Loopback, RFC1918, or mDNS — 'the house', not the internet."""
    h = str(host).strip().lower()
    if h in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    if h.endswith(".local") or h.endswith(".lan") or h.endswith(".home"):
        return True
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (
            a == 172 and 16 <= b <= 31
        ):
            return True
    return False


# ── client seam (tests inject a fake factory here) ────────────────────


def _client_factory():
    if _mqtt is None:
        return None
    try:  # paho-mqtt >= 2.0 requires an explicit callback API version
        return _mqtt.Client(_mqtt.CallbackAPIVersion.VERSION2,
                            client_id="jarvis")
    except AttributeError:               # paho 1.x
        return _mqtt.Client(client_id="jarvis")


def _auth(c) -> None:
    s = _settings()
    if s["user"]:
        c.username_pw_set(s["user"], s["password"] or None)


def _connect(c, s) -> None:
    _auth(c)
    c.connect(s["host"], s["port"], keepalive=30)


def _broker_error(s, e) -> str:
    return (
        f"MQTT broker NOT reachable at {s['host']}:{s['port']} "
        f"({type(e).__name__}: {e}). Start your broker (e.g. "
        f"'mosquitto' locally) or fix mqtt_host/mqtt_port in settings."
    )


# ── actions ───────────────────────────────────────────────────────────


def _publish(topic: str, payload: str, retain: bool, qos: int) -> str:
    c = _client_factory()
    if c is None:
        return ("paho-mqtt is not installed — MQTT disabled. Install with: "
                "pip install paho-mqtt (free, open source).")
    s = _settings()
    try:
        _connect(c, s)
        c.loop_start()
        try:
            info = c.publish(topic, payload, qos=qos, retain=retain)
            if getattr(info, "rc", 0) != 0:
                return f"Publish to '{topic}' failed (rc={info.rc})."
            try:
                info.wait_for_publish(timeout=5.0)
            except TypeError:            # older paho without timeout kw
                info.wait_for_publish()
            finally:
                pass
        finally:
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:
                pass
        shown = payload if len(payload) <= 120 else payload[:117] + "..."
        extra = " (retained)" if retain else ""
        return (f"Published to '{topic}'{extra} → {shown}\n"
                f"Broker {s['host']}:{s['port']}")
    except Exception as e:
        return _broker_error(s, e)


def _listen(topic: str, timeout: float) -> str:
    c = _client_factory()
    if c is None:
        return ("paho-mqtt is not installed — MQTT disabled. Install with: "
                "pip install paho-mqtt (free, open source).")
    s = _settings()
    msgs: list[dict] = []
    try:
        _connect(c, s)
        try:
            c.subscribe(topic, qos=1)
        except TypeError:
            c.subscribe(topic)
        deadline = time.time() + max(0.1, min(timeout, 60.0))

        def _on_message(_c, _u, m):
            try:
                raw = m.payload.decode("utf-8", errors="replace")
            except Exception:
                raw = str(m.payload)
            msgs.append({
                "topic": getattr(m, "topic", topic) if isinstance(
                    getattr(m, "topic", ""), str) else str(
                    getattr(m, "topic", topic)),
                "payload": raw[:2000],
            })
            deadline_box[0] = time.time()   # got one → exit early
        deadline_box = [deadline]
        try:
            c.on_message = _on_message
        except Exception:
            pass
        while time.time() < deadline_box[0]:
            try:
                c.loop(0.2)
            except Exception:
                break
        try:
            c.disconnect()
        except Exception:
            pass
    except Exception as e:
        return _broker_error(s, e)

    if not msgs:
        return (f"No messages on '{topic}' in {timeout}s "
                f"(broker {s['host']}:{s['port']}).")
    lines = [f"{len(msgs)} message(s) on '{topic}':"]
    for m in msgs[:20]:
        lines.append(f"  {m['topic']}: {m['payload'][:300]}")
    if len(msgs) > 20:
        lines.append(f"  … {len(msgs) - 20} more")
    return "\n".join(lines)


def _status() -> str:
    c = _client_factory()
    if c is None:
        s = _settings()
        return (f"paho-mqtt not installed — cannot probe broker. Target "
                f"{s['host']}:{s['port']}. Install: pip install paho-mqtt")
    s = _settings()
    try:
        _connect(c, s)
        try:
            c.loop(timeout=1.0)           # CONNACK
        except Exception:
            pass
        try:
            c.disconnect()
        except Exception:
            pass
        return (f"Broker REACHABLE at {s['host']}:{s['port']} "
                f"(TCP connected).")
    except Exception as e:
        return _broker_error(s, e)


_HELP = """MQTT cheat-sheet (works with Home Assistant / zigbee2mqtt /
Tasmota / ESPHome):
  pub topic=zigbee2mqtt/0x003 payload='{"state":"ON","brightness":200}'
  pub topic=homeassistant/light/kitchen/set payload='{"state":"ON"}'
  sub topic=zigbee2mqtt/+ timeout=10          ← device states stream in
  sub topic=homeassistant/# timeout=15         ← HA discovery tree
  status                                       ← broker up?
Config keys in settings: mqtt_host (localhost), mqtt_port (1883),
mqtt_user / mqtt_password (optional). Privacy mode allows only
loopback/private-network brokers."""


def smart_home(parameters: dict, ctx: dict | None = None) -> str:
    parameters = parameters or {}
    action = str(parameters.get("action") or "").lower()
    if action in {"", "help"}:
        return _HELP

    topic = str(parameters.get("topic") or "").strip()
    s = _settings()

    # Privacy: smart_home is gated (internet brokers = data leaving the
    # house) but a loopback/LAN target is NOT the cloud → re-allow it.
    blocked = _privacy.gate("smart_home")
    if blocked and not _is_local_host(s["host"]):
        return (f"Privacy mode is ON — MQTT target {s['host']} is outside "
                f"the local network, so I didn't touch it. Only "
                f"loopback/private-network brokers are allowed while "
                f"privacy mode is ON.")

    if action in {"status", "ping"}:
        return _status()

    if action in {"pub", "publish", "send"}:
        if not topic:
            return "pub needs a topic, e.g. topic=zigbee2mqtt/0x003"
        payload = parameters.get("payload")
        if payload is None or str(payload) == "":
            # State shorthand: only MQTT-ish fields — never the dispatch
            # `action` key itself (that's this tool's verb, not data).
            payload = json.dumps(
                {k: v for k, v in parameters.items()
                 if k in {"state", "brightness", "temperature", "color"}
                 and v is not None}
            ) or "{}"
        else:
            payload = str(payload)
        retain = bool(parameters.get("retain") in (True, "true", "True", 1))
        try:
            qos = max(0, min(2, int(parameters.get("qos") or 0)))
        except (TypeError, ValueError):
            qos = 0
        return _publish(topic, payload, retain, qos)

    if action in {"sub", "subscribe", "listen"}:
        if not topic:
            return "sub needs a topic filter, e.g. topic=zigbee2mqtt/#"
        try:
            timeout = float(parameters.get("timeout") or 10)
        except (TypeError, ValueError):
            timeout = 10.0
        return _listen(topic, timeout)

    return ("Unknown action — use: pub | sub | status | help "
            "(topic, payload, timeout, retain, qos)")


TOOL = {
    "name": "smart_home",
    "description": (
        "MQTT pub/sub for local smart-home control (Home Assistant, "
        "zigbee2mqtt, Tasmota, ESPHome). Actions: pub (topic + payload, "
        "optional retain/qos), sub (topic filter + timeout seconds — "
        "streams device state messages back), status (broker reachable?), "
        "help (topic cheat-sheet). Config: mqtt_host/mqtt_port/mqtt_user/"
        "mqtt_password in settings; defaults localhost:1883. Privacy mode "
        "allows only loopback/private-network brokers."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "pub | sub | status | help"},
            "topic": {"type": "STRING",
                      "description": "MQTT topic or filter (zigbee2mqtt/…)"},
            "payload": {"type": "STRING",
                        "description": "Message body (pub); JSON or plain"},
            "timeout": {"type": "NUMBER",
                        "description": "Seconds to listen (sub, default 10)"},
            "retain": {"type": "BOOLEAN",
                       "description": "Retain the message on the broker"},
            "qos": {"type": "NUMBER", "description": "0 | 1 | 2"},
        },
        "required": ["action"],
    },
    "handler": smart_home,
}
