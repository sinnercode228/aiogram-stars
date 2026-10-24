<!-- What changes and why. Link the issue if there is one. -->

- [ ] A test that failed before this change; for a bug fix, the test that shows the bug
- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy --strict src` and `mypy` pass
- [ ] `pytest` passes, also with `STARS_TEST_POSTGRES_URL` set if storage code changed
- [ ] README.md and README.ru.md say the same, and `tests/test_readme.py` passes
- [ ] An entry in CHANGELOG.md under the next version
