/*
 * Offline "Hey Galaxy" engine on vosk-browser (Kaldi compiled to WebAssembly).
 *
 * Audio never leaves this device: the microphone feeds local recognizers and the buffers are discarded.
 * The library and the models are served by this server under /static/voice/. The constructor refuses a model URL
 * from any other origin, so nothing is fetched from elsewhere.
 *
 * Two recognizers, one job each:
 *   wake recognizer     restricted grammar (wake phrases + [unk]); runs while listening.
 *   command recognizer  free text; created on wake and released after the command or the time window.
 *
 * Only the adapter functions below touch the vosk-browser API or the browser audio stack. If the vendored version
 * differs, change those functions and nothing else. The adapters target: Vosk.createModel(url),
 * new model.KaldiRecognizer(sampleRate[, grammarJson]), recognizer.on("result" | "partialresult", fn),
 * recognizer.acceptWaveform(buffer), recognizer.remove(), model.terminate().
 */
(function (root) {
  'use strict';

  var G = (typeof module !== 'undefined' && module.exports) ? require('./voice_wakeup.js') : root.GalaxyVoice;

  var UNK = '[unk]';
  var COMMAND_WINDOW_MS = 6000;
  var COOLDOWN_MS = 1500;
  var WAKE_GRAMMAR = {
    low: ['hey galaxy', 'ok galaxy', 'हे गैलेक्सी', 'ओके गैलेक्सी'],
    medium: ['hey galaxy', 'ok galaxy', 'okay galaxy', 'हे गैलेक्सी', 'ओके गैलेक्सी', 'हे गैलक्सी', 'ओके गैलक्सी'],
    high: ['hey galaxy', 'ok galaxy', 'okay galaxy', 'hey galaxi', 'ok galaxi',
           'हे गैलेक्सी', 'ओके गैलेक्सी', 'हे गैलक्सी', 'ओके गैलक्सी']
  };

  // ---- adapters: the only code that touches vosk-browser or the browser audio stack ----
  function newRecognizer(model, sampleRate, grammar) {
    return grammar ? new model.KaldiRecognizer(sampleRate, JSON.stringify(grammar))
                   : new model.KaldiRecognizer(sampleRate);
  }

  function openMicrophone(onBuffer) {
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false })
      .then(function (stream) {
        var Ctx = root.AudioContext || root.webkitAudioContext;
        var ctx = new Ctx();
        var source = ctx.createMediaStreamSource(stream);
        var node = ctx.createScriptProcessor(4096, 1, 1);   // deprecated API; an AudioWorklet can replace it
        var mute = ctx.createGain();
        mute.gain.value = 0;                                // the processor must reach the destination, silently
        node.onaudioprocess = function (event) { onBuffer(event.inputBuffer); };
        source.connect(node);
        node.connect(mute);
        mute.connect(ctx.destination);
        return {
          rate: ctx.sampleRate,
          stop: function () {
            node.disconnect();
            source.disconnect();
            stream.getTracks().forEach(function (track) { track.stop(); });
            ctx.close();
          }
        };
      });
  }

  // ---- pure helpers (tested in Node) ----
  function messageText(message) {
    var r = message && message.result;
    if (!r) return '';
    return String(r.text || r.partial || '').trim();
  }

  function assertSameOrigin(url, origin) {
    if (!origin) return;   // no page origin (Node tests): the caller supplies one when the rule matters
    var resolved = new URL(url, origin);
    if (resolved.origin !== origin) {
      throw new Error('offline model must be served by this server, not ' + resolved.origin);
    }
  }

  function isDenied(err) {
    return !!err && (err.name === 'NotAllowedError' || err.name === 'SecurityError' || err.denied === true);
  }

  function dropRecognizer(rec) {
    if (!rec) return;
    try { rec.remove(); } catch (e) { /* already released */ }
  }

  function LocalVoskVoiceAssistant(options) {
    var o = options || {};
    this.opts = {
      lang: o.lang || 'en-IN',
      sensitivity: WAKE_GRAMMAR[o.sensitivity] ? o.sensitivity : 'medium',
      tts: !!o.tts,
      modelUrl: o.modelUrl || '/static/voice/models/vosk-model-small-en-in-0.4.tar.gz',
      origin: o.origin !== undefined ? o.origin : (root.location ? root.location.origin : null),
      vosk: o.vosk,
      microphone: o.microphone || openMicrophone,
      chime: o.chime || null,
      speak: o.speak || null,
      now: o.now || Date.now,
      setTimer: o.setTimer || setTimeout,
      clearTimer: o.clearTimer || clearTimeout,
      onState: o.onState || function () {},
      onCommand: o.onCommand || function () {},
      onError: o.onError || function () {}
    };
    this.model = null;
    this.modelPromise = null;
    this.wake = null;
    this.command = null;
    this.mic = null;
    this.rate = 16000;
    this.running = false;
    this.capturing = false;
    this.captureTimer = null;
    this.state = 'off';
    this.lastWakeAt = -Infinity;
  }

  LocalVoskVoiceAssistant.prototype._state = function (state) {
    this.state = state;
    this.opts.onState(state);
  };

  // Loads the model once. Concurrent callers share the load; a failed load can be retried.
  LocalVoskVoiceAssistant.prototype.init = function () {
    var self = this, vosk;
    if (this.model) return Promise.resolve(this.model);
    if (this.modelPromise) return this.modelPromise;
    try {
      assertSameOrigin(this.opts.modelUrl, this.opts.origin);
      vosk = this.opts.vosk || root.Vosk;
      if (!vosk || typeof vosk.createModel !== 'function') throw new Error('offline speech library is not installed');
    } catch (err) {
      return Promise.reject(err);
    }
    this.modelPromise = new Promise(function (resolve) {
      resolve(vosk.createModel(self.opts.modelUrl));
    }).then(function (model) {
      self.model = model;
      return model;
    }, function (err) {
      self.modelPromise = null;
      throw err;
    });
    return this.modelPromise;
  };

  LocalVoskVoiceAssistant.prototype.start = function () {
    var self = this;
    if (this.running) return Promise.resolve(true);
    if (!root.WebAssembly) {
      this._state('unsupported');
      return Promise.resolve(false);
    }
    this._state('loading');
    return this.init()
      .then(function () {
        return self.opts.microphone(function (buffer) { self._onAudio(buffer); });
      })
      .then(function (mic) {
        self.mic = mic;
        self.rate = mic.rate;
        self.running = true;
        self._buildWake();
        self._state('listening');
        return true;
      })
      .catch(function (err) {
        self.running = false;
        if (self.mic) {
          try { self.mic.stop(); } catch (e) { /* already stopped */ }
          self.mic = null;
        }
        self._state(isDenied(err) ? 'denied' : 'error');
        self.opts.onError(err);
        return false;
      });
  };

  LocalVoskVoiceAssistant.prototype._buildWake = function () {
    var self = this;
    dropRecognizer(this.wake);
    this.wake = newRecognizer(this.model, this.rate, WAKE_GRAMMAR[this.opts.sensitivity].concat([UNK]));
    this.wake.on('partialresult', function (m) { self._onWakeText(messageText(m), false); });
    this.wake.on('result', function (m) { self._onWakeText(messageText(m), true); });
  };

  LocalVoskVoiceAssistant.prototype._onAudio = function (buffer) {
    if (!this.running) return;
    if (this.capturing) {
      if (this.command) this.command.acceptWaveform(buffer);
    } else if (this.wake) {
      this.wake.acceptWaveform(buffer);
    }
  };

  LocalVoskVoiceAssistant.prototype._onWakeText = function (text, isFinal) {
    var hit;
    if (!text || !this.running || this.capturing) return;
    if (this.opts.now() - this.lastWakeAt < COOLDOWN_MS) return;
    if (!isFinal && this.opts.sensitivity === 'low') return;   // low: only final results count
    hit = G.detectWakeWord(text, this.opts.sensitivity);
    if (hit.matched) this._onWake(hit.command);
  };

  LocalVoskVoiceAssistant.prototype._onWake = function (command) {
    this.lastWakeAt = this.opts.now();
    this._playChime();
    this._state('activated');
    if (command) this._dispatch(command);
    else this._startCapture();
  };

  LocalVoskVoiceAssistant.prototype._startCapture = function () {
    var self = this;
    this.capturing = true;
    this.command = newRecognizer(this.model, this.rate, null);
    this.command.on('result', function (m) { self._onCommandResult(messageText(m)); });
    this.captureTimer = this.opts.setTimer(function () { self._endCapture(''); }, COMMAND_WINDOW_MS);
  };

  LocalVoskVoiceAssistant.prototype._onCommandResult = function (text) {
    var residue;
    if (!this.capturing || !text) return;
    residue = G.detectWakeWord(text, 'low');
    if (residue.matched && !residue.command) return;   // the wake phrase again, not a command
    this._endCapture(text);
  };

  LocalVoskVoiceAssistant.prototype._endCapture = function (text) {
    if (!this.capturing) return;
    this.opts.clearTimer(this.captureTimer);
    this.captureTimer = null;
    this.capturing = false;
    dropRecognizer(this.command);
    this.command = null;
    if (text) this._dispatch(text);
    else if (this.running) this._state('listening');
  };

  LocalVoskVoiceAssistant.prototype._dispatch = function (text) {
    this._state('thinking');
    this.opts.onCommand(text);
  };

  LocalVoskVoiceAssistant.prototype._playChime = function () {
    if (!this.opts.chime) return;
    try { this.opts.chime(); } catch (e) { /* no audio output: the orb still pulses */ }
  };

  // Call after the answer is shown. Speaks only when TTS is on; the speaker itself enforces local voices.
  LocalVoskVoiceAssistant.prototype.done = function (replyText) {
    if (this.opts.tts && replyText && this.opts.speak) this.opts.speak(replyText, this.opts.lang);
    if (this.running && !this.capturing) this._state('listening');
  };

  LocalVoskVoiceAssistant.prototype.setSensitivity = function (level) {
    if (!WAKE_GRAMMAR[level]) return;
    this.opts.sensitivity = level;
    if (this.running) this._buildWake();
  };

  LocalVoskVoiceAssistant.prototype.setTts = function (on) {
    this.opts.tts = !!on;
  };

  LocalVoskVoiceAssistant.prototype.stop = function () {
    this.running = false;
    if (this.captureTimer) this.opts.clearTimer(this.captureTimer);
    this.captureTimer = null;
    this.capturing = false;
    dropRecognizer(this.command);
    this.command = null;
    dropRecognizer(this.wake);
    this.wake = null;
    if (this.mic) {
      try { this.mic.stop(); } catch (e) { /* already stopped */ }
      this.mic = null;
    }
    this._state('off');
  };

  LocalVoskVoiceAssistant.prototype.dispose = function () {
    this.stop();
    if (this.model && typeof this.model.terminate === 'function') {
      try { this.model.terminate(); } catch (e) { /* already terminated */ }
    }
    this.model = null;
    this.modelPromise = null;
  };

  var api = {
    LocalVoskVoiceAssistant: LocalVoskVoiceAssistant,
    WAKE_GRAMMAR: WAKE_GRAMMAR, UNK: UNK,
    COOLDOWN_MS: COOLDOWN_MS, COMMAND_WINDOW_MS: COMMAND_WINDOW_MS,
    assertSameOrigin: assertSameOrigin, messageText: messageText, isDenied: isDenied
  };
  root.LocalVoskVoiceAssistant = LocalVoskVoiceAssistant;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
