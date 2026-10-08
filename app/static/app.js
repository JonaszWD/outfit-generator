/* Small behaviors, kept out of the templates so the Content-Security-Policy can forbid inline
   scripts. Everything is progressive: the pages work without it. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-dialog-open]");
    if (opener) {
      var dialog = document.querySelector(opener.getAttribute("data-dialog-open"));
      if (dialog && typeof dialog.showModal === "function") {
        event.preventDefault();  // a link falls back to its page only without dialog support
        dialog.showModal();
      }
      return;
    }
    var closer = event.target.closest("[data-dialog-close]");
    if (closer) {
      var parent = closer.closest("dialog");
      if (parent) { parent.close(); }
    }
  });

  // Choosing a photo in the add sheet uploads it straight away.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (input.matches && input.matches("input[type=file][data-autosubmit]") && input.files.length) {
      input.form.requestSubmit();
    }
  });

  // Phone photos are several MB, and a serverless host rejects request bodies over about
  // 4.5 MB. <input type="file" data-resize> is shrunk in the browser (longest side 1600px,
  // JPEG) when its form is submitted. A file the browser cannot decode (HEIC outside Safari)
  // is sent unchanged, and the server still validates and re-encodes everything.
  var RESIZE_MAX_SIDE = 1600;
  var RESIZE_QUALITY = 0.85;
  var RESIZE_SKIP_BELOW = 600 * 1024;

  function shrink(file) {
    if (!file.type || file.type.indexOf("image/") !== 0 || file.size < RESIZE_SKIP_BELOW ||
        typeof createImageBitmap !== "function") {
      return Promise.resolve(file);
    }
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
      var scale = Math.min(1, RESIZE_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      if (bitmap.close) { bitmap.close(); }
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) {
          if (!blob || blob.size >= file.size) { resolve(file); return; }
          var name = file.name.replace(/\.[^.]*$/, "") + ".jpg";
          resolve(new File([blob], name, { type: "image/jpeg", lastModified: file.lastModified }));
        }, "image/jpeg", RESIZE_QUALITY);
      });
    }).catch(function () { return file; });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.querySelectorAll || form.dataset.resized === "1") { return; }
    var inputs = Array.prototype.filter.call(
      form.querySelectorAll("input[type=file][data-resize]"),
      function (input) { return input.files && input.files.length; }
    );
    if (!inputs.length || typeof DataTransfer !== "function") { return; }
    event.preventDefault();
    Promise.all(inputs.map(function (input) {
      return Promise.all(Array.prototype.map.call(input.files, shrink)).then(function (files) {
        var transfer = new DataTransfer();
        files.forEach(function (file) { transfer.items.add(file); });
        input.files = transfer.files;
      });
    })).then(function () {
      form.dataset.resized = "1";
      form.requestSubmit();  // runs this handler again, which now lets the submit through
      delete form.dataset.resized;
    });
  }, true);

  // <input type="file" data-preview="#target">: show what was chosen before it is uploaded.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("input[type=file][data-preview]")) { return; }
    var target = document.querySelector(input.getAttribute("data-preview"));
    if (!target) { return; }
    var current = document.querySelector(input.getAttribute("data-hide") || "#none");
    var files = Array.prototype.filter.call(input.files, function (file) {
      return file.type.indexOf("image/") === 0 && file.type.indexOf("hei") === -1;
    });
    if (target.tagName === "IMG") {
      target.hidden = files.length === 0;
      if (files.length) { target.src = URL.createObjectURL(files[0]); }
      if (current && files.length) { current.hidden = true; }
      return;
    }
    target.replaceChildren();
    files.forEach(function (file) {
      var img = document.createElement("img");
      img.className = "preview";
      img.alt = "Preview of " + file.name;
      img.src = URL.createObjectURL(file);
      target.appendChild(img);
    });
  });

  // Range inputs show their value next to the slider.
  document.querySelectorAll("input[type=range][data-output]").forEach(function (slider) {
    var output = document.querySelector(slider.getAttribute("data-output"));
    function update() { if (output) { output.textContent = slider.value; } }
    slider.addEventListener("input", update);
    update();
  });

  // In-page camera for "Take a photo now". Falls back to the phone's own camera picker
  // (the plain <input capture>) when the browser has no getUserMedia or permission is denied.
  (function () {
    var opener = document.querySelector("[data-camera-open]");
    var fallback = document.querySelector("[data-camera-fallback]");
    var dialog = opener && document.querySelector(opener.getAttribute("data-camera-open"));
    var input = document.querySelector("#pick-camera");
    if (!opener || !dialog || !input || !navigator.mediaDevices ||
        !navigator.mediaDevices.getUserMedia || !window.DataTransfer) { return; }
    opener.hidden = false;
    if (fallback) { fallback.hidden = true; }
    var video = dialog.querySelector("video");
    var errorBox = dialog.querySelector("[data-camera-error]");
    var count = dialog.querySelector("[data-camera-count]");
    var stream = null;
    var shots = new DataTransfer();

    function stop() {
      if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); }
      stream = null;
      video.srcObject = null;
    }

    function fail(message) {
      errorBox.textContent = message;
      errorBox.hidden = false;
    }

    opener.addEventListener("click", function () {
      errorBox.hidden = true;
      dialog.showModal();
      navigator.mediaDevices
        .getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
        .then(function (s) { stream = s; video.srcObject = s; })
        .catch(function () {
          stop();
          fail("The camera is not available. Allow camera access in your browser, or choose from your photos.");
        });
    });

    dialog.querySelector("[data-camera-shoot]").addEventListener("click", function () {
      if (!stream || !video.videoWidth) { return; }
      var canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      canvas.toBlob(function (blob) {
        if (!blob) { fail("Could not take the photo. Try again."); return; }
        var name = "camera-" + Date.now() + ".jpg";
        shots.items.add(new File([blob], name, { type: "image/jpeg" }));
        input.files = shots.files;
        count.textContent = shots.files.length + " photo" + (shots.files.length === 1 ? "" : "s") + " taken.";
      }, "image/jpeg", 0.92);
    });

    opener.addEventListener("click", function () {
      var sheet = opener.closest("dialog");
      if (sheet) { sheet.close(); }
    });

    dialog.querySelector("[data-camera-done]").addEventListener("click", function () {
      dialog.close();
      if (shots.files.length) { input.form.requestSubmit(); }  // upload what was taken
    });
    dialog.addEventListener("close", stop);
  })();
})();
