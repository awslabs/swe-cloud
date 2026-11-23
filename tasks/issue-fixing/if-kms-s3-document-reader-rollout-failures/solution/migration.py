import copy
import itertools
import math
from codec import b64, un64, open_payload, now
from shares import group, threshold, combine, interpolate


def contexts(stored):
    yield stored
    for value in dict.fromkeys(stored.values()):
        variant = {name: value for name in stored}
        if variant != stored:
            yield variant


def inspect(kms, doc, cache=None):
    """Retain provenance, binding and alternate ordinates; KMS is not AEAD."""
    by_group = {}
    cache = {} if cache is None else cache
    for index, entry in enumerate(doc['kms']):
        choices = by_group.setdefault(group(entry), [])
        for context in contexts(entry['context']):
            identity = entry['arn'], entry['enc'], tuple(sorted(context.items()))
            if identity not in cache:
                try:
                    cache[identity] = kms.decrypt(KeyId=entry['arn'], CiphertextBlob=un64(entry['enc']),
                                                  EncryptionContext=context)['Plaintext']
                except Exception:
                    cache[identity] = None
            part = cache[identity]
            if part is None:
                continue
            if threshold(doc) == 1:
                if len(part) != 32:
                    continue
            elif len(part) != 33 or part[0] != group(entry):
                continue
            choices.append((index, context, part))
    return by_group


def quorums(doc, key, inventory):
    choices = {g: tuple(dict.fromkeys(c[2] for c in values)) for g, values in inventory.items()}
    authenticated = {}
    for groups in itertools.combinations(choices, threshold(doc)):
        for parts in itertools.product(*(choices[g] for g in groups)):
            data_key = parts[0] if threshold(doc) == 1 else combine(parts)
            if data_key not in authenticated:
                try:
                    open_payload(doc, data_key, key)
                    authenticated[data_key] = True
                except Exception:
                    authenticated[data_key] = False
            if authenticated[data_key]:
                yield data_key, parts


def recover(kms, doc, key):
    witness = next(quorums(doc, key, inspect(kms, doc)), None)
    if witness is None:
        raise ValueError('No authenticated key-group quorum')
    return witness[0]


def candidate(kms, doc, key, retired, decrypt_only=()):
    cache = {}
    inventory = inspect(kms, doc, cache)
    templates = {}
    for entry in doc['kms']:
        templates.setdefault((group(entry), entry['arn']), entry)
    groups = sorted({g for g, arn in templates})
    direct = {}
    for g, choices in inventory.items():
        for index, context, part in choices:
            entry = doc['kms'][index]
            if context == entry['context']:
                direct.setdefault((g, entry['arn']), {}).setdefault(part, index)

    # Deduplicate full polynomials, not just the payload key. Different current
    # share refreshes have different values that cannot all be re-encrypted by
    # this principal. A first successful basis is insufficient planning evidence.
    polynomials = {}
    witnesses = quorums(doc, key, inventory)
    if not any(arn in decrypt_only for g, arn in templates):
        witnesses = itertools.islice(witnesses, 1)
    for data_key, basis in witnesses:
        values = tuple(data_key if threshold(doc) == 1 else interpolate(basis, g) for g in groups)
        polynomials.setdefault(values, dict(zip(groups, values)))
    if not polynomials:
        raise ValueError('No authenticated key-group quorum')

    entries = copy.deepcopy(doc['kms'])
    needed = set()
    restricted = set(decrypt_only)
    for chosen_groups in itertools.combinations(groups, threshold(doc)):
        identities = [[pair for pair in templates if pair[0] == g] for g in chosen_groups]
        for selection in itertools.product(*identities):
            # Every survivor selection must admit a quorum. The chosen polynomial
            # may differ between selections: a globally common one need not exist.
            feasible = [p for p in polynomials.values() if all(
                arn not in restricted or p[g] in direct.get((g, arn), {})
                for g, arn in selection)]
            if not feasible:
                raise ValueError('No permitted completion for failover selection')
            wanted = min(feasible, key=lambda p: sum(
                p[g] not in direct.get((g, arn), {}) for g, arn in selection))
            for pair in selection:
                g, arn = pair
                available = direct.setdefault(pair, {})
                part = wanted[g]
                if part not in available:
                    # Plan and build in memory. A failure returns no partial repair.
                    template = templates[pair]
                    entry = copy.deepcopy(template)
                    entry['enc'] = b64(kms.encrypt(KeyId=arn, Plaintext=part,
                                                  EncryptionContext=entry['context'])['CiphertextBlob'])
                    entry['created_at'] = now()
                    check = kms.decrypt(KeyId=arn, CiphertextBlob=un64(entry['enc']),
                                        EncryptionContext=entry['context'])['Plaintext']
                    if check != part:
                        raise ValueError('KMS wrapping did not round trip')
                    available[part] = len(entries)
                    entries.append(entry)
                needed.add(available[part])
    result = copy.deepcopy(doc)
    if retired:
        result['kms'] = [e for i, e in enumerate(entries) if i in needed]
    else:
        result['kms'] = compatible(kms, doc, entries, key, inventory, cache, restricted)
    # Emit the flat wire representation as ordered group blocks.
    order = list(dict.fromkeys(group(e) for e in doc['kms']))
    result['kms'] = [e for g in order for e in result['kms'] if group(e) == g]
    result['sops_version'] = '3.3.0'
    return None if result == doc else result


def old_context(context, pick):
    if not context:
        return {}
    value = context[sorted(context)[pick % len(context)]]
    return {k: value for k in context}


def old_parts(doc, key, inventory, pick):
    """Reproduce grouped first-success selection, without a recovery fallback."""
    parts = {}
    by_index = {(i, tuple(sorted(ctx.items()))): part
                for choices in inventory.values() for i, ctx, part in choices}
    for g in dict.fromkeys(group(e) for e in doc['kms']):
        for i, entry in enumerate(doc['kms']):
            if group(entry) != g:
                continue
            part = by_index.get((i, tuple(sorted(old_context(entry['context'], pick).items()))))
            if part is not None:
                if threshold(doc) == 1:
                    try:
                        open_payload(doc, part, key)
                    except Exception:
                        continue
                parts[g] = part
                break
    return parts


def old_witness(doc, key, parts):
    if threshold(doc) == 1:
        trials = [(part,) for part in parts.values()]
    else:
        trials = [tuple(parts.values())] if len(parts) >= threshold(doc) else []
    for basis in trials:
        data_key = basis[0] if threshold(doc) == 1 else combine(basis)
        try:
            open_payload(doc, data_key, key)
            return data_key, basis
        except Exception:
            pass
    return None


def compatible(kms, source, entries, key, original, cache, restricted):
    """Complete direct coverage without activating incompatible old selections.

    Retaining an entry prefix does not retain the set of successful *groups*.
    In particular, a new uniform-context wrap can wake a previously skipped
    group. Compatibility additions go before direct additions
    within that group, and are validated through the complete deployed reader.
    """
    count = math.lcm(*(max(1, len(e['context'])) for e in source['kms']))
    baselines = {}
    for pick in range(count):
        parts = old_parts(source, key, original, pick)
        witness = old_witness(source, key, parts)
        if witness is not None:
            baselines[pick] = parts, witness
    result = copy.deepcopy(source)
    result['kms'] = entries
    inventory = inspect(kms, result, cache)
    guards = []
    for pick, (before, (data_key, basis)) in baselines.items():
        after = old_parts(result, key, inventory, pick)
        if old_witness(result, key, after) is not None:
            continue
        for g in after.keys() - before.keys():
            part = data_key if threshold(source) == 1 else interpolate(basis, g)
            if part == after[g]:
                continue
            for template in source['kms']:
                context = old_context(template['context'], pick)
                if (group(template) != g or template['arn'] in restricted
                        or context == template['context']):
                    continue
                entry = copy.deepcopy(template)
                entry['enc'] = b64(kms.encrypt(KeyId=entry['arn'], Plaintext=part,
                                               EncryptionContext=context)['CiphertextBlob'])
                entry['created_at'] = now()
                guards.append(entry)
                break
            else:
                raise ValueError('No compatible completion for newly readable group')
    # Preserve each original group's ordered alternatives; place compatibility
    # additions before any direct additions which could change group selection.
    result['kms'] = list(source['kms']) + guards + entries[len(source['kms']):]
    inventory = inspect(kms, result, cache)
    for pick in baselines:
        if old_witness(result, key, old_parts(result, key, inventory, pick)) is None:
            raise ValueError('Candidate would break an old grouped read')
    return result['kms']
