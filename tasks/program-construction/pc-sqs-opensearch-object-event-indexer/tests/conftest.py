import pytest
from harness import workspace
from pipeline import Pipeline, create_domain, delete_domain


def pytest_collection_modifyitems(items):
    """The first test that needs the OpenSearch domain also pays for provisioning it."""
    for item in items:
        if "opensearch" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break


@pytest.fixture(scope="session")
def opensearch():
    name, url = create_domain()
    try:
        yield url
    finally:
        delete_domain(name)


@pytest.fixture
def pipe(opensearch):
    with workspace() as root:
        pipeline = Pipeline(opensearch, root)
        try:
            yield pipeline
        finally:
            pipeline.close()
