/*
 * Galaxy device permissions: the microphone, the location and the camera. Each one first shows a short card that says
 * why it is needed, then the browser's own prompt. Every request resolves to { state, message, ... } and never throws.
 * States: granted, denied, declined, unsupported, error.
 */
(function (root) {
  'use strict';

  var L = root.GalaxyLogic;
  var GEO_OPTIONS = { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 };
  var pendingAnswer = null;

  function $(id) { return document.getElementById(id); }

  function secureContext() { return root.isSecureContext !== false; }

  function queryState(name) {
    var permissions = root.navigator && root.navigator.permissions;
    if (!permissions || typeof permissions.query !== 'function') { return Promise.resolve('prompt'); }
    return permissions.query({ name: name }).then(function (status) { return status.state; }, function () { return 'prompt'; });
  }

  // The explanation card. It does not cover the chat. Resolves true when the person allows, false otherwise.
  function ask(title, body) {
    var card = $('permCard');
    if (!card) { return Promise.resolve(true); }
    if (pendingAnswer) { pendingAnswer(false); }
    $('permTitle').textContent = title;
    $('permBody').textContent = body;
    card.hidden = false;
    return new Promise(function (resolve) {
      pendingAnswer = function (answer) {
        pendingAnswer = null;
        card.hidden = true;
        $('permYes').onclick = null;
        $('permNo').onclick = null;
        resolve(answer);
      };
      $('permYes').onclick = function () { pendingAnswer(true); };
      $('permNo').onclick = function () { pendingAnswer(false); };
      $('permYes').focus();
    });
  }

  function failure(error) {
    var mapped = L.describeMediaError(error && error.name);
    return { state: mapped.state, message: mapped.message };
  }

  function stopStream(stream) {
    if (stream && typeof stream.getTracks === 'function') {
      stream.getTracks().forEach(function (track) { track.stop(); });
    }
  }

  function microphone() {
    if (!secureContext()) {
      return Promise.resolve({ state: 'error', message: 'Mic ke liye https chahiye.' });
    }
    var media = root.navigator && root.navigator.mediaDevices;
    if (!media || typeof media.getUserMedia !== 'function') {
      return Promise.resolve({ state: 'unsupported', message: 'Ye browser mic access nahi deta.' });
    }
    return queryState('microphone').then(function (state) {
      if (state === 'denied') {
        return { state: 'denied', message: 'Mic ki permission band hai. Browser settings me allow karein.' };
      }
      return ask('Microphone',
                 'Voice commands ke liye mic chahiye. Audio is device par hi process hota hai.')
        .then(function (allowed) {
          if (!allowed) {
            return { state: 'declined', message: 'Voice band hai. Text se poochhiye.' };
          }
          return media.getUserMedia({ audio: true }).then(function (stream) {
            stopStream(stream);   // the permission is what we needed; the voice engine opens its own stream later
            return { state: 'granted', message: 'Mic ready hai.' };
          }, failure);
        });
    });
  }

  function toLocation(position) {
    return { lat: position.coords.latitude, lon: position.coords.longitude, accuracy: position.coords.accuracy };
  }

  function location() {
    var geo = root.navigator && root.navigator.geolocation;
    if (!geo) {
      return Promise.resolve({ state: 'unsupported', message: 'Ye browser location nahi deta.' });
    }
    if (!secureContext()) {
      return Promise.resolve({ state: 'error', message: 'Location ke liye https chahiye.' });
    }
    return queryState('geolocation').then(function (state) {
      if (state === 'denied') {
        return { state: 'denied', message: 'Location ki permission band hai. Settings me allow karein.' };
      }
      return ask('Location',
                 'Location sirf location bhejne par ya Live on hone par bheji jayegi. Kabhi bhi band kar sakte hain.')
        .then(function (allowed) {
          if (!allowed) {
            return { state: 'declined', message: 'Location nahi bheji jayegi.' };
          }
          return new Promise(function (resolve) {
            geo.getCurrentPosition(function (position) {
              resolve({ state: 'granted', location: toLocation(position) });
            }, function (error) {
              var mapped = L.describeGeoError(error && error.code);
              resolve({ state: mapped.state, message: mapped.message });
            }, GEO_OPTIONS);
          });
        });
    });
  }

  // Live tracking: only after location() was granted, and only until stopLive() is called.
  function startLive(onUpdate, onError) {
    var geo = root.navigator && root.navigator.geolocation;
    if (!geo) { return null; }
    return geo.watchPosition(function (position) {
      onUpdate(toLocation(position));
    }, function (error) {
      onError(L.describeGeoError(error && error.code));
    }, GEO_OPTIONS);
  }

  function stopLive(watchId) {
    var geo = root.navigator && root.navigator.geolocation;
    if (watchId !== null && watchId !== undefined && geo) { geo.clearWatch(watchId); }
  }

  function camera() {
    return ask('Camera',
               'Camera phone ke camera app se khulega. Photo sirf aap lete hain.')
      .then(function (allowed) {
        if (!allowed) { return { state: 'declined', message: 'Theek hai, photo nahi lenge.' }; }
        return { state: 'granted', message: '' };
      });
  }

  // The file picker. It must open from a tap, so callers invoke it straight from a click handler.
  function pickFile(accept, capture) {
    return new Promise(function (resolve) {
      var input = $('fileInput');
      if (!input) { resolve(null); return; }
      input.value = '';
      input.accept = accept;
      if (capture) { input.setAttribute('capture', capture); } else { input.removeAttribute('capture'); }
      input.onchange = function () {
        var file = input.files && input.files[0];
        input.onchange = null;
        resolve(file || null);
      };
      input.click();
    });
  }

  root.GalaxyDevice = {
    ask: ask, microphone: microphone, location: location, camera: camera,
    startLive: startLive, stopLive: stopLive, pickFile: pickFile, queryState: queryState
  };
})(typeof window !== 'undefined' ? window : globalThis);
