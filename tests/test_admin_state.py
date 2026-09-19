from __future__ import annotations

import json
import os
import tempfile

import pytest

from app.state import State


def _tmp_state(tmp_path):
    return State(str(tmp_path / "admin.json"))


def test_fresh_state_needs_setup(tmp_path):
    st = _tmp_state(tmp_path)
    assert st.needs_setup is True


def test_setup_sets_password_and_flag(tmp_path):
    st = _tmp_state(tmp_path)
    st.setup("secret123")
    assert st.needs_setup is False
    assert st.verify("secret123") is True
    assert st.verify("wrong") is False


def test_setup_requires_min_length(tmp_path):
    st = _tmp_state(tmp_path)
    with pytest.raises(ValueError):
        st.setup("12345")
    assert st.needs_setup is True


def test_setup_twice_is_rejected(tmp_path):
    st = _tmp_state(tmp_path)
    st.setup("secret123")
    with pytest.raises(RuntimeError):
        st.setup("secret123")


def test_password_persists_across_instances(tmp_path):
    st = _tmp_state(tmp_path)
    st.setup("secret123")
    st2 = _tmp_state(tmp_path)  # 重新加载文件
    assert st2.needs_setup is False
    assert st2.verify("secret123") is True


def test_change_password(tmp_path):
    st = _tmp_state(tmp_path)
    st.setup("old12345")
    st.change_password("old12345", "new12345")
    assert st.verify("new12345") is True
    assert st.verify("old12345") is False


def test_change_password_with_wrong_old(tmp_path):
    st = _tmp_state(tmp_path)
    st.setup("old12345")
    with pytest.raises(ValueError):
        st.change_password("wrong", "new12345")


def test_file_permissions_600(tmp_path):
    st = _tmp_state(tmp_path)
    st.setup("secret123")
    mode = oct(os.stat(st.path).st_mode & 0o777)
    assert mode == oct(0o600)
