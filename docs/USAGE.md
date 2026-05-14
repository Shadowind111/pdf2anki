# Usage Guide

## Recommended Workflow

1. Install dependencies with `py -m pip install -r requirements.txt`.
2. Run `py .\pdf2anki.py`.
3. Use the left workflow rail to move through the app: choose a PDF, configure models, then generate cards.
4. Configure a text model API key, model name, and Base URL.
5. If one API supports both text and images, keep `视觉/OCR 复用文本接口` enabled.
6. If you use a proxy or relay site, enable `文本使用代理` or `视觉使用代理` and enter the proxy Base URL.
7. Process 3-5 pages first, watching the right-side run summary and OCR preview.
8. Only process large books after confirming OCR, visual parsing, and cloze quality in Anki.

## API Notes

`pdf2anki` uses OpenAI-compatible `chat/completions` requests.

- Official/direct Base URL example: `https://api.openai.com/v1`
- Proxy Base URL example: `https://your-relay.example.com/v1`
- The app accepts URLs with or without `/chat/completions`.
- Vision/OCR requires a model and endpoint that supports `image_url` message content.
- The right-side run summary mirrors the selected file, page range, learning depth, and progress.
- The OCR preview panel updates only when a page is rendered for OCR or visual parsing.

## Card Quality Tips

Good medical Cloze cards should:

- Keep enough clinical context and a topic/title on the front.
- Avoid asking for isolated numbers without units or clues.
- Keep compound criteria complete, especially `and/or` conditions.
- Prefer one clinical judgment task per short card.
- Use structured tables or lists for processes, classifications, and comparisons.
- Avoid low-value blanks such as table numbers, figure numbers, and vague words like "level" when the real answer is an indicator name.

## Troubleshooting

### Proxy or relay timeout

The app retries transient API failures. After repeated vision/OCR failures, it temporarily skips visual parsing so text-card generation can continue.

### OCR is not triggered

OCR is used when extracted text is too short, garbled, or medically uninformative. For normal selectable PDF text, the app avoids extra OCR calls to reduce cost.

### Large tables do not fit

Generated Anki cards wrap tables in a horizontally scrollable container and reduce table font size on mobile. If a table is still too large, split the page range or edit the generated card.

### Generated facts look questionable

Use the back-side source page and source excerpt to verify the card against the original PDF. Clinically important cards should always be checked before serious use.
