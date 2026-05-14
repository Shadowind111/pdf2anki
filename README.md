# pdf2anki

Turn medical PDFs into Anki Cloze cards with OCR, visual chart parsing, source tracking, and cloze-quality repair.

`pdf2anki` is a desktop tool for medical learners, residents, and clinicians who want to convert guidelines, consensus statements, papers, manuals, and books into reviewable Anki cards. It extracts text from PDF pages, optionally calls a vision-capable model for OCR and tables/flowcharts, then runs a three-stage LLM workflow:

1. Extract medical knowledge units.
2. Design memory-oriented cards.
3. Review and repair Cloze deletions.

## Features

- PDF text extraction with OCR fallback for scanned or garbled pages.
- Optional visual parsing for tables, algorithms, flowcharts, and embedded images.
- Provider-agnostic OpenAI-compatible API settings.
- Separate text and vision/OCR model settings, or reuse one multimodal API for both.
- Proxy/relay-site support with independent proxy Base URL fields.
- Guided PyQt6 workbench UI with a workflow rail, task summary, OCR preview, and clearer run log.
- Local config saving in `pdf2anki_config.json`.
- Resume support with local SQLite state.
- Anki `.apkg` export with page number, source excerpt, knowledge type, and clinical note on the back.
- Cloze rules tuned for clinical learning: avoid isolated low-value blanks, preserve compound criteria, and add front-card context from PDF outlines when available.

## Repository Metadata

- Repository name: `pdf2anki`
- Short description: `Medical PDF to Anki Cloze card generator with OCR, visual parsing, and clinical cloze QA.`
- Topics: `anki`, `medical-education`, `pdf`, `ocr`, `pyqt6`, `llm`, `flashcards`, `cloze`, `clinical-medicine`, `openai-compatible`

## Installation

Python 3.10+ is recommended.

```powershell
git clone https://github.com/YOUR_NAME/pdf2anki.git
cd pdf2anki
py -m pip install -r requirements.txt
```

## Quick Start

```powershell
py .\pdf2anki.py
```

Then:

1. Follow the left workflow rail: choose a PDF, configure models, then generate cards.
2. Fill in a text-model API key, model name, and Base URL.
3. If your text model is also vision-capable, keep `视觉/OCR 复用文本接口` checked.
4. If you use a proxy or relay site, check `文本使用代理` or `视觉使用代理` and enter the proxy Base URL.
5. Start with a small page range, such as 3-5 pages.
6. Watch the right-side run summary and OCR preview while cards are generated.
7. Import the generated `.apkg` file into Anki.

## API Configuration

The app expects OpenAI-compatible `chat/completions` APIs.

For a direct official endpoint:

- Base URL example: `https://api.openai.com/v1`
- The app automatically calls `/chat/completions`.

For a proxy or relay site:

- Check `文本使用代理` or `视觉使用代理`.
- Enter the proxy Base URL, for example `https://your-relay.example.com/v1`.
- Do not include `/chat/completions` unless your provider explicitly requires it; the app normalizes both formats.

The real runtime config is ignored by Git. You can copy the example:

```powershell
copy .\pdf2anki_config.example.json .\pdf2anki_config.json
```

## Privacy and Safety

Do not commit:

- API keys
- private PDFs
- generated `.apkg` decks
- `pdf2anki_state.db`
- `pdf2anki_config.json`

Medical disclaimer: generated cards are study aids only. Always verify clinically important facts against the original source and qualified medical judgment.

## Development

Basic checks:

```powershell
py -m py_compile .\pdf2anki.py
```

Project structure:

```text
pdf2anki/
  pdf2anki.py
  requirements.txt
  pdf2anki_config.example.json
  README.md
  LICENSE
  .gitignore
  docs/
  examples/
```

## Roadmap

- Manual card review/edit queue before export.
- More provider presets.
- Automated regression tests for cloze quality.
- Specialty-specific prompt presets.
