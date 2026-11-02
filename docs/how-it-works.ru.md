# Как устроен aiogram-stars

[English](how-it-works.md) · Русский

Что библиотека гарантирует и за счёт чего: pre-checkout, подписанные payload, `on_paid` один раз
на платёж, очередь возвратов, хранилища и подписки. Коротко всё это есть в
[README](https://github.com/sinnercode228/aiogram-stars/blob/main/README.ru.md).

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
[Застрявшие платежи](failure-modes.ru.md#застрявшие-платежи).

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

## Очередь возвратов

Возврат попадает в очередь, когда `on_paid` бросает `RejectPayment` или у оплаченного инвойса не
сходится подпись, когда застрявший платёж отклоняют вручную через `stars.reject_payment` (см.
[Застрявшие платежи](failure-modes.ru.md#застрявшие-платежи)), и при вызове `stars.refund(charge_id, reason=...)`.
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
[Застрявшие платежи](failure-modes.ru.md#застрявшие-платежи).

## Хранилища

Какое хранилище выбрать и как создать `SqlAlchemyStorage`, написано в
[README](https://github.com/sinnercode228/aiogram-stars/blob/main/README.ru.md#хранилища). Свой класс подойдёт для всего остального, где можно
сделать каждый метод атомарным.

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

## Подписки

Как создать ссылку на инвойс с подпиской, показано в
[README](https://github.com/sinnercode228/aiogram-stars/blob/main/README.ru.md#подписки).

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
