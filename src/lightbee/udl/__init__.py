"""Read-only access to the Unified Data Library (UDL).

This package is the only place in Light Bee that may speak HTTP to anything,
and it only ever issues GET. `tools/gates.py` enforces both rules statically;
`tests/test_udl_queries.py::test_a_full_run_only_ever_reads` records every
request a full set of reads makes and enforces them at run time.
"""
