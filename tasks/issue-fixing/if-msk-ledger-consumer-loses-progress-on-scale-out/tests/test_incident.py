import asyncio
from collections import Counter
import pytest
from driver import run


@pytest.fixture(scope='module')
def observed():
    return asyncio.run(run())


def responses(o, api, worker=None):
    return [e for e in o['wire'] if e['kind'] == 'response' and e['api'] == api
            and (worker is None or e['worker'] == worker)]


def test_broker_schedule(observed):
    o = observed
    s = o['status']
    assert s['warm'], 'single-member delivery did not catch up'
    assert not s['gateway_errors'], s['gateway_errors']
    assert len(s['revoked']) == 3 and len(s['retained']) == 3, s
    replies = [e for e in o['wire'] if e['kind'] == 'marker_reply']
    assert {e['partition'] for e in replies if e['error'] == 27} == set(s['revoked']), 'owned progress never reached the broker during the first round'
    held = [e for e in o['wire'] if e['kind'] == 'held_commit']
    assert held, 'retained partition stopped serving during rejoin'
    c = held[0]
    assert any(e['corr'] == c['corr'] and e['worker'] == 'W1' and
               any(p['error'] == 22 for p in e['parts'].values()) for e in responses(o, 8)), 'stale request did not receive the broker generation rejection'


def test_handover_progress(observed):
    o = observed
    boundaries = [e for e in o['wire'] if e['kind'] == 'rejoin_boundary']
    assert boundaries, 'cooperative handover did not finish'
    boundary = boundaries[0]['ts']
    assert o['status']['revoked'], 'no partition handover observed'
    for p in o['status']['revoked']:
        key = f"{o['topic']}:{p}"
        applied = [e['offset'] for e in o['effects'] if e['worker_id'] == 'W1' and e['partition'] == p and e['ts'] < boundary]
        accepted = [e['parts'][key]['offset'] for e in responses(o, 8, 'W1')
                    if e['ts'] < boundary and key in e['parts'] and e['parts'][key]['error'] == 0]
        assert applied and accepted and max(accepted) == max(applied)+1, (p, applied, accepted)
        fetches = [e['parts'][key]['offset'] for e in responses(o, 9, 'W2') if key in e['parts']]
        assert fetches and fetches[0] == max(applied)+1, (p, fetches, applied)
        first = [e['offset'] for e in o['effects'] if e['worker_id'] == 'W2' and e['partition'] == p]
        assert first and min(first) == max(applied)+1, (p, first, applied)


def test_retained_progress_and_continuity(observed):
    o = observed
    markers = [e for e in o['wire'] if e['kind'] == 'produced_retained']
    assert markers, 'second round never started'
    m = markers[0]
    applies = [e for e in o['effects'] if e['partition'] == m['partition'] and e['offset'] == m['offset'] and e['worker_id'] == 'W1']
    assert len(applies) == 1 and applies[0]['ts']-m['ts'] < 5, 'owned record was not promptly applied'
    held = [e for e in o['wire'] if e['kind'] == 'held_commit']
    assert held, 'missing manual commit while retained ownership was active'
    key = f"{o['topic']}:{m['partition']}"
    assert held[0]['offsets'].get(key, -1) >= m['offset']+1
    syncs = [e for e in responses(o, 14, 'W1') if e.get('gen') == o['status']['baseline_generation']+2 and e['error'] == 0]
    assert syncs, 'second round did not finish'
    assert any(e['ts'] <= syncs[0]['ts']+5 and e['ts'] >= syncs[0]['ts'] and
               key in e['parts'] and e['parts'][key]['error'] == 0 and e['parts'][key]['offset'] >= m['offset']+1
               for e in responses(o, 8, 'W1')), 'idle retained progress was abandoned'


def test_membership_stays_healthy(observed):
    o, s = observed, observed['status']
    assert s['alive'] == {'W1': True, 'W2': True}, s['alive']
    desc = s['describe']
    assert desc['state'] == 'Stable' and desc['protocol'] == 'cooperative-sticky', desc
    assert {m['member'] for m in desc['members']} == set(s['members'].values()) and len(desc['members']) == 2, desc
    assignments = [set(m['assignment'].get(o['topic'], [])) for m in desc['members']]
    assert assignments[0].isdisjoint(assignments[1]) and assignments[0] | assignments[1] == set(range(6))
    gens = {e['gen'] for e in responses(o, 11) if e['error'] == 0}
    assert gens == {s['baseline_generation']+i for i in range(3)}, gens
    for api in (12, 14):
        assert not [e for e in responses(o, api) if e.get('error') in (22, 25)]
    assert not [e for e in o['wire'] if e['kind'] == 'request' and e['api'] in (12, 14) and e.get('gen') == -1]
    settled = [e['ts'] for e in o['wire'] if e['kind'] == 'settled']
    assert settled and s['observed_until']-settled[0] >= 14
    assert not [e for e in o['wire'] if e['kind'] == 'request' and e['api'] in (11, 13) and e['ts'] > settled[0]]
    joins = [e for e in o['wire'] if e['kind'] == 'request' and e['api'] == 11]
    assert all(e['protocol_type'] == 'consumer' and list(e['protocols']) == ['cooperative-sticky'] and not e['instance'] for e in joins)


def test_effects_and_durable_offsets(observed):
    o = observed
    expected = Counter((e['partition'], e['offset']) for e in o['produced'])
    actual = Counter((e['partition'], e['offset']) for e in o['effects'])
    assert actual == expected, (expected, actual)
    for p in range(6):
        end = max(e['offset'] for e in o['produced'] if e['partition'] == p)+1
        assert o['status']['log_ends'].get(str(p)) == end
        assert o['status']['offsets'].get(str(p)) == end, (p, o['status']['offsets'], end)


def test_processing_commit_order(observed):
    o = observed
    requests = [e for e in o['wire'] if e['kind'] == 'request' and e['api'] == 8]
    # Broker offsets cover all earlier deliveries, not just the maximum completed
    # HTTP call. Observe the prefix at every request, including rejected attempts.
    for req in requests:
        for key, offset in req['offsets'].items():
            p = int(key.rsplit(':', 1)[1])
            required = {e['offset'] for e in o['produced']
                        if e['partition'] == p and e['offset'] < offset}
            applied = {e['offset'] for e in o['effects']
                       if e['partition'] == p and e['ts'] <= req['ts']}
            assert required <= applied, ('commit skipped unfinished accounting', req, required-applied)
            assert offset <= o['status']['log_ends'][str(p)], req
    # The attempt budget begins when the delivery prefix becomes complete.
    for effect in o['effects']:
        p, offset = effect['partition'], effect['offset']
        key = f"{o['topic']}:{p}"
        prefix = [e['ts'] for e in o['effects'] if e['partition'] == p and e['offset'] <= offset]
        ready = max(prefix)
        attempts = [e for e in requests if e['worker'] == effect['worker_id'] and
                    e['offsets'].get(key, -1) >= offset+1 and e['ts'] >= ready]
        assert attempts and min(e['ts'] for e in attempts)-ready < 1, (effect, ready)


def test_ownership_and_service(observed):
    o = observed
    boundary = [e['ts'] for e in o['wire'] if e['kind'] == 'rejoin_boundary']
    assert boundary, 'no ownership transition observed'
    for e in o['effects']:
        assert e['member_id'] == o['status']['members'].get(e['worker_id']), e
        syncs = [s for s in responses(o, 14, e['worker_id']) if s.get('error') == 0 and
                 e['partition'] in s.get('assignment', {}).get(o['topic'], []) and s['ts'] <= e['ts']]
        assert syncs, ('application before ownership', e)
        if e['worker_id'] == 'W1' and e['partition'] in o['status']['revoked']:
            assert e['ts'] < boundary[0], ('application after relinquishing ownership', e)
    for marker in [e for e in o['wire'] if e['kind'] in ('produced_marker', 'produced_handover', 'produced_retained', 'produced_tail', 'produced_burst')]:
        owners = {'W2'} if marker['kind'] == 'produced_tail' else {'W1'}
        if marker['kind'] == 'produced_handover':
            # Undispatched backlog can legitimately move to the new owner.
            owners = {'W1', 'W2'}
        matches = [e for e in o['effects'] if e['partition'] == marker['partition'] and
                   e['offset'] == marker['offset'] and e['worker_id'] in owners]
        # tail and burst records arrive 18 at a time behind 1.5 s calls; allow grader slack
        bound = 10 if marker['kind'] in ('produced_tail', 'produced_burst') else 5
        assert matches and matches[0]['ts']-marker['ts'] < bound, marker


def test_dispatched_calls_drained_before_handover(observed):
    o = observed
    boundary = [e['ts'] for e in o['wire'] if e['kind'] == 'rejoin_boundary']
    assert boundary, 'no completed cooperative release'
    for call in o['calls']:
        if call['worker_id'] != 'W1' or call['partition'] not in o['status']['revoked']:
            continue
        applied = [e for e in o['effects'] if all(e[k] == call[k]
                   for k in ('worker_id', 'partition', 'offset'))]
        assert len(applied) == 1 and applied[0]['ts'] < boundary[0], call
