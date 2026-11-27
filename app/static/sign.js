(function () {
  const form = document.getElementById("sign-form");
  const nameInput = document.getElementById("full_name");
  const modeInput = document.getElementById("mode");
  const preview = document.getElementById("typed-preview");
  const drawPanel = document.getElementById("draw-panel");
  const pad = document.getElementById("pad");
  const fileInput = document.getElementById("signature_png");
  const clearButton = document.getElementById("clear-pad");
  if (!form || !nameInput) return;

  let mode = "typed";
  const ctx = pad ? pad.getContext("2d") : null;
  let drawing = false;

  function initials(name) {
    return name.trim().split(/\s+/).filter(Boolean).map(function (part) { return part[0]; }).join("").toUpperCase().slice(0, 4);
  }

  function fit(node) {
    node.style.fontSize = "";
    if (!node.textContent || node.scrollWidth <= node.clientWidth) return;
    const size = parseFloat(getComputedStyle(node).fontSize);
    node.style.fontSize = (size * node.clientWidth / node.scrollWidth * 0.97) + "px";
  }

  function paint() {
    const name = nameInput.value.trim();
    if (preview) preview.textContent = name;
    document.querySelectorAll("[data-live]").forEach(function (node) {
      const kind = node.dataset.live;
      if (kind === "signature") {
        node.textContent = mode === "typed" ? name : "";
        node.style.backgroundImage = "";
      } else if (kind === "name") {
        node.textContent = name;
      } else if (kind === "initials") {
        node.textContent = initials(name);
      } else if (kind === "date") {
        node.textContent = form.dataset.date || "";
      }
      fit(node);
    });
  }

  window.addEventListener("resize", paint);
  if (document.fonts) {
    // The script font can arrive after the first fit, which would leave a long name cut off.
    document.fonts.addEventListener("loadingdone", paint);
    Promise.all([
      document.fonts.load('40px "Great Vibes"'),
      document.fonts.load('16px "Lato"')
    ]).then(paint, paint);
  }

  function sizePad() {
    if (!pad || !ctx) return;
    const rect = pad.getBoundingClientRect();
    const scale = 2;
    pad.width = Math.max(1, rect.width * scale);
    pad.height = Math.max(1, rect.height * scale);
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.lineWidth = 2.2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#3a1210";
  }

  function point(event) {
    const rect = pad.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  }

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      mode = tab.dataset.mode;
      modeInput.value = mode;
      document.querySelectorAll(".tab").forEach(function (item) { item.classList.toggle("on", item === tab); });
      if (drawPanel) drawPanel.hidden = mode !== "drawn";
      if (preview) preview.hidden = mode === "drawn";
      if (mode === "drawn") sizePad();
      paint();
    });
  });

  nameInput.addEventListener("input", paint);
  paint();

  if (pad && ctx) {
    sizePad();
    pad.addEventListener("pointerdown", function (event) {
      drawing = true;
      pad.setPointerCapture(event.pointerId);
      const at = point(event);
      ctx.beginPath();
      ctx.moveTo(at[0], at[1]);
    });
    pad.addEventListener("pointermove", function (event) {
      if (!drawing) return;
      const at = point(event);
      ctx.lineTo(at[0], at[1]);
      ctx.stroke();
    });
    pad.addEventListener("pointerup", function () { drawing = false; });
    pad.addEventListener("pointerleave", function () { drawing = false; });
  }

  if (clearButton) {
    clearButton.addEventListener("click", function () {
      sizePad();
    });
  }

  form.addEventListener("submit", function (event) {
    if (mode !== "drawn" || form.dataset.ready === "1") return;
    event.preventDefault();
    if (!pad || !ctx) {
      mode = "typed";
      modeInput.value = "typed";
      form.submit();
      return;
    }
    const pixels = ctx.getImageData(0, 0, pad.width, pad.height).data;
    let ink = 0;
    for (let index = 3; index < pixels.length; index += 16) {
      if (pixels[index] > 10) ink += 1;
    }
    if (ink < 8) {
      const message = form.querySelector(".flash.bad") || document.createElement("p");
      message.className = "flash bad";
      message.textContent = "Draw your signature, or switch back to typing it.";
      form.prepend(message);
      return;
    }
    pad.toBlob(function (blob) {
      const transfer = new DataTransfer();
      transfer.items.add(new File([blob], "signature.png", { type: "image/png" }));
      fileInput.files = transfer.files;
      form.dataset.ready = "1";
      form.submit();
    }, "image/png");
  });
})();
