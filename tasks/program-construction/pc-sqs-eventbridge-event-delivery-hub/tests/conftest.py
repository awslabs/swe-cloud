import pytest

from hub import Hub, Queues, eventbridge, provision_engines, uid


def pytest_collection_modifyitems(config, items):
    # Engine provisioning is session setup charged to the first test that needs it.
    for item in items:
        if "engines" in item.fixturenames:
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.failed:
        item.failed_phase = True
    return report


@pytest.fixture(scope="session")
def engines():
    engines, cleanup = provision_engines()
    try:
        yield engines
    finally:
        for step in reversed(cleanup):
            try:
                step()
            except Exception:
                pass


@pytest.fixture
def make_hub(engines, request):
    """Start servers with extra settings; each is stopped and cleaned up after the test."""
    hubs = []

    def make(start=True, **settings):
        hub = Hub(engines, **settings)
        hubs.append(hub)
        return hub.start() if start else hub

    yield make
    for hub in reversed(hubs):
        if getattr(request.node, "failed_phase", False):
            print(f"--- server {hub.id} log tail ---\n{hub.server_log()}")
        hub.close()


@pytest.fixture(scope="module")
def hub(engines):
    """The default server shared by a module: TOPICS user.created/updated/deleted."""
    server = Hub(engines)
    try:
        server.start()
        yield server
    finally:
        server.close()


@pytest.fixture
def queues():
    owned = Queues()
    yield owned
    owned.cleanup()


@pytest.fixture
def bus(queues):
    """An EventBridge bus whose events a rule copies into a test queue: (bus_name, target_queue_url)."""
    name = "bus-" + uid()
    rule = "rule-" + uid()
    target = queues.create("ebtarget")
    eventbridge.create_event_bus(Name=name)
    try:
        eventbridge.put_rule(Name=rule, EventBusName=name, EventPattern='{"source": [{"prefix": ""}]}')
        eventbridge.put_targets(Rule=rule, EventBusName=name, Targets=[{"Id": "q", "Arn": queues.arn(target)}])
        yield name, target
    finally:
        for step in (lambda: eventbridge.remove_targets(Rule=rule, EventBusName=name, Ids=["q"]),
                     lambda: eventbridge.delete_rule(Name=rule, EventBusName=name),
                     lambda: eventbridge.delete_event_bus(Name=name)):
            try:
                step()
            except Exception:
                pass
