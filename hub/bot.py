"""Тонкий адаптер aiogram над Service: только личные чаты, вся логика — в service.py."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)

from hub.service import Button, Reply, Service


def _button(b: Button) -> InlineKeyboardButton:
    extra = {"style": b.style} if b.style is not None else {}
    if b.web_app is not None:
        return InlineKeyboardButton(text=b.text, web_app=WebAppInfo(url=b.web_app), **extra)
    return InlineKeyboardButton(text=b.text, callback_data=b.data, **extra)


def reply_markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    rows = [[_button(b) for b in row] for row in reply.buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send(message: Message, reply: Reply | None) -> None:
    if reply is not None:
        # предпросмотр ссылки может «погасить» одноразовую ссылку входа
        await message.answer(
            reply.text,
            reply_markup=reply_markup(reply),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def build_router(service: Service, bot_username: str) -> Router:
    router = Router()
    private = F.chat.type == "private"

    @router.message(Command("start"), private)
    async def on_start(message: Message, command: CommandObject) -> None:
        user = message.from_user
        await _send(message, await service.start(user.id, user.full_name, command.args))

    @router.message(Command("invite"), private)
    async def on_invite(message: Message) -> None:
        await _send(message, await service.invite(message.from_user.id, bot_username))

    @router.message(Command("tasks"), private)
    async def on_tasks(message: Message) -> None:
        await _send(message, await service.tasks(message.from_user.id))

    @router.message(Command("sections"), private)
    async def on_sections(message: Message) -> None:
        await _send(message, await service.sections(message.from_user.id))

    @router.message(Command("section"), private)
    async def on_section(message: Message, command: CommandObject) -> None:
        name = command.args or ""
        await _send(message, await service.section_command(message.from_user.id, name))

    @router.message(Command("brief"), private)
    async def on_brief(message: Message) -> None:
        await _send(message, await service.brief(message.from_user.id))

    @router.message(Command("web"), private)
    async def on_web(message: Message) -> None:
        await _send(message, await service.web_link(message.from_user.id))

    @router.message(Command("settings"), private)
    async def on_settings(message: Message) -> None:
        await _send(message, await service.settings_button(message.from_user.id))

    @router.message(Command("help"), private)
    async def on_help(message: Message) -> None:
        await _send(message, await service.help(message.from_user.id))

    @router.message(F.text, private)
    async def on_text(message: Message) -> None:
        await _send(message, await service.text(message.from_user.id, message.text))

    @router.callback_query(F.message.chat.type == "private")
    async def on_callback(query: CallbackQuery) -> None:
        await query.answer()
        reply = await service.callback(query.from_user.id, query.data or "")
        if reply is not None:
            await query.message.answer(reply.text, reply_markup=reply_markup(reply))

    return router
