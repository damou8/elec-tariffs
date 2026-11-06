# elec-tariffs

Cost French electricity consumption hour by hour under Base, HP/HC and Tempo contracts.

`elec-tariffs` takes hourly consumption (as a Linky meter reports it) and works out what it cost under one or more contracts: which tranche each hour falls into, its price on that day, and its share of the yearly subscription. It also downloads the data that needs:

- the regulated prices (Tarif Bleu) of every period since 2012, from [data.gouv.fr](https://www.data.gouv.fr/fr/datasets/historique-des-tarifs-reglementes-de-vente-delectricite-pour-les-consommateurs-residentiels/);
- the color of each Tempo day, from RTE.

It is async (aiohttp), fully typed and has no dependency on Home Assistant. It was written for the [Electricity Contracts](https://github.com/damou8/electricity_contracts) Home Assistant integration.

It is not affiliated with EDF, RTE or Enedis.

## Install

```bash
pip install elec-tariffs
```

Python 3.14 or later.

## Use

```python
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import aiohttp

from elec_tariffs import (
    ContractKind,
    ContractRules,
    RtePublicTempoClient,
    TrvClient,
    cost_hour,
)

PARIS = ZoneInfo("Europe/Paris")


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        prices = await TrvClient(session).async_price_periods(ContractKind.TEMPO, 6)
        colors = await RtePublicTempoClient(session).async_calendar(
            date(2026, 1, 4), date(2026, 1, 5)
        )

    # 1.8 kWh used from 19:00 to 20:00 on 5 January 2026.
    cost = cost_hour(
        datetime(2026, 1, 5, 19, tzinfo=PARIS),
        1.8,
        ContractRules(kind=ContractKind.TEMPO),
        prices,
        colors,
    )
    print(cost.energy, cost.variable, cost.fixed, cost.total)


asyncio.run(main())
```

An HP/HC contract takes its own off-peak ranges, to the half hour, and can make weekends off-peak:

```python
from datetime import time

from elec_tariffs import ContractKind, ContractRules, TimeRange

rules = ContractRules(
    kind=ContractKind.HPHC,
    off_peak=(
        TimeRange(start=time(12, 30), end=time(14)),
        TimeRange(start=time(22), end=time(6, 30)),
    ),
    weekend_off_peak=True,
)
```

Prices you set yourself are `PricePeriod` objects: a first and last day (`None` while in force), the yearly fixed part and a price per tranche, all with taxes.

## How an hour is costed

- **Tranches.** Each hour is split into two half hours with half the energy each, so an off-peak range that starts or ends on a half hour is followed. This assumes the consumption is even within the hour.
- **Tempo.** A Tempo day runs from 6:00 to 6:00, so a night takes the color of the day that started the evening before. Off-peak hours are 22:00 to 6:00.
- **Prices** change at midnight on the first day of a price period (1 February and 1 August for the regulated prices). An hour uses the prices of its calendar day.
- **Subscription.** The yearly fixed part is spread evenly over the hours of the year (8,760, or 8,784 in a leap year).
- **Missing data.** An hour whose Tempo color or price isn't known raises `MissingColorError` or `MissingPriceError` instead of guessing.

## Tempo calendar sources

| Source | Account | Notes |
| --- | --- | --- |
| `RtePublicTempoClient` | none | The backend of RTE's public [Tempo calendar page](https://www.services-rte.com/fr/visualisez-les-donnees-publiees-par-rte/calendrier-des-offres-de-fourniture-de-type-tempo.html). It isn't documented and RTE can change it without notice, so its answers are checked strictly: an unexpected format raises `TempoFormatError` instead of giving wrong colors. |
| `RteOfficialTempoClient` | free [data.rte-france.com](https://data.rte-france.com/catalog/-/api/consumption/Tempo-Like-Supply-Contract/v1.1) application | RTE's documented "Tempo Like Supply Contract" API, with OAuth2 client credentials. The token is kept until it expires and renewed once if refused. Long ranges are split into requests of 366 days at most. **Not yet tested against the live API**: its tests follow the format used by other clients. |

`FallbackTempoSource` asks each source in turn, for example the public one first and the official one when it fails. It records which source answered and the errors of the others.

Past colors never change, so callers should cache them and only ask for the current season and the next day.

To create the official API application: sign up on data.rte-france.com, open the "Tempo Like Supply Contract" API, choose "S'abonner à l'API" (this creates an application), then copy its client ID and secret.

## Errors

Every error derives from `ElecTariffsError`:

- `MissingColorError`, `MissingPriceError` (with the `day` concerned);
- `TrvServerError`, `TrvFormatError` for data.gouv.fr;
- `TempoServerError` (network or 5xx), `TempoRateLimitError` (429), `TempoAuthError` (credentials refused) and `TempoFormatError` for the Tempo sources.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .
.venv/bin/ruff check && .venv/bin/ruff format --check && .venv/bin/mypy
.venv/bin/pytest --cov
```

The tests use recorded answers in `tests/fixtures/` and a local HTTP server; they make no network requests.

Releases are published to PyPI by GitHub Actions when a GitHub release is published; the tag (`vX.Y.Z`) must match `src/elec_tariffs/_version.py`.

## License

[MIT](LICENSE)
