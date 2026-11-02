# Настройки

[English](configuration.md) · Русский

Все параметры `StarsPayments` и поля `RefundPolicy`, как вызываются колбэки, как подключить
библиотеку к polling и вебхукам, публичный API и логи.

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

## Колбэки

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

## Подключение

`stars.setup(dp)` добавляет роутер с двумя платёжными хендлерами и вешает очередь возвратов на
запуск и остановку. Этот роутер встаёт первым среди роутеров, подключённых к `dp`, поэтому
какой-нибудь `@router.message()` без фильтров в одном из них платёжные апдейты не перехватит.
Хендлеры, зарегистрированные прямо на `dp`, по-прежнему срабатывают раньше. Обрабатывается
только `currency == "XTR"`, остальные валюты идут в ваши хендлеры. `setup` вызывается один раз
на `StarsPayments`; можно и `setup(router)` на роутер, который потом подключается к диспетчеру.

С polling больше ничего делать не надо. С вебхуками очереди возвратов нужен бот, переданный в
`setup_application`, и важен порядок вызовов:

```python
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

app = web.Application()
setup_application(app, dp, bot=bot)  # first, so that on shutdown the outbox stops first
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
```

aiohttp выполняет хуки остановки в том порядке, в каком их добавили, а `SimpleRequestHandler`
добавляет хук, который закрывает сессию бота. Если он зарегистрирован первым, сессия закроется,
пока возврат ещё в пути; эта попытка упадёт и повторится позже, израсходовав одну из попыток.
Без `bot=...` запуск упадёт с `RuntimeError`, в котором это написано. Если ваш процесс вообще не
запускает startup-хуки aiogram, вызывайте `await stars.start(bot)` и `await stars.stop()` сами.

## API

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
