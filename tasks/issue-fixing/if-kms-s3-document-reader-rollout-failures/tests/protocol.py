"""Verifier-owned document codec and AWS observations. No application imports."""
import base64
import json
import itertools
import hashlib
import os
import random
import threading
import time
import uuid
import boto3
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENDPOINT = os.environ.get('VERIFY_ENDPOINT', 'http://localhost:4566')
APP = os.environ.get('VERIFY_APP', '/app')


def aws(service):
    return boto3.client(service, endpoint_url=ENDPOINT, region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(connect_timeout=2, read_timeout=5, retries={'max_attempts': 0},
                                      s3={'addressing_style': 'path'}))


def b64(raw):
    return base64.b64encode(raw).decode()


def un64(raw):
    return base64.b64decode(raw)


def encode(doc):
    return json.dumps(doc, separators=(',', ':')).encode()


def seal(dk, text, key):
    nonce = os.urandom(12)
    return {'nonce': b64(nonce), 'ciphertext': b64(AESGCM(dk).encrypt(nonce, text.encode(), key.encode()))}


def plain(doc, dk, key):
    p = doc['payload']
    return AESGCM(dk).decrypt(un64(p['nonce']), un64(p['ciphertext']), key.encode()).decode()


def variant(ctx, pick):
    if not ctx:
        return {}
    value = ctx[sorted(ctx)[pick % len(ctx)]]
    return {k: value for k in ctx}


def contexts(ctx):
    result = [ctx]
    for p in range(len(ctx)):
        candidate = variant(ctx, p)
        if candidate not in result:
            result.append(candidate)
    return result


def multiply(a, b):
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        a = ((a << 1) ^ (0x11b if a & 128 else 0)) & 255
        b >>= 1
    return out


def inverse(a):
    out = 1
    for _ in range(254):
        out = multiply(out, a)
    return out


INVERSES = {i: inverse(i) for i in range(1, 256)}


def evaluate(parts, coordinate=0):
    if not parts or len({p[0] for p in parts}) != len(parts):
        raise ValueError('not distinct groups')
    out = bytearray(32)
    for part in parts:
        factor = 1
        for other in parts:
            if other[0] != part[0]:
                factor = multiply(factor, multiply(coordinate ^ other[0], INVERSES[part[0] ^ other[0]]))
        for i, ordinate in enumerate(part[1:]):
            out[i] ^= multiply(ordinate, factor)
    return bytes([coordinate]) + out


def sharing(secret, groups, needed):
    coefficients = [secret] + [os.urandom(32) for _ in range(needed - 1)]
    result = {}
    for g in groups:
        ordinates = bytearray(32)
        for i in range(32):
            for coefficient in coefficients[::-1]:
                ordinates[i] = multiply(ordinates[i], g) ^ coefficient[i]
        result[g] = bytes([g]) + ordinates
    return result


def group(entry):
    return entry.get('group', 1)


def threshold(doc):
    return doc.get('shamir_threshold', 1)


class Corpus:
    def __init__(self):
        self.s3, self.kms = aws('s3'), aws('kms')
        self.bucket = 'rollout-' + uuid.uuid4().hex[:18]
        outputs = deploy(aws('cloudformation'), 'rollout-seed-' + self.bucket,
                         Path(__file__).parent / 'cdk_seed/seed.template.json', BucketName=self.bucket)
        self.arns = [outputs[f'Key{i}Arn'] for i in range(2)]
        self.s3.put_object(Bucket=self.bucket, Key='control/keys.json', Body=encode({'arns': self.arns}))
        self.decrypt_only = set()
        self.lock = threading.RLock()
        self.cache = {}
        self.quorum_cache = {}
        self.payloads = {}
        self.baseline_usable = {}
        self.policies = {}
        self.expected, self.config, self.readable, self.due = {}, {}, {}, {}
        self.prior = {}
        salt = uuid.uuid4().hex[:7]
        self.keys = [f'docs/{n}-{salt}.json' for n in ('ledger', 'settlement', 'portal', 'gateway', 'staff', 'batch', 'defaults')]
        specs = [([(0, 1)], {'app': 'ledger-' + salt, 'env': 'prod', 'team': 'platform'}),
                 ([(0, 0), (1, 1)], {'app': 'settlement-' + salt, 'env': 'prod'}),
                 ([(1, 0)], {'app': 'portal-' + salt, 'env': 'staging'}),
                 ([(0, None)], {'app': 'gateway-' + salt, 'env': 'prod'}),
                 ([(1, 0)], {'app': 'staff-' + salt}),
                 ([(0, 0)], {'app': salt, 'env': salt}),
                 ([(1, 0)], {})]
        for extra in range(random.randrange(3)):
            self.keys.append(f'docs/service-{extra}-{salt}.json')
            specs.append(([(extra % 2, 0)], {'service': f'{extra}-{salt}'}))
        if random.choice([False, True]):
            specs[0][0].append((0, 2))
        for i, (wrappings, ctx) in enumerate(specs):
            wrappings = random.sample(wrappings, len(wrappings))
            key = self.keys[i]
            dk = os.urandom(32)
            text = f'deployment {salt}: document {i} token {uuid.uuid4().hex}'
            entries = []
            needed = (2, 2, 3)[i] if i < 3 else 1
            groups = random.sample(range(2, 80), needed + 1) if needed > 1 else [1]
            parts = sharing(dk, groups, needed)
            historical = sharing(os.urandom(32), groups, needed)
            refreshed = sharing(dk, groups, needed)
            for g in groups:
                for index, pick in wrappings:
                    # A recovered share-refresh inventory is not a rectangular
                    # cohort: its last group has only the refreshed polynomial.
                    if i == 0 and g == groups[-1]:
                        continue
                    arn = self.arns[index]
                    binding = ctx if pick is None else variant(ctx, pick)
                    value = parts[g] if needed > 1 else dk
                    blob = self.kms.encrypt(KeyId=arn, Plaintext=value, EncryptionContext=binding)['CiphertextBlob']
                    entry = {'arn': arn, 'context': ctx, 'enc': b64(blob), 'created_at': '2026-08-01T00:00:00Z'}
                    if needed > 1:
                        entry['group'] = g
                    entries.append(entry)
                if i == 0:
                    # A second current sharing serves a different legacy ordering.
                    alternate = {'group': g, 'arn': self.arns[0], 'context': ctx,
                                 'created_at': '2026-08-01T00:00:00Z'}
                    alternate['enc'] = b64(self.kms.encrypt(KeyId=alternate['arn'], Plaintext=refreshed[g],
                                                           EncryptionContext=variant(ctx, 0))['CiphertextBlob'])
                    entries.append(alternate)
            if needed > 1:
                # Imported direct-context entries from an older rotation come
                # first. KMS accepts them, but their quorum does not open payload.
                obsolete = []
                for g in groups:
                    e = dict(next(e for e in entries if group(e) == g))
                    e['enc'] = b64(self.kms.encrypt(KeyId=e['arn'], Plaintext=historical[g],
                                                   EncryptionContext=ctx)['CiphertextBlob'])
                    obsolete.append(e)
                # One damaged retained blob is not a reason to abandon a group.
                broken = dict(obsolete[0])
                broken['enc'] = b64(os.urandom(48))
                entries = [broken] + obsolete + entries
            # Include a correctly bound, old-stamped control: version alone is not evidence.
            version = '3.3.0' if i == 3 else '3.2.0'
            doc = {'sops_version': version, 'lastmodified': '2026-08-01T00:00:00Z',
                   'payload': seal(dk, text, key), 'kms': entries}
            if needed > 1:
                doc['shamir_threshold'] = needed
            self.config[key] = {(group(e), e['arn']): dict(e['context']) for e in entries}
            self.policies[key] = (needed, set(groups))
            self.payloads[key] = dict(doc['payload'])
            self.expected[key] = text
            self.prior[key] = {text}
            self.put(key, doc)
            self.readable[key] = self.picks(key, doc)
            self.due[key] = 0

        # Recovery import after key retirement: Decrypt is still allowed, but
        # the rollout principal cannot mint ciphertexts under this key. Three
        # restricted coordinates have pairwise-overlapping current cohorts and
        # no globally shared polynomial. A writable fourth group is incomplete.
        restricted = self.kms.create_key(Description='retired import wrapping key')['KeyMetadata']['Arn']
        self.decrypt_only.add(restricted)
        self.arns.append(restricted)
        self.archive_key = f'docs/archive-{salt}.json'
        self.keys.append(self.archive_key)
        key = self.archive_key
        groups = random.sample(range(82, 230), 5)
        early, groups = groups[0], groups[1:]
        dk = os.urandom(32)
        cohorts = [sharing(dk, [early] + groups, 2) for _ in range(3)]
        obsolete = sharing(os.urandom(32), groups, 2)
        context = {'service': 'archive-' + salt, 'stage': 'prod', 'team': 'records'}
        entries = []

        def wrap(g, arn, part, binding):
            return {'group': g, 'arn': arn, 'context': dict(context),
                    'created_at': '2026-08-01T00:00:00Z',
                    'enc': b64(self.kms.encrypt(KeyId=arn, Plaintext=part,
                                               EncryptionContext=binding)['CiphertextBlob'])}

        # This high-priority group was skipped by the two working old
        # orderings. Its uniform binding is damaged, while its other identity
        # has a current part under a third request ordering. A restored direct
        # uniform entry becomes visible to every old request ordering.
        uniform = {} if random.choice([False, True]) else {'service': 'archive-' + salt}
        dormant = wrap(early, self.arns[0], cohorts[0][early], uniform)
        dormant['context'] = uniform
        dormant['enc'] = b64(os.urandom(48))
        entries.append(dormant)
        entries.append(wrap(early, self.arns[1], cohorts[2][early], variant(context, 2)))

        for g, membership in zip(groups[:3], [(0, 1), (0, 2), (1, 2)]):
            # Obsolete direct wrappings are believable KMS successes, not usable
            # evidence about the current payload. Array positions carry no epoch.
            entries.append(wrap(g, restricted, obsolete[g], context))
            for cohort in random.sample(membership, len(membership)):
                entries.append(wrap(g, restricted, cohorts[cohort][g], context))
            for pick in (0, 1):
                entries.append(wrap(g, restricted, cohorts[pick][g], variant(context, pick)))
        for arn in self.arns[:2]:
            for pick in (0, 1):
                entries.append(wrap(groups[3], arn, cohorts[pick][groups[3]], variant(context, pick)))
        doc = {'sops_version': '3.3.0', 'lastmodified': '2026-08-01T00:00:00Z',
               'shamir_threshold': 2, 'kms': entries,
               'payload': seal(dk, 'archive ' + uuid.uuid4().hex, key)}
        self.config[key] = {(group(e), e['arn']): dict(e['context']) for e in entries}
        self.policies[key] = (2, set([early] + groups))
        self.payloads[key] = dict(doc['payload'])
        self.expected[key] = plain(doc, dk, key)
        self.prior[key] = {self.expected[key]}
        self.put(key, doc)
        self.readable[key] = self.picks(key, doc)
        self.due[key] = 0
        self.inventory = {'arns': self.arns, 'decrypt_only': sorted(self.decrypt_only)}
        self.s3.put_object(Bucket=self.bucket, Key='control/keys.json', Body=encode(self.inventory))

    def put(self, key, doc):
        if key not in self.baseline_usable:
            valid = self.valid_quorums(key, doc)
            indices = set().union(*(set().union(*q) for q in valid)) if valid else set()
            self.baseline_usable[key] = {(group(e), e['arn']) for i, e in enumerate(doc['kms']) if i in indices}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=encode(doc))

    def get(self, key):
        r = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(r['Body'].read())

    def unwrap(self, entry, context):
        ck = (entry['arn'], entry['enc'], json.dumps(context, sort_keys=True))
        if ck not in self.cache:
            try:
                self.cache[ck] = self.kms.decrypt(KeyId=entry['arn'], CiphertextBlob=un64(entry['enc']),
                                                 EncryptionContext=context)['Plaintext']
            except Exception:
                self.cache[ck] = None
        return self.cache[ck]

    def candidates(self, doc, direct=False, pick=None):
        by_group = {}
        for index, entry in enumerate(doc['kms']):
            options = [variant(entry['context'], pick)] if pick is not None else (
                [entry['context']] if direct else contexts(entry['context']))
            for ctx in options:
                part = self.unwrap(entry, ctx)
                if part is None:
                    continue
                if threshold(doc) == 1:
                    if len(part) != 32:
                        continue
                elif len(part) != 33 or part[0] != group(entry):
                    continue
                by_group.setdefault(group(entry), []).append((index, part))
        return by_group

    def valid_quorums(self, key, doc, direct=False):
        digest = hashlib.sha256(encode(doc)).digest()
        cache_key = key, digest, direct
        if cache_key in self.quorum_cache:
            return self.quorum_cache[cache_key]
        candidates = self.candidates(doc, direct=direct)
        valid = []
        # Deduplicate equal ordinates within a group before expensive arithmetic,
        # but retain every entry index that the ordinate makes usable.
        choices = {}
        for g, values in candidates.items():
            choices[g] = {}
            for index, part in values:
                choices[g].setdefault(part, set()).add(index)
        for groups in itertools.combinations(choices, threshold(doc)):
            for parts in itertools.product(*(choices[g] for g in groups)):
                dk = parts[0] if threshold(doc) == 1 else evaluate(parts)[1:]
                try:
                    if plain(doc, dk, key) == self.expected[key]:
                        valid.append(tuple(choices[g][part] for g, part in zip(groups, parts)))
                except Exception:
                    continue
        self.quorum_cache[cache_key] = valid
        return valid

    def old_key(self, key, doc, pick):
        found = {}
        for g in dict.fromkeys(group(e) for e in doc['kms']):
            for entry in (e for e in doc['kms'] if group(e) == g):
                part = self.unwrap(entry, variant(entry['context'], pick))
                if part is None:
                    continue
                if threshold(doc) > 1 and (len(part) != 33 or part[0] != g):
                    continue
                if threshold(doc) == 1:
                    try:
                        if plain(doc, part, key) == self.expected[key]:
                            return part
                    except Exception:
                        pass
                    continue
                found[g] = part
                break
        if threshold(doc) == 1:
            trials = list(found.values())
        elif len(found) >= threshold(doc):
            trials = [evaluate(list(found.values()))[1:]]
        else:
            trials = []
        for dk in trials:
            try:
                if plain(doc, dk, key) == self.expected[key]:
                    return dk
            except Exception:
                continue
        return None

    def old_read(self, key, doc, pick):
        return self.old_key(key, doc, pick) is not None

    def picks(self, key, doc):
        # Context lengths can differ: a request ordering is periodic at the LCM.
        import math
        count = math.lcm(*(max(1, len(ctx)) for ctx in self.config[key].values()))
        return {p for p in range(count) if self.old_read(key, doc, p)}

    def check(self, key, doc, legacy=True, fixed=False, final=False, published=True):
        identities = {(group(e), e['arn']) for e in doc['kms']}
        assert identities == set(self.config[key]), f'{key}: group/ARN set changed'
        needed, groups = self.policies[key]
        assert threshold(doc) == needed and {group(e) for e in doc['kms']} == groups, f'{key}: threshold policy changed'
        assert doc['payload'] == self.payloads[key], f'{key}: payload ciphertext changed'
        for e in doc['kms']:
            assert e['context'] == self.config[key][group(e), e['arn']], f'{key}: configured context changed'
        valid = self.valid_quorums(key, doc)
        usable = set().union(*(set().union(*q) for q in valid)) if valid else set()
        required = identities if published or fixed or final else self.baseline_usable[key]
        for identity in required:
            assert any(i in usable and (group(e), e['arn']) == identity for i, e in enumerate(doc['kms'])), f'{key}: no recoverable share for {identity}'
        if fixed or final:
            direct = self.valid_quorums(key, doc, direct=True)
            supported = set()
            direct_usable = set()
            for quorum in direct:
                direct_usable.update(set().union(*quorum))
                for indices in itertools.product(*quorum):
                    supported.add(frozenset((group(doc['kms'][i]), doc['kms'][i]['arn']) for i in indices))
            for chosen in itertools.combinations(groups, needed):
                arns = [[(g, arn) for g2, arn in identities if g2 == g] for g in chosen]
                for selection in itertools.product(*arns):
                    assert frozenset(selection) in supported, f'{key}: standard-context quorum missing for {selection}'
            if final:
                assert direct_usable == set(range(len(doc['kms']))), f'{key}: obsolete wrapping remains'
        if legacy:
            for p in self.readable[key]:
                assert self.old_read(key, doc, p), f'{key}: old read ordering {p} stopped working'

    def old_write(self, key, pick, rotate=False, write_pick=None):
        doc = self.get(key)
        dk = self.old_key(key, doc, pick)
        assert dk is not None, f'{key}: legacy writer cannot read current document'
        write_pick = pick if write_pick is None else write_pick
        if rotate:
            dk = os.urandom(32)
        else:
            self.expected[key] += ' / acknowledged edit ' + uuid.uuid4().hex
            self.prior[key].add(self.expected[key])
        result = {name: doc[name] for name in ('sops_version', 'lastmodified', 'payload', 'kms')}
        if 'shamir_threshold' in doc:
            result['shamir_threshold'] = doc['shamir_threshold']
        result['kms'] = [{name: e[name] for name in ('arn', 'context', 'enc', 'created_at', 'group') if name in e} for e in doc['kms']]
        if rotate:
            parts = sharing(dk, sorted({group(e) for e in result['kms']}), threshold(doc))
            for e in result['kms']:
                e['enc'] = b64(self.kms.encrypt(KeyId=e['arn'], Plaintext=dk if threshold(doc) == 1 else parts[group(e)],
                              EncryptionContext=variant(e['context'], write_pick))['CiphertextBlob'])
        result['payload'] = seal(dk, self.expected[key], key)
        result['lastmodified'] = '2026-10-23T00:00:00Z'
        self.put(key, result)
        self.payloads[key] = dict(result['payload'])
        self.readable[key] = self.picks(key, result)
        self.due[key] = time.monotonic() + 15
        return result
