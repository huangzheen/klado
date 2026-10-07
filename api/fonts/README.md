# Not a font directory any more — deliberately.

This folder used to hold `NotoSansSC-Regular.otf` and `NotoSerifSC-Regular.otf`, which the
container image once copied into `/usr/share/fonts/truetype/noto/` so fontconfig could find
them. There is no container build in this repository any more; the note below is kept
because the *reason* the folder is empty still matters to anyone who renders Chinese text.

⚠️ **They were not fonts.** Both files were ~300 KB of GitHub HTML — a 404 page saved with a
`.otf` extension (`file` says "HTML document text"; the first bytes are `<!DOCTYPE html>`).
So the image had **no usable Chinese font at all**, and fontconfig's fallback is a Latin-only
face: every Chinese glyph rendered as a tofu box while the PDF's text layer looked perfect.

The two `COPY fonts/ /usr/share/fonts/truetype/noto/` lines are kept (an empty source
directory still needs to exist), but nothing here should be trusted as a font again:

* The **report deck** declares its own `@font-face` for `frontend/out/vendor/fonts/*.woff2`.
* The **knowledge export** (`services/knowledge_export.py`) embeds those same faces in the
  document it hands to Chromium.

If you ever add a real face here, verify it first — `file <name>.otf` must say *OpenType* —
because the previous failure was silent for months.
