"""Tiny MQTT helper (paho-mqtt 2.x). Every message on the bus is one JSON object."""
import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger("bus")


class Bus:
    def __init__(self, host="localhost", port=1883, name="afterglow"):
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=name)
        self.handlers = {}
        self.c.on_message = self._on_message
        self.c.on_connect = self._on_connect
        self.c.connect(host, port, keepalive=30)
        self.c.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("connected to MQTT (%s)", reason_code)
        # Re-subscribe after a reconnect. A copy: the main thread may still be subscribing, and a
        # dict that changes size mid-loop raised here and killed paho's thread (the module went deaf).
        for topic in list(self.handlers):
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg):
        if not msg.payload:  # an empty retained message: someone cleared that topic
            return
        try:
            data = json.loads(msg.payload)
        except ValueError:
            log.warning("non-JSON on %s", msg.topic)
            return
        if not isinstance(data, dict):
            log.warning("non-object JSON on %s", msg.topic)
            return
        for pattern, fn in list(self.handlers.items()):
            if mqtt.topic_matches_sub(pattern, msg.topic):
                try:
                    fn(msg.topic, data)
                except Exception:
                    # Uncaught, this kills paho's network thread and the module goes deaf.
                    log.exception("handler for %s failed on %s", pattern, msg.topic)

    def publish(self, topic: str, obj: dict, retain=False):
        self.c.publish(topic, json.dumps(obj, separators=(",", ":")), qos=0, retain=retain)

    def subscribe(self, topic: str, fn):
        self.handlers[topic] = fn
        self.c.subscribe(topic)

    def clear_retained(self, topic: str):
        """Delete the broker's retained message on this topic (an empty retained publish)."""
        try:
            self.c.publish(topic, b"", qos=0, retain=True).wait_for_publish(timeout=2)
        except (RuntimeError, ValueError) as e:
            log.warning("couldn't clear retained %s: %s", topic, e)

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


class PrintBus:
    """Stand-in for Bus with --dry-run: prints instead of publishing."""

    def __init__(self, *a, **k):
        self.handlers = {}

    def publish(self, topic, obj, retain=False):
        print(topic, json.dumps(obj, separators=(",", ":")), flush=True)

    def subscribe(self, topic, fn):
        self.handlers[topic] = fn

    def clear_retained(self, topic):
        pass

    def close(self):
        pass
