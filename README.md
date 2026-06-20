# Research Corpus Pipeline

<!-- Public-release provenance marker: Scott Leimroth, Copyright 2026. -->

Portable empty starter corpus for ingesting, rating, and searching research PDFs.

This public copy contains no private paper database, no source PDFs, no vector DB, and no API keys. A new user creates their own database from scratch.

## What It Does

Research Corpus Pipeline turns a folder of research PDFs into a searchable, rated literature corpus.

The basic workflow is simple:

1. Collect PDFs of papers you are interested in.
2. Put those PDFs into `papers-staging/`.
3. Run `RUN.bat`.
4. The pipeline ingests the PDFs, extracts metadata and text, evaluates each paper, stores the results in SQLite, and keeps managed copies of accepted source PDFs.
5. Run `BUILD_VECTOR.bat` or `UPDATE_VECTORDB.bat` to build semantic search over the corpus.
6. Use `SEARCH.bat` to interrogate the corpus by meaning, not just by keyword.

The important part is that the pipeline does not just dump every PDF into a database. It builds an evidence package for each document, decides whether it is a ratable research paper, reference item, supplement, erratum, malformed file, duplicate, or other corpus item, and records why that decision was made.

For ratable research papers, the evaluator assigns a quality rating:

- `landmark`
- `strong`
- `adequate`
- `weak`
- `flawed`

For useful non-paper material, it can keep the document without pretending it is an empirical paper:

- `not_ratable_reference_material`
- `not_applicable`

## What Gets Rated

The rating system is built around a PhD-level evaluation and red-team standard. It looks at the paper itself, not just the title, abstract, journal name, citation count, or author reputation.

The rating variables are grounded in guideline papers and reporting standards for quality scientific publishing: the kinds of guidelines that describe what a well reported, methodologically sound paper should include. The aim is to check whether the paper gives enough information to assess its design, methods, statistics, transparency, limitations, and reproducibility.

The pipeline extracts and evaluates variables such as:

- article type, including empirical paper, review, meta-analysis, systematic review, theory paper, commentary, book, reference work, supplement, or erratum
- title, authors, year, journal, DOI, publisher, language, volume, issue, and pages
- abstract and keywords
- ethics approval, consent, privacy/anonymisation, and vulnerable-population handling
- total sample size, age, sex breakdown, population type, clinical group details, recruitment method, and WEIRD-sample bias
- study design, controls, randomisation, blinding, counterbalancing, order effects, attrition, exclusions, and missing-data handling
- statistical approach, exact tests used, multiple-comparison correction, effect sizes, power analysis, and sample-size justification
- open data, open code, funding declarations, and conflicts of interest
- measurement quality, including reliability, validity, pilot testing, manipulation checks, and domain-specific measurement standards
- stimulus materials, procedure detail, equipment/software specifications, and whether the study can be replicated
- analysis pipeline, calibration procedures, specific techniques, and code availability
- reporting standards, limitations, generalisability, data availability, competing interests, and author-contribution reporting
- review/meta-analysis-specific checks such as registration, risk of bias, heterogeneity, publication bias, and sensitivity analyses
- supplements and parent-child document relationships
- domain tags, methods tags, construct tags, population tags, paradigm tags, stimulus tags, and analysis tags

It also applies a destructive red-team pass to stress-test ratings, especially generous ratings. That pass attacks the paper for sample size, statistical rigor, measurement validity, controls, bias, reproducibility, overclaiming, article-type mismatch, missing evidence, and era-appropriate standards. If the red team finds that the first rating is too generous, the final stored rating is downgraded.

The result is a rated corpus that makes quality, method, and limitations easier to compare across papers.

## Quick Start

1. Double-click `SETUP.bat`.
2. Choose an AI provider when asked.
3. Put PDFs into `papers-staging/`.
4. Double-click `RUN.bat`.
5. After papers are added, run `BUILD_VECTOR.bat` or `UPDATE_VECTORDB.bat` for semantic search.

## AI Provider Choice

The setup asks which AI provider to use.

Recommended for most people:

`OpenRouter`

OpenRouter is a single top-up account that can use cheaper AI models. It supports both normal paper text evaluation and rare scanned/image PDF checks through one key.

Setup explained:

1. Go to `https://openrouter.ai`
2. Create an account
3. Add a small credit amount, such as 5-10 USD
4. Create an API key
5. Paste it into setup

Other choices:

- `DeepSeek`: cheap and good for normal text PDFs, but limited vision/scanned-PDF support.
- `OpenAI`: good text and vision if the user already has an OpenAI key, but usually more expensive.
- `Anthropic`: strong quality if the user already has a Claude/Anthropic key, but usually more expensive.
- `Local/free mode`: no API bill, but requires local AI software and is harder to set up.

If unsure, choose `I'm not sure` in setup and it will explain the choices.

## MetaCheck

This copy includes the Real MetaCheck/GROBID integration.

The corpus pipeline and paper-rating workflow were already developed and working before this MetaCheck integration was added. MetaCheck was later integrated because it is a useful fit: it adds another structured best-practice check to the existing evaluation pipeline and helps make the corpus assessment more methodologically robust.

MetaCheck is a ScienceVerse project for checking research outputs for best-practice signals. ScienceVerse is led by Lisa DeBruine and Daniel Lakens, with broader contributor support. GROBID is used to convert PDFs into structured TEI XML before MetaCheck-style checks run.

For ratable research-paper PDFs:

- GROBID converts the PDF to TEI XML.
- Real MetaCheck runs on the GROBID output.
- The SQLite DB records MetaCheck status and evidence.

For books, translations, supplements, conference items, errata, and other non-ratable items:

- They can still be kept in the corpus where useful.
- They are not rated as research papers.
- MetaCheck is marked `not_applicable`.

For scanned/image/malformed PDFs:

- The corpus pipeline still tries OCR and normal processing.
- If GROBID cannot process the PDF, the DB records `technical_unavailable`.
- This is not a MetaCheck pass or fail; it is a transparent missing-evidence note.

## Main Files

| File/folder | Purpose |
|---|---|
| `SETUP.bat` | Run once on a machine. Installs tools and guides AI provider setup. |
| `RUN.bat` | GUI ingest runner. |
| `RUN_NoGUI.bat` | Console ingest runner. |
| `papers-staging/` | Put new PDFs here. |
| `papers-rejected/` | Files not added to the corpus. |
| `_system/CorpusStore/papers.db` | The user's SQLite database. Starts empty. |
| `_system/CorpusStore/source-pdfs/` | Managed copies of accepted PDFs. Starts empty. |
| `BUILD_VECTOR.bat` | Full vector DB build. |
| `UPDATE_VECTORDB.bat` | Incremental vector DB update. |
| `SEARCH.bat` | Search UI. |

## Privacy And Keys

API keys are stored encrypted in `_system/secrets/api_keys.env.enc`.

The vault may contain OpenRouter, DeepSeek, OpenAI, or Anthropic keys. The setup asks the user for a passphrase. The passphrase is not stored.

This public folder should be shared only after confirming:

- no files in `_system/CorpusStore/source-pdfs/`
- empty `papers.db`
- no vector DB
- no logs/cache/backups
- no encrypted or plaintext personal key files

## Attribution And Copyright

This public starter template was prepared by Scott Leimroth.

Copyright 2026 Scott Leimroth. All rights reserved unless a later repository license states otherwise.

This project integrates with, but does not own, MetaCheck, GROBID, Python packages, Docker images, or other third-party tools. Those projects keep their own names, authorship, and licences.
