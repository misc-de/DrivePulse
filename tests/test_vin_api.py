from __future__ import annotations


def test_vin_is_url_encoded_in_nhtsa_request(monkeypatch):
    """A VIN containing URL-reserved characters must not break out of the
    path segment. Regression: previously a VIN like 'ABC?x=1' would inject
    a second query parameter into the NHTSA URL."""
    import urllib.request

    from drivepulse_app.vin import api as vin_api

    captured: list[str] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"Results": []}'

    def fake_urlopen(req, timeout=0):
        captured.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    vin_api._fetch_nhtsa("ABC?format=html&x=")

    assert len(captured) == 1
    url = captured[0]
    # Path segment ends at the query-string '?format=json' baked into the template.
    # The user-supplied '?' must be percent-encoded so it does not start a query.
    assert "%3F" in url or "%3f" in url
    assert "?format=json" in url
    # The leaked NHTSA endpoint should not be diverted to format=html.
    path_and_query = url.split("DecodeVin/", 1)[1]
    assert path_and_query.split("?", 1)[1] == "format=json"


def test_vin_is_url_encoded_in_autodev_request(monkeypatch):
    import urllib.request

    from drivepulse_app.vin import api as vin_api

    captured: list[str] = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{}'

    def fake_urlopen(req, timeout=0):
        captured.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    vin_api._fetch_autodev("ABC/../etc", "dummy-key")

    assert len(captured) == 1
    assert "%2F" in captured[0] or "%2f" in captured[0]
    assert "/etc" not in captured[0]
