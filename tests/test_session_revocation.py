"""
Session revocation and token lifetime — regression tests for the dashboard.

The bug these exist to prevent: `/api/revoke-devices` cleared only
`_device_sessions` and left every already-issued bearer token in `_tokens`.
The UI was told "revoked: N", the phone in the room kept working. These tests
drive the real HTTP surface, because the point of the fix is what a caller can
still DO after revoking — not what one dict looks like.

Also covered: bearer tokens now expire (they used to live until the process
died), and the state behind a dead token is actually freed rather than
ignored, which is what made a long-running server grow without bound.

Every test is offline and needs no display server.
"""
from __future__ import annotations

import time

import pytest


# ────────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────────

def _server():
    from dashboard import server as ds
    return ds.DashboardServer()


def _client(srv):
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _pair_phone(srv, pin: str = "PIN123", device: str = "device-token-abc"):
    """Put the server in the state `auto-login` / `device-login` leaves behind:
    a live bearer token plus a paired device that can re-mint one."""
    tok = srv._mint_token(pin, srv._TOKEN_TTL)
    _enc = srv._new_enc_key(tok)
    srv._device_sessions[device] = {
        "session_key": pin,
        "expires":     time.time() + srv._DEVICE_TTL,
    }
    return tok


# ────────────────────────────────────────────────────────────────────────────
# the reported bug — revoking must actually revoke
# ────────────────────────────────────────────────────────────────────────────

class TestRevokeActuallyRevokes:
    def test_revoked_phone_token_stops_authenticating(self):
        """The whole bug, at the HTTP layer: an authenticated call that worked
        before the revoke must be refused after it."""
        srv = _server()
        phone = _pair_phone(srv)

        with _client(srv) as tc:
            before = tc.post("/api/command", json={"text": "hello"},
                             headers={"Authorization": f"Bearer {phone}"})
            assert before.status_code == 200, "precondition: token was live"

            assert tc.post("/api/revoke-devices",
                           headers={"Authorization": f"Bearer {phone}"}
                           ).status_code == 200

            after = tc.post("/api/command", json={"text": "hello"},
                            headers={"Authorization": f"Bearer {phone}"})
            assert after.status_code == 401, (
                "revoke reported success but the old token still authenticated")

    def test_revoke_clears_device_relogin(self):
        """Revoking must also stop the paired device from minting a fresh
        token — otherwise the attacker just logs back in."""
        srv = _server()
        phone = _pair_phone(srv, device="dev-1")

        with _client(srv) as tc:
            tc.post("/api/revoke-devices",
                    headers={"Authorization": f"Bearer {phone}"})
            relogin = tc.post("/api/device-login",
                              json={"device_token": "dev-1"})
            assert relogin.status_code == 401

    def test_hud_token_survives_revoke(self):
        """The local desktop HUD is loopback-only and must not be blanked by a
        phone being revoked, or the workspace panels break mid-session."""
        srv = _server()
        hud = srv.ui_session()
        phone = _pair_phone(srv)

        with _client(srv) as tc:
            tc.post("/api/revoke-devices",
                    headers={"Authorization": f"Bearer {phone}"})
            still_ok = tc.get("/api/files",
                              headers={"Authorization": f"Bearer {hud}"})
            assert still_ok.status_code == 200

    def test_revoke_reports_what_it_really_did(self):
        srv = _server()
        phone = _pair_phone(srv)

        with _client(srv) as tc:
            body = tc.post("/api/revoke-devices",
                           headers={"Authorization": f"Bearer {phone}"}).json()
            assert body["revoked"] >= 1
            assert body["devices"] >= 1

    def test_revoke_requires_auth(self):
        srv = _server()
        _pair_phone(srv)
        with _client(srv) as tc:
            assert tc.post("/api/revoke-devices").status_code == 401


# ────────────────────────────────────────────────────────────────────────────
# token lifetime
# ────────────────────────────────────────────────────────────────────────────

class TestTokenExpiry:
    def test_expired_token_is_refused_at_auth(self):
        """Expiry is enforced on the auth path, not only when the sweep runs —
        otherwise a dead token keeps working until the next sweep fires."""
        srv = _server()
        tok = srv._mint_token("PIN", srv._TOKEN_TTL)
        srv._token_expiry[tok] = time.time() - 1

        with _client(srv) as tc:
            r = tc.post("/api/command", json={"text": "x"},
                        headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 401

    def test_fresh_token_is_accepted(self):
        srv = _server()
        tok = srv._mint_token("PIN", srv._TOKEN_TTL)
        with _client(srv) as tc:
            r = tc.post("/api/command", json={"text": "x"},
                        headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200

    def test_hud_token_never_expires(self):
        srv = _server()
        hud = srv.ui_session()
        assert srv._token_expiry[hud] == float("inf")
        srv._sweep_tokens(force=True)
        assert hud in srv._tokens

    def test_expired_device_cannot_relogin(self):
        """A device whose own clock ran out is refused even before a sweep, and
        fails closed if the record predates the `expires` field."""
        srv = _server()
        srv._device_sessions["old"] = {"session_key": "PIN",
                                       "expires": time.time() - 1}
        srv._device_sessions["legacy"] = {"session_key": "PIN"}   # no field

        with _client(srv) as tc:
            assert tc.post("/api/device-login",
                           json={"device_token": "old"}).status_code == 401
            assert tc.post("/api/device-login",
                           json={"device_token": "legacy"}).status_code == 401


# ────────────────────────────────────────────────────────────────────────────
# state is freed, not merely ignored
# ────────────────────────────────────────────────────────────────────────────

class TestNoUnboundedGrowth:
    def test_sweep_frees_every_trace_of_a_dead_token(self):
        srv = _server()
        tok = srv._mint_token("PIN", srv._TOKEN_TTL)
        srv._new_enc_key(tok)
        srv._token_expiry[tok] = time.time() - 1

        srv._sweep_tokens(force=True)

        assert tok not in srv._tokens
        assert tok not in srv._token_keys
        assert tok not in srv._token_enckey
        assert tok not in srv._token_expiry

    def test_many_logins_do_not_grow_state(self):
        """Before the fix, every login added to `_tokens` forever. 500 expired
        logins must leave nothing behind."""
        srv = _server()
        hud = srv.ui_session()
        for i in range(500):
            tok = srv._mint_token(f"PIN{i}", srv._TOKEN_TTL)
            srv._new_enc_key(tok)
            srv._token_expiry[tok] = time.time() - 1

        srv._sweep_tokens(force=True)

        assert srv._tokens == {hud}
        assert len(srv._token_keys) == 1
        # The HUD token never gets an `enc` key (it only ever sends the bearer
        # header, never an encrypted command), so all 500 must be gone.
        assert srv._token_enckey == {}

    def test_aes_cache_drops_keys_no_live_token_uses(self):
        srv = _server()
        for i in range(20):
            tok = srv._mint_token(f"PIN{i}", srv._TOKEN_TTL)
            srv._new_enc_key(tok)
            srv._token_expiry[tok] = time.time() - 1

        srv._sweep_tokens(force=True)
        assert srv._aes_cache == {}

    def test_sweep_is_throttled(self):
        """The sweep runs on the auth path, so it must not walk the dicts on
        every single request."""
        srv = _server()
        srv._sweep_tokens(force=True)
        first = srv._last_sweep
        srv._sweep_tokens()               # not forced — inside the window
        assert srv._last_sweep == first
