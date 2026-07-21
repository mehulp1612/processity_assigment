"""Pure domain logic: money rounding and GST computation.

Nothing in this package touches the DB, the network, or the agent. It is the
part of the system whose correctness we can prove with plain unit tests, so the
money math is trustworthy before any tool or model is wired up.
"""
