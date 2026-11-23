from seam import seam
from transport import upload, notify


def publish(config, event_id, filename):
    envelope = upload(config, event_id, filename)
    seam('after_upload', subscriber='publisher', event_id=event_id)
    notify(config, envelope)
    seam('after_notify', subscriber='publisher', event_id=event_id)
