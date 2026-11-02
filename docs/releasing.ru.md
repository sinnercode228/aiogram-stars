# Релиз

[English](releasing.md) · Русский

Как мейнтейнер выпускает версию на PyPI.

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
