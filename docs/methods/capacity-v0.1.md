# Capacity and GPU estimate v0.1 (model v0)

Written 2026-10-21. Code: `services/marden/models/capacity.py`, `capacity_methods.py`. Model `capacity`, version `0.1`. Spec 27.

## Output

One `CapacityEstimate` per campus and per building (every one). Each carries `missing`. A building's estimate uses its own stated MW (`facility_mw` on the building: Filed / reported), its **campus share** (the campus's stated facility MW x the building's share of the campus's traced footprint area, +/- 20%; an estimate, never shown as a stated per-building figure), and its footprint (stated `sqft`, else the polygon, reported as `footprintSqft`) x developer density; the generation is the campus's. `racks` = GPUs / 72 for GB200 / GB300 NVL72 (`gpusPerRack`). The `dc_tab:capacity` view lists the campus, then its buildings.

## Step 1: facility MW

Every method yields a low / base / high range with its evidence rows. A method with no inputs is not shown.

| Method | Inputs | Range |
| --- | --- | --- |
| Filed | campus `facility_mw` | the figure. Only tag F is "filed": it is the answer and `replacedByFiling`. An R figure (press, Epoch) is a reported check |
| Utility load | `large_load_request` {mw}, `grid_mw` | smaller to larger of the two |
| Prime generation | plants serving the campus (`plant_serves`, not Denied / Withdrawn): unit rows (count × `rating_mw_each`, diesel excluded as backup) and `capacity_mw` | smaller figure less one unit (N+1) to the larger |
| Backup generators | `backup_generator_count` | n/d: no permit Marden holds states the standby rating or redundancy, so no MW is derived |
| Campus share (building) | campus `facility_mw` x the building's share of the campus's traced footprint area | +/- 20% |
| Footprint | `sqft_total` (campus) or the footprint polygon area (building) × facility MW per sq ft of the same developer's campuses that state both, else every such campus (quartiles) | weakest |
| Disclosed GPU count | `chips` × kW per GPU ÷ GPU share of IT × PUE | implied MW |

Cooling capacity is not collected, so that method is not used.

**Combining:**

- A filed (F) figure is the answer (confidence High).
- Otherwise take the two strongest methods with figures, in spec 27's order: reported facility MW, utility load, prime generation, GPU count, footprint.
  - If they overlap, the range is the overlap.
  - If not, it is the span of the two, centred on the stronger.
  - Confidence is Medium when their figures are within 25% of each other, else Low.
- `disagreement` is set when any two methods differ by more than 25%.

## Step 2: IT MW

IT MW = facility MW ÷ PUE. PUE is the published data-center range (series `data_center_pue`: 1.10–1.54, base 1.32). Low facility MW is divided by high PUE, and the reverse.

## Step 3: GPUs and value

- **Generation**: the campus's disclosed chip types (the largest count), else by online year: 2026 or later GB300, 2025 GB200, earlier H100. The year rule is labelled in the methodVersion.
- **kW per GPU**: the chip's published power (`gpu_model:<chip>` tdp_w, tag R).
- **GPU share of IT**: calibrated, leave-one-out, on every campus that discloses both chips and IT MW. Base is the median, the range the quartiles. On 2026-10-21: 24 campuses, base 0.478, range 0.18–0.58.
- **GPUs** = IT MW × share ÷ kW per GPU.
- **Value** = GPUs × the chip's new-price range (`gpu_model:<chip>` new_price_usd), in $B.

## Calibration (why "v0")

- On 2026-10-21, 49 campuses have both a GPU estimate and a disclosed chip count. The base estimate is off by a median 62% (interquartile 33–78%). Abilene: 435k estimated against 252k disclosed (B200 / B300).
- Of 102 campuses: 0 have a filed (F) facility MW, 12 are Medium confidence and 90 Low; 68 have disagreeing methods.
- The label stays until permit generator ratings, utility filings and cooling capacity are collected, and the share is fitted against filed counts.

## Runs

Weekly, Monday 09:45 UTC. Also within a minute of a campus or building capacity fact, a serving plant's capacity or units, or the GPU / PUE series.
