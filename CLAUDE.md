# elec-tariffs

Library behind the Electricity Contracts Home Assistant integration (damou8/electricity_contracts). No Home Assistant import here.

## Commands

```bash
.venv/bin/pip install -r requirements-dev.txt -e .
.venv/bin/ruff check && .venv/bin/ruff format --check && .venv/bin/mypy
.venv/bin/pytest --cov   # fails under 95 % coverage
```

## Conventions

- Python 3.14, `mypy --strict`, ruff `ALL`. Annotations are lazy: no `TYPE_CHECKING` blocks.
- Keep `aiohttp` in `requirements-dev.txt` at Home Assistant's pin (`homeassistant/package_constraints.txt`).
- The version lives in `src/elec_tariffs/_version.py`; the release tag must be `v` + that version.
- Tests make no network request: recorded answers in `tests/fixtures/`, HTTP through the `server` fixture (a local aiohttp server) and the clients' `base_url` / `urls` arguments.

## Pitfalls

- aioresponses 0.7.9 doesn't work with aiohttp 3.14 (`ClientResponse` needs `stream_writer`): don't bring it back.
- aiohttp 3.14 deprecates `BasicAuth`; the official RTE client builds the header itself.
- aiohttp retries a GET once when a reused connection drops: a test of a dropped connection must queue the failure twice.
- The public RTE endpoint is undocumented. Keep its parsing strict (`TempoFormatError` on anything unexpected) rather than lenient.
