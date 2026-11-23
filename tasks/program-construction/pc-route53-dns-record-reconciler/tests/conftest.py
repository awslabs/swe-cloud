"""Fixtures: fresh Route 53 hosted zones and a candidate-owned project directory."""

import pytest
from dnstest import Project, Zone
from harness import workspace


@pytest.fixture
def zones():
    """Factory for fresh, uniquely named hosted zones; all are deleted at teardown."""
    created = []

    def make(prefix="t", name=None):
        zone = Zone(prefix, name)
        created.append(zone)
        return zone

    yield make
    for zone in created:
        try:
            zone.delete()
        except Exception as error:  # Cleanup must not mask the test result.
            print(f"cleanup of {zone.name} failed: {error}")


@pytest.fixture
def project():
    with workspace() as root:
        yield Project(root)
