# Seasonal flag and raster: parity with the predecessor

Bluestaq Limited. UNCLASSIFIED. 10 November 2026.

**Result: agreement on every case.** Light Bee's NumPy `seasonal_flag` and `build_grid` were run beside the predecessor's pandas `seasonal_comparison` and `build_grid` (its `src/smg/grid.py`, unmodified) on the same inputs.

| Check | Cases | Agreement |
|---|---|---|
| Verdict, comparable bins and median offset | 60 randomised synthetic objects (steps from -0.5 to 1.0 mag, relocations, 2 to 60 observations a night, added scatter) | 60 of 60 |
| Raster values, counts and daily longitude | 60 days, 3,600 observations | identical (values to 1e-5 mag) |

Method: `parity_seasonal.py.txt` in this folder (renamed so it is not analysed or shipped), run in a scratch environment with pandas 2.2.3 and numpy 2.4.6. Light Bee itself carries no pandas.

Deliberate difference from the build plan: the raster draws every observed cell (no 3-point minimum), as the predecessor did, so the heatmap matches the reference plots. The 3-point minimum applies to the seasonal comparison bins, as in the predecessor.
