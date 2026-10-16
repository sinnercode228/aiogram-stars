"""A Bot API that runs in the test process: it records every call and fails on request."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections import defaultdict
from collections.abc import AsyncGenerator
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerPreCheckoutQuery,
    CreateInvoiceLink,
    EditUserStarSubscription,
    GetMe,
    GetUpdates,
    RefundStarPayment,
    SendInvoice,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import Update

from tests.helpers import USER_ID

BOT_ID = 42
TOKEN = f"{BOT_ID}:TEST-token-for-the-fake-session"
Method = TypeVar("Method", bound=TelegramMethod[Any])


class FakeSession(BaseSession):
    """Answers Bot API methods locally, the way Telegram would, and records them.

    ``fail`` queues errors for the next calls of a method. Error replies go through aiogram's
    own ``check_response``, so the bot raises the exception classes it raises in production.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.delays: dict[type[TelegramMethod[Any]], float] = {}
        self._errors: defaultdict[type[TelegramMethod[Any]], list[Any]] = defaultdict(list)
        self._ids = itertools.count(1000)

    def calls(self, method: type[Method]) -> list[Method]:
        """The recorded calls of ``method``, oldest first."""
        return [request for request in self.requests if isinstance(request, method)]

    def fail(
        self,
        method: type[TelegramMethod[Any]],
        error: str | BaseException,
        *,
        status: int = 400,
        times: int = 1,
        retry_after: int | None = None,
    ) -> None:
        """Make the next ``times`` calls of ``method`` fail.

        ``error`` is either the description of a Bot API error reply with HTTP ``status``, or
        an exception to raise as the HTTP client would.
        """
        if isinstance(error, BaseException):
            self._errors[method].extend([error] * times)
            return
        reply: dict[str, Any] = {"ok": False, "error_code": status, "description": error}
        if retry_after is not None:
            reply["parameters"] = {"retry_after": retry_after}
        self._errors[method].extend([(status, reply)] * times)

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        self.requests.append(method)
        if delay := self.delays.get(type(method)):
            await asyncio.sleep(delay)
        if queued := self._errors.get(type(method)):
            error = queued.pop(0)
            if isinstance(error, BaseException):
                raise error
            status, reply = error
            self.check_response(bot, method, status, json.dumps(reply))
        result = await self._result(method)
        response = self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result}))
        return response.result  # type: ignore[return-value]

    async def _result(self, method: TelegramMethod[Any]) -> Any:
        if isinstance(
            method, (AnswerPreCheckoutQuery, RefundStarPayment, EditUserStarSubscription)
        ):
            return True
        if isinstance(method, CreateInvoiceLink):
            return f"https://t.me/$invoice-{next(self._ids)}"
        if isinstance(method, GetMe):
            return {"id": BOT_ID, "is_bot": True, "first_name": "Shop", "username": "shop_bot"}
        if isinstance(method, GetUpdates):
            try:
                return [await asyncio.wait_for(self.updates.get(), 0.05)]
            except asyncio.TimeoutError:
                return []
        if isinstance(method, (SendInvoice, SendMessage)):
            message: dict[str, Any] = {
                "message_id": next(self._ids),
                "date": 1_760_000_000,
                "chat": {"id": method.chat_id, "type": "private"},
            }
            if isinstance(method, SendMessage):
                message["text"] = method.text
            else:
                message["invoice"] = {
                    "title": method.title,
                    "description": method.description,
                    "start_parameter": method.start_parameter or "",
                    "currency": method.currency,
                    "total_amount": sum(price.amount for price in method.prices),
                }
            return message
        msg = f"the fake session does not know {type(method).__name__}"
        raise NotImplementedError(msg)

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        nothing: tuple[bytes, ...] = ()  # the tests download no files
        for chunk in nothing:  # pragma: no cover
            yield chunk


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(TOKEN, session=session), session


_update_ids = itertools.count(1)


def _user(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": "Ann", "language_code": "en"}


def pre_checkout_update(
    bot: Bot,
    payload: str,
    amount: int,
    *,
    user_id: int = USER_ID,
    currency: str = "XTR",
    query_id: str = "query-1",
) -> Update:
    """A real ``pre_checkout_query`` update, mounted on ``bot``."""
    raw = {
        "update_id": next(_update_ids),
        "pre_checkout_query": {
            "id": query_id,
            "from": _user(user_id),
            "currency": currency,
            "total_amount": amount,
            "invoice_payload": payload,
        },
    }
    return Update.model_validate(raw, context={"bot": bot})


def payment_update(
    bot: Bot,
    payload: str,
    amount: int,
    *,
    charge_id: str = "charge-1",
    user_id: int = USER_ID,
    currency: str = "XTR",
    update_id: int | None = None,
    **extra: Any,
) -> Update:
    """A real message update with ``successful_payment``, mounted on ``bot``.

    ``extra`` adds fields of ``SuccessfulPayment``, such as ``is_recurring``.
    """
    raw = {
        "update_id": next(_update_ids) if update_id is None else update_id,
        "message": {
            "message_id": next(_update_ids),
            "date": 1_760_000_000,
            "chat": {"id": user_id, "type": "private"},
            "from": _user(user_id),
            "successful_payment": {
                "currency": currency,
                "total_amount": amount,
                "invoice_payload": payload,
                "telegram_payment_charge_id": charge_id,
                "provider_payment_charge_id": "",
                **extra,
            },
        },
    }
    return Update.model_validate(raw, context={"bot": bot})


def message_update(bot: Bot, text: str, *, user_id: int = USER_ID) -> Update:
    """A real text message update from ``user_id``, mounted on ``bot``."""
    raw = {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_update_ids),
            "date": 1_760_000_000,
            "chat": {"id": user_id, "type": "private"},
            "from": _user(user_id),
            "text": text,
            "entities": (
                [{"type": "bot_command", "offset": 0, "length": len(text.split(maxsplit=1)[0])}]
                if text.startswith("/")
                else []
            ),
        },
    }
    return Update.model_validate(raw, context={"bot": bot})
