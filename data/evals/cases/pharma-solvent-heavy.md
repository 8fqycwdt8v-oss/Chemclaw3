---
id: pharma-solvent-heavy
metrics: [e_factor, pmi]
output:
  input_masses_kg: [10, 5, 500, 300, 2]
  product_mass_kg: 12
---
Solvent-dominated pharmaceutical step: 500 kg reaction solvent + 300 kg aqueous
workup dwarf the 12 kg product. PMI ≈ 68 and E-factor ≈ 67 both exceed the 50
limit, so the harness flags this as a green-chemistry regression — the case that
proves a gated metric can fail, not only pass.
