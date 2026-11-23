"""Config filters: an item matching any filter for its type is kept (state filtered)."""
import fnmatch
import re


class Filter:
    def __init__(self, spec):
        if isinstance(spec, str):
            spec = {"value": spec}
        self.property = spec.get("property")
        self.type = spec.get("type", "exact")
        self.value = str(spec["value"])
        self.invert = bool(spec.get("invert", False))

    def match(self, value):
        value = "" if value is None else str(value)
        if self.type == "exact":
            return value == self.value
        if self.type == "glob":
            return fnmatch.fnmatchcase(value, self.value)
        if self.type == "regex":
            return re.search(self.value, value) is not None
        if self.type == "contains":
            return self.value in value
        raise ValueError(f"unknown filter type {self.type}")


class Filters:
    def __init__(self, config):
        self._by_type = {t: [Filter(s) for s in specs] for t, specs in (config or {}).items()}

    def get(self, type_name):
        return self._by_type.get(type_name)

    def validate(self):
        for filters in self._by_type.values():
            for f in filters:
                f.match("")
