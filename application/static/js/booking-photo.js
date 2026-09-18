/* Pick the square of a booking portrait before it is uploaded.
 *
 * The browser only PREVIEWS the crop and reports it: the picked rectangle
 * goes along as three numbers in natural-image pixels (crop_x, crop_y,
 * crop_size) and Pillow does the actual cut server-side. Nothing is
 * re-encoded here, so what gets stored doesn't depend on the browser's
 * canvas, and a visitor without JavaScript still gets the centre crop.
 *
 * The model is the usual one: a square window, the image laid over it,
 * dragged with the pointer and scaled with the slider. The window is what
 * the photo will be.
 */
(function () {
  "use strict";

  var input = document.getElementById("photo");
  var stage = document.getElementById("photo-crop");
  if (!input || !stage) { return; }

  var frame = stage.querySelector(".crop-frame");
  var img = stage.querySelector("img");
  var zoom = stage.querySelector("input[type=range]");
  var fields = {
    x: document.getElementById("crop_x"),
    y: document.getElementById("crop_y"),
    size: document.getElementById("crop_size"),
  };

  var VIEW = 240;        // the square window, in CSS pixels
  var natural = { w: 0, h: 0 };
  var scale = 1;         // natural pixels → CSS pixels
  var minScale = 1;
  var pos = { x: 0, y: 0 };   // top-left of the image inside the window
  var drag = null;

  function clamp() {
    // The window must stay covered: the image may not be dragged off it.
    var w = natural.w * scale;
    var h = natural.h * scale;
    pos.x = Math.min(0, Math.max(VIEW - w, pos.x));
    pos.y = Math.min(0, Math.max(VIEW - h, pos.y));
  }

  function paint() {
    clamp();
    img.style.width = (natural.w * scale) + "px";
    img.style.height = (natural.h * scale) + "px";
    img.style.transform = "translate(" + pos.x + "px," + pos.y + "px)";
    // What the window shows, expressed in the image's own pixels.
    fields.x.value = Math.round(-pos.x / scale);
    fields.y.value = Math.round(-pos.y / scale);
    fields.size.value = Math.round(VIEW / scale);
  }

  function fit() {
    minScale = Math.max(VIEW / natural.w, VIEW / natural.h);
    scale = minScale;
    zoom.min = "1";
    zoom.max = "3";
    zoom.step = "0.01";
    zoom.value = "1";
    pos.x = (VIEW - natural.w * scale) / 2;
    pos.y = (VIEW - natural.h * scale) / 2;
    paint();
  }

  function zoomTo(factor) {
    // Keep the centre of the window where it is while scaling around it.
    var next = minScale * factor;
    var cx = (VIEW / 2 - pos.x) / scale;
    var cy = (VIEW / 2 - pos.y) / scale;
    scale = next;
    pos.x = VIEW / 2 - cx * scale;
    pos.y = VIEW / 2 - cy * scale;
    paint();
  }

  input.addEventListener("change", function () {
    var file = input.files && input.files[0];
    if (!file) { stage.classList.add("d-none"); return; }
    var reader = new FileReader();
    reader.onload = function () {
      img.onload = function () {
        natural = { w: img.naturalWidth, h: img.naturalHeight };
        stage.classList.remove("d-none");
        fit();
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });

  zoom.addEventListener("input", function () {
    zoomTo(parseFloat(zoom.value) || 1);
  });

  frame.addEventListener("pointerdown", function (ev) {
    drag = { x: ev.clientX - pos.x, y: ev.clientY - pos.y };
    frame.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  frame.addEventListener("pointermove", function (ev) {
    if (!drag) { return; }
    pos.x = ev.clientX - drag.x;
    pos.y = ev.clientY - drag.y;
    paint();
  });
  ["pointerup", "pointercancel"].forEach(function (name) {
    frame.addEventListener(name, function () { drag = null; });
  });
  frame.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var step = ev.deltaY < 0 ? 0.08 : -0.08;
    var next = Math.min(3, Math.max(1, (parseFloat(zoom.value) || 1) + step));
    zoom.value = String(next);
    zoomTo(next);
  }, { passive: false });
})();
