# fx_recon — the distributed driver's end-to-end fixture model (1), muscl's shape (issue #610, PR-B2)

No spec stands behind this file: `test_driver_render.DistributedDriverEndToEndTest` reads it as
the `tests.md` of the IR its builder returns, and holds the IR to this line with gate (o).

distribute: x = grid.nx (ghost grid.ng, extended grid.nx_total), y = grid.ny (ghost grid.ng, extended grid.ny_total)
