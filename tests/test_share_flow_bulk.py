"""Bulk-share entry points on ShareFlow.

These cover the share-selected paths added for the load-tours and
recent-tours headers. The flow's heavyweight UI methods (vehicle dialog
etc.) are intentionally not exercised — we only assert that bulk-share
constructs the right payload and dispatches it to the sync client.
"""
from __future__ import annotations

import threading


class _FakeClient:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"ok": True, "tours_added": 2}
        self.calls: list[dict] = []

    def share_import(self, payload: dict) -> dict | None:
        self.calls.append(payload)
        return self.result


def _make_flow(client: _FakeClient | None, monkeypatch=None):
    # ShareFlow doesn't touch its parent or db on the share_tours path, so a
    # bare object suffices. ``share_import`` is the only client method called.
    # Stub _show_toast so the success/error paths don't reach the real Adw
    # toast machinery (which would need a live root widget).
    from drivepulse_app.share.flow import ShareFlow

    if monkeypatch is not None:
        monkeypatch.setattr(ShareFlow, "_show_toast", lambda self, msg: None)

    return ShareFlow(
        parent_widget=None,
        db=None,
        language="en",
        get_client_fn=lambda: client,
    )


def _wait_for_bg_threads():
    # The flow dispatches share_import in a daemon thread; join the most
    # recently started one to make assertions deterministic.
    for t in list(threading.enumerate()):
        if t.daemon and t.name not in ("MainThread",) and t is not threading.current_thread():
            t.join(timeout=2.0)


def test_share_tours_batches_into_single_payload(monkeypatch, drivepulse_module):
    """Bulk share must build ONE share_tours payload with all selected
    tours — not one request per tour. This is what makes the new
    select-mode share button meaningfully different from N per-row shares."""
    from drivepulse_app.share import flow as flow_mod

    monkeypatch.setattr(flow_mod.GLib, "idle_add", lambda cb, *args: cb(*args) or False)
    client = _FakeClient(result={"ok": True, "tours_added": 3})
    sf = _make_flow(client, monkeypatch)

    tours = [
        {"id": 1, "name": "A", "created_at": "2026-01-01T00:00:00+00:00", "waypoints_json": "[]"},
        {"id": 2, "name": "B", "created_at": "2026-01-02T00:00:00+00:00", "waypoints_json": "[]"},
        {"id": 3, "name": "C", "created_at": "2026-01-03T00:00:00+00:00", "waypoints_json": "[]"},
    ]
    sf.share_tours(tours)
    _wait_for_bg_threads()

    assert len(client.calls) == 1, "bulk share must use a single share_import call"
    payload = client.calls[0]
    assert payload["version"] == 1
    assert payload["type"] == "share_tours"
    assert [t["name"] for t in payload["tours"]] == ["A", "B", "C"]
    # build_tour_payload strips the local id — only protocol fields are sent.
    assert all("id" not in t for t in payload["tours"])


def test_share_tour_singular_delegates_to_share_tours(monkeypatch, drivepulse_module):
    """share_tour(t) is now a thin wrapper over share_tours([t]); the
    resulting payload should still match the old single-tour contract."""
    from drivepulse_app.share import flow as flow_mod

    monkeypatch.setattr(flow_mod.GLib, "idle_add", lambda cb, *args: cb(*args) or False)
    client = _FakeClient()
    sf = _make_flow(client, monkeypatch)

    sf.share_tour({"id": 7, "name": "Solo", "created_at": "2026-02-01T00:00:00+00:00", "waypoints_json": "[]"})
    _wait_for_bg_threads()

    assert len(client.calls) == 1
    assert client.calls[0]["type"] == "share_tours"
    assert len(client.calls[0]["tours"]) == 1
    assert client.calls[0]["tours"][0]["name"] == "Solo"


def test_share_tours_no_op_on_empty_list(monkeypatch, drivepulse_module):
    """An empty selection must not trigger any network/sync call — guards
    against accidental fires from a select-mode click with nothing checked."""
    client = _FakeClient()
    sf = _make_flow(client, monkeypatch)
    sf.share_tours([])
    _wait_for_bg_threads()
    assert client.calls == []


def test_share_tours_without_client_skips_send(monkeypatch, drivepulse_module):
    """When sync isn't configured, share_tours must show the no-sync toast
    and skip the import call — not raise."""
    from drivepulse_app.share.flow import ShareFlow

    toasts: list[str] = []
    monkeypatch.setattr(ShareFlow, "_show_toast", lambda self, msg: toasts.append(msg))
    sf = ShareFlow(parent_widget=None, db=None, language="en", get_client_fn=lambda: None)
    sf.share_tours([{"name": "X", "created_at": "2026-01-01", "waypoints_json": "[]"}])
    _wait_for_bg_threads()
    assert len(toasts) == 1
