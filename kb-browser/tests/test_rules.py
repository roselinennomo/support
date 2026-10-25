from app.rules import clean_hits, host_allowed, is_login_url

PATTERNS = ["login", "sso", "mfa", "auth/"]


def test_login_detection_by_url():
    assert is_login_url("https://sso.vendor.com/oauth/authorize?x=1", PATTERNS)
    assert is_login_url("https://kb.vendor.com/Login?next=/kb", PATTERNS)
    assert not is_login_url("https://kb.vendor.com/kb/search?q=os6860", PATTERNS)
    assert not is_login_url("", PATTERNS)


def test_only_authorised_http_hosts_are_navigable():
    allowed = ["kb.vendor.com"]
    assert host_allowed("https://kb.vendor.com/a", allowed)
    assert host_allowed("https://docs.kb.vendor.com/a", allowed)
    assert not host_allowed("https://evilkb.vendor.com.attacker.io/a", allowed)
    assert not host_allowed("http://169.254.169.254/latest/meta-data", allowed)
    assert not host_allowed("file:///etc/passwd", allowed)
    assert not host_allowed("javascript:alert(1)", allowed)


def test_hits_are_deduplicated_limited_and_restricted_to_the_kb_host():
    raw = [
        {"title": "  Stacking  \n lien ", "url": "https://kb.vendor.com/a#top"},
        {"title": "Doublon", "url": "https://kb.vendor.com/a"},
        {"title": "Externe", "url": "https://ads.example.com/x"},
        {"title": "Vide", "url": ""},
        {"title": "B", "url": "https://kb.vendor.com/b"},
        {"title": "C", "url": "https://kb.vendor.com/c"},
    ]

    hits = clean_hits(raw, 2, ["kb.vendor.com"])

    assert hits == [
        {"title": "Stacking lien", "url": "https://kb.vendor.com/a", "meta": ""},
        {"title": "B", "url": "https://kb.vendor.com/b", "meta": ""},
    ]
