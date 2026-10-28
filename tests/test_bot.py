from types import SimpleNamespace

from hub.bot import build_router, reply_markup
from hub.service import HELP, Button, Reply


class FakeService:
    """Подмена Service: возвращает заданный ответ и помнит вызовы."""

    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []

    def __getattr__(self, name):
        async def method(*args):
            self.calls.append((name, args))
            return self.reply

        return method


def handler(router, name):
    for h in router.message.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(name)


def message(text, chat_type="private", user_id=7, name="Аня"):
    sent = []
    previews = []

    async def answer(text, reply_markup=None, link_preview_options=None):
        sent.append((text, reply_markup))
        previews.append(link_preview_options)

    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=user_id, full_name=name),
        answer=answer,
        sent=sent,
        previews=previews,
    )


def test_reply_markup_rows():
    reply = Reply("x", [[Button("A", "a"), Button("B", "b")], [Button("C", "c")]])
    markup = reply_markup(reply)
    rows = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
    assert rows == [[("A", "a"), ("B", "b")], [("C", "c")]]
    assert reply_markup(Reply("x")) is None


def test_reply_markup_style_only_when_set():
    reply = Reply("x", [[Button("A", "a"), Button("B", "b", "danger")]])
    a, b = reply_markup(reply).inline_keyboard[0]
    assert a.style is None
    assert b.style == "danger"
    assert "style" not in a.model_dump(exclude_none=True)


async def test_none_reply_sends_nothing():
    service = FakeService(None)
    msg = message("привет")
    await handler(build_router(service, "bot"), "on_text")(msg)
    assert service.calls == [("text", (7, "привет"))]
    assert msg.sent == []


async def test_reply_is_sent_with_markup():
    service = FakeService(Reply("ок", [[Button("A", "a")]]))
    msg = message("привет")
    await handler(build_router(service, "bot"), "on_text")(msg)
    assert msg.sent[0][0] == "ок"
    assert msg.sent[0][1].inline_keyboard[0][0].callback_data == "a"


async def test_start_passes_payload_and_name():
    service = FakeService(Reply("hi"))
    msg = message("/start abc")
    await handler(build_router(service, "bot"), "on_start")(msg, SimpleNamespace(args="abc"))
    assert service.calls == [("start", (7, "Аня", "abc"))]


async def test_invite_gets_bot_username():
    service = FakeService(None)
    await handler(build_router(service, "mybot"), "on_invite")(message("/invite"))
    assert service.calls == [("invite", (7, "mybot"))]


async def test_section_passes_name():
    service = FakeService(None)
    await handler(build_router(service, "b"), "on_section")(
        message("/section Дом"), SimpleNamespace(args="Дом")
    )
    await handler(build_router(service, "b"), "on_section")(
        message("/section"), SimpleNamespace(args=None)
    )
    assert service.calls == [("section_command", (7, "Дом")), ("section_command", (7, ""))]


async def test_help_goes_through_service():
    service = FakeService(Reply(HELP))
    msg = message("/help")
    await handler(build_router(service, "b"), "on_help")(msg)
    assert service.calls == [("help", (7,))]
    text = msg.sent[0][0]
    for cmd in ("/tasks", "/sections", "/section", "/brief", "/invite", "/help"):
        assert cmd in text


async def test_help_silent_for_stranger():
    msg = message("/help")
    await handler(build_router(FakeService(None), "b"), "on_help")(msg)
    assert msg.sent == []


async def test_callback_answers_and_sends_new_message():
    service = FakeService(Reply("готово"))
    answers = []
    sent = []

    async def answer(text=None):
        answers.append(text)

    async def msg_answer(text, reply_markup=None):
        sent.append(text)

    cb = SimpleNamespace(
        data="d:1",
        from_user=SimpleNamespace(id=7),
        answer=answer,
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), answer=msg_answer),
    )
    router = build_router(service, "b")
    await router.callback_query.handlers[0].callback(cb)
    assert answers == [None]
    assert sent == ["готово"]
    assert service.calls == [("callback", (7, "d:1"))]


def test_group_chat_ignored():
    """У каждого хендлера есть магический фильтр, пропускающий личный чат и режущий группу."""
    router = build_router(FakeService(), "b")
    private = message("x")
    group = message("x", chat_type="supergroup")
    cb = lambda m: SimpleNamespace(message=m)  # noqa: E731
    for handlers, wrap in (
        (router.message.handlers, lambda m: m),
        (router.callback_query.handlers, cb),
    ):
        for h in handlers:
            owners = [getattr(f.callback, "__self__", None) for f in h.filters]
            magic = [o for o in owners if hasattr(o, "resolve")]
            checks = [(m.resolve(wrap(private)), m.resolve(wrap(group))) for m in magic]
            assert (True, False) in [(bool(a), bool(b)) for a, b in checks], h.callback.__name__


def test_reply_markup_web_app_button():
    reply = Reply("x", [[Button("Дашборд", "", web_app="https://desk.example.com/settings")]])
    button = reply_markup(reply).inline_keyboard[0][0]
    assert button.web_app.url == "https://desk.example.com/settings"
    assert button.callback_data is None


async def test_web_and_settings_go_through_service_without_link_preview():
    service = FakeService(Reply("https://x/auth/link?t=1"))
    msg = message("/web")
    await handler(build_router(service, "b"), "on_web")(msg)
    await handler(build_router(service, "b"), "on_settings")(message("/settings"))
    assert service.calls == [("web_link", (7,)), ("settings_button", (7,))]
    assert msg.previews[0].is_disabled is True
