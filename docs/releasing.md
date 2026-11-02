# Releasing

English · [Русский](releasing.ru.md)

How the maintainer releases a version to PyPI.

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and every release
has an entry in [CHANGELOG.md](https://github.com/sinnercode228/aiogram-stars/blob/main/CHANGELOG.md).

1. Set the version in `pyproject.toml` and `__version__` in `src/aiogram_stars/__init__.py`;
   `tests/test_packaging.py` fails if the two differ.
2. In `CHANGELOG.md`, replace `Unreleased` in the version's heading with the release date, and
   point the version's link at its tag.
3. Commit, then push a tag: `git tag v0.2.0 && git push origin v0.2.0`.

[`release.yml`](https://github.com/sinnercode228/aiogram-stars/blob/main/.github/workflows/release.yml)
checks that the tag matches the version, runs the tests (without PostgreSQL; the CI workflow
runs them with it on every push to `main`), builds the sdist and the wheel, runs
`twine check --strict`, and uploads them from a job in the GitHub environment `pypi`. The upload
uses PyPI trusted publishing: GitHub gives the job a short-lived OIDC token, and no PyPI token
is stored anywhere.

The owner sets this up once:

1. On PyPI, open <https://pypi.org/manage/account/publishing/> and add a pending publisher with
   project name `aiogram-stars`, owner `sinnercode228`, repository `aiogram-stars`, workflow
   `release.yml` and environment `pypi`. It has to be a pending one because the project does not
   exist on PyPI before the first upload; that upload turns it into the project's publisher.
2. On GitHub, create the environment `pypi` under *Settings → Environments*. Add required
   reviewers to it if every release should wait for a click.
