"""Fleet Manager turns: a fleet message runs as a bus turn and ends with exactly one terminal (P1a); the
terminal and every late reply go back to the bound Fleet Manager through a signed, durable outbox (P1b).

Design: fleet-manager docs/plan/improve-fleet-member/native-parity.md (sections 3.3.1-3.3.15).
"""
