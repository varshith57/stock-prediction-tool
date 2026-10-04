import pytest

from stockapp import cli


def test_config_command_prints_config(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["config"]) == 0
    assert '"certainty_bar": 0.9' in capsys.readouterr().out


def test_telegram_test_fails_cleanly_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def fail(_text: str) -> None:
        raise cli.AlertError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set")

    monkeypatch.setattr(cli, "send_message", fail)
    assert cli.main(["telegram-test"]) == 1
    assert "FAILED" in capsys.readouterr().err
