/*
 * Galaxy page logic that does not touch the DOM or the network: input checks, payloads, error wording, timeouts.
 * Shared by app.js, device_permissions.js and voice_ui.js, and tested in Node (tests/js/app_logic.test.js).
 */
(function (root) {
  'use strict';

  var MAX_MESSAGE = 4000;
  var MAX_UPLOAD = 5 * 1024 * 1024;
  var REQUEST_TIMEOUT_MS = 25000;
  var UPLOAD_TYPES = { 'image/png': true, 'image/jpeg': true, 'image/gif': true, 'image/webp': true, 'application/pdf': true };
  var CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

  function round6(n) { return Math.round(n * 1e6) / 1e6; }

  function cleanText(value, limit) {
    var text = String(value === null || value === undefined ? '' : value);
    if (typeof text.normalize === 'function') { text = text.normalize('NFC'); }
    return text.replace(CONTROL, '').trim().slice(0, limit);
  }

  function checkMessage(value) {
    if (typeof value !== 'string') { return { value: '', error: 'Message text hona chahiye.' }; }
    var text = cleanText(value, MAX_MESSAGE + 1);
    if (text.length > MAX_MESSAGE) { return { value: '', error: 'Message 4000 characters se lamba hai.' }; }
    return { value: text, error: null };
  }

  function checkLocation(location) {
    if (location === null || location === undefined) { return { value: null, error: null }; }
    var lat = typeof location.lat === 'number' ? location.lat : NaN;
    var lon = typeof location.lon === 'number' ? location.lon : NaN;
    if (!isFinite(lat) || !isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
      return { value: null, error: 'Location galat hai.' };
    }
    var accuracy = typeof location.accuracy === 'number' && isFinite(location.accuracy)
      && location.accuracy >= 0 && location.accuracy <= 100000 ? location.accuracy : null;
    return { value: { lat: round6(lat), lon: round6(lon), accuracy: accuracy }, error: null };
  }

  function checkFile(file) {
    if (!file) { return { error: 'File chuniye.' }; }
    if (!UPLOAD_TYPES[file.type]) { return { error: 'Ye file type support nahi hai. Image ya PDF chuniye.' }; }
    if (!(file.size > 0)) { return { error: 'File khali hai.' }; }
    if (file.size > MAX_UPLOAD) { return { error: 'File 5 MB se badi hai.' }; }
    return { error: null };
  }

  function safeName(name) {
    var base = String(name || 'file').split(/[\\/]/).pop();
    var clean = base.replace(/[^A-Za-z0-9._-]/g, '_').replace(/^[._]+|[._]+$/g, '').slice(0, 120);
    return clean || 'file';
  }

  function buildChatPayload(text, options) {
    var opts = options || {};
    var message = checkMessage(text);
    if (message.error) { return { payload: null, error: message.error }; }
    if (!message.value) { return { payload: null, error: 'Kuch likhiye ya bolein.' }; }
    var location = checkLocation(opts.location);
    if (location.error) { return { payload: null, error: location.error }; }
    var payload = {
      message: message.value,
      conversation_id: 'default',
      source: opts.source === 'voice' ? 'voice' : 'text'
    };
    if (location.value) { payload.location = location.value; }
    return { payload: payload, error: null };
  }

  function parseJson(raw) {
    try { return { ok: true, value: JSON.parse(raw) }; } catch (e) { return { ok: false, value: null }; }
  }

  function isReply(result) {
    return !!(result && result.body && typeof result.body.text === 'string' && result.body.text.trim() !== '');
  }

  function describeFailure(result) {
    if (!result) { return 'Kuch gadbad hui. Dobara try karein.'; }
    if (result.timeout) { return 'Jawab me der ho rahi hai. Dobara try karein.'; }
    if (result.networkError) { return 'Server tak nahi pahunch paya. Internet check karein, ya 1 minute baad try karein.'; }
    if (result.parseError) { return 'Server ne kuch aur bheja. Dobara try karein.'; }
    if (isReply(result)) { return result.body.text; }
    return 'Server se valid jawab nahi mila (status ' + (result.status || '?') + ').';
  }

  function describeMediaError(name) {
    switch (name) {
      case 'NotAllowedError':
      case 'PermissionDeniedError':
      case 'SecurityError':
        return { state: 'denied', message: 'Permission nahi mili. Browser settings me is site ko allow karein.' };
      case 'NotFoundError':
      case 'DevicesNotFoundError':
        return { state: 'error', message: 'Is device par ye hardware nahi mila.' };
      case 'NotReadableError':
      case 'TrackStartError':
        return { state: 'error', message: 'Hardware kisi aur app me use ho raha hai.' };
      case 'OverconstrainedError':
      case 'ConstraintNotSatisfiedError':
        return { state: 'error', message: 'Is device par ye setting support nahi hoti.' };
      case 'AbortError':
        return { state: 'error', message: 'Request radd ho gayi. Dobara try karein.' };
      default:
        return { state: 'error', message: 'Kuch gadbad hui. Dobara try karein.' };
    }
  }

  function describeGeoError(code) {
    if (code === 1) { return { state: 'denied', message: 'Location ki permission nahi mili. Settings me allow karein.' }; }
    if (code === 2) { return { state: 'error', message: 'Location abhi nahi mil rahi. GPS on karke dobara try karein.' }; }
    if (code === 3) { return { state: 'error', message: 'Location mil ne me der ho gayi. Dobara try karein.' }; }
    return { state: 'error', message: 'Location nahi mil payi.' };
  }

  function formatLocation(location) {
    if (!location) { return ''; }
    var accuracy = typeof location.accuracy === 'number' ? ' ±' + Math.round(location.accuracy) + ' m' : '';
    return location.lat.toFixed(4) + ', ' + location.lon.toFixed(4) + accuracy;
  }

  function withTimeout(promise, ms) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { reject(new Error('timeout')); }, ms);
      promise.then(function (value) { clearTimeout(timer); resolve(value); },
                   function (error) { clearTimeout(timer); reject(error); });
    });
  }

  var api = {
    MAX_MESSAGE: MAX_MESSAGE, MAX_UPLOAD: MAX_UPLOAD, REQUEST_TIMEOUT_MS: REQUEST_TIMEOUT_MS,
    cleanText: cleanText, checkMessage: checkMessage, checkLocation: checkLocation, checkFile: checkFile,
    safeName: safeName, buildChatPayload: buildChatPayload, parseJson: parseJson, isReply: isReply,
    describeFailure: describeFailure, describeMediaError: describeMediaError, describeGeoError: describeGeoError,
    formatLocation: formatLocation, withTimeout: withTimeout
  };
  root.GalaxyLogic = api;
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; }
})(typeof window !== 'undefined' ? window : globalThis);
