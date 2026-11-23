import hashlib


def handle(subscriber, event_id, body):
    print(f"APPLIED {subscriber} {event_id} {hashlib.sha256(body).hexdigest()}", flush=True)
