"""Tests for opt-in BBCSO host setup helpers."""

from aimusic.audio import plugin_host


def test_capture_plugin_state_can_open_from_an_adjacent_saved_patch(tmp_path, monkeypatch):
    plugin_path = tmp_path / "BBCSO.vst3"
    plugin_path.write_text("fixture")
    initial_state = tmp_path / "violas.state"
    initial_state.write_bytes(b"violas")
    output_state = tmp_path / "cellos.state"

    class FakePlugin:
        raw_state = b"default"

        def show_editor(self):
            assert self.raw_state == b"violas"
            self.raw_state = b"cellos"

    fake_plugin = FakePlugin()

    class FakePedalboard:
        @staticmethod
        def load_plugin(path, *, initialization_timeout):
            assert path == str(plugin_path)
            assert initialization_timeout == 60.0
            return fake_plugin

    monkeypatch.setattr(plugin_host, "_pedalboard_api", lambda: FakePedalboard)

    result = plugin_host.capture_plugin_state(
        plugin_path,
        output_state,
        initial_state_path=initial_state,
    )

    assert result == output_state
    assert output_state.read_bytes() == b"cellos"
