# Solax Cloud for Home Assistant

Reads your SolaX inverter's live data from Solax Cloud (realtime API v2) into
Home Assistant, including battery charge and discharge energy for the energy
dashboard.

The data comes from the cloud, so it updates as often as your Pocket WiFi/LAN
dongle uploads (typically every 5 minutes). The integration polls every
minute and ignores repeated readings of the same upload.

## Installation

1. In HACS, add `https://github.com/flo-az/Home-assistant-Solax-cloud` as a
   [custom repository](https://hacs.xyz/docs/faq/custom_repositories/) of
   type *Integration*.
2. Download **Solax Cloud** and restart Home Assistant.
3. Add the integration under *Settings → Devices & services*.

## Configuration

In Solax Cloud, open the API page (the **API** button at the top of the
page). You need:

| Field | Where to find it |
|---|---|
| API address | Shown on the API page, e.g. `https://global.solaxcloud.com` |
| Token ID | Shown on the API page |
| Dongle serial number | Registration number of the Pocket WiFi/LAN dongle, **not** the inverter serial (Solax Cloud lists it under *Devices*, type *Dongle*) |

If Solax Cloud later refuses the token (for example after you regenerate it,
or a token from before the v2 API), Home Assistant asks for a new one under
*Settings → Devices & services*.

## Sensors

One sensor per field of the realtime API, plus two computed ones:

| Sensor | Unit | Notes |
|---|---|---|
| AC power | W | Inverter output |
| Total solar power | W | Sum of all MPPT inputs |
| MPPT1 power, MPPT2 power | W | MPPT3/MPPT4 exist but are disabled by default |
| Feed-in power | W | Power to the grid |
| Battery power | W | Positive while charging, negative while discharging |
| State of charge | % | |
| Battery status | | Normal, fault, disconnected |
| Battery charge energy, Battery discharge energy | kWh | Computed, see below |
| Yield today, Yield total | kWh | Solar production |
| Feed-in energy | kWh | Total exported to the grid |
| Grid import energy | kWh | Total imported from the grid |
| EPS phase 1–3 power | W | Backup output |
| Meter 2 power | W | Disabled by default |
| Inverter status | | Normal, standby, off-grid, … ([full list](custom_components/solax_cloud/sensor.py)) |
| Upload time | | When the data was uploaded (UTC timestamp) |
| Last cloud upload | | The same, as the plant's local time text |
| Inverter serial, Dongle serial | | |

Values the API does not report for your system (for example no battery) are
*unknown*. While Solax Cloud is unreachable, all sensors are *unavailable*.

### Battery energy

The API only reports the battery's power at each upload. *Battery charge
energy* and *Battery discharge energy* add it up: each interval between two
uploads counts the average of their two readings. Gaps longer than 15
minutes (Home Assistant or the cloud was down) are skipped rather than
guessed, and the totals survive restarts. They are estimates: compare them
with the daily charged/discharged values in the Solax app.

## Energy dashboard

Under *Settings → Dashboards → Energy*:

| Section | Setting | Sensor |
|---|---|---|
| Electricity grid | Grid consumption | Grid import energy |
| Electricity grid | Return to grid | Feed-in energy |
| Solar panels | Solar production | Yield total |
| Home battery storage | Energy going in to the battery | Battery charge energy |
| Home battery storage | Energy coming out of the battery | Battery discharge energy |
| Home battery storage | Battery power (optional) | Battery power, **inverted** (it is positive while charging) |
| Home battery storage | State of charge (optional) | State of charge |

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
```

The tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
with recorded API responses, plus [Hypothesis](https://hypothesis.readthedocs.io/)
property tests that feed the integration arbitrary API behaviour.
`requirements_test.txt` pins the Home Assistant version the tests run against.

API reference: *SolaXCloud User API V1.2*, linked from the API page in Solax
Cloud.

## Credits

Based on [frank8the9tank/Home-assistant-Solax-cloud](https://github.com/frank8the9tank/Home-assistant-Solax-cloud).
