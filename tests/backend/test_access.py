from aiogram import types

from app.access import ACCESS_DENIED_MESSAGE, AllowlistMiddleware


def _message(username):
    user = types.User.model_construct(id=1, is_bot=False, first_name="T", username=username)
    return types.Message.model_construct(message_id=1, from_user=user, text="hi")


async def test_owner_reaches_handler():
    handled = []

    async def handler(event, data):
        handled.append(event)
        return "ok"

    result = await AllowlistMiddleware()(handler, _message("galilei"), {})

    assert result == "ok"
    assert len(handled) == 1


async def test_stranger_is_turned_away(monkeypatch):
    sent = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(types.Message, "answer", fake_answer)

    handled = []

    async def handler(event, data):
        handled.append(event)

    result = await AllowlistMiddleware()(handler, _message("stranger"), {})

    assert result is None
    assert handled == []  # the handler never runs for a blocked user
    assert sent == [ACCESS_DENIED_MESSAGE]
