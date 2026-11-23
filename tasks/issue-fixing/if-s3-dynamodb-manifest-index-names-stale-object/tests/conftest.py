import os
import sys
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import faultproxy  # noqa: E402
import harness  # noqa: E402


def _floci_live():
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:4566/_floci/health", timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


@pytest.fixture(scope="session")
def proxy():
    p = faultproxy.FaultProxy(3)
    p.start()
    yield p
    p.stop()


@pytest.fixture(scope="session")
def world(proxy):
    if not _floci_live():
        pytest.exit("environment: Floci is not answering on :4566", returncode=3)
    return harness.World(proxy.ports)


class Scenario:
    def __init__(self, world, proxy):
        self.world = world
        self.proxy = proxy
        self.roles = []
        self.problems = []

    def commit(self, slot, dataset, version, data, committer):
        role = self.world.commit(slot, dataset, version, data, committer)
        self.roles.append(role)
        return role

    def resolve(self, slot, dataset, version=None, legacy=False):
        role = self.world.resolve(slot, dataset, version, legacy)
        self.roles.append(role)
        return role

    def fail(self, message):
        self.problems.append(message)

    def extend(self, problems):
        self.problems.extend(problems)

    def report(self):
        if self.proxy.violations:
            self.problems.append(
                "requests used conditional-create or copy-source precondition headers, which "
                "the gateway rejects: " + "; ".join(r.describe() for r in self.proxy.violations)
            )
        if self.problems:
            details = "\n\n".join(self.problems)
            roles = "\n\n".join(role.describe() for role in self.roles)
            log = "\n".join(self.proxy.log()[-80:])
            raise AssertionError(f"{details}\n\n--- roles ---\n{roles}\n\n--- gateway log ---\n{log}")


@pytest.fixture
def scenario(world, proxy):
    proxy.reset()
    s = Scenario(world, proxy)
    yield s
    for role in s.roles:
        role.kill()
    proxy.reset()
