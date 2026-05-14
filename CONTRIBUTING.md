# Contributing

Thanks for helping improve `pdf2anki`.

## Good First Contributions

- Improve prompt templates for specific specialties.
- Add tests for deterministic Cloze cleanup rules.
- Add provider presets for OpenAI-compatible APIs.
- Improve UI preview and card review workflows.
- Add documentation screenshots.

## Development Setup

```powershell
py -m pip install -r requirements.txt
py -m py_compile .\pdf2anki.py
```

## Pull Request Notes

- Do not commit API keys, local config, PDFs, databases, or generated decks.
- Keep medical claims traceable to source text.
- Prefer small, reviewable changes.
- Include before/after examples for Cloze-quality changes.
