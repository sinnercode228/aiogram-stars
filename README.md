# aiogram-stars

Telegram Stars payments for aiogram 3: signed invoice payloads, a pre-checkout
deadline, exactly-once handling of successful payments and a durable refund outbox.

Full documentation is coming with the first release. Until then, see
[`examples/shop_bot.py`](examples/shop_bot.py) and [CHANGELOG.md](CHANGELOG.md).

## Releasing

Releases are published to PyPI by [`.github/workflows/release.yml`](.github/workflows/release.yml)
when a tag `v<version>` matching the version in `pyproject.toml` is pushed. It uses PyPI
trusted publishing, so no token is stored in GitHub. One-time setup on PyPI:

1. Open <https://pypi.org/manage/account/publishing/> and add a **pending publisher**
   (or, once the project exists, *Manage project → Publishing → Add a new publisher*).
2. Fill in: PyPI project name `aiogram-stars`, owner `sinnercode228`, repository
   `aiogram-stars`, workflow name `release.yml`, environment name `pypi`.
3. In the GitHub repository settings, create the environment `pypi` (optionally with
   required reviewers, so every release waits for an approval).

## License

MIT, see [LICENSE](LICENSE).
