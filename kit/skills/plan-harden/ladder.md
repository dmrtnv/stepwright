# Decide, don't ask

The decision ladder every stage skill and `system.md` point to.

1. Repo answers it (existing pattern, CLAUDE.md/rule docs, memory, types, tests) → follow it; consistency beats local optimum unless the pattern is what's being fixed.
2. A check settles it (typecheck, test, scratch experiment, mutant) → run it; the result decides.
3. Outside facts → research: official docs for the pinned version > installed source > changelog/issues > maintainers > blogs. Each claim: URL + quote + version/date; 2 independent sources if load-bearing. UI/product work: scan 2–3 comparable products or design systems.
4. Choose by: correctness/safety > consistency with codebase > simplicity > reliability > maintainability > performance.
5. Two-way door (revertible, no contract/data change) → decide now. One-way door (schema, stored data, public API/URL, auth/security, new dependency, cost) → compare ≥2 designs, decide, flag ⚑.
6. Ask only owner-only things: business/product rules, money, legal, outward comms, irreversible production data, scope beyond the brief, loosening a gate. Taste with a defensible default → decide + ⚑. Every question carries a recommendation.
