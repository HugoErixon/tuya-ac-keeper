# Tuya AC Keeper

En liten styrtjanst for att halla en fast temperatur med en Tuya-styrd AC och externa temperaturgivare. Den:

- laser en eller flera separata sensorer
- aggregerar dem till en styrtemperatur
- styr AC:n via konfigurerbara Tuya-DP:er
- loggar sensorvarden, AC-status och styrbeslut till SQLite
- exponerar ett litet HTTP-API som en traningsdashboard kan lasa

Projektet startar i simulatorlage, sa logik och dashboardflode kan testas innan riktiga Tuya-nycklar och DP-nummer ar ifyllda.

## Snabbstart

```powershell
cd C:\Users\Hocke\Documents\Codex\2026-06-14\jag-vill-att-vi-ska-bygga\work\tuya-ac-keeper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
Copy-Item config.example.yaml config.yaml
ac-keeper once --config config.yaml
ac-keeper api --config config.yaml
```

API:t hamnar som standard pa `http://127.0.0.1:8089`.

Stoppa en bakgrundsstartad API-process med:

```powershell
Stop-Process -Id (Get-Content .\data\api.pid)
```

## Dashboard-endpoints

- `GET /health` - enkel driftstatus
- `GET /api/current` - senaste sensorvarden, AC-status och senaste styrbeslut
- `GET /api/readings?hours=24` - tidsserie for temperaturer
- `GET /api/control-events?hours=24` - historik over styrbeslut
- `GET /api/export.csv?hours=24` - CSV-export for dashboard/import
- `POST /api/control/once` - kor ett styrvarv direkt

SQLite-databasen ligger enligt config i `data/ac_keeper.sqlite`.

## Koppla pa Tuya

1. Kor Tuya-discovery enligt TinyTuya-flodet:

   ```powershell
   python -m tinytuya wizard
   python -m tinytuya scan
   ```

2. Fyll i `ac.provider: tinytuya`, `device_id`, `address`, `local_key`, `version` och DP-nummer i `config.yaml`.

3. Fyll i externa sensorer under `sensors`. De kan vara `tinytuya`, `http_json` eller `simulated`.

4. Borja med `controller.dry_run: true`. Nar loggen visar rimliga beslut, satt `dry_run: false`.

Tuya-DP:er skiljer sig mellan AC-modeller. Vanliga fardpunkter ar `power`, `mode`, `target_temperature` och ibland `current_temperature`, men exakt nummer och temperatur-skala maste verifieras per enhet.

## Kontrollmodell

Styrningen ar medvetet enkel:

- over `target_c + hysteresis_c`: AC pa i kyla
- under `target_c - hysteresis_c`: varme om `heat_enabled` ar sant, annars av
- inom bandet: hall nuvarande lage
- `min_cycle_seconds` hindrar snabb pa/av-cykling

Forsta riktiga installationen bor koras i `dry_run` i minst ett par timmar sa sensorplacering, skalning och hysteresis kan kontrolleras mot verkliga rumsvarden.
