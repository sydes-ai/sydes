"""Behavioral (runtime) evidence for a change, consumed from DiffGenome.

Sydes' own structural analysis says what *may* be connected. DiffGenome's
`diffgenome-change/1` artifact says what was *observed* to execute in isolated
tests and what can be *reconstructed* across stand-in seams, with the evidence
grade of every reconstruction. This package merges the two without flattening
them: every merged edge keeps its evidence class, and a static path DiffGenome
never executed stays a possible path, never a proven one. No framework or
runtime knowledge lives here — that stays inside DiffGenome's adapters.
"""
