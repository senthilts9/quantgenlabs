"""Crypto desk tests, carried over from senthilts9/cryptodesk.

A PACKAGE, not a plain directory, for a specific reason: this suite has its own
test_execution.py and so does backend/tests/ -- one covers the crypto order path,
the other covers TWAP/VWAP/POV scheduling. Without this __init__.py pytest
imports both as the top-level module `test_execution` and the second one silently
shadows the first.

NAMED crypto_desk, NOT cryptodesk. tests/ has no __init__.py, so pytest puts it
on sys.path -- and a directory named `cryptodesk` there shadows the actual
package, making every `from cryptodesk.core import ...` resolve to this test
directory instead. The failure is a confusing ModuleNotFoundError for
cryptodesk.core on a package that plainly has a core/.

These run against backend/cryptodesk/, which is AHEAD of the upstream repo: same
39 modules plus seven more (execution/algos, tca, persistence, marketdata/perps,
history, risk/oracle, venues/coinbase_exchange) and a store.py roughly 2.5x the
size. All 249 pass against it unchanged, which is the useful fact -- the upstream
tests still describe this code correctly, so they are a real regression net and
not a snapshot of something that has moved on.

Offline and deterministic: no network, no credentials, no venue keys.
"""
