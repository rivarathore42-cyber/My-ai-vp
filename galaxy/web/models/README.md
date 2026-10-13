# Offline Vosk models

Place these archives here, unmodified (the names match the defaults in voice_ui.js):

- `vosk-model-small-en-in-0.4.tar.gz` (English, Indian accent) for lang en-IN
- `vosk-model-small-hi-0.22.tar.gz` (Hindi) for lang hi-IN

Before shipping, check each archive against the checksum published with that model. Confirm that the wake phrases are in the
model's vocabulary: "galaxy" and "गैलेक्सी" must be recognised by the model, because the grammar can only use known words.
