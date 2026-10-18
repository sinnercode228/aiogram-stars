# aiogram-stars

[English](https://github.com/sinnercode228/aiogram-stars/blob/main/README.md) · Русский

[![CI](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml/badge.svg)](https://github.com/sinnercode228/aiogram-stars/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)
[![Python versions](https://img.shields.io/pypi/pyversions/aiogram-stars.svg)](https://pypi.org/project/aiogram-stars/)

Оплата в Telegram Stars для ботов на aiogram 3. От вас две async-функции: `check` решает,
принимать ли платёж, `on_paid` выдаёт товар. Библиотека отвечает на pre-checkout раньше, чем
истекут десять секунд, которые на это даёт Telegram, вызывает `on_paid` один раз на каждый
`telegram_payment_charge_id` и держит возвраты в вашей базе, пока Telegram их не подтвердит.

Я вынес её из платёжного кода [Zernolist](https://github.com/sinnercode228/tg-shop-miniapp),
демо-магазина кофе в Telegram Mini App, и переделал так, чтобы она подходила любому боту на
aiogram.

## Что ломается без неё

**`successful_payment` приходит дважды.** Если бот не ответил на вебхук вовремя, Telegram
пришлёт апдейт снова. С polling и `handle_as_tasks=False` апдейт, обработку которого оборвал
перезапуск, придёт ещё раз: aiogram подтверждает его offset только следующим `getUpdates`.
Обработчик, который ставит заказу «оплачен» и отгружает товар, отгрузит его дважды. Проверка в
памяти процесса не поможет, если бот запущен в двух процессах.

**Возврат упал, и его никто не повторил.** `refundStarPayment` — это один HTTP-запрос из
обработчика. Таймаут, 502 от Telegram или flood control (429 с `retry_after`) — исключение
уходит в лог, а звёзды к покупателю так и не возвращаются. Бывает и наоборот: запрос прошёл, а
ответ потерялся. Тогда повторный вызов получит `CHARGE_ALREADY_REFUNDED`, и это надо считать
успехом.

**На pre-checkout есть десять секунд.** Столько Telegram ждёт `answerPreCheckoutQuery`, потом
отменяет оплату. Медленный запрос остатков к базе стоит продажи, а обработчик, который упал с
исключением, не отвечает совсем.

**Payload принимают на веру.** Типичный обработчик разбирает `order:42` и отмечает заказ 42
оплаченным. Засчитается любой инвойс в этом формате, выставленный с токеном вашего бота, кто бы
его ни создал и с какой угодно ценой: соседний сервис с тем же токеном, отладочный скрипт,
старая версия бота.

## Установка

```sh
pip install aiogram-stars                 # только MemoryStorage
pip install "aiogram-stars[sqlite]"       # SqlAlchemyStorage: SQLAlchemy 2 и aiosqlite
pip install "aiogram-stars[postgres]"     # SqlAlchemyStorage: SQLAlchemy 2 и asyncpg
pip install "aiogram-stars[sqlalchemy]"   # только SQLAlchemy 2, драйвер на ваш выбор
```

Нужны Python 3.10 и новее и aiogram 3.15 и новее; кроме aiogram, обязательных зависимостей нет.
CI гоняет тесты на Python 3.10–3.13 с последним aiogram и на Python 3.10 с самыми старыми
поддерживаемыми версиями: aiogram 3.15.0, SQLAlchemy 2.0.30, aiosqlite 0.20.0, asyncpg 0.29.0.

Поставить текущую `main`:
`pip install "aiogram-stars @ git+https://github.com/sinnercode228/aiogram-stars"`.

## Быстрый старт

Сохраните в `bot.py`:

```python
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram_stars import Checkout, MemoryStorage, Payment, RejectPayment, StarsPayments

stock = {"coffee": 3}


async def check(checkout: Checkout) -> None:
    if stock["coffee"] == 0:
        raise RejectPayment("Sold out, sorry.")  # declines the pre-checkout query


async def on_paid(payment: Payment, message: Message) -> None:
    if stock["coffee"] == 0:  # the last bag went while this buyer was paying
        raise RejectPayment("Sold out. Your Stars are on their way back.")  # queues a refund
    stock["coffee"] -= 1
    await message.answer(f"Order {payment.order_id} is paid, thank you!")


stars = StarsPayments(
    MemoryStorage(), secret=os.environ["STARS_SECRET"], check=check, on_paid=on_paid
)
dp = Dispatcher()
stars.setup(dp)


@dp.message(Command("buy"))
async def buy(message: Message, bot: Bot) -> None:
    order_id = f"{message.chat.id}-{message.message_id}"
    await stars.send_invoice(
        bot, message.chat.id, order_id=order_id, amount=50, title="Coffee", description="250 g"
    )


if __name__ == "__main__":
    asyncio.run(dp.start_polling(Bot(os.environ["BOT_TOKEN"])))
```

```sh
export BOT_TOKEN=123456:ABC...   # от @BotFather
export STARS_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python bot.py
```

По шагам:

1. На `/buy` бот присылает инвойс на 50 звёзд: `currency="XTR"`, пустой `provider_token`,
   payload вида `<order_id>.<подпись>`.
2. Telegram присылает `pre_checkout_query`. Библиотека сверяет подпись с суммой, вызывает
   `check` и отвечает. `RejectPayment("Sold out, sorry.")` отклоняет оплату, и покупатель видит
   этот текст.
3. Telegram присылает `successful_payment`. Библиотека записывает платёж и вызывает `on_paid`;
   повторный апдейт с тем же `telegram_payment_charge_id` отбрасывается. Если `on_paid` бросил
   `RejectPayment`, платёж помечается отклонённым и возврат встаёт в очередь той же записью в
   базу. Фоновая задача сразу вызывает `refundStarPayment` и при ошибках повторяет его (по
   умолчанию 8 попыток примерно за час).

`MemoryStorage` при перезапуске забывает всё, в том числе возвраты в очереди, так что живому
боту нужен [`SqlAlchemyStorage`](#хранилища).
[`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
— магазин посложнее: платежи в SQLite (или в `DATABASE_URL`), заказы в памяти, `check`
проверяет, что заказ есть, принадлежит покупателю и ещё не оплачен, `/refund <charge_id>`
возвращает звёзды за свой платёж, бот пишет, когда они вернулись, и отвечает на `/paysupport`.
Код выше при каждом запуске CI прогоняет на фейковом Bot API
[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py),
пример —
[`tests/test_example.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_example.py).

## Что гарантируется и за счёт чего

### Ответ на pre-checkout успевает за 10 секунд

Сначала проверяется подпись payload: это хеш в памяти, без запросов куда-либо. Если подпись не
сошлась, запрос отклоняется с текстом `checkout_error`, а `check` не вызывается.

`check` запускается отдельной задачей, и на неё отведено `checkout_timeout` секунд, по
умолчанию 8. Значение должно быть больше 0 и меньше 10: что осталось от десяти секунд, уходит
на сам запрос с ответом. Как только время вышло, запрос отклоняется. Отменённую проверку
библиотека дожидается уже после ответа и пишет предупреждение, если та не остановилась, так что
`check`, застрявший в `finally`, ответ не задержит.

Одобрить платёж `check` может одним способом: вернуть `None`. `RejectPayment(reason)` отклоняет
его, и покупатель видит `reason`. Любое другое исключение, любое другое значение (`False`
тоже) и проверка, которая отменила сама себя, отклоняют платёж с `checkout_error` и пишутся в
лог с уровнем ERROR.

Где смотреть: [`_payments.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_payments.py),
`StarsPayments._decide` и `_run_check`.

### Payload подписан под заказ и сумму

Payload выглядит как `<order_id>.<tag>`, где `tag` — первые 16 байт
HMAC-SHA256(`secret`, `"aiogram-stars/payload/v1\n<amount>\n<order_id>"`) в base64url без
`=` на конце, 22 символа. Bot API пускает в payload не больше 128 байт, поэтому на номер заказа
остаётся до 105 байт UTF-8 (`PayloadSigner.MAX_ORDER_ID_BYTES`). Теги сравниваются через
`hmac.compare_digest`. Секрет должен быть не короче 16 байт.

Сумма входит в подпись, поэтому payload, оплаченный на другую `total_amount`, проверку не
проходит, а в `Checkout.amount` всегда та сумма, под которую вы подписывали. Payload, сделанный
без вашего секрета, отклоняется на pre-checkout. Если такой платёж всё же дошёл, звёзды
возвращаются.

Чтобы сменить секрет, передайте старый в `previous_secrets`: подписанные им payload по-прежнему
проходят проверку, а новые подписываются уже `secret`.

Где смотреть: [`_payload.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_payload.py).

### `on_paid` вызывается один раз на платёж

На каждый `successful_payment` в `XTR` библиотека вставляет строку с ключом
`telegram_payment_charge_id` и статусом `processing` и захватывает её на `payment_lease` (по
умолчанию 10 минут). В SQL это `INSERT ... ON CONFLICT (charge_id) DO NOTHING`. `on_paid`
вызывает только тот обработчик, который вставил строку; остальные копии апдейта пишутся в лог
с уровнем INFO и отбрасываются. Когда `on_paid` закончил, результат записывается через
`UPDATE ... WHERE lease_token = <токен этого обработчика>`:

| `on_paid` | Статус платежа | Возврат |
| --- | --- | --- |
| вернул управление | `accepted` | нет |
| бросил `RejectPayment(reason)` | `rejected`, в `error` лежит `reason` | в очереди, записан той же транзакцией, с `reason` |
| бросил другое исключение | `error`, в `error` лежит `"<тип>: <сообщение>"` | нет; исключение уходит дальше в aiogram |
| не вызывался, потому что подпись не сошлась | `rejected` | в очереди, записан той же транзакцией |

[`tests/test_concurrency.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_concurrency.py)
отдаёт один апдейт по три раза двум диспетчерам, у каждого свой engine, а база общая (SQLite или
PostgreSQL). `on_paid` выполняется один раз. Если оба отклоняют платёж, возврат отправляется
тоже один раз.

Если процесс умер посреди `on_paid`, строка так и остаётся в `processing`. Апдейт по этому
платежу, пришедший после того, как захват истёк, забирает платёж и вызывает `on_paid` заново.
Только Telegram такие апдейты почти не присылает, поэтому после истечения захвата платёж
появляется в `stars.stuck_payments()`, и закрыть его должен человек, см.
[Застрявшие платежи](#застрявшие-платежи).

При остановке диспетчер вызывает `StarsPayments.stop()`, и тот ждёт до `payment_lease`, пока
закончатся незавершённые `on_paid`. Без этого их отменил бы `asyncio.run()`, которым
заканчивается `dp.run_polling()`, а Telegram эти апдейты второй раз не прислал бы.

Где смотреть: [`_payments.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_payments.py)
(`_handle_payment`, `_settle`, `stop`) и
[`sqlalchemy.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/sqlalchemy.py)
(`claim_payment`, `settle_payment`).

### Возврат сначала записывается, потом отправляется, и только один раз

Возврат — это строка в `stars_refunds`, где `charge_id` служит первичным ключом, поэтому у
платежа не бывает двух возвратов. Фоновая задача захватывает возвраты, которым подошёл срок, и
вызывает `refundStarPayment`; подробности в разделе [Очередь возвратов](#очередь-возвратов).

Где смотреть: [`_outbox.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_outbox.py)
и [`_policy.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_policy.py).

## Хранилища

| Хранилище | Когда брать |
| --- | --- |
| `aiogram_stars.MemoryStorage` | Тесты и бот в одном процессе, которому не жалко потерять записи при перезапуске. |
| `aiogram_stars.sqlalchemy.SqlAlchemyStorage` | SQLite или PostgreSQL через async engine SQLAlchemy 2; процессов может быть сколько угодно. |
| Свой класс | Всё остальное, где можно сделать каждый метод атомарным. |

```python
from sqlalchemy.ext.asyncio import create_async_engine
from aiogram_stars.sqlalchemy import SqlAlchemyStorage

engine = create_async_engine("postgresql+asyncpg://shop:password@localhost/shop")
storage = SqlAlchemyStorage(engine)
await storage.create_tables()  # создаёт только то, чего ещё нет
```

С другими диалектами `SqlAlchemyStorage` не работает и бросает `ValueError`. Engine он не
закрывает, это ваша забота. Если схемой управляет Alembic, вместо `create_tables()` добавьте
`aiogram_stars.sqlalchemy.metadata` в `target_metadata` (Alembic принимает список).

Ни `SELECT ... FOR UPDATE`, ни advisory-блокировок здесь нет. Каждый захват — это
`INSERT ... ON CONFLICT DO NOTHING` или `UPDATE` с условием в короткой транзакции, и какой
процесс получит строку, решает `WHERE`. Ручное закрытие застрявшего платежа и повторная
постановка возврата в очередь — тоже `UPDATE` с условием. Таблицы в PostgreSQL:

```sql
CREATE TABLE stars_payments (
    charge_id VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL,
    order_id VARCHAR(128) NOT NULL,
    amount INTEGER NOT NULL,
    payload VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_recurring BOOLEAN NOT NULL,
    is_first_recurring BOOLEAN NOT NULL,
    subscription_expires_at TIMESTAMP WITH TIME ZONE,
    error TEXT,
    lease_token VARCHAR(64),
    leased_until TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (charge_id)
);

CREATE TABLE stars_refunds (
    charge_id VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason TEXT,
    last_error TEXT,
    lease_token VARCHAR(64),
    leased_until TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (charge_id)
);

CREATE INDEX ix_stars_refunds_due ON stars_refunds (status, next_attempt_at);
```

В SQLite колонки времени имеют тип `DATETIME` и хранят UTC без смещения, а наружу хранилище
отдаёт aware-datetime в UTC.

Своё хранилище должно реализовать одиннадцать async-методов протокола
[`Storage`](https://github.com/sinnercode228/aiogram-stars/blob/main/src/aiogram_stars/_storage.py):
`claim_payment`, `settle_payment`, `get_payment`, `list_stuck_payments`, `settle_stuck_payment`,
`add_refund`, `get_refund`, `claim_refund`, `finish_refund_attempt`, `list_failed_refunds` и
`retry_refund`; `list_stuck_payments`, `settle_stuck_payment`, `list_failed_refunds` и
`retry_refund` появились в 0.2.0. Требования такие: не больше одного платежа и одного возврата
на `charge_id`, каждый метод атомарен даже при нескольких процессах, на naive datetime и на
`limit` меньше 1 — `ValueError`.
[`tests/test_storage.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_storage.py)
прогоняет один и тот же контракт на памяти, SQLite и PostgreSQL; подставьте своё хранилище в
фикстуру `storage`, и тесты проверят его так же.

## Настройки

```python
StarsPayments(
    storage,
    secret=...,
    on_paid=...,
    check=None,
    on_refunded=None,
    on_refund_failed=None,
    previous_secrets=(),
    checkout_timeout=8.0,
    checkout_error="This payment cannot be completed right now. Please try again later.",
    payment_lease=timedelta(minutes=10),
    refunds=RefundPolicy(),
    clock=...,  # текущее время в UTC
)
```

| Параметр | Что делает |
| --- | --- |
| `storage` | Где лежат платежи и возвраты. |
| `secret` | Ключ подписи payload, `str` или `bytes`, от 16 байт. Держите его вне кода, как токен бота. Если сменить секрет и не положить старый в `previous_secrets`, все уже разосланные инвойсы перестанут проходить проверку. |
| `on_paid` | `async (payment: Payment, ...)`, вызывается один раз на платёж. |
| `check` | `async (checkout: Checkout, ...) -> None`, вызывается на pre-checkout с проверенным payload. Без него одобряется любой payload, подписанный этим ботом. |
| `on_refunded` | `async (refund: Refund, ...)`, вызывается, когда Telegram подтвердил возврат. |
| `on_refund_failed` | `async (refund: Refund, ...)`, вызывается, когда не удалась последняя попытка. |
| `previous_secrets` | Старые ключи. Их payload проходят проверку, новых ими не подписывают. |
| `checkout_timeout` | Сколько секунд может работать `check`: больше 0 и меньше 10. |
| `checkout_error` | Текст для покупателя, когда подпись не сошлась или `check` не уложился во время или упал. Пустым быть не может: при отказе Telegram требует сообщение. По умолчанию он английский, для русскоязычного бота задайте свой, например `"Не получилось провести оплату, попробуйте ещё раз чуть позже."`. |
| `payment_lease` | `timedelta`. Сколько платёж закреплён за обработчиком, который его ведёт; после этого его показывает `stuck_payments()`, а повторный апдейт может вызвать `on_paid` ещё раз. Столько же максимум ждёт остановка бота, пока закончатся `on_paid`. Ставьте больше, чем `on_paid` может выполняться. |
| `refunds` | `RefundPolicy`, см. ниже. |
| `clock` | Возвращает текущее время как aware `datetime`. Тесты передают свои часы. |

Поля `RefundPolicy`:

| Поле | По умолчанию | Что значит |
| --- | --- | --- |
| `max_attempts` | `8` | Сколько попыток до статуса `failed`. |
| `first_delay` | `timedelta(seconds=30)` | Пауза после первой неудачи; каждая следующая вдвое длиннее. |
| `max_delay` | `timedelta(hours=1)` | Самая длинная пауза. |
| `request_timeout` | `30` | Сколько секунд может идти один вызов `refundStarPayment`. |
| `lease` | `timedelta(minutes=5)` | На сколько воркер захватывает возврат, который отправляет. Должно быть больше `request_timeout`. |
| `poll_interval` | `10.0` | Сколько секунд между проверками очереди. |

Осторожно с единицами: `checkout_timeout`, `request_timeout` и `poll_interval` задаются числом
секунд, остальные длительности — `timedelta`. Настройки, с которыми ничего не заработает
(захват не длиннее таймаута запроса, пустой `checkout_error`, `checkout_timeout=10`), падают с
`ValueError` прямо при создании объекта.

### Колбэки

Колбэки вызываются так же, как aiogram вызывает хендлеры. Первый аргумент — объект, который
они обрабатывают, а дальше они получают из данных хендлера те именованные аргументы, которые
перечислили в сигнатуре, или все сразу, если принимают `**kwargs`. `check` и `on_paid` видят
данные своего апдейта: `bot`, `event_from_user`, то, что положили мидлвари, и именованные
аргументы `Dispatcher(...)`. `on_paid` дополнительно получает `message`, служебное сообщение с
платежом. `on_refunded` и `on_refund_failed` работают в фоновой задаче и получают `bot` и данные
диспетчера.

```python
async def on_paid(payment: Payment, message: Message, db: Database) -> None:
    await db.mark_paid(payment.order_id, charge_id=payment.charge_id)


dp = Dispatcher(db=Database(...))  # теперь db может попросить любой хендлер и колбэк
```

Колбэки должны быть `async def`. При создании `StarsPayments` проверяется только то, что у
колбэка есть позиционный аргумент. Обычная функция в `on_paid` выполнится, потом `await` её
результата бросит `TypeError`, и платёж запишется как `error`, хотя товар, может быть, уже ушёл.

### Подключение

`stars.setup(dp)` добавляет роутер с двумя платёжными хендлерами и вешает очередь возвратов на
запуск и остановку. Этот роутер встаёт первым среди роутеров, подключённых к `dp`, поэтому
какой-нибудь `@router.message()` без фильтров в одном из них платёжные апдейты не перехватит.
Хендлеры, зарегистрированные прямо на `dp`, по-прежнему срабатывают раньше. Обрабатывается
только `currency == "XTR"`, остальные валюты идут в ваши хендлеры. `setup` вызывается один раз
на `StarsPayments`; можно и `setup(router)` на роутер, который потом подключается к диспетчеру.

С polling больше ничего делать не надо. С вебхуками очереди возвратов нужен бот, переданный в
`setup_application`, и важен порядок вызовов:

```python
app = web.Application()
setup_application(app, dp, bot=bot)  # first, so that on shutdown the outbox stops first
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
```

aiohttp выполняет хуки остановки в том порядке, в каком их добавили, а `SimpleRequestHandler`
добавляет хук, который закрывает сессию бота. Если он зарегистрирован первым, сессия закроется,
пока возврат ещё в пути; эта попытка упадёт и повторится позже, израсходовав одну из попыток.
Без `bot=...` запуск упадёт с `RuntimeError`, в котором это написано. Если ваш процесс вообще не
запускает startup-хуки aiogram, вызывайте `await stars.start(bot)` и `await stars.stop()` сами.

### API

Из `aiogram_stars` экспортируются `StarsPayments`, `Checkout`, `Payment`, `PaymentStatus`,
`Refund`, `RefundStatus`, `RefundPolicy`, `RejectPayment`, `InvalidPayload`, `PayloadSigner`,
`Storage`, `MemoryStorage`, `SUBSCRIPTION_PERIOD` и `__version__`, из `aiogram_stars.sqlalchemy`
— `SqlAlchemyStorage` и `metadata`.

| Метод `StarsPayments` | Что делает |
| --- | --- |
| `setup(router)` | Регистрирует хендлеры и хуки запуска и остановки. |
| `await send_invoice(bot, chat_id, *, order_id, amount, title, description, label=None, **options)` | Отправляет инвойс в звёздах с подписанным payload и возвращает сообщение. `options` уходят в `Bot.send_invoice`, например `photo_url` или `reply_markup`. |
| `await create_invoice_link(bot, *, order_id, amount, title, description, label=None, subscription_period=None, **options)` | Возвращает ссылку на инвойс: для `WebApp.openInvoice` в Mini App или для подписки. |
| `make_payload(order_id, amount)` | Подписанный payload для инвойсов, которые два метода выше не покрывают, например `InputInvoiceMessageContent` в inline-режиме. |
| `await get_payment(charge_id)`, `await get_refund(charge_id)` | Запись из хранилища или `None`. |
| `await refund(charge_id, *, user_id=None, reason=None)` | Ставит возврат в очередь и возвращает его. |
| `await send_due_refunds(bot)` | Одна попытка по каждому возврату, которому пора, прямо сейчас и в вызывающей задаче. |
| `await stuck_payments(*, limit=100)` | Платежи, которые должен закрыть человек: `error` и `processing` с истёкшим захватом. |
| `await accept_payment(charge_id)` | Переводит застрявший платёж в `accepted`. |
| `await reject_payment(charge_id, *, reason)` | Переводит застрявший платёж в `rejected` и той же транзакцией ставит возврат в очередь. |
| `await failed_refunds(*, limit=100)` | Возвраты, у которых кончились попытки. |
| `await retry_refund(charge_id)` | Снова ставит в очередь возврат, у которого кончились попытки. |
| `await cancel_subscription(bot, charge_id, *, user_id=None, is_canceled=True)` | Отключает продление подписки. |
| `await start(bot, **data)`, `await stop()`, `running` | Ручной запуск и остановка очереди возвратов. |

У `Payment` есть `charge_id`, `user_id`, `order_id` (всегда `str`), `amount`, `payload`,
`status`, `created_at`, `updated_at`, `is_recurring`, `is_first_recurring`,
`subscription_expires_at` и `error`. У `Checkout` — `order_id`, `amount`, `user_id` и исходный
`query`. Статусы — строковые enum: `str(PaymentStatus.ACCEPTED) == "accepted"`. Всё время —
aware-datetime в UTC.

Логи идут в логгер `aiogram_stars`. INFO: повторные апдейты, прошедшие возвраты, отказы от
`check`, платежи, закрытые вручную, и возвраты, заново поставленные в очередь. WARNING: payload,
отклонённые на pre-checkout, платежи, отклонённые в `on_paid`, повторы возвратов, flood control.
ERROR: то, на что должен посмотреть человек, например медленный или упавший `check`, исключение
в `on_paid`, оплаченный инвойс с неверной подписью, возврат, у которого кончились попытки,
проход очереди, на котором не ответила база, платёж, который ещё обрабатывается в момент
остановки. Записи уровня ERROR стоит отправлять туда, где вы получаете алерты.

## Очередь возвратов

Возврат попадает в очередь, когда `on_paid` бросает `RejectPayment` или у оплаченного инвойса не
сходится подпись, когда застрявший платёж отклоняют вручную через `stars.reject_payment` (см.
[Застрявшие платежи](#застрявшие-платежи)), и при вызове `stars.refund(charge_id, reason=...)`.
Этот вызов записывает возврат и возвращает его, из вызывающей задачи ничего не отправляется.
Второй вызов для того же платежа вернёт уже записанный возврат. Для платежа, которого хранилище
не знает, нужен `user_id=`, иначе будет `LookupError`, а `user_id`, который не совпал с
плательщиком, даст `ValueError`.

Фоновая задача (`aiogram-stars-refunds`) стартует вместе с диспетчером. Очередь она проверяет
сразу после того, как в этом же процессе появился новый возврат, а в остальное время раз в
`poll_interval`. Возвраты она захватывает по одному, начиная с самого раннего
`next_attempt_at`, засчитывает попытку и вызывает
`refundStarPayment(user_id, telegram_payment_charge_id)` с `request_timeout`.

| Ответ Telegram | Статус возврата | Что дальше |
| --- | --- | --- |
| `True` | `done` | `on_refunded` |
| `400 CHARGE_ALREADY_REFUNDED` | `done` | `on_refunded`; прошлая попытка прошла, а ответ на неё потерялся |
| `429` с `retry_after=N` | `pending` | N секунд процесс не отправляет никаких возвратов, а этот возврат ждёт не меньше N секунд |
| любая другая ошибка: прочие 400 вроде `BALANCE_TOO_LOW`, 5xx, таймауты, сетевые ошибки | `pending` | повтор после паузы |
| ошибка на попытке номер `max_attempts` | `failed` | `on_refund_failed`; дальше разбирается человек |

С настройками по умолчанию паузы такие: 30 секунд, потом 1, 2, 4, 8, 16 и 32 минуты, так что
восьмая, последняя попытка случается примерно через 64 минуты после первой. Каждая пауза
заканчивается на первом проходе очереди после неё. В `Refund.last_error` лежит последняя
ошибка, например `TelegramBadRequest: Telegram server says - Bad Request: BALANCE_TOO_LOW`.

Очередь можно запускать в нескольких процессах на одной базе. Захват — это `UPDATE` с условием,
поэтому один возврат в каждый момент отправляет один воркер. Если воркер умер посреди попытки,
его захват истекает через `lease`, и возврат отправляет другой воркер; потерянная попытка всё
равно засчитана. Попытка, которая пережила свой захват, не затрёт результат воркера, забравшего
возврат. При остановке очередь даёт текущей попытке закончиться, но ждёт не дольше
`request_timeout`.

`await stars.send_due_refunds(bot)` делает по одной попытке для каждого возврата, которому
пора, и возвращает их. Это для своей периодической задачи, если бот не запускает фоновую, и для
тестов.

`on_refunded` и `on_refund_failed` вызываются один раз, после того как результат записан.
Исключение в них попадает в лог и на возврат не влияет. Если процесс умрёт между записью
результата и колбэком, колбэк пропадёт, но статус в таблице будет верным.

Возвраты, у которых кончились попытки, показывает `await stars.failed_refunds()`, а
`await stars.retry_refund(charge_id)` ставит такой возврат в очередь заново, см.
[Застрявшие платежи](#застрявшие-платежи).

## Застрявшие платежи

Методы из этого раздела появились в 0.2.0. Без человека не обойтись в двух случаях: платёж,
который больше никто не доведёт до конца, и возврат, у которого кончились попытки.

Платёж застрял, если он в `error`, потому что `on_paid` бросил исключение, или всё ещё в
`processing`, хотя его `payment_lease` истёк: процесс убили посреди `on_paid` или база
отказала, когда записывался результат. Для платежа в `error` `on_paid` больше не вызывается,
даже если апдейт придёт ещё раз, а для платежа в `processing` — только если Telegram пришлёт
апдейт ещё раз, а он делает это редко (см. [ограничения](#ограничения)).
`await stars.stuck_payments()` возвращает такие платежи, начиная с самых старых, не больше
`limit` штук (по умолчанию 100). Выясните, что случилось с заказом, и закройте платёж:

| Вызов | Платёж | Возврат |
| --- | --- | --- |
| `await stars.accept_payment(charge_id)` | `accepted`; в `error` остаётся то, что бросил `on_paid` | не ставится; если по платежу уже был возврат, он остаётся как есть |
| `await stars.reject_payment(charge_id, reason=...)` | `rejected`, в `error` лежит `reason` | в очереди, записан той же транзакцией, с `reason`, как при `RejectPayment` из `on_paid`; если по платежу уже был возврат, он остаётся как есть, и `failed` придётся поставить заново через `retry_refund` |

Оба метода возвращают платёж в том виде, в каком он записан. Меняют они его, только пока он
застрял (в SQL это один `UPDATE` с условием). Поэтому платёж, чей захват ещё не истёк и чей
`on_paid`, возможно, ещё работает, даёт `ValueError`, и уже закрытый платёж тоже. Если два
администратора закрывают один платёж одновременно, один получит платёж, другой — `ValueError`.
На платёж, которого нет в хранилище, будет `LookupError`. Закрытие снимает захват: процесс,
который держал платёж, свой результат уже не запишет, а запоздавшая копия апдейта `on_paid` не
вызовет.

Процесс, который жив, но работает дольше `payment_lease`, ещё сидит в `on_paid`, когда его
платёж попадает в застрявшие. Если закрыть платёж в этот момент, останется ваше решение, а
результат `on_paid` будет отброшен, и об этом появится запись в логе с уровнем ERROR. Это ещё
одна причина ставить `payment_lease` больше, чем может выполняться `on_paid`.

`await stars.failed_refunds()` возвращает возвраты, у которых кончились попытки, начиная с самых
старых; ошибка лежит в `last_error`. Когда причина устранена, `await stars.retry_refund(charge_id)`
ставит возврат в очередь снова: он становится `pending` со сроком «сейчас», `attempts`
обнуляется, так что у него опять `max_attempts` попыток, и очередь его отправит. Если возврат не
в статусе `failed`, будет `ValueError`, если возврата по этому платежу нет — `LookupError`.
Если возврат снова не пройдёт, очередь ещё раз вызовет `on_refund_failed`, а если пройдёт —
`on_refunded`.

Задача, которая присылает оба списка в чат администратора:

```python
from aiogram import Bot
from aiogram_stars import StarsPayments

ADMIN_CHAT_ID = 123456789  # куда отправлять отчёт


async def report_stuck(stars: StarsPayments, bot: Bot) -> None:
    lines = [
        f"payment {p.charge_id}: order {p.order_id}, {p.amount} Stars, {p.error or p.status}"
        for p in await stars.stuck_payments(limit=20)
    ]
    lines += [f"refund {r.charge_id}: {r.last_error}" for r in await stars.failed_refunds(limit=20)]
    text = "\n".join(lines)
    for start in range(0, len(text), 4096):  # в одно сообщение помещается до 4096 символов
        # parse_mode=None: ошибки — простой текст, даже если у бота задан режим разметки
        await bot.send_message(ADMIN_CHAT_ID, text[start : start + 4096], parse_mode=None)
```

Запускайте её своим планировщиком или один раз после старта бота. Этот код тоже прогоняет
[`tests/test_readme.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/test_readme.py).
В [`examples/shop_bot.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/examples/shop_bot.py)
то же самое сделано командами для администратора из `ADMIN_ID`: `/stuck` показывает оба
списка, а `/accept`, `/reject` и `/retry` принимают `charge_id`.

## Подписки

```python
from aiogram_stars import SUBSCRIPTION_PERIOD

link = await stars.create_invoice_link(
    bot,
    order_id=f"club-{user_id}",
    amount=100,
    title="Coffee club",
    description="A bag of coffee every month",
    subscription_period=SUBSCRIPTION_PERIOD,
)
```

Период Telegram принимает один — 30 дней (`SUBSCRIPTION_PERIOD`, 2592000 секунд), и только в
ссылках на инвойс: у `sendInvoice` такого параметра нет. Цена подписки по документации Bot API
не больше 10000 звёзд.

Каждое продление — это новый `successful_payment` с новым `charge_id` и payload первого
инвойса, так что `on_paid` вызывается раз в период. `Payment.is_recurring` стоит у всех таких
платежей, `is_first_recurring` — только у первого, а в `subscription_expires_at` лежит конец
оплаченного периода (из `subscription_expiration_date`).

`await stars.cancel_subscription(bot, charge_id)` вызывает `editUserStarSubscription` с
`is_canceled=True`: подписка работает до конца оплаченного периода и больше не продлевается.
`is_canceled=False` позволяет пользователю снова включить подписку, которую отключил бот.
`user_id=` нужен только для платежа, которого нет в хранилище.

Продления приходят с тем payload, который подписали при оформлении подписки. Поэтому старый
секрет держите в `previous_secrets`, пока живы подписки, подписанные им. Продление, которое
отклонили (в `on_paid`, вручную или потому что подпись больше не сходится), вернётся покупателю,
но сама подписка продолжится, и через месяц спишется снова. Вызывайте ещё и
`cancel_subscription`.

## Ограничения

- **Один бот.** `StarsPayments` подписывает одним секретом, а очередь отправляет все возвраты
  из своего хранилища тем ботом, с которым её запустили. Двум ботам нужны два `StarsPayments` и
  два набора таблиц; на общих таблицах каждая очередь пыталась бы вернуть платежи чужого бота.
- **Только звёзды.** Инвойсы в других валютах, платный контент и подарки остаются на вас.
- **Застрявший платёж ждёт человека.** Telegram почти никогда не присылает
  `successful_payment` второй раз: polling подтверждает апдейт следующим `getUpdates`, а вебхук
  с `handle_in_background=True` сразу получает 200. Поэтому если процесс убили внутри `on_paid`
  (SIGKILL, нехватка памяти, супервизор, который не ждёт `payment_lease`) или база отказала в
  момент записи результата, платёж останется в `processing`, и `on_paid` за него, скорее всего,
  больше никто не вызовет; за платёж в `error` его тоже не вызовут повторно. Когда захват
  истечёт, платёж появится в `stuck_payments()`, и закрыть его нужно вручную, см.
  [Застрявшие платежи](#застрявшие-платежи). Сама библиотека `stuck_payments()` не вызывает, а о
  платеже, чей процесс убили, ничего не пишет в лог, так что нужна своя задача, которая его
  вызывает, например отчёт из того раздела. В SQL каждый такой вызов читает всю таблицу
  `stars_payments`: индекса по `status` нет. Если же база отказала, когда платёж только
  записывался, строки нет вовсе, и след остаётся лишь в логе и в `getStarTransactions`, который
  библиотека не вызывает.
- **Каждый платёж с неверной подписью возвращается.** В том числе инвойсы, выставленные до того,
  как бот перешёл на библиотеку, продления подписок, оформленных до этого, и payload,
  подписанные секретом, которого уже нет в `previous_secrets`. Чтобы старый формат и дальше
  обрабатывался по-старому, повесьте его хендлеры прямо на диспетчер: они сработают раньше
  роутера библиотеки.

  ```python
  @dp.pre_checkout_query(F.invoice_payload.startswith("order:"))
  async def old_checkout(query: PreCheckoutQuery) -> None: ...


  @dp.message(F.successful_payment.invoice_payload.startswith("order:"))
  async def old_payment(message: Message) -> None: ...
  ```

- **Колбэки не повторяются.** `on_refunded` и `on_refund_failed` вызываются один раз, `on_paid`
  тоже (см. таблицу выше). Возврат, который заново поставили в очередь через `retry_refund`,
  проходит попытки сначала, и если он снова не пройдёт, `on_refund_failed` вызовется ещё раз.
- **`checkout_timeout` ограничивает только `check`.** Время, за которое апдейт дошёл до бота, и
  сам запрос `answerPreCheckoutQuery` тратятся из тех же десяти секунд.
- **Тесты с Telegram не разговаривают.** Они идут на фейковой сессии Bot API, а её ответы
  разбирает тот же код aiogram, что и настоящие, поэтому ошибки вылетают теми же классами
  исключений, что и в бою. Период в 30 дней и потолок в 10000 звёзд взяты из документации Bot
  API.

## Как тестировать бота

Понадобятся `MemoryStorage`, сессия Bot API, которая записывает вызовы вместо того чтобы их
выполнять, и `dp.feed_update`. Payload, который был бы в вашем инвойсе, даёт
`stars.make_payload(order_id, amount)`. С pytest и pytest-asyncio (`asyncio_mode = "auto"`):

```python
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerPreCheckoutQuery, TelegramMethod
from aiogram.types import Update
from aiogram_stars import MemoryStorage, Payment, PaymentStatus, StarsPayments


class RecordingSession(BaseSession):
    """Answers every Bot API call with True and keeps the calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.calls.append(method)
        return True

    def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        pass


def update(bot: Bot, **event: Any) -> Update:
    return Update.model_validate({"update_id": 1, **event}, context={"bot": bot})


async def test_a_payment_delivered_twice_is_handled_once() -> None:
    paid: list[Payment] = []

    async def on_paid(payment: Payment) -> None:
        paid.append(payment)

    session = RecordingSession()
    bot = Bot("42:TEST", session=session)
    stars = StarsPayments(MemoryStorage(), secret="test-secret-0123456789", on_paid=on_paid)
    dp = Dispatcher()
    stars.setup(dp)
    payload = stars.make_payload("order-1", 50)
    buyer = {"id": 7, "is_bot": False, "first_name": "Ann"}

    query = {"id": "q1", "from": buyer, "currency": "XTR", "total_amount": 50}
    query["invoice_payload"] = payload
    await dp.feed_update(bot, update(bot, pre_checkout_query=query))
    [answer] = session.calls
    assert isinstance(answer, AnswerPreCheckoutQuery) and answer.ok

    receipt = {
        "currency": "XTR",
        "total_amount": 50,
        "invoice_payload": payload,
        "telegram_payment_charge_id": "ch-1",
        "provider_payment_charge_id": "",
    }
    chat = {"id": 7, "type": "private"}
    message = {"message_id": 1, "date": 0, "chat": chat, "from": buyer}
    message["successful_payment"] = receipt
    await dp.feed_update(bot, update(bot, message=message))
    await dp.feed_update(bot, update(bot, message=message))  # Telegram sent it again

    assert [p.order_id for p in paid] == ["order-1"]
    stored = await stars.get_payment("ch-1")
    assert stored is not None and stored.status is PaymentStatus.ACCEPTED
```

Ещё пригодится:

- `await stars.send_due_refunds(bot)` прогоняет очередь возвратов прямо в тесте, и ждать фоновую
  задачу не нужно.
- С `clock=` время двигается только тогда, когда его двигает тест: так можно пройти всё
  расписание повторов или дождаться, пока истечёт захват.
- [`tests/telegram.py`](https://github.com/sinnercode228/aiogram-stars/blob/main/tests/telegram.py)
  — фейк побогаче (лицензия MIT, копируйте). Он отдаёт правдоподобные ответы на `sendInvoice` и
  `sendMessage`, отвечает на `getUpdates` для тестов с polling, а его `fail()` заставляет
  следующие вызовы метода падать с любой ошибкой Bot API, в том числе с `retry_after`.

## Релиз

Версии идут по [Semantic Versioning](https://semver.org/lang/ru/), у каждого релиза есть
запись в [CHANGELOG.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CHANGELOG.md).

1. Поменяйте версию в `pyproject.toml` и `__version__` в `src/aiogram_stars/__init__.py`;
   `tests/test_packaging.py` упадёт, если они разойдутся.
2. В `CHANGELOG.md` замените `Unreleased` в заголовке версии датой релиза, а ссылку версии
   направьте на её тег.
3. Закоммитьте и запушьте тег: `git tag v0.2.0 && git push origin v0.2.0`.

[`release.yml`](https://github.com/sinnercode228/aiogram-stars/blob/main/.github/workflows/release.yml)
проверяет, что тег совпадает с версией, гоняет тесты (без PostgreSQL; с ним их гоняет CI на
каждый пуш в `main`), собирает sdist и wheel, проверяет их `twine check --strict` и загружает из
джобы в GitHub-окружении `pypi`. Загрузка идёт через trusted publishing: GitHub выдаёт джобе
короткоживущий OIDC-токен, и токен PyPI нигде не хранится.

Владельцу репозитория нужно один раз сделать две вещи:

1. На PyPI открыть <https://pypi.org/manage/account/publishing/> и добавить pending publisher:
   project name `aiogram-stars`, owner `sinnercode228`, repository `aiogram-stars`, workflow
   `release.yml`, environment `pypi`. Именно pending, потому что до первой загрузки проекта на
   PyPI нет; первая загрузка превращает его в обычного издателя проекта.
2. На GitHub создать окружение `pypi` в *Settings → Environments*. Если каждый релиз должен ждать
   подтверждения, добавьте туда required reviewers.

## Разработка

```sh
git clone https://github.com/sinnercode228/aiogram-stars
cd aiogram-stars
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,sqlalchemy]"

ruff check && ruff format --check
mypy --strict src && mypy        # просто mypy проверяет ещё тесты и примеры
pytest --cov
```

Тесты на PostgreSQL пропускаются, пока не задан `STARS_TEST_POSTGRES_URL`, например
`postgresql+asyncpg://postgres:postgres@localhost:5432/starslib_test`. Они удаляют и создают
заново обе таблицы в этой базе, так что дайте им отдельную.

Любое изменение поведения приходит с тестом, который до него падал, а исправление бага
начинается с теста, который этот баг показывает. Код проходит `mypy --strict`, у публичных
объектов есть docstring, логи пишутся через `logging.getLogger(__name__)`. Новый метод в
`Storage` ломает все хранилища, написанные вне этого репозитория, поэтому сначала откройте
issue. Баги и вопросы — в [GitHub issues](https://github.com/sinnercode228/aiogram-stars/issues).

Остальное (самые старые поддерживаемые версии, проверка примеров из README, CHANGELOG, стиль
коммитов) описано в [CONTRIBUTING.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CONTRIBUTING.md),
по-английски, с коротким разделом на русском в конце.

## Лицензия

MIT, текст в [LICENSE](https://github.com/sinnercode228/aiogram-stars/blob/main/LICENSE).
