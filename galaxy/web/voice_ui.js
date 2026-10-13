/*
 * Galaxy voice UI: settings modal, the voice orb, the tap-to-open card, and the bridge to the chat page.
 * The page defines window.galaxyVoiceSend(text). The page calls window.galaxyOnAction(action) and
 * window.galaxyOnReply(text) after each answer. Settings live in localStorage and drive the assistant instance.
 */
(function () {
  'use strict';
  var G = window.GalaxyVoice;
  var MODEL_URLS = {
    'en-IN': '/static/voice/models/vosk-model-small-en-in-0.4.tar.gz',
    'hi-IN': '/static/voice/models/vosk-model-small-hi-0.22.tar.gz'
  };
  var SENS = ['low', 'medium', 'high'];
  var REPLY_SPEECH_LIMIT = 600;
  var STATE_TEXT = {
    off: 'Wake-up is off.',
    loading: 'Loading the offline speech model…',
    listening: 'Listening for "Hey Galaxy"…',
    activated: 'Listening to your command…',
    thinking: 'Thinking…',
    denied: 'Microphone permission was denied.',
    error: 'Could not start the offline speech engine.',
    unsupported: 'This browser cannot run the offline speech engine.'
  };

  function $(id) { return document.getElementById(id); }

  var settings = G.loadSettings(window.localStorage);
  var orb = G.mountOrb();
  var pendingAction = null;
  var awaitingVoiceReply = false;
  var audioCtx = null;

  function chimeNow() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      G.chime(audioCtx);
    } catch (e) { /* no audio output: the orb still pulses */ }
  }

  var assistant = null;
  if (typeof window.LocalVoskVoiceAssistant === 'function' && window.Vosk) {
    assistant = new window.LocalVoskVoiceAssistant({
      lang: settings.lang,
      sensitivity: settings.sensitivity,
      tts: settings.tts,
      modelUrl: MODEL_URLS[settings.lang] || MODEL_URLS['en-IN'],
      chime: chimeNow,
      speak: speakOrWarn,
      onState: function (state) {
        orb.setState(state);
        $('gvStatus').textContent = STATE_TEXT[state] || state;
      },
      onCommand: function (text) {
        awaitingVoiceReply = true;
        if (!window.galaxyVoiceSend(text)) {
          awaitingVoiceReply = false;
          assistant.done('Pehle wala jawab aane do, phir bolein.');
        }
      },
      onError: function (err) { if (window.console) console.warn('voice engine:', err && err.message); }
    });
  } else {
    $('gvEngine').textContent = 'Offline speech library is not installed (galaxy/web/vendor/vosk.js). Voice Wake-Up is unavailable.';
    $('gvWake').disabled = true;
  }

  // The chat page calls these. Actions open only after a tap, and only to destinations the browser allows.
  window.galaxyOnAction = function (action) {
    if (action && action.type === 'permission_request' && window.GalaxyPermission) {
      window.GalaxyPermission.open(action);   // every Android action waits for the user's decision
    }
  };

  window.galaxyOnReply = function (text) {
    if (!awaitingVoiceReply) return;   // only answers to voice commands are spoken
    awaitingVoiceReply = false;
    if (assistant) assistant.done(String(text || '').slice(0, REPLY_SPEECH_LIMIT));
  };

  $('gvTapGo').onclick = function () {
    if (pendingAction) {
      if (pendingAction.type === 'dial') window.location.href = pendingAction.url;
      else window.open(pendingAction.url, '_blank', 'noopener');
    }
    pendingAction = null;
    $('gvTap').hidden = true;
  };
  $('gvTapCancel').onclick = function () { pendingAction = null; $('gvTap').hidden = true; };

  function openModal(open) {
    $('gvModal').classList.toggle('open', open);
    if (open) $('gvDone').focus();
  }
  $('gvFab').onclick = function () { openModal(true); };
  $('gvDone').onclick = function () { openModal(false); };
  $('gvModal').onclick = function (e) { if (e.target === this) openModal(false); };
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') openModal(false); });
  window.addEventListener('pagehide', function () { if (assistant) assistant.stop(); });

  function persist() {
    settings.sensitivity = SENS[Number($('gvSens').value)] || 'medium';
    settings.tts = $('gvTts').checked;
    settings.wake = $('gvWake').checked;
    G.saveSettings(window.localStorage, settings);
  }

  $('gvSens').value = String(Math.max(0, SENS.indexOf(settings.sensitivity)));
  $('gvTts').checked = settings.tts;
  $('gvWake').checked = false;   // the microphone starts only after a tap
  if (settings.wake) {
    $('gvStatus').textContent = 'Voice Wake-Up was on. Turn it on again after reload (the microphone needs a tap).';
  }

  // One clear notice per session when speech is on but this device has no offline voice for the language.
  var warnedNoVoice = false;
  function speakOrWarn(text, lang) {
    var spoken = G.speakReply(text, lang, true);
    if (!spoken && !warnedNoVoice) {
      warnedNoVoice = true;
      say('Is device par offline English (India) voice nahi mili. Settings me Text-to-speech ka voice data install karein.', 'info');
    }
    return spoken;
  }
  function say(message, tone) {
    if (typeof window.galaxyNotify === 'function') { window.galaxyNotify(message, tone); }
  }
  // The microphone is asked for first, with its explanation card; the voice engine starts only once it is granted.
  function gateMicrophone() {
    if (!window.GalaxyDevice) { return Promise.resolve({ state: 'granted', message: '' }); }
    return window.GalaxyDevice.microphone();
  }

  $('gvWake').onchange = function () {
    var box = this;
    persist();
    if (!assistant) { box.checked = false; return; }
    if (box.checked) {
      gateMicrophone().then(function (perm) {
        if (perm.state !== 'granted') {
          box.checked = false;
          persist();
          say(perm.message, perm.state === 'declined' ? 'info' : 'warn');
          return;
        }
        return assistant.start().then(function (ok) {
          if (!ok) box.checked = false;
          persist();
        });
      });
    } else {
      assistant.stop();
      persist();
    }
  };
  $('gvSens').oninput = function () {
    persist();
    if (assistant) assistant.setSensitivity(settings.sensitivity);
  };
  $('gvTts').onchange = function () {
    persist();
    if (assistant) assistant.setTts(settings.tts);
  };
})();
