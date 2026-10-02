# DeskRanger Status

The public status page of DeskRanger: **https://status.deskranger.app**
(until the domain is set up: https://tom301080.github.io/deskranger-status/).

It runs entirely on GitHub, outside the hosting it watches, so it stays up
when our services are down.

- **Checks** (`.github/workflows/probe.yml`): about every five minutes a job
  calls every service listed in `services.toml`. A check that fails is
  tried again after five seconds before it counts. Slower than
  `degraded_ms` counts as slow.
- **Data**: the branch `data` holds `current.json` (latest result),
  `daily.json` (per day: checks, up, slow, response time; 90 days),
  `events.json` (status changes) and `meta.json`. It is one commit that is
  replaced on every run, so the repository does not grow.
- **Page** (`.github/workflows/publish.yml`): `status/build.py` renders the
  page in German (`/`) and English (`/en/`), an Atom feed (`feed.xml`) and
  `status.json`. It is published when a status changes, at least hourly, and
  on every push to `main`. In the browser the page refreshes the current
  status every minute from the `data` branch.

## Post a notice

Copy `incidents/TEMPLATE.toml.example` to `incidents/<date>-<name>.toml`,
fill it in and push to `main`. Add an `[[update]]` for each step and finish
with `status = "resolved"`. Outages the checks see are listed automatically
("automatisch erkannt") even without a notice.

## Add a service

Add a `[[component]]` with one or more `[[component.check]]` entries to
`services.toml` (`expect_status`, optional `expect_text`). `stage = "test"`
marks services of the test environment.

## Run locally

```sh
python3 status/probe.py --data /tmp/status-data
python3 status/build.py --data /tmp/status-data --out /tmp/status-site
python3 -m unittest discover -s status
```

Standard library only (Python 3.11+). Decision record: ADR 0043 in the
DeskRanger repository.
