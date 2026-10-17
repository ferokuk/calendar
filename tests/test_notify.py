"""Уведомления админу."""

import pytest

import config
import notify


@pytest.fixture
def sent(paths, monkeypatch):
    messages = []
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(notify, "send", lambda text: messages.append(text) or True)
    return messages


def test_one_off_alert_is_deduplicated_and_never_recovers(sent):
    notify.alert("Ошибка при обработке", "BadRequest: boom", key="bot.handler", sticky=False)
    notify.alert("Ошибка при обработке", "BadRequest: boom", key="bot.handler", sticky=False)

    assert len(sent) == 1
    assert notify.recovered("bot.handler", "Снова работает") is False
    assert len(sent) == 1
