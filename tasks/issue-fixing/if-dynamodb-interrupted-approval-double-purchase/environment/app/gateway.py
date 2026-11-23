import hashlib
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TAG_KEY = 'cudly-idempotency-token'


def token_for(execution_id, index):
    return hashlib.sha256(f'{execution_id}:{index}'.encode()).hexdigest()


class Gateway:
    def __init__(self):
        self.url = os.environ.get('PROVIDER_URL', 'http://127.0.0.1:8080').rstrip('/')

    def request(self, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request(self.url + path, data=body, headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=120) as response:
            return json.load(response)

    def describe(self, kind, token):
        return self.request('/describe?' + urlencode(dict(provider_type=kind, tag_key=TAG_KEY, tag_value=token)))

    def purchase(self, rec, token):
        data = dict(provider_type=rec['provider_type'], offering_id=rec['offering_id'], count=int(rec['count']))
        if rec['provider_type'] == 'token':
            data.update(client_token=token, tags={TAG_KEY: token})
        return self.request('/purchase', data)['commitment_id']

    def tag(self, commitment_id, token):
        return self.request('/tag', dict(commitment_id=commitment_id, tags={TAG_KEY: token}))

    def buy(self, rec, token):
        if rec['provider_type'] != 'none':
            found = self.describe(rec['provider_type'], token)
            if found:
                return found[0]['commitment_id']
        commitment_id = self.purchase(rec, token)
        if rec['provider_type'] == 'tag_after':
            self.tag(commitment_id, token)
        return commitment_id
