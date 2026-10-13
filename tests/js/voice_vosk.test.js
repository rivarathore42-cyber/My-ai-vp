// Node tests for the offline engine (galaxy/web/voice_wakeup_vosk.js) and the pure voice helpers.
// The Vosk library and the microphone are replaced by fakes; the real library is not needed to test the wrapper.
'use strict';
const assert = require('assert');
const path = require('path');
const WEB = path.join(__dirname, '..', '..', 'galaxy', 'web');
const G = require(path.join(WEB, 'voice_wakeup.js'));
const V = require(path.join(WEB, 'voice_wakeup_vosk.js'));
const LVA = V.LocalVoskVoiceAssistant;
const MODEL_EN = '/static/voice/models/vosk-model-small-en-in-0.4.tar.gz';

function fakeModel() {
  const model = { recognizers: [], terminated: false, terminate() { this.terminated = true; } };
  model.KaldiRecognizer = function (rate, grammarJson) {
    const rec = { rate, grammar: grammarJson ? JSON.parse(grammarJson) : null, handlers: {}, fed: 0, removed: false };
    rec.on = (event, fn) => { rec.handlers[event] = fn; };
    rec.acceptWaveform = () => { rec.fed += 1; };
    rec.remove = () => { rec.removed = true; };
    model.recognizers.push(rec);
    return rec;
  };
  return model;
}

function harness(extra) {
  const h = { states: [], commands: [], errors: [], timers: [], spoken: [], chimes: 0, clock: 0, mic: null, models: [] };
  const vosk = {
    loads: [],
    failNext: 0,
    createModel(url) {
      this.loads.push(url);
      if (this.failNext > 0) { this.failNext -= 1; return Promise.reject(new Error('download failed')); }
      const model = fakeModel();
      h.models.push(model);
      return Promise.resolve(model);
    }
  };
  h.vosk = vosk;
  const opts = Object.assign({
    origin: 'https://galaxy.local',
    vosk,
    microphone: (onBuffer) => {
      h.mic = { onBuffer, rate: 16000, stopped: false, stop() { this.stopped = true; } };
      return Promise.resolve(h.mic);
    },
    now: () => h.clock,
    setTimer: (fn) => { const t = { fn, cleared: false }; h.timers.push(t); return t; },
    clearTimer: (t) => { if (t) t.cleared = true; },
    onState: (s) => h.states.push(s),
    onCommand: (c) => h.commands.push(c),
    onError: (e) => h.errors.push(e),
    chime: () => { h.chimes += 1; },
    speak: (text) => h.spoken.push(text)
  }, extra || {});
  h.assistant = new LVA(opts);
  return h;
}

const activeWake = (h) => h.models[0].recognizers.filter((r) => r.grammar && !r.removed).pop();
const activeCommand = (h) => h.models[0].recognizers.filter((r) => !r.grammar && !r.removed).pop();
const lastState = (h) => h.states[h.states.length - 1];
const liveTimer = (h) => h.timers.filter((t) => !t.cleared).pop();

const tests = [];
function test(label, fn) { tests.push({ label, fn }); }

// ---- helpers and the wrapper's initialisation ----
test('same-origin rule: local paths pass, other origins are refused', () => {
  V.assertSameOrigin('/static/voice/models/m.tar.gz', 'https://galaxy.local');
  assert.throws(() => V.assertSameOrigin('https://cdn.example/m.tar.gz', 'https://galaxy.local'), /this server/);
  V.assertSameOrigin('https://cdn.example/m.tar.gz', null);   // no page origin (Node): the check is skipped
});

test('message text comes from final, partial, or empty results', () => {
  assert.strictEqual(V.messageText({ result: { text: ' hey galaxy ' } }), 'hey galaxy');
  assert.strictEqual(V.messageText({ result: { partial: 'hey gal' } }), 'hey gal');
  assert.strictEqual(V.messageText({}), '');
});

test('start loads the local model once and reaches listening at the microphone rate', async () => {
  const h = harness();
  assert.strictEqual(await h.assistant.start(), true);
  assert.deepStrictEqual(h.vosk.loads, [MODEL_EN]);
  assert.deepStrictEqual(h.states, ['loading', 'listening']);
  const wake = activeWake(h);
  assert.strictEqual(wake.rate, 16000);
  assert.ok(wake.grammar.includes('hey galaxy') && wake.grammar.includes('[unk]'));
});

test('concurrent init calls share one model load', async () => {
  const h = harness();
  await Promise.all([h.assistant.init(), h.assistant.init()]);
  assert.strictEqual(h.vosk.loads.length, 1);
});

test('a model from another origin is refused before any load', async () => {
  const h = harness({ modelUrl: 'https://cdn.example/m.tar.gz' });
  assert.strictEqual(await h.assistant.start(), false);
  assert.strictEqual(h.vosk.loads.length, 0);
  assert.strictEqual(lastState(h), 'error');
});

test('a missing library is reported as an error, not a crash', async () => {
  const h = harness({ vosk: null });
  assert.strictEqual(await h.assistant.start(), false);
  assert.strictEqual(lastState(h), 'error');
  assert.match(h.errors[0].message, /not installed/);
});

test('a failed load can be retried', async () => {
  const h = harness();
  h.vosk.failNext = 1;
  assert.strictEqual(await h.assistant.start(), false);
  assert.strictEqual(await h.assistant.start(), true);
  assert.strictEqual(h.vosk.loads.length, 2);
});

test('microphone permission denial reports denied and does not start', async () => {
  const h = harness({
    microphone: () => Promise.reject(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
  });
  assert.strictEqual(await h.assistant.start(), false);
  assert.strictEqual(lastState(h), 'denied');
});

test('dispose stops the microphone and terminates the model', async () => {
  const h = harness();
  await h.assistant.start();
  const wake = activeWake(h);
  h.assistant.dispose();
  assert.strictEqual(h.mic.stopped, true);
  assert.strictEqual(wake.removed, true);
  assert.strictEqual(h.models[0].terminated, true);
  assert.strictEqual(lastState(h), 'off');
});

// ---- wake word behaviour ----
test('grammar follows the sensitivity, and changing it rebuilds the wake recognizer', async () => {
  const h = harness({ sensitivity: 'low' });
  await h.assistant.start();
  const first = activeWake(h);
  assert.ok(!first.grammar.includes('okay galaxy'));
  h.assistant.setSensitivity('high');
  assert.strictEqual(first.removed, true);
  assert.ok(activeWake(h).grammar.includes('hey galaxi'));
  h.assistant.setSensitivity('bogus');
  assert.strictEqual(h.assistant.opts.sensitivity, 'high');
});

test('a final wake phrase: chime, activated, then a free-text command recognizer', async () => {
  const h = harness();
  await h.assistant.start();
  const wake = activeWake(h);
  const fedBefore = wake.fed;
  wake.handlers.result({ result: { text: 'hey galaxy' } });
  assert.strictEqual(lastState(h), 'activated');
  assert.strictEqual(h.chimes, 1);
  const cmd = activeCommand(h);
  assert.ok(cmd && cmd.grammar === null, 'command recognizer is free text');
  h.mic.onBuffer({});
  assert.strictEqual(cmd.fed, 1);
  assert.strictEqual(wake.fed, fedBefore, 'wake recognizer is paused during capture');
  cmd.handlers.result({ result: { text: 'open youtube' } });
  assert.deepStrictEqual(h.commands, ['open youtube']);
  assert.strictEqual(lastState(h), 'thinking');
  assert.strictEqual(cmd.removed, true);
});

test('wake phrase with a command in the same utterance dispatches at once', async () => {
  const h = harness();
  await h.assistant.start();
  activeWake(h).handlers.result({ result: { text: 'Hey Galaxy, call Hansraj' } });
  assert.deepStrictEqual(h.commands, ['call hansraj']);
  assert.strictEqual(activeCommand(h), undefined, 'no capture window needed');
});

test('low sensitivity ignores partial results; medium acts on them', async () => {
  const low = harness({ sensitivity: 'low' });
  await low.assistant.start();
  activeWake(low).handlers.partialresult({ result: { partial: 'hey galaxy' } });
  assert.strictEqual(lastState(low), 'listening');
  const medium = harness({ sensitivity: 'medium' });
  await medium.assistant.start();
  activeWake(medium).handlers.partialresult({ result: { partial: 'hey galaxy' } });
  assert.strictEqual(lastState(medium), 'activated');
});

test('noise and unknown words never wake the assistant', async () => {
  const h = harness();
  await h.assistant.start();
  const wake = activeWake(h);
  wake.handlers.result({ result: { text: '[unk]' } });
  wake.handlers.result({ result: { text: 'open youtube' } });
  assert.strictEqual(h.chimes, 0);
  assert.strictEqual(lastState(h), 'listening');
});

test('cooldown: a repeat wake right after a command is ignored', async () => {
  const h = harness();
  await h.assistant.start();
  const wake = activeWake(h);
  h.clock = 0;
  wake.handlers.result({ result: { text: 'hey galaxy call hansraj' } });
  h.clock = 500;
  wake.handlers.result({ result: { text: 'hey galaxy open youtube' } });
  assert.strictEqual(h.commands.length, 1);
  h.clock = 2000;
  wake.handlers.result({ result: { text: 'hey galaxy open youtube' } });
  assert.strictEqual(h.commands.length, 2);
});

test('capture window expires with no speech: back to listening, recognizer released', async () => {
  const h = harness();
  await h.assistant.start();
  activeWake(h).handlers.result({ result: { text: 'hey galaxy' } });
  const cmd = activeCommand(h);
  liveTimer(h).fn();
  assert.strictEqual(lastState(h), 'listening');
  assert.deepStrictEqual(h.commands, []);
  assert.strictEqual(cmd.removed, true);
});

test('the wake phrase repeated inside the command window is not taken as a command', async () => {
  const h = harness();
  await h.assistant.start();
  activeWake(h).handlers.result({ result: { text: 'hey galaxy' } });
  const cmd = activeCommand(h);
  cmd.handlers.result({ result: { text: 'hey galaxy' } });
  assert.deepStrictEqual(h.commands, []);
  cmd.handlers.result({ result: { text: 'call hansraj' } });
  assert.deepStrictEqual(h.commands, ['call hansraj']);
});

test('done() speaks only when TTS is on, then returns to listening', async () => {
  const on = harness({ tts: true });
  await on.assistant.start();
  on.assistant.done('Answer');
  assert.deepStrictEqual(on.spoken, ['Answer']);
  assert.strictEqual(lastState(on), 'listening');
  const off = harness({ tts: false });
  await off.assistant.start();
  off.assistant.done('Answer');
  assert.deepStrictEqual(off.spoken, []);
});

test('stop() releases everything and audio after stop is ignored', async () => {
  const h = harness();
  await h.assistant.start();
  const wake = activeWake(h);
  const fed = wake.fed;
  h.assistant.stop();
  assert.strictEqual(h.mic.stopped, true);
  assert.strictEqual(wake.removed, true);
  assert.strictEqual(lastState(h), 'off');
  h.mic.onBuffer({});
  assert.strictEqual(wake.fed, fed);
});

// ---- pure helpers in voice_wakeup.js ----
test('speakReply uses only voices that run on this device', () => {
  const Utt = function (text) { this.text = text; };
  const networkOnly = { getVoices: () => [{ lang: 'en-IN', localService: false }], speak(u) { this.last = u; } };
  assert.strictEqual(G.speakReply('hello', 'en-IN', true, networkOnly, Utt), false);
  assert.strictEqual(networkOnly.last, undefined);
  const mixed = { getVoices: () => [{ lang: 'en-IN', localService: false, name: 'network' },
                                     { lang: 'en-IN', localService: true, name: 'device' }],
                  speak(u) { this.last = u; } };
  assert.strictEqual(G.speakReply('hello', 'en-IN', true, mixed, Utt), true);
  assert.strictEqual(mixed.last.voice.name, 'device');
  assert.strictEqual(G.speakReply('hello', 'en-IN', false, mixed, Utt), false);
});

test('speech prefers the exact Indian English voice, then any offline voice in the language', () => {
  const us = { lang: 'en-US', localService: true, name: 'us' };
  const indian = { lang: 'en-IN', localService: true, name: 'in' };
  assert.strictEqual(G.pickVoice([us, indian], 'en-IN').name, 'in');
  assert.strictEqual(G.pickVoice([us], 'en-IN').name, 'us');
  assert.strictEqual(G.pickVoice([{ lang: 'en_in', localService: true, name: 'in2' }], 'en-IN').name, 'in2');
  assert.strictEqual(G.pickVoice([{ lang: 'en-IN', localService: false, name: 'net' }], 'en-IN'), null);
});

test('speech strips chat formatting, code and links before it speaks, and caps the length', () => {
  assert.strictEqual(G.speechText('**Answer:** `2 + 2 = 4`\n- Step one\n# Head [docs](https://example.com) | x', 600),
    'Answer: 2 + 2 = 4 Step one Head docs x');
  assert.strictEqual(G.speechText('a'.repeat(700), 600).length, 600);
});

test('the browser opens only allowed destinations', () => {
  const safe = ['https://www.youtube.com/results?search_query=free+fire',
                'https://wa.me/919876543210', 'https://web.whatsapp.com/',
                'tel:+919876543210', 'whatsapp://send?phone=919876543210'];
  const unsafe = ['javascript:alert(1)', 'https://evil.example/x', 'http://www.youtube.com/',
                  'tel:abc', 'tel:+91', 'data:text/html,hi', '', undefined];
  safe.forEach((u) => assert.ok(G.isSafeActionUrl(u), 'should allow ' + u));
  unsafe.forEach((u) => assert.ok(!G.isSafeActionUrl(u), 'should refuse ' + u));
});

test('the Web Speech controller is gone from the pure module', () => {
  assert.strictEqual(G.VoiceWakeUp, undefined);
  assert.strictEqual(typeof G.chime, 'function');
});

(async () => {
  let passed = 0, failed = 0;
  for (const t of tests) {
    try { await t.fn(); passed += 1; }
    catch (e) { failed += 1; console.error('FAIL', t.label, '\n  ', e.message); }
  }
  console.log(`${passed} voice Vosk checks passed` + (failed ? ` (${failed} failed)` : ''));
  if (failed) process.exitCode = 1;
})();
