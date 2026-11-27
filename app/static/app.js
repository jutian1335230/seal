(function () {
  const filter = document.getElementById("filter");
  if (filter) {
    filter.addEventListener("input", function () {
      const query = filter.value.trim().toLowerCase();
      document.querySelectorAll("[data-filter]").forEach(function (row) {
        row.hidden = Boolean(query) && row.dataset.filter.indexOf(query) === -1;
      });
    });
  }

  const drop = document.getElementById("drop");
  const agreement = document.getElementById("agreement");
  if (drop && agreement) {
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.add("hot");
      });
    });
    drop.addEventListener("dragleave", function () { drop.classList.remove("hot"); });
    drop.addEventListener("drop", function (event) {
      event.preventDefault();
      drop.classList.remove("hot");
      if (event.dataTransfer.files.length) {
        agreement.files = event.dataTransfer.files;
        const strong = drop.querySelector("strong");
        if (strong) strong.textContent = event.dataTransfer.files[0].name;
      }
    });
    agreement.addEventListener("change", function () {
      if (agreement.files.length) {
        const strong = drop.querySelector("strong");
        if (strong) strong.textContent = agreement.files[0].name;
      }
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const input = document.getElementById(button.dataset.copy);
      if (!input) return;
      try {
        await navigator.clipboard.writeText(input.value);
        const previous = button.textContent;
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = previous; }, 1200);
      } catch (error) {
        input.focus();
        input.select();
      }
    });
  });

  const stateNode = document.getElementById("file-state");
  if (!stateNode) return;
  const state = JSON.parse(stateNode.textContent);
  const pages = document.getElementById("pages");
  const inspector = document.getElementById("inspector");
  const signerSelect = document.getElementById("inspector-signer");
  const statusLine = document.getElementById("save-state");
  const sizes = {
    signature: [0.32, 0.05],
    initials: [0.1, 0.04],
    name: [0.28, 0.032],
    date: [0.2, 0.032]
  };
  let tool = "select";
  let selected = null;
  let drag = null;

  function setStatus(text) {
    if (statusLine) statusLine.textContent = text || "";
  }

  function signerById(id) {
    return state.signers.find(function (signer) { return signer.id === id; });
  }

  function tagFor(kind, signerId) {
    const signer = signerById(signerId);
    const who = signer ? signer.name.split(" ")[0] : "Signer";
    const label = kind.charAt(0).toUpperCase() + kind.slice(1);
    return label + " · " + who;
  }

  function classFor(signerId) {
    const signer = signerById(signerId);
    return signer && signer.role !== "client" ? "firm" : "client";
  }

  function fillSignerSelect() {
    if (!signerSelect) return;
    signerSelect.innerHTML = "";
    state.signers.forEach(function (signer) {
      const option = document.createElement("option");
      option.value = signer.id;
      option.textContent = signer.name + (signer.role === "client" ? " · client" : " · our side");
      signerSelect.appendChild(option);
    });
  }

  function paintField(field, data) {
    field.dataset.signer = data.signer_id;
    field.dataset.kind = data.kind;
    field.style.left = (data.x * 100) + "%";
    field.style.top = (data.y * 100) + "%";
    field.style.width = (data.w * 100) + "%";
    field.style.height = (data.h * 100) + "%";
    field.classList.toggle("firm", data.css === "firm");
    field.classList.toggle("client", data.css !== "firm");
    const tag = field.querySelector(".field-tag");
    if (tag) tag.textContent = data.tag || tagFor(data.kind, data.signer_id);
  }

  function buildField(data) {
    const field = document.createElement("div");
    field.className = "field " + (data.css || classFor(data.signer_id));
    field.dataset.id = data.id;
    field.dataset.kind = data.kind;
    field.dataset.signer = data.signer_id;
    const tag = document.createElement("span");
    tag.className = "field-tag";
    tag.textContent = data.tag || tagFor(data.kind, data.signer_id);
    field.appendChild(tag);
    if (state.editable) {
      const handle = document.createElement("i");
      handle.className = "handle";
      handle.dataset.resize = "";
      field.appendChild(handle);
    }
    paintField(field, data);
    return field;
  }

  function selectField(field) {
    document.querySelectorAll(".field.selected").forEach(function (item) { item.classList.remove("selected"); });
    selected = field;
    if (!field || !inspector) return;
    field.classList.add("selected");
    inspector.hidden = false;
    if (signerSelect) signerSelect.value = field.dataset.signer;
  }

  async function send(url, method, body) {
    const response = await fetch(url, {
      method: method,
      headers: body ? { "Content-Type": "application/json", "Accept": "application/json" } : { "Accept": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    });
    if (!response.ok) {
      let detail = "Couldn't save that change.";
      try {
        const payload = await response.json();
        if (payload.detail) detail = payload.detail;
      } catch (error) { /* keep the fallback */ }
      setStatus(detail);
      throw new Error(detail);
    }
    setStatus("Saved");
    if (response.status === 204) return null;
    return response.json();
  }

  function defaultSigner() {
    return state.signers.find(function (signer) { return signer.role === "client"; }) || state.signers[0];
  }

  document.querySelectorAll(".tool").forEach(function (button) {
    button.addEventListener("click", function () {
      tool = button.dataset.tool;
      document.querySelectorAll(".tool").forEach(function (item) { item.classList.toggle("on", item === button); });
      const hint = document.getElementById("tool-hint");
      if (!hint) return;
      hint.textContent = tool === "select"
        ? "Drag a field to move it. Drag the corner to resize it."
        : "Click the line where the " + tool + " should go.";
    });
  });

  if (pages && state.editable) {
    pages.addEventListener("click", async function (event) {
      if (event.target.closest(".field")) return;
      if (tool === "select") return;
      const sheet = event.target.closest(".sheet");
      if (!sheet) return;
      const signer = defaultSigner();
      if (!signer) {
        setStatus("Add a signer before placing fields.");
        return;
      }
      const rect = sheet.getBoundingClientRect();
      const size = sizes[tool];
      // People click on the line they want signed, so the box sits on top of the click.
      const x = ((event.clientX - rect.left) / rect.width) - size[0] / 2;
      const y = ((event.clientY - rect.top) / rect.height) - size[1] * 0.9;
      try {
        const created = await send("/api/files/" + state.fileId + "/fields", "POST", {
          signer_id: signer.id,
          kind: tool,
          page: Number(sheet.dataset.page),
          x: x,
          y: y,
          w: size[0],
          h: size[1]
        });
        const overlay = sheet.querySelector(".overlay");
        const node = buildField(created);
        overlay.appendChild(node);
        selectField(node);
      } catch (error) { /* status already set */ }
    });

    pages.addEventListener("pointerdown", function (event) {
      const field = event.target.closest(".field");
      if (!field) return;
      event.stopPropagation();
      selectField(field);
      const sheet = field.closest(".sheet");
      const rect = sheet.getBoundingClientRect();
      const box = field.getBoundingClientRect();
      drag = {
        field: field,
        sheet: sheet,
        mode: event.target.closest("[data-resize]") ? "resize" : "move",
        dx: event.clientX - box.left,
        dy: event.clientY - box.top,
        startW: box.width / rect.width,
        startH: box.height / rect.height
      };
      field.setPointerCapture(event.pointerId);
    });

    pages.addEventListener("pointermove", function (event) {
      if (!drag) return;
      const rect = drag.sheet.getBoundingClientRect();
      if (drag.mode === "move") {
        const width = parseFloat(drag.field.style.width) / 100;
        const height = parseFloat(drag.field.style.height) / 100;
        let x = (event.clientX - drag.dx - rect.left) / rect.width;
        let y = (event.clientY - drag.dy - rect.top) / rect.height;
        x = Math.min(Math.max(x, 0), 1 - width);
        y = Math.min(Math.max(y, 0), 1 - height);
        drag.field.style.left = (x * 100) + "%";
        drag.field.style.top = (y * 100) + "%";
      } else {
        const left = parseFloat(drag.field.style.left) / 100;
        const top = parseFloat(drag.field.style.top) / 100;
        let width = Math.max(0.06, (event.clientX - rect.left) / rect.width - left);
        let height = Math.max(0.03, (event.clientY - rect.top) / rect.height - top);
        width = Math.min(width, 1 - left);
        height = Math.min(height, 1 - top);
        drag.field.style.width = (width * 100) + "%";
        drag.field.style.height = (height * 100) + "%";
      }
    });

    pages.addEventListener("pointerup", async function () {
      if (!drag) return;
      const field = drag.field;
      drag = null;
      try {
        const updated = await send("/api/files/" + state.fileId + "/fields/" + field.dataset.id, "PATCH", {
          x: parseFloat(field.style.left) / 100,
          y: parseFloat(field.style.top) / 100,
          w: parseFloat(field.style.width) / 100,
          h: parseFloat(field.style.height) / 100
        });
        paintField(field, updated);
      } catch (error) { /* status already set */ }
    });
  }

  fillSignerSelect();
  if (signerSelect) {
    signerSelect.addEventListener("change", async function () {
      if (!selected) return;
      try {
        const updated = await send("/api/files/" + state.fileId + "/fields/" + selected.dataset.id, "PATCH", {
          signer_id: signerSelect.value
        });
        paintField(selected, updated);
      } catch (error) { /* status already set */ }
    });
  }

  const removeButton = document.getElementById("inspector-delete");
  async function removeSelected() {
    if (!selected) return;
    const field = selected;
    try {
      await send("/api/files/" + state.fileId + "/fields/" + field.dataset.id, "DELETE");
      field.remove();
      selected = null;
      if (inspector) inspector.hidden = true;
    } catch (error) { /* status already set */ }
  }
  if (removeButton) removeButton.addEventListener("click", removeSelected);
  document.addEventListener("keydown", function (event) {
    if (!selected) return;
    if (event.target.matches("input, textarea, select")) return;
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      removeSelected();
    }
  });

  if (state.status !== "complete") {
    setInterval(async function () {
      try {
        const response = await fetch("/api/files/" + state.fileId + "/status");
        if (!response.ok) return;
        const data = await response.json();
        if (data.status !== state.status) location.reload();
      } catch (error) { /* leave the page as it is */ }
    }, 12000);
  }
})();
