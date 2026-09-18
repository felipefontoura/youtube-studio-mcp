"""Regression tests for OAuth scope handling.

The production bug (2026-09-18): the channel's token was authorised WITHOUT
`youtube.force-ssl`, so `youtube_list_comments`, `youtube_post_comment`,
`youtube_reply_to_comment` and `youtube_list_captions` all failed with

    HTTP 403 "Request had insufficient authentication scopes"

while every other tool worked fine, and `youtube_auth_status` claimed all five
scopes were present. Two causes, both covered here:

1. `SCOPES` did not include `youtube.force-ssl` at all, so it was never
   requested at consent time. Per Google's discovery document,
   `commentThreads.list`/`comments.list` accept NO other scope.
2. `Credentials.from_authorized_user_info(info, SCOPES)` overwrites
   `.scopes` with the requested list, so `creds.scopes` reported what the code
   ASKED FOR rather than what was GRANTED — hiding the gap.
"""

import json

import pytest

from youtube_mcp.auth import (
    CAPTION_SCOPES,
    COMMENT_SCOPES,
    FORCE_SSL,
    SCOPES,
    TOKEN_JSON_ENV,
    AuthError,
    YouTubeAuth,
)

# What the real production token grants. `expiry` is far in the future because
# google-auth derives `Credentials.valid` from it — without it a fake token reads
# as expired and authenticate() would try to refresh.
PRODUCTION_TOKEN = {
    "token": "ya29.fake",
    "refresh_token": "1//fake",
    "client_id": "123.apps.googleusercontent.com",
    "client_secret": "fake-secret",
    "expiry": "2099-01-01T00:00:00Z",
    "scopes": [
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
        "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
    ],
}

# A token authorised after the fix.
FIXED_TOKEN = {**PRODUCTION_TOKEN, "scopes": [*PRODUCTION_TOKEN["scopes"], FORCE_SSL]}


def write_token(tmp_path, token):
    (tmp_path / "token.json").write_text(json.dumps(token))
    return YouTubeAuth(config_dir=tmp_path)


def test_force_ssl_is_requested_at_consent():
    """Cause 1: the scope must be in SCOPES, or the flow can never grant it."""
    assert FORCE_SSL in SCOPES


def test_comment_and_caption_scopes_include_force_ssl():
    assert FORCE_SSL in COMMENT_SCOPES
    assert FORCE_SSL in CAPTION_SCOPES


def test_granted_scopes_reads_the_token_not_the_request(tmp_path):
    """Cause 2: report what was granted, not what we asked for."""
    yt_auth = write_token(tmp_path, PRODUCTION_TOKEN)
    granted = yt_auth.granted_scopes()
    assert FORCE_SSL not in granted
    assert "https://www.googleapis.com/auth/youtube.readonly" in granted
    # The old implementation echoed SCOPES here, which is exactly what hid the bug.
    creds = yt_auth._load_token()
    assert FORCE_SSL in creds.scopes


def test_missing_scopes_detects_the_production_gap(tmp_path):
    yt_auth = write_token(tmp_path, PRODUCTION_TOKEN)
    assert yt_auth.missing_scopes([FORCE_SSL]) == [FORCE_SSL]


def test_status_reports_granted_and_missing(tmp_path):
    yt_auth = write_token(tmp_path, PRODUCTION_TOKEN)
    st = yt_auth.status()
    assert st["authenticated"] is True
    assert st["missing_scopes"] == [FORCE_SSL]
    assert FORCE_SSL not in st["scopes"]
    assert st["token_source"] == "file"
    assert st["has_refresh_token"] is True


def test_require_scopes_raises_actionable_error(tmp_path):
    yt_auth = write_token(tmp_path, PRODUCTION_TOKEN)
    with pytest.raises(AuthError) as e:
        yt_auth.require_scopes(COMMENT_SCOPES, "youtube_list_comments")
    msg = str(e.value)
    assert "youtube_list_comments" in msg
    assert FORCE_SSL in msg
    # Must say the rest of the server still works, and how to fix it.
    assert "Nothing else is affected" in msg
    assert TOKEN_JSON_ENV in msg


def test_no_false_alarm_when_token_lacks_scopes_key(tmp_path):
    """An older token without a `scopes` key must not be reported as missing."""
    token = {k: v for k, v in PRODUCTION_TOKEN.items() if k != "scopes"}
    yt_auth = write_token(tmp_path, token)
    assert yt_auth.granted_scopes() == []
    assert yt_auth.missing_scopes([FORCE_SSL]) == []
    yt_auth.require_scopes(COMMENT_SCOPES, "youtube_list_comments")  # no raise


def test_fixed_token_has_no_missing_scopes(tmp_path):
    yt_auth = write_token(tmp_path, FIXED_TOKEN)
    assert yt_auth.missing_scopes(SCOPES) == []
    yt_auth.require_scopes(COMMENT_SCOPES, "youtube_list_comments")  # no raise
    assert yt_auth.status()["missing_scopes"] == []


def test_token_can_be_provisioned_by_env(tmp_path, monkeypatch):
    """Lets a deployment be re-authorised without access to config_dir."""
    monkeypatch.setenv(TOKEN_JSON_ENV, json.dumps(FIXED_TOKEN))
    yt_auth = YouTubeAuth(config_dir=tmp_path)  # no token.json on disk at all
    assert yt_auth.token_available is True
    assert yt_auth.status()["token_source"] == "env"
    assert yt_auth.missing_scopes(SCOPES) == []
    assert FORCE_SSL in yt_auth.granted_scopes()


def test_env_token_takes_priority_over_file(tmp_path, monkeypatch):
    write_token(tmp_path, PRODUCTION_TOKEN)
    monkeypatch.setenv(TOKEN_JSON_ENV, json.dumps(FIXED_TOKEN))
    yt_auth = YouTubeAuth(config_dir=tmp_path)
    assert yt_auth.missing_scopes([FORCE_SSL]) == []


def test_broken_env_token_falls_back_to_file(tmp_path, monkeypatch):
    yt_auth = write_token(tmp_path, PRODUCTION_TOKEN)
    monkeypatch.setenv(TOKEN_JSON_ENV, "not json at all")
    assert yt_auth.token_available is True
    assert yt_auth.granted_scopes()  # read from the file
