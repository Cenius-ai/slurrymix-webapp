# Using Slurrymix

Every command below is real output from the shipped tool. Run them from the
directory that holds your `slurrymix.json` (or add `--state PATH` to any of them).
Throughout, `slurrymix` means `python3 slurrymix.py` (or the console command after
`install.sh`).

## 1. Register the field

```bash
slurrymix field add --name "North 12" --area 4.5 --crop wheat --target-n 170
```

```
added field North 12 (#1): 4.5 ha of wheat, target 170 kg N/ha (765 kg N this season)
```

`--target-n` is the nitrogen the crop is allowed over the whole season; Slurrymix
multiplies it by the area to get the field's season budget (765 kg N here).

```bash
slurrymix field list
```

```
Fields (1)
ID  NAME      AREA (ha)  CROP   TARGET (kg N/ha)
──  ────────  ─────────  ─────  ────────────────
 1  North 12        4.5  wheat               170
```

With nothing stored yet, the command invites the exact next step:

```
$ slurrymix field list
no fields yet - register one with:
  slurrymix field add --name "North 12" --area 4.5 --crop wheat --target-n 170
```

## 2. Record what your slurry is worth

```bash
slurrymix batch add --label "Pit A March" --source cattle --n 4.2 --dm 8.5 --sampled 2026-03-02
slurrymix batch list
```

```
added batch Pit A March (#1): cattle slurry, 4.2 kg N/m3, 8.5% dry matter

Slurry batches (1)
ID  LABEL        SOURCE  N (kg N/m3)  DRY MATTER (%)  SAMPLED
──  ───────────  ──────  ───────────  ──────────────  ──────────
 1  Pit A March  cattle          4.2             8.5  2026-03-02
```

`--source` accepts `cattle`, `pig`, `poultry` or `other`; `--dm` must be between 0
and 100; `--sampled` is an ISO date. A duplicate label is refused by name.

## 3. Define the store and its level

```bash
slurrymix storage add --name "Lagoon 1" --capacity 1200
slurrymix storage set --name "Lagoon 1" --level 950
slurrymix storage
```

```
added storage Lagoon 1 (#1): capacity 1200 m3, level 0 m3
Lagoon 1: level 950 m3 of 1200 m3 (79% full, 250 m3 free)

Storage units (1)
ID  NAME      LEVEL      CAPACITY  FREE       PERCENT FULL      FLAG
──  ────────  ─────────  ────────  ─────────  ────────────────  ────
 1  Lagoon 1  950 m3     1200 m3   250 m3     [########--] 79%
```

Going over capacity is flagged, warned about on stderr, and **still stored** — the
reading is real, and you need to see it:

```
$ slurrymix storage set --name "Lagoon 1" --level 1300
warning: Lagoon 1 is over capacity by 100 m3 (108%) - plan spreads to draw it down
Lagoon 1: level 1300 m3 of 1200 m3 (108% full, -100 m3 free)
```

## 4. Work out how much to spread

Inline values when you have a lab figure in front of you:

```bash
slurrymix mix --n 4.2 --area 3 --target 170
```

```
Slurry mix - nitrogen target
INPUT             VALUE        SOURCE
────────────────  ───────────  ───────────────
nitrogen content  4.2 kg N/m3  inline --n
field area        3 ha         inline --area
target rate       170 kg N/ha  inline --target
target nitrogen   510 kg N     rate x area

Result
QUANTITY            VALUE
──────────────────  ───────────────────────────────────────
required slurry     121.4 m3
per-hectare rate    40.5 m3/ha
nitrogen delivered  509.9 kg N (170 kg N/ha)
applied vs target   target 170 kg N/ha - shortfall 0.1 kg N
```

Or from the records you already keep, which also reports the budget left afterwards:

```bash
slurrymix mix --field "North 12" --batch "Pit A March"
```

```
required slurry     182.1 m3
per-hectare rate    40.5 m3/ha
nitrogen delivered  764.8 kg N (170 kg N/ha)
remaining season budget for North 12 after this spread: 0.2 kg N (in hand)
note: nitrogen taken from stored batch 'Pit A March' (4.2 kg N/m3)
```

The volume is rounded to 0.1 m3, so the delivered nitrogen can differ from the target
by a tenth of a kilogram — that is the surplus/shortfall line you see above.

Refusals are specific and name the flag:

```
$ slurrymix mix --n 0 --area 3 --target 170
error: n must be greater than 0
$ slurrymix mix --area abc
error: area must be a number
hint: for example --area 4.2
$ slurrymix mix --n 4,2 --area 3 --target 170
error: n must be a number - use a dot as the decimal separator
```

## 5. Plan the season

```bash
slurrymix plan add --field "North 12" --batch "Pit A March" --storage "Lagoon 1" --date 2026-03-14
slurrymix plan add --field "South 3" --batch "Lagoon B April" --storage "Slurry pit" --date 2026-04-02 --volume 60
slurrymix plan
```

```
planned entry #1: 182.1 m3 (calculated from the field's nitrogen target) of Pit A March onto North 12 from Lagoon 1 on 2026-03-14 - 765 kg N
planned entry #2: 60 m3 (from --volume) of Lagoon B April onto South 3 from Slurry pit on 2026-04-02 - 216 kg N

Rotation plan (2 planned)
ID  DATE        FIELD      BATCH           STORAGE     VOLUME (m3)  N (kg N)  STATUS
──  ──────────  ─────────  ──────────────  ──────────  ───────────  ────────  ───────
 1  2026-03-14  North 12   Pit A March     Lagoon 1          182.1       765  planned
 2  2026-04-02  South 3    Lagoon B April  Slurry pit           60       216  planned

total planned 242.1 m3 / 981 kg N over 2 entry(s); 0 completed
```

Leave `--volume` off and the volume is calculated to hit the field's full nitrogen
target with that batch; the output says so. Entries are listed by date, not by the
order you typed them.

If an entry would push the field past the nitrogen it is still allowed this season,
Slurrymix stores it anyway and warns:

```
warning: North 12 exceeds its remaining 40 kg N this season by 50 kg N
```

Change your mind? `slurrymix plan remove --id 2` removes an entry that has not been
completed. A completed spread cannot be removed — it is a record now.

## 6. Complete a spread

```bash
slurrymix plan done --id 1
slurrymix storage
```

```
entry #1 done: 182.1 m3 / 765 kg N of Pit A March applied to North 12 on 2026-03-14
Lagoon 1 is now 767.9 m3 of 1200 m3 (64% full, 432.1 m3 free)
North 12 season 2026: 765 kg N applied against a 765 kg N target
```

The application record, the entry's `done` status and the storage drawdown are all
in one atomic write — a crash cannot leave a spread logged but the store un-credited.
If the store was short of slurry, the level is clamped at 0 m3 with a warning instead
of going negative. `slurrymix plan` now lists only outstanding work; `slurrymix plan
--all` shows the completed entries too.

## 7. Read the season back at a glance

```bash
slurrymix status
```

```
Slurrymix status - season 2026
RECORDS                       COUNT
────────────  ─────────────────────
fields                            3
batches                           2
storages                          2
plan entries  3 (2 planned, 1 done)
applications                      1

SEASON TOTALS        VALUE
────────────────  ────────
planned volume      180 m3
planned nitrogen  720 kg N
applied volume    182.1 m3
applied nitrogen  765 kg N

Nitrogen per field (season 2026)
FIELD        TARGET (kg N)  PLANNED (kg N)  APPLIED (kg N)  REMAINING (kg N)  APPLIED OF TARGET
───────────  ─────────────  ──────────────  ──────────────  ────────────────  ─────────────────
North 12               765               0             765                 0               100%
South 3                300             216               0                84                 0%
Riverside 7            960             504               0               456                 0%

Storage levels
NAME        LEVEL (m3)  CAPACITY (m3)  FREE (m3)  PERCENT FULL      DAYS OF SPREADING
──────────  ──────────  ─────────────  ─────────  ────────────────  ────────────────────
Lagoon 1         767.9           1200      432.1  [######----] 64%  7 days at 120 m3/day
Slurry pit         380            450         70  [########--] 84%  7 days at 60 m3/day
```

**Days of spreading** is the remaining outstanding volume for that store divided by
the days it is planned over, giving an m3/day rate; the days figure is how long the
slurry currently in the store lasts at that rate. A store with no outstanding spreads
prints `no planned spreads` rather than a guessed number.

For scripts, `slurrymix status --json` prints one object with the keys `fields`,
`batches`, `storages`, `plan` and `totals`. `--json` also works on `mix`, `field list`,
`batch list`, `plan`, `storage` and `export`.

## 8. Hand the numbers to a spreadsheet

```bash
slurrymix export --out .
```

```
wrote plan.csv (3 row(s))
wrote storage.csv (2 row(s))
```

```
id,planned_date,field,batch,storage,volume_m3,n_kg,status
1,2026-03-14,North 12,Pit A March,Lagoon 1,182.1,765,done
...
```

UTF-8 with a header row, quoted correctly (a field name containing a comma and a
double quote round-trips through any spreadsheet). An existing file is not touched
unless you pass `--force`:

```
$ slurrymix export --out .
error: plan.csv exists; pass --force to overwrite
```

## 9. The guided wizard

```bash
slurrymix wizard
```

```
Slurrymix wizard - record a batch, plan a spread, check storage
press Enter to accept the value in brackets; Ctrl-C exits without writing anything

Batch label [Pit B April]:
Slurry source (cattle|pig|poultry|other) [cattle]:
Nitrogen content kg N/m3 [4.2]:
Dry matter % (0-100, blank to skip) [8.5]:
Field to spread on (North 12, South 3, Riverside 7) [North 12]:
Storage unit (Lagoon 1, Slurry pit) [Lagoon 1]:
Planned date [2026-10-15]:
Volume m3 [182.1]:
```

Every prompt takes the default with Enter, and the volume default is the mix
calculation for the chosen field. A validation message is the same text the flag
command would print, and the wizard re-asks instead of exiting. The state file is
written **once, at the end**: Ctrl-C or a closed input leaves it exactly as it was and
exits with code 130. With no fields or storage units yet, the wizard records the batch
and tells you the two commands to run before planning.

## Where things are stored

```json
{
  "schema_version": 1,
  "units": "metric",
  "updated_at": "2026-03-14T09:12:41+00:00",
  "fields": [ { "id": 1, "name": "North 12", "area_ha": 4.5, "crop": "wheat", "target_n_kg_per_ha": 170.0 } ],
  "batches": [ { "id": 1, "label": "Pit A March", "source": "cattle", "n_kg_per_m3": 4.2, "dry_matter_pct": 8.5, "sampled_on": "2026-03-02" } ],
  "storages": [ { "id": 1, "name": "Lagoon 1", "capacity_m3": 1200.0, "level_m3": 767.9, "updated_at": "..." } ],
  "plan": [ { "id": 1, "field_id": 1, "batch_id": 1, "storage_id": 1, "planned_date": "2026-03-14", "volume_m3": 182.1, "n_kg": 765.0, "status": "done" } ],
  "applications": [ { "id": 1, "entry_id": 1, "field_id": 1, "batch_id": 1, "applied_on": "2026-03-14", "volume_m3": 182.1, "n_applied_kg": 765.0 } ]
}
```

Records refer to each other by id, so renaming a field propagates through the plan,
and a hand-edit that leaves a dangling id is reported by name instead of crashing.
Back the file up by copying it; keep a season per directory if that suits you.
