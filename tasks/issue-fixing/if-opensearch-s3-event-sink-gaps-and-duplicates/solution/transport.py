import requests
from . import config


def request(method, path, **kwargs):
    # Independent sessions keep the bulk worker and query worker independent.
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, config.OS_ENDPOINT + path, timeout=(1, 2), **kwargs)
