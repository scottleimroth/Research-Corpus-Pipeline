# PhD-Level Evaluation & Red-Team Operational Standard

Distilled from MASTER_PROTOCOL.md and audit support files (2026-02). Use at thesis-examination rigor. Apply only standards relevant to design, measures, and era.

## 1. Rating definitions

| Rating | Meaning |
|--------|---------|
| **LANDMARK** | Paradigm-defining or field-changing contribution; applicable standards excellently met; large/defensible sample; measurement and statistics appropriate; honest limitations; survives destructive red-team. "All standards met" alone is **STRONG**, not LANDMARK. |
| **STRONG** | Most applicable standards met to a high level; no fatal flaws; adequate power/sample; clear contribution; survives red-team. |
| **ADEQUATE** | Minimum applicable standards met; notable limitations; smaller sample or confounds; suggestive, not definitive. |
| **WEAK** | Multiple standard violations; underpowered, poorly controlled, outdated methods without acknowledgment, or over-interpreted. |
| **FLAWED** | Fatal methodological/statistical errors; conclusions unsupported; circular reasoning or fundamental design failure. |
| **NOT_APPLICABLE** | Valid corpus item but not an empirical/review research paper (legacy label). |
| **NOT_RATABLE_REFERENCE_MATERIAL** | Books, manuals, handbooks, dictionaries, standards docs, tutorials, protocols-as-reference — ingest for utility, not empirical quality. |

## 2. Evidence rules

- Use the **full text / evidence pack** provided. No abstract-only rating unless input is explicitly insufficient — then note `insufficient_evidence` and do not overclaim.
- Verify **methods match claims** (abstracts and titles mislead).
- Citation count, journal prestige, and author fame **must not** inflate rating — judge what is on the page.

## 3. Article types (classify before rating)

| Type | Rating approach |
|------|-----------------|
| Empirical research | Full LANDMARK→FLAWED scale |
| Review / systematic review / meta-analysis | PRISMA/STROBE/MOOSE as applicable; not penalized as underpowered RCT |
| Theoretical / commentary | Argument quality, evidence cited, scope — not sample size |
| Book / manual / handbook / reference | `not_ratable_reference_material` only |
| Supplement / erratum / child document | Not primary corpus ratings — route via parent linkage |

## 4. Domain standards (apply when relevant)

- **EEG/MEG:** COBIDAS 2020; era-appropriate ERP standards (Picton, Keil, Jasper/Cobb for older work).
- **HRV/ECG/psychophysiology:** Quigley 2024; Task Force / Malik 1996 foundations; reporting of preprocessing and artifact handling.
- **Observational / clinical:** STROBE; CONSORT for RCTs; PRISMA for reviews.
- **Cross-cutting:** APA reporting; open science / preregistration when era-appropriate; effect sizes and uncertainty; reproducibility (data/code).

Judge each paper against standards that **existed and applied** at publication time.

## 5. Historical / foundational context

- Do **not** excuse fatal flaws because a paper is old.
- Do **not** downgrade solely for lacking modern reporting that did not exist then (e.g. preregistration in 1985).
- Record modern limitations clearly in `historical_context_note` / `era_judgment`.
- A historically foundational paper may be **LANDMARK** or **STRONG** for influence if methods were acceptable for its era — **explicit justification required**.

## 6. Non-ratable / reference policy

Books, edited volumes used as reference, product manuals, dictionaries, methods handbooks, and pure standards documents → **`not_ratable_reference_material`**. Record reference/background/methodological utility in justification — not empirical strength. Skip empirical red-team (note "Skipped — non-ratable reference document").

## 7. Calibration anchors (short — not exhaustive)

**LANDMARK examples (audit batches):** Benjamini & Hochberg FDR control; major handbooks/syntheses only when paradigm-defining (e.g. Oxford Handbook of Music and the Brain — verify type: reference vs research chapter); Respiratory Rhythms of the Predictive Mind (empirical/theoretical — verify claims).

**STRONG examples:** Bauer et al. 2015 EEG beta tempo preference; Beauchaine et al. 2019 RSA meta-analysis — verify methods match STRONG bar.

**ADEQUATE:** Typical mid-tier empirical papers meeting minimum standards with clear limitations (majority of rated audit corpus).

**WEAK / likely downgrade cases (audit support):**
- STRONG meta-analysis reclassified when article is synthesis not primary research (e.g. IOM-Caffeine style: STRONG → ADEQUATE).
- Pre-modern reporting judged by era but overstated claims (e.g. Vos-Metric Tones 1973: STRONG → ADEQUATE unless true landmark methods).
- Handbook/editorial volume mis-rated as empirical LANDMARK → reference or lower tier.

Red-team must **attack** initial LANDMARK/STRONG ratings hardest; ADEQUATE may move to WEAK if warranted; WEAK → FLAWED when fatal flaws found.

## 8. Destructive red-team checklist

Attack every STRONG/LANDMARK and scrutinize ADEQUATE:

1. Sample size / power / effect sizes
2. Selection bias / WEIRD samples
3. Controls / confounds / randomization / blinding
4. Measurement validity & reliability (EEG/HRV/ECG preprocessing, electrodes, epochs, HRV task standards)
5. Stimulus characterization & replicability
6. Statistical approach & multiple comparisons
7. Missing data / exclusions / attrition
8. Overclaiming beyond evidence
9. Article type vs rating mismatch (review rated as RCT-quality empirical)
10. Reproducibility (open data/code)
11. Limitations honesty
12. Historical context vs fatal flaw distinction

## 9. Reclassification audit schema

Store under `classification.red_team_audit` (JSON). Required fields:

```json
{
  "original_rating": "strong",
  "final_rating": "adequate",
  "rating_changed": true,
  "change_direction": "downgrade|upgrade|unchanged",
  "change_reason": "string",
  "framework_violation": ["methodology"],
  "confidence": "high|medium|low",
  "red_team_summary": "string",
  "survived_red_team": false,
  "key_attack_points": ["string"],
  "historical_context_note": "string or null",
  "article_type_consistency_note": "string or null",
  "auditor": "claude-sonnet-4-5-20250929",
  "timestamp": "ISO-8601"
}
```

**framework_violation** values (one or more, or `none`): `methodology`, `sample_size`, `statistical_rigor`, `measurement_validity`, `reporting_quality`, `reproducibility`, `overclaiming`, `article_type_mismatch`, `domain_fit`, `historical_context`, `insufficient_evidence`, `none`.

Legacy fields `red_team_notes`, `red_team_survival`, `recommended_rating`, `red_team_downgrade_reason` remain populated for backward compatibility.
