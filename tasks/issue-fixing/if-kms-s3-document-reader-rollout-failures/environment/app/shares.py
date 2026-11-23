"""Bytewise Shamir arithmetic over GF(256), modulus x^8+x^4+x^3+x+1.

A share is its one-byte, nonzero coordinate followed by 32 ordinate bytes.
This module does arithmetic only; successful interpolation is not authentication.
"""
import os


def mul(a, b):
    value = 0
    while b:
        if b & 1:
            value ^= a
        a <<= 1
        if a & 256:
            a ^= 0x11b
        b >>= 1
    return value


def power(a, n):
    value = 1
    while n:
        if n & 1:
            value = mul(value, a)
        a = mul(a, a)
        n >>= 1
    return value


def interpolate(parts, x=0):
    """Evaluate the unique polynomial through distinct share coordinates at x."""
    if not parts or any(len(p) != 33 or not p[0] for p in parts):
        raise ValueError('invalid share')
    if len({p[0] for p in parts}) != len(parts):
        raise ValueError('duplicate share coordinate')
    result = bytearray(32)
    for i, part in enumerate(parts):
        weight = 1
        for j, other in enumerate(parts):
            if i != j:
                weight = mul(weight, mul(x ^ other[0], power(part[0] ^ other[0], 254)))
        for j, value in enumerate(part[1:]):
            result[j] ^= mul(value, weight)
    return bytes([x]) + result


def combine(parts):
    return interpolate(parts)[1:]


def split(secret, coordinates, threshold):
    if len(secret) != 32 or not 1 <= threshold <= len(coordinates):
        raise ValueError('invalid split')
    if len(set(coordinates)) != len(coordinates) or any(not 1 <= x <= 255 for x in coordinates):
        raise ValueError('invalid coordinates')
    coefficients = [secret] + [os.urandom(32) for _ in range(threshold - 1)]
    result = {}
    for x in coordinates:
        values = bytearray(32)
        for j in range(32):
            for coefficient in reversed(coefficients):
                values[j] = mul(values[j], x) ^ coefficient[j]
        result[x] = bytes([x]) + values
    return result


def group(entry):
    return entry.get('group', 1)


def threshold(doc):
    return doc.get('shamir_threshold', 1)


def material(doc, entry, data_key, parts):
    return data_key if threshold(doc) == 1 else parts[group(entry)]
