# Telegram Media Reference Parsing

## Summary

Hermes could mistake malformed Markdown or example `MEDIA:` directives in an
agent response for real local attachments. This produced Telegram warnings such
as:

```text
[Telegram] Failed to send media (.png\):
File not found: /tmp/chart.png\

[Telegram] Failed to send media ():
File not found: \
```

Valid Telegram document, image, audio, and video delivery was not itself
broken. The failure occurred before Telegram upload, while Hermes was extracting
local paths from response text.

## Root Cause

`BasePlatformAdapter.extract_media()` in `gateway/platforms/base.py` used a
permissive fallback for text following `MEDIA:`. It consequently accepted:

- an unquoted trailing Markdown line-break escape as part of a filename;
- a standalone backslash as a nonempty path; and
- `MEDIA:` examples inside inline or fenced code as delivery directives.

For example:

```text
MEDIA:/tmp/chart.png\
MEDIA:\
```

were parsed as the paths `/tmp/chart.png\` and `\`. Telegram's adapter then
performed the expected existence check and reported that those paths did not
exist.

Media extraction runs on the agent response before Telegram MarkdownV2
formatting. Therefore, Telegram formatting did not introduce these
backslashes; they were already present in the generated response text.

The filenames `/tmp/cron-voice.mp3`, `/tmp/chart.png`, and `/tmp/voice.ogg`
also appear as literal fixtures in `tests/cron/test_scheduler.py`. Their names
alone do not establish that a production cron job generated the warnings. The
same parser is shared by ordinary agent, background-task, streaming, and cron
delivery paths.

## Corrected Behavior

Media extraction now:

- removes one trailing Markdown line-break escape from an unquoted reference
  only when the preceding path ends in a supported media extension;
- rejects a standalone backslash as a malformed media reference;
- ignores `MEDIA:` examples inside inline and fenced code;
- removes only references that were accepted for delivery;
- preserves backslashes inside filenames and in explicitly quoted paths; and
- continues to support quoted paths containing spaces and Unicode characters.

The Telegram adapter also validates local attachments before upload. Its error
result now distinguishes an empty reference, a missing path, a non-regular
file, and an unreadable file. Telegram API exceptions remain separate from
local validation failures.

## Delivery Path

The relevant runtime path is:

```text
Agent or cron output
    -> BasePlatformAdapter.extract_media()
    -> extension-based media routing
    -> TelegramAdapter.send_image_file(), send_voice(), send_video(),
       or send_document()
    -> local file validation
    -> Telegram API upload
```

Cron delivery first extracts the same directives in
`cron.scheduler._deliver_result()` and forwards the resulting paths separately
from the cleaned text. Cron does not delete those referenced files in this
delivery path. Auto-generated voice replies have a separate lifecycle: their
owned temporary file is removed only after the awaited send finishes.

## Regression Coverage

Tests in `tests/gateway/test_platform_base.py` cover:

- trailing Markdown escapes on PNG, MP3, and OGG references;
- a standalone backslash;
- multiple media references;
- quoted paths with spaces and Unicode;
- literal and internal backslashes;
- inline and fenced code examples; and
- preservation of rejected text beside a valid directive.

Existing Telegram tests cover successful uploads and missing files with mocked
Telegram APIs. No automated test sends a real Telegram message.

## Manual Telegram Verification

After deploying the change and restarting the gateway, ask Hermes to create
valid temporary files and return the directives outside a code block:

```text
MEDIA:'/tmp/hermes telegram ü test.txt'
MEDIA:/tmp/hermes-media-test.png\
MEDIA:/tmp/hermes-voice-test.wav
```

Expected results:

- the text file arrives as a document;
- the PNG arrives as an image without treating `\` as part of its path;
- the WAV arrives as audio; and
- no `File not found: ...\` or `File not found: \` warning is logged.

Then return the following as a fenced code example:

````text
```text
MEDIA:/tmp/nonexistent-example.png\
MEDIA:\
```
````

It must remain visible as text and must not trigger an attachment attempt.

## Deployment and Rollback

No configuration, database migration, Telegram setting, or cron-job change is
required. Restart the gateway after deploying so the running process imports
the updated parser and adapter.

To roll back, revert the media parsing and Telegram validation changes in:

- `gateway/platforms/base.py`
- `gateway/platforms/telegram.py`
- `tests/gateway/test_platform_base.py`

Then restart the gateway. Do not remove temporary files or alter production
cron jobs as part of rollback.
