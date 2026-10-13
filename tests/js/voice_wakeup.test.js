// Node test for the pure logic of galaxy/web/voice_wakeup.js (run: node tests/js/voice_wakeup.test.js).
'use strict';
const assert = require('assert');
const path = require('path');
const G = require(path.join(__dirname, '..', '..', 'galaxy', 'web', 'voice_wakeup.js'));

let passed = 0;
function check(label, fn) {
  try { fn(); passed++; } catch (e) { console.error('FAIL', label, '\n  ', e.message); process.exitCode = 1; }
}

// Wake phrases at the start of a sentence (all sensitivities)
check('English wake word with command', () => {
  assert.deepStrictEqual(G.detectWakeWord('Hey Galaxy, open YouTube and search Free Fire gameplay', 'low'),
    { matched: true, command: 'open youtube and search free fire gameplay' });
});
check('Ok Galaxy with call command', () => {
  assert.deepStrictEqual(G.detectWakeWord('ok galaxy call hansraj', 'medium'), { matched: true, command: 'call hansraj' });
});
check('Wake word alone waits for a command', () => {
  assert.deepStrictEqual(G.detectWakeWord('Hey Galaxy', 'low'), { matched: true, command: '' });
});
check('Hindi wake phrases (Devanagari)', () => {
  assert.strictEqual(G.detectWakeWord('हे गैलेक्सी कॉल करो', 'low').matched, true);
  assert.strictEqual(G.detectWakeWord('ओके गैलेक्सी यूट्यूब खोलो', 'medium').matched, true);
});
check('Devanagari command text keeps its vowel signs', () => {
  assert.strictEqual(G.detectWakeWord('हे गैलेक्सी हैन्सराज को कॉल करो', 'low').command, 'हैन्सराज को कॉल करो');
});
check('Punctuation does not block the match', () => {
  assert.strictEqual(G.detectWakeWord('Hey, Galaxy!', 'low').matched, true);
});

// Sensitivity: how strict the match is
check('low rejects a wake phrase in the middle', () => {
  assert.strictEqual(G.detectWakeWord('open youtube hey galaxy', 'low').matched, false);
});
check('medium accepts a wake phrase in the middle and keeps the rest', () => {
  assert.deepStrictEqual(G.detectWakeWord('open youtube hey galaxy', 'medium'), { matched: true, command: 'open youtube' });
});
check('high accepts a one-letter misrecognition of Galaxy', () => {
  assert.deepStrictEqual(G.detectWakeWord('hey galaxi what time', 'high'), { matched: true, command: 'what time' });
});
check('low and medium reject that misrecognition', () => {
  assert.strictEqual(G.detectWakeWord('hey galaxi what time', 'low').matched, false);
  assert.strictEqual(G.detectWakeWord('hey galaxi what time', 'medium').matched, false);
});

// Non-wake speech
check('ordinary speech is not a wake word', () => {
  assert.strictEqual(G.detectWakeWord('please call mom', 'high').matched, false);
  assert.strictEqual(G.detectWakeWord('', 'high').matched, false);
});

// Confidence gate
check('medium accepts confidence 0.5 and rejects 0.2', () => {
  assert.strictEqual(G.shouldAccept(0.5, 'medium'), true);
  assert.strictEqual(G.shouldAccept(0.2, 'medium'), false);
});
check('unknown confidence (0 or missing) is not rejected', () => {
  assert.strictEqual(G.shouldAccept(0, 'low'), true);
  assert.strictEqual(G.shouldAccept(undefined, 'low'), true);
});

// Settings persistence with a fake storage
check('settings load defaults when storage is empty or corrupt', () => {
  const empty = { getItem: () => null };
  assert.deepStrictEqual(G.loadSettings(empty), G.DEFAULTS);
  assert.deepStrictEqual(G.loadSettings({ getItem: () => '{not json' }), G.DEFAULTS);
});
check('settings reject invalid values and keep valid ones', () => {
  const s = G.loadSettings({ getItem: () => JSON.stringify({ wake: true, sensitivity: 'extreme', tts: 'yes', lang: 'hi-IN' }) });
  assert.strictEqual(s.wake, true);
  assert.strictEqual(s.sensitivity, G.DEFAULTS.sensitivity);
  assert.strictEqual(s.tts, G.DEFAULTS.tts);
  assert.strictEqual(s.lang, 'hi-IN');
});
check('settings round-trip through storage', () => {
  let stored = null;
  const storage = { setItem: (k, v) => { stored = v; }, getItem: () => stored };
  G.saveSettings(storage, { wake: true, sensitivity: 'high', tts: true, lang: 'en-IN' });
  assert.deepStrictEqual(G.loadSettings(storage), { wake: true, sensitivity: 'high', tts: true, lang: 'en-IN' });
});

console.log(`${passed} voice wake-up checks passed${process.exitCode ? ' (with failures)' : ''}`);
