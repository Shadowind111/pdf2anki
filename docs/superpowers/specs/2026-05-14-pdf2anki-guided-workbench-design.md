# pdf2anki Guided Workbench UI Design

## Goal

Optimize the existing PyQt6 desktop interface for a medical PDF to Anki workflow without changing the PDF extraction, LLM, database, resume, or Anki export logic.

## Confirmed Direction

Use the guided workbench layout selected in the visual companion:

- A left rail shows the three work stages: select PDF, configure models, generate cards.
- The center work area keeps the primary task controls, configuration form, progress bar, and log.
- The right rail keeps OCR/image preview and adds a compact run summary.

## Visual System

- Use a restrained product-tool style for long study sessions: tinted light background, white panels, navy primary, blue selection states, green success, red errors, amber warnings.
- Keep typography on the native Windows-friendly sans stack: Microsoft YaHei, Segoe UI, system-ui.
- Remove structural emoji from buttons, group titles, and status labels. Use text, state color, spacing, and hierarchy instead.
- Keep panel radius modest and consistent. Avoid nested decorative cards.

## Interaction And Feedback

- Keep one primary action: start generation. Resume and pause are secondary stateful actions.
- Show disabled form states clearly when proxy or visual API reuse controls hide an input's effect.
- Keep progress visible in both the main progress bar and the right summary.
- Surface missing PDF/API errors in the log with clear recovery text.
- Preserve the existing OCR preview behavior and scale images smoothly inside the right panel.

## Implementation Scope

- Refactor only `MainWindow` UI construction and UI helper methods in `pdf2anki.py`.
- Add small helper methods for card creation, field rows, step states, summary refresh, and display text cleanup.
- Do not split the application into multiple modules in this pass.
- Do not alter core processing, prompt, database schema, or exported card styling.

## Verification

- Run `py -m py_compile pdf2anki.py`.
- Instantiate the PyQt window offscreen and capture a screenshot for visual QA.
- Check the UI still creates the expected widgets used by existing event handlers.
