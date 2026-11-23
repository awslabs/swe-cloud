"""Resource type registry. Types are scanned, and so queued and processed, in registration order."""


class Registration:
    def __init__(self, name, lister, depends_on=()):
        self.name = name
        self.lister = lister
        # Only honoured with the wait_on_dependencies feature flag (experimental, as in libnuke).
        self.depends_on = list(depends_on)


_registrations = []


def register(registration):
    if any(r.name == registration.name for r in _registrations):
        raise ValueError(f"resource type {registration.name} already registered")
    _registrations.append(registration)


def get(name):
    for registration in _registrations:
        if registration.name == name:
            return registration
    raise KeyError(name)


def ordered():
    return list(_registrations)
