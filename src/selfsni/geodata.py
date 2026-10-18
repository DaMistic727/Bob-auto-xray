"""Minimal, dependency-free index reader for Xray protobuf geodata files."""
from pathlib import Path

from .core import InstallError, ROOT, require


REQUIRED = {
    'geosite.dat': {'category-ai-!cn', 'category-ip-geo-detect'},
    'geosite_RU.dat': {'category-ads-all', 'ru-available-only-inside'},
    'geoip.dat': {'private', 'ua'},
    'geoip_RU.dat': {'ru'},
}


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise InstallError('Truncated geodata varint')
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if byte < 0x80:
            return value, offset
    raise InstallError('Invalid geodata varint')


def _fields(data):
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise InstallError('Invalid geodata field')
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
        elif wire == 2:
            length, offset = _varint(data, offset)
            value = data[offset:offset + length]
            offset += length
            if len(value) != length:
                raise InstallError('Truncated geodata field')
        elif wire == 5:
            value = data[offset:offset + 4]
            offset += 4
        else:
            raise InstallError('Unsupported geodata wire type')
        if offset > len(data):
            raise InstallError('Truncated geodata field')
        yield number, wire, value


def categories(path):
    result = set()
    for number, wire, entry in _fields(Path(path).read_bytes()):
        if number != 1 or wire != 2:
            continue
        for inner_number, inner_wire, value in _fields(entry):
            if inner_number == 1 and inner_wire == 2:
                try:
                    result.add(value.decode('utf-8').lower())
                except UnicodeDecodeError as exc:
                    raise InstallError('Invalid geodata category name') from exc
                break
    require(result, 'Empty or unrecognized geodata file: ' + str(path))
    return result


def verify_server_geodata(root=None):
    root = Path(root or ROOT / 'vendor/routing')
    found = {}
    for name, expected in REQUIRED.items():
        found[name] = categories(root / name)
        missing = expected - found[name]
        require(not missing, f'{name} lacks required categories: {sorted(missing)}')
    return found
