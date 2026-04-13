"""Tests for playbook grow+refine."""



def test_playbook_append_creates_staging(tmp_vault):
    from models import PLAYBOOK_STAGING_DIR
    from playbook import _playbook_append
    _playbook_append("myproject", {"title": "First lesson", "essence": "body content here"})
    staging = PLAYBOOK_STAGING_DIR / "myproject.jsonl"
    assert staging.exists()
    assert "First lesson" in staging.read_text()


def test_playbook_append_skips_empty(tmp_vault):
    from models import PLAYBOOK_STAGING_DIR
    from playbook import _playbook_append
    _playbook_append("myproject", {"title": "Empty", "essence": ""})
    staging = PLAYBOOK_STAGING_DIR / "myproject.jsonl"
    # Should not create file for empty essence
    assert not staging.exists() or staging.read_text() == ""
