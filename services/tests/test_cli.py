from __future__ import annotations

import pytest

from marden import cli
from marden.core import sources


def test_sources_lists_declared_and_stored(capsys, committed):
    sources.declare_source(
        "test_fred", "FRED (test)", "api", "https://api.stlouisfed.org", 1440, "FRED API terms: free key, attribution"
    )
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "test_fred" in out and "declared" in out
    assert cli.main(["sources", "--sync"]) == 0
    assert committed.execute("SELECT name FROM source WHERE id = 'test_fred'").fetchone()["name"] == "FRED (test)"


def test_rebuild_unknown_view_and_dirty(capsys):
    assert cli.main(["rebuild", "no_such_view"]) == 2
    assert "unknown view" in capsys.readouterr().out
    assert cli.main(["rebuild", "dirty"]) == 0


def test_groups_appear_when_actions_register(capsys):
    calls = []

    @cli.action("collect", "test_source", help="test collector")
    def run(args):
        calls.append(args.action)
        return 0

    try:
        assert cli.main(["collect", "test_source"]) == 0
        assert calls == ["test_source"]
        with pytest.raises(SystemExit):
            cli.main(["seed", "anything"])  # no seed loaders registered: not a command
        with pytest.raises(ValueError):
            cli.command("collect", help="clash")(lambda args: 0)
    finally:
        cli._groups["collect"].pop("test_source", None)
