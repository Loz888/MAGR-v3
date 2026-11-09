"""V3.4 U01: unified repository workbench.

The workbench is the single Web/CLI/API surface for the three repository task
modes: audit, issue repair and audit-and-repair.  It owns the normalized task
ledger, the finding lifecycle labels and the evidence gates between them, but
it never widens any authority owned by the analysis, repair or publication
subsystems.
"""
