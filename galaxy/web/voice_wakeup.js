/*
 * Galaxy voice wake-up: pure logic shared by the offline engine (voice_wakeup_vosk.js) and the UI (voice_ui.js).
 * No speech recognition happens here. Wake-word matching, settings, the chime, local-only TTS and the orb helper live here.
 * Tests: tests/js/voice_wakeup.test.js.
 */
(function (root) {
  'use strict';

  var WAKE_PHRASES = ['hey galaxy', 'ok galaxy', 'okay galaxy', 'हे गैलेक्सी', 'ओके गैलेक्सी', 'हे गैलक्सी', 'ओके गैलक्सी'];
  var WAKE_LEAD = ['hey', 'ok', 'okay', 'हे', 'ओके'];
  var SENSITIVITY = {
    low:    { minConfidence: 0.6,  anywhere: false, fuzzy: false },
    medium: { minConfidence: 0.45, anywhere: true,  fuzzy: false },
    high:   { minConfidence: 0.3,  anywhere: true,  fuzzy: true }
  };
  var COMMAND_WINDOW_MS = 6000;
  var STORAGE_KEY = 'galaxy.voice.v1';
  var DEFAULTS = { wake: false, sensitivity: 'medium', tts: false, lang: 'en-IN' };

  // Lowercase, keep letters, combining marks (Devanagari vowel signs) and digits; everything else becomes a space.
  function normalize(s) {
    return String(s || '').toLowerCase().replace(/[^\p{L}\p{M}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
  }

  function editDistance(a, b) {
    var prev = [], cur, i, j;
    for (j = 0; j <= b.length; j++) prev[j] = j;
    for (i = 1; i <= a.length; i++) {
      cur = [i];
      for (j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      prev = cur;
    }
    return prev[b.length];
  }

  function isGalaxyToken(token) {
    return editDistance(token, 'galaxy') <= 1;
  }

  // Returns { matched, command }. "command" is whatever was said around the wake phrase.
  function detectWakeWord(transcript, sensitivity) {
    var cfg = SENSITIVITY[sensitivity] || SENSITIVITY.medium;
    var text = normalize(transcript);
    var i, p, padded, idx, before, after, tokens;
    if (!text) return { matched: false, command: '' };

    for (i = 0; i < WAKE_PHRASES.length; i++) {
      p = normalize(WAKE_PHRASES[i]);
      if (text === p) return { matched: true, command: '' };
      if (text.indexOf(p + ' ') === 0) return { matched: true, command: text.slice(p.length).trim() };
    }

    if (cfg.anywhere) {
      padded = ' ' + text + ' ';
      for (i = 0; i < WAKE_PHRASES.length; i++) {
        p = normalize(WAKE_PHRASES[i]);
        idx = padded.indexOf(' ' + p + ' ');
        if (idx >= 0) {
          before = padded.slice(0, idx).trim();
          after = padded.slice(idx + p.length + 2).trim();
          return { matched: true, command: (before + ' ' + after).trim() };
        }
      }
    }

    if (cfg.fuzzy) {
      tokens = text.split(' ');
      for (i = 0; i + 1 < tokens.length; i++) {
        if (WAKE_LEAD.indexOf(tokens[i]) !== -1 && isGalaxyToken(tokens[i + 1])) {
          return { matched: true, command: tokens.slice(i + 2).join(' ') };
        }
      }
    }
    return { matched: false, command: '' };
  }

  // Confidence gate. Many engines report 0 when they do not know; those results are not rejected.
  function shouldAccept(confidence, sensitivity) {
    var cfg = SENSITIVITY[sensitivity] || SENSITIVITY.medium;
    if (typeof confidence !== 'number' || confidence === 0) return true;
    return confidence >= cfg.minConfidence;
  }

  function loadSettings(storage) {
    var out = {}, saved, raw, key;
    for (key in DEFAULTS) out[key] = DEFAULTS[key];
    try {
      raw = storage ? storage.getItem(STORAGE_KEY) : null;
      saved = raw ? JSON.parse(raw) : null;
      if (saved && typeof saved === 'object') {
        if (typeof saved.wake === 'boolean') out.wake = saved.wake;
        if (typeof saved.tts === 'boolean') out.tts = saved.tts;
        if (SENSITIVITY[saved.sensitivity]) out.sensitivity = saved.sensitivity;
        if (typeof saved.lang === 'string' && /^[a-z]{2,3}-[A-Z]{2}$/.test(saved.lang)) out.lang = saved.lang;
      }
    } catch (e) { /* corrupt or unavailable storage: defaults apply */ }
    return out;
  }

  function saveSettings(storage, settings) {
    try { storage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (e) { /* storage full or disabled */ }
  }

  // Short two-tone chime (Web Audio, fully local).
  function chime(ctx) {
    if (!ctx) return;
    if (ctx.state === 'suspended' && ctx.resume) ctx.resume();
    [880, 1320].forEach(function (freq, i) {
      var t0 = ctx.currentTime + i * 0.12;
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.15, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.4);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.42);
    });
  }

  // Speaks only with voices that run on this device (localService). Network voices are never used.
  function normalizeLang(value) { return String(value || '').toLowerCase().replace('_', '-'); }

  // The voice the speaker uses: an offline voice for the requested locale first, then any offline voice in the same
  // language. A network voice is never used, because a reply must not leave the device.
  function pickVoice(voices, lang) {
    var want = normalizeLang(lang || 'en-IN');
    var base = want.slice(0, 2);
    var local = (voices || []).filter(function (v) { return v && v.localService; });
    var exact = local.filter(function (v) { return normalizeLang(v.lang) === want; })[0];
    if (exact) return exact;
    return local.filter(function (v) { return normalizeLang(v.lang).indexOf(base) === 0; })[0] || null;
  }

  // Text for the speaker: chat formatting, code, links and bullets are removed, and the length is capped.
  function speechText(text, limit) {
    var s = String(text || '')
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/`([^`]*)`/g, '$1')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/\*([^*]+)\*/g, '$1')
      .replace(/^\s*#{1,6}\s*/gm, '')
      .replace(/^\s*[-*+]\s+/gm, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, ' ')
      .replace(/\|/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return s.slice(0, limit || 600);
  }

  // Speaks a reply with a steady pace. Returns false when speech is off, when there is nothing to say, or when this
  // device has no offline voice for the language.
  function speakReply(text, lang, enabled, synth, UtteranceCtor) {
    var s = synth || root.speechSynthesis;
    var U = UtteranceCtor || root.SpeechSynthesisUtterance;
    if (!enabled || !text || !s || !U) return false;
    var voices = s.getVoices ? s.getVoices() : [];
    var voice = pickVoice(voices, lang);
    var spoken = speechText(text, 600);
    if (!voice || !spoken) return false;
    var utterance = new U(spoken);
    utterance.voice = voice;
    utterance.lang = voice.lang;
    utterance.rate = 0.95;
    utterance.pitch = 1;
    s.speak(utterance);
    return true;
  }

  // Defence in depth: the browser opens only destinations the server is allowed to produce.
  var SAFE_HOSTS = ['www.youtube.com', 'web.whatsapp.com', 'wa.me'];
  function isSafeActionUrl(url) {
    var s = String(url || '');
    if (/^tel:\+?\d{8,15}$/.test(s)) return true;
    if (/^whatsapp:\/\/send\?phone=\d{8,15}$/.test(s)) return true;
    try {
      var u = new URL(s);
      return u.protocol === 'https:' && SAFE_HOSTS.indexOf(u.hostname) !== -1;
    } catch (e) {
      return false;
    }
  }

  var ORB_HTML = '<span class="orb-ring"></span><span class="orb-core"></span>' +
                 '<span class="orb-bars"><i></i><i></i><i></i><i></i><i></i></span>';

  // The voice orb (bottom-right). data-state drives the CSS: off | listening | activated | thinking | denied | error.
  function mountOrb(container) {
    var orb = document.createElement('div');
    orb.className = 'galaxy-orb';
    orb.setAttribute('role', 'status');
    orb.setAttribute('aria-label', 'Galaxy voice status');
    orb.setAttribute('data-state', 'off');
    orb.innerHTML = ORB_HTML;
    (container || document.body).appendChild(orb);
    return { el: orb, setState: function (state) { orb.setAttribute('data-state', state); } };
  }

  var api = {
    mountOrb: mountOrb,
    WAKE_PHRASES: WAKE_PHRASES, SENSITIVITY: SENSITIVITY, DEFAULTS: DEFAULTS,
    normalize: normalize, editDistance: editDistance, detectWakeWord: detectWakeWord, shouldAccept: shouldAccept,
    loadSettings: loadSettings, saveSettings: saveSettings,
    speakReply: speakReply, pickVoice: pickVoice, speechText: speechText, isSafeActionUrl: isSafeActionUrl, chime: chime
  };
  root.GalaxyVoice = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
