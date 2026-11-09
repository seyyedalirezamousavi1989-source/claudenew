# LocalChat 2.0.5 — Phase 12: Large-document chat handoff

## Problem addressed
A normal chat attachment is intentionally represented by a bounded text preview. Large PDFs could therefore be clipped to fit the selected model's context window; the model then claimed that the rest of the book was unavailable. A separate long-task engine already existed, but users had to open the Tasks panel and upload the same file again.

## Changes
- Chat attachment uploads now preserve the original bytes in a private local `.source` sidecar, in addition to the bounded preview/index record.
- Attachment source sidecars are removed when their attachment is deleted or its metadata expires. The source path is not exposed in the public attachment response.
- The normal attachment upload ceiling is aligned with the existing long-task limit at 128 MiB.
- When a user explicitly asks to translate or summarize an attached supported document, chat automatically creates a durable `TaskService` job from the original bytes. It does not send the clipped preview as if it were the whole book.
- The selected local Ollama model is passed to the task service; target language is inferred from explicit language wording, defaulting to Persian.
- The existing Tasks panel opens automatically after job creation, showing progress and allowing pause/resume/download.
- Ordinary file questions and uploads without an explicit translation/summarization request continue through the normal chat path.
- Supported automatic handoff formats: PDF, DOCX, TXT, Markdown, RST, LOG, CSV and TSV. Scanned PDFs without extractable text still require OCR; this phase reports the existing document-engine limitation rather than inventing text.

## Safety / storage
- Original files remain on the local machine and are not executed.
- ZIP project archives and images are deliberately excluded from automatic document translation routing.
- Attachment sidecars are private implementation data and are omitted from public attachment metadata.
- Existing attachment metadata and task databases are not deleted or reset.

## Validation
- Python compileall: passed.
- Focused new/related unit tests: 5 passed before the HTTP-level routing test; the latter also passed separately.
- `test_features.py`: 49 passed, 8 subtests passed.
- `test_task_api.py`: 8 passed.
- `test_tasks.py` + `test_task_auto_recovery.py`: 16 passed.
- `test_server.py`: 38 passed, 15 subtests passed (including an HTTP-level test proving that «ترجمه کن» uses the original uploaded PDF bytes and the selected local model).
- `test_media_and_version.py`: 26 passed.
- A combined multi-file test invocation exceeded the environment timeout, so the suites were run separately; no claim is made that the entire repository suite passed.
- Real Ollama translation of a large PDF and browser end-to-end upload were not available in this environment and must be verified on the user's Windows machine.
