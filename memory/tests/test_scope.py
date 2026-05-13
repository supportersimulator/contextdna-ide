from memory.scope import normalize, CANONICAL, LEGACY_MAP


def test_canonical_passthrough():
    for s in CANONICAL:
        assert normalize(s) == s


def test_legacy_mapping():
    assert normalize('global') == 'core'
    assert normalize('project') == 'workspace'


def test_invalid():
    assert normalize('nope') is None
