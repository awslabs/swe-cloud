"""Discovery documents, signing keys and token signatures."""

import pytest

from harness import write_fixture
from support import TENANTS, b64url_int, bare_get, form_post, jwt_part, rs256_valid, wider_init

# A throwaway key pair provisioned as certificate "cert-acme" and used by acme's application.
ACME_CERT = """-----BEGIN CERTIFICATE-----
MIIDKTCCAhGgAwIBAgIUaxjWngmrVMDG0/KvMeGeVGqlI5cwDQYJKoZIhvcNAQEL
BQAwIzENMAsGA1UECgwEYWNtZTESMBAGA1UEAwwJY2VydC1hY21lMCAXDTI2MTEx
NzIxMTg1M1oYDzIxMjYxMDI0MjExODUzWjAjMQ0wCwYDVQQKDARhY21lMRIwEAYD
VQQDDAljZXJ0LWFjbWUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDj
m/8kNZ57EIbKWhHM+mjzRAIuDYmSFT+zAndUhQHxSILRKLdw+RoQhoZS2oTBawEl
CVhlFVuk2gmkkW0PFzNO8HfNC1kg34vwMMx0tC5aNGx1S6S/QqsT3lhS46aLxBK3
PrfK67oQqh6W9hM2rrZwVOyiJlPiU/3TDH1MWVz0P4SoNr0qaZctKav0YZexjT53
lJO+UNGoT+wMWp0Un9zOjX4ryzpYUs0L+4RUot6BteN02xZNJx+jfACAOobBA3Hb
aN4TTkGjvxK0mH8zukjX4Hy1B1hdg4l94z44DRohe7w5u/lGfZ7hToGow2OlRaEw
UdUfKRFSryFIUoQN64XpAgMBAAGjUzBRMB0GA1UdDgQWBBQ4Ni0KyUY/AXhxyrNZ
iAEWOEe2iTAfBgNVHSMEGDAWgBQ4Ni0KyUY/AXhxyrNZiAEWOEe2iTAPBgNVHRMB
Af8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQB+WiQW8zciIc70EtQ3QBG8HNvh
0kO4XSv23wYo6jqX99iWW2/f826ieHiFP8Wss0JZcQzPRSwxyLZ0hFws5Sb4/sl5
MGECspukO9Mm2qOvGjcCLCWHAWSXCjapUS5z2q2YKPse41sHB5ZWVPy17MrZn29Y
bJY7ouR2eIDyPDAwDsIFU6eSkfujl1LTrfcaYj2hj0hdjDy4s+co9ScjkzKPTMM+
TcVD/y7/mqpaHmcJ8h0mEUuPqr0hLcbzwEEDZKEy4stTOdvCMVQgPw/R7envqqeK
o+coyK4gUZxiOQetYhIX5L7T/JvL1cOEXRxnQwmYTVSgkPBDSRC33W7ochpi
-----END CERTIFICATE-----
"""
ACME_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEoQIBAAKCAQEA45v/JDWeexCGyloRzPpo80QCLg2JkhU/swJ3VIUB8UiC0Si3
cPkaEIaGUtqEwWsBJQlYZRVbpNoJpJFtDxczTvB3zQtZIN+L8DDMdLQuWjRsdUuk
v0KrE95YUuOmi8QStz63yuu6EKoelvYTNq62cFTsoiZT4lP90wx9TFlc9D+EqDa9
KmmXLSmr9GGXsY0+d5STvlDRqE/sDFqdFJ/czo1+K8s6WFLNC/uEVKLegbXjdNsW
TScfo3wAgDqGwQNx22jeE05Bo78StJh/M7pI1+B8tQdYXYOJfeM+OA0aIXu8Obv5
Rn2e4U6BqMNjpUWhMFHVHykRUq8hSFKEDeuF6QIDAQABAoIBAEjN5j/L0uSkdakW
RGlfk+B0WYonlGCg4V4+JLNpVCS8v/NtZbgCRh8RGLg544sG0jfplFfJDCZgBAOa
7NqN3kCa1IdG4PyN5PIwecW+uLDr4eFnD8yJ47UYQwJIhUZhsmbg7eM/oaWg9OXu
0liEbCDkWUjIn75ehWtTrGSWlRAfAq4zmZCNSJ4VV7X3Q0eWbR4feNmkVAy+MzaT
0fc9AC8/BwCm/yA5lCjAi4QJb+KRro8vurVn2WwSl3FQsYYrEPVxRllUrhq6Eedl
ofvaRNDF4757SUiPSdb9LBerlTLUTfON83FrHL6+qDWegS/mDl2zMdbwmMsAhUw9
Aey9Vn0CgYEA6kKqE1v7z8c1wDuoZNgY0n4pkh7xfArPotdrcYIdDrd5ECO4GkHR
OkooLNRRaEf+iEAUmpqdCT3DSsku5VR1IFvuxOK2t/TwFTcJTYqAs2Woa2+W/gL9
RA+8Bjxt0TYTnehxB6frJkOEtrLNi0UHf70JBvWcqkudEvErI0kgaWcCgYEA+LtS
tUJlUF0M/pj4+pcyMyIKQ2UbV4HZoSLGTV/YA+GHEq1zE+KreupP2h0Vidy9SiwK
TDQNkFwGaI+P+VDBFQF4g20/oW4ht/+fa0iTfhWImWfv+JxXFEYMgpNfVQ2hexgB
6oguSC9yr2tbks+/V6/P+Si5k++eowY+4lQu9C8CgYAHJTdo1cJtiQcDsQ4m3UyM
hxqfgWAeR3oj3mxBjxVx0HjZ3C0OZ/mIzKBDFcrR4nhKEaJPQO4PTRn62v4aMqRL
82HqEXw/SjJUg1Q+UD4yuqQTetjhMJgnDk/O5OL4oayhgWiJMj8C8MZIiFWqrNVY
uRxgYPeBXkMRQ9fsAJwDewKBgQCOjgX8Ht6kLYB6FsB0Ft3Nko/hSnxeLezGPoD6
7ZiHuOVKXiCbYAk5/Drh3lNu3RWcAhuwyxCPET8XBFjW4mKXIG2qnZ2fbthidV4w
BbuKwtq0WW6exTPfSbp3x/EWdSZ8vyix+3TTKGaJhXVJcknO/tOqkJ2z3NzFwJmf
6/GxPwJ/GMe+My5isNpSSYA0Rc8WBYaQIQT2MugxfrR1EeHHc1N3T79ou8YaIufU
xkE8jOLKeuNlVW7/8yX0hFTIx3iKP1hBhbuQJWhOBPsrqQc34/fgnZoHqhiOW1ur
NWURAdoOQKI4bj2TJw9NUZ1M/kLEZnDIIIHS3VR6VAf++3AAEg==
-----END RSA PRIVATE KEY-----
"""
ACME_MODULUS = int(
    "E39BFF24359E7B1086CA5A11CCFA68F344022E0D8992153FB30277548501F14882D128B770F91A10868652DA84C16B0125095865155BA4DA09A4916D0F17334EF077CD0B5920DF8BF030CC74B42E5A346C754BA4BF42AB13DE5852E3A68BC412B73EB7CAEBBA10AA1E96F61336AEB67054ECA22653E253FDD30C7D4C595CF43F84A836BD2A69972D29ABF46197B18D3E779493BE50D1A84FEC0C5A9D149FDCCE8D7E2BCB3A5852CD0BFB8454A2DE81B5E374DB164D271FA37C00803A86C10371DB68DE134E41A3BF12B4987F33BA48D7E07CB507585D83897DE33E380D1A217BBC39BBF9467D9EE14E81A8C363A545A13051D51F291152AF214852840DEB85E9",
    16,
)


ACME_CERT_RECORD = {"owner": "admin", "name": "cert-acme", "displayName": "Acme signing", "scope": "JWT", "type": "x509",
                    "cryptoAlgorithm": "RS256", "bitSize": 2048, "expireInYears": 20, "certificate": ACME_CERT, "privateKey": ACME_KEY}


@pytest.fixture
def signed(launch):
    """acme's application signs with the provisioned cert-acme; globex's application has no certificate of its own."""
    return launch(wider_init(certs=[ACME_CERT_RECORD], app_cert="cert-acme"))


def client_token(d, org):
    t = TENANTS[org]
    reply = form_post(d, "/api/login/oauth/access_token", {"grant_type": "client_credentials", "client_id": t["client_id"], "client_secret": t["secret"]})
    assert reply.code == 200, reply
    return reply.json["access_token"]


def keys(d, path="/.well-known/jwks"):
    reply = bare_get(d, path)
    assert reply.code == 200 and isinstance(reply.json, dict), reply
    return {k["kid"]: k for k in reply.json["keys"]}


def discovery(d, path="/.well-known/openid-configuration"):
    reply = bare_get(d, path)
    assert reply.code == 200 and isinstance(reply.json, dict), reply
    return reply.json


ENDPOINTS = {
    "authorization_endpoint": "/login/oauth/authorize",
    "token_endpoint": "/api/login/oauth/access_token",
    "userinfo_endpoint": "/api/userinfo",
    "introspection_endpoint": "/api/login/oauth/introspect",
    "revocation_endpoint": "/api/login/oauth/revoke",
    "end_session_endpoint": "/api/logout",
}


def test_discovery_document_names_the_endpoints(signed):
    doc = discovery(signed)
    origin = f"http://127.0.0.1:{signed.port}"
    assert doc["issuer"] == origin
    assert doc["jwks_uri"] == origin + "/.well-known/jwks"
    assert {k: doc[k] for k in ENDPOINTS} == {k: origin + v for k, v in ENDPOINTS.items()}


def test_discovery_advertises_supported_values(signed):
    doc = discovery(signed)
    assert {"password", "client_credentials", "refresh_token"} <= set(doc["grant_types_supported"])
    assert doc["scopes_supported"] == ["openid", "email", "profile", "address", "phone", "offline_access"]
    assert doc["token_endpoint_auth_methods_supported"] == ["client_secret_basic", "client_secret_post", "private_key_jwt", "none"]
    assert doc["subject_types_supported"] == ["public"]


def test_organization_discovery_points_at_organization_keys(signed):
    doc = discovery(signed, "/.well-known/acme/openid-configuration")
    origin = f"http://127.0.0.1:{signed.port}"
    assert (doc["issuer"], doc["jwks_uri"]) == (origin, origin + "/.well-known/acme/jwks")
    assert keys(signed, "/.well-known/acme/jwks").keys() == keys(signed).keys()


def test_jwks_publishes_provisioned_certificate(signed):
    key = keys(signed)["cert-acme"]
    assert (key["kty"], key["alg"], key["use"]) == ("RSA", "RS256", "sig")
    assert b64url_int(key["n"]) == ACME_MODULUS and b64url_int(key["e"]) == 65537
    body = "".join(line for line in ACME_CERT.strip().splitlines() if not line.startswith("-----"))
    assert key["x5c"] == [body]


def test_jwks_publishes_builtin_certificate(signed):
    key = keys(signed)["cert-built-in"]
    assert (key["kty"], key["alg"]) == ("RSA", "RS256")
    assert b64url_int(key["n"]).bit_length() == 4096
    assert signed.sql("SELECT owner, name, bit_size FROM cert WHERE name = 'cert-built-in'") == [("admin", "cert-built-in", 4096)]


def test_application_certificate_signs_its_tokens(signed):
    token = client_token(signed, "acme")
    assert jwt_part(token, 0) == {"alg": "RS256", "kid": "cert-acme", "typ": "JWT"}
    assert rs256_valid(token, ACME_MODULUS, 65537)


def test_application_without_certificate_signs_with_builtin_key(signed):
    token = client_token(signed, "globex")
    assert jwt_part(token, 0)["kid"] == "cert-built-in"
    key = keys(signed)["cert-built-in"]
    assert rs256_valid(token, b64url_int(key["n"]), b64url_int(key["e"]))
    assert not rs256_valid(token, ACME_MODULUS, 65537)


def test_issuer_follows_configured_origin(unstarted):
    d = unstarted
    d.write_files(wider_init(certs=[ACME_CERT_RECORD], app_cert="cert-acme"))
    write_fixture(d.root / "conf/app.conf", d.config_text().replace(f"origin = http://127.0.0.1:{d.port}", "origin = https://login.acme.test"),
                  mode=0o644)
    d.start("-createDatabase")
    doc = discovery(d)
    assert (doc["issuer"], doc["token_endpoint"]) == ("https://login.acme.test", "https://login.acme.test/api/login/oauth/access_token")
    assert jwt_part(client_token(d, "acme"), 1)["iss"] == "https://login.acme.test"


@pytest.mark.timeout(150)
def test_builtin_signing_key_survives_restart(signed):
    before = keys(signed)["cert-built-in"]["n"]
    token = client_token(signed, "globex")
    signed.stop()
    signed.start()
    key = keys(signed)["cert-built-in"]
    assert key["n"] == before
    assert rs256_valid(token, b64url_int(key["n"]), b64url_int(key["e"]))
