(() => {
  const API = (window.APP_CONFIG?.API_BASE_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const fileInput = $("fileInput");
  let selectedFiles = [];
  let state = { documents: [], merged_files: [], storage: { used_bytes: 0, limit_bytes: 1073741824 } };
  let draggedId = null;
  let toastTimer;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }
  function timeLeft(timestamp) {
    const seconds = Math.max(0, Math.floor(timestamp - Date.now() / 1000));
    if (seconds < 60) return `${seconds}s left`;
    if (seconds < 3600) return `${Math.ceil(seconds / 60)}m left`;
    return `${Math.floor(seconds / 3600)}h ${Math.ceil((seconds % 3600) / 60)}m left`;
  }
  function notify(message, error = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.className = "toast", 3500);
  }
  async function request(path, options = {}) {
    let response;
    try {
      response = await fetch(`${API}${path}`, options);
    } catch {
      throw new Error("Could not reach the API. Check the API URL and backend availability.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status}).`);
    return data;
  }
  async function refresh() {
    try {
      state = await request("/api/state");
      render();
    } catch (error) {
      notify(error.message, true);
    }
  }
  function render() {
    const docs = state.documents || [];
    $("documentCount").textContent = `${docs.length} document${docs.length === 1 ? "" : "s"}`;
    $("documentList").innerHTML = docs.length ? docs.map((doc, index) => {
      const ext = doc.original_name.split(".").pop().toUpperCase().slice(0, 4);
      return `<div class="document-row" draggable="true" data-id="${doc.id}">
        <span class="drag-handle" title="Drag to reorder" aria-label="Drag to reorder">⠿</span>
        <span class="file-icon">${escapeHtml(ext)}</span>
        <div class="file-info">
          <div class="file-name" title="${escapeHtml(doc.original_name)}">${escapeHtml(doc.original_name)}</div>
          <div class="file-meta">${formatBytes(doc.size)} · ${timeLeft(doc.expires_at)}</div>
        </div>
        <div class="row-actions">
          <button class="icon-button" data-action="up" data-id="${doc.id}" title="Move up" aria-label="Move up" ${index === 0 ? "disabled" : ""}>↑</button>
          <button class="icon-button" data-action="down" data-id="${doc.id}" title="Move down" aria-label="Move down" ${index === docs.length - 1 ? "disabled" : ""}>↓</button>
          <button class="icon-button delete" data-action="delete" data-id="${doc.id}" title="Remove" aria-label="Remove">×</button>
        </div>
      </div>`;
    }).join("") : '<div class="empty-state">Your uploaded documents will appear here.</div>';
    $("mergeButton").disabled = docs.length === 0;
    const outputs = state.merged_files || [];
    $("mergedList").innerHTML = outputs.length ? outputs.map(file => `
      <div class="merged-row">
        <span class="file-icon">PDF</span>
        <div class="file-info">
          <div class="file-name">Merged document · ${new Date(file.created_at * 1000).toLocaleString()}</div>
          <div class="file-meta">${formatBytes(file.size)} · ${timeLeft(file.expires_at)}</div>
        </div>
        <a class="button button-secondary" href="${API}/api/merged/${file.id}" target="_blank" rel="noopener">Download</a>
        <button class="icon-button delete" data-merged-delete="${file.id}" title="Delete merged PDF" aria-label="Delete merged PDF">×</button>
      </div>`).join("") : '<div class="empty-state">No merged PDFs yet.</div>';
    const used = state.storage?.used_bytes || 0;
    const limit = state.storage?.limit_bytes || 1073741824;
    $("storageText").textContent = `${formatBytes(used)} of ${formatBytes(limit)} used`;
    $("storageBar").style.width = `${Math.min(100, used / limit * 100)}%`;
  }
  function setSelected(files) {
    selectedFiles = Array.from(files);
    $("uploadStatus").textContent = selectedFiles.length
      ? `${selectedFiles.length} file(s) selected · ${formatBytes(selectedFiles.reduce((n, f) => n + f.size, 0))}`
      : "No files selected.";
    $("uploadButton").disabled = selectedFiles.length === 0;
  }
  fileInput.addEventListener("change", () => setSelected(fileInput.files));
  $("uploadButton").addEventListener("click", async () => {
    if (!selectedFiles.length) return;
    const form = new FormData();
    selectedFiles.forEach(file => form.append("files", file));
    $("uploadButton").disabled = true;
    $("uploadButton").textContent = "Uploading…";
    try {
      const result = await request("/api/documents", { method: "POST", body: form });
      const messages = [];
      if (result.uploaded.length) messages.push(`${result.uploaded.length} file(s) uploaded.`);
      if (result.errors.length) messages.push(result.errors.map(e => `${e.filename}: ${e.error}`).join(" "));
      state = result.state;
      render();
      notify(messages.join(" ") || "No files uploaded.", result.errors.length > 0);
      setSelected([]);
      fileInput.value = "";
    } catch (error) {
      notify(error.message, true);
    } finally {
      $("uploadButton").textContent = "Upload selected files";
      $("uploadButton").disabled = selectedFiles.length === 0;
    }
  });
  $("documentList").addEventListener("click", async event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.dataset.id;
    const index = state.documents.findIndex(d => d.id === id);
    try {
      if (button.dataset.action === "delete") {
        state = await request(`/api/documents/${id}`, { method: "DELETE" });
      } else {
        const next = [...state.documents];
        const target = button.dataset.action === "up" ? index - 1 : index + 1;
        if (target < 0 || target >= next.length) return;
        [next[index], next[target]] = [next[target], next[index]];
        state = await request("/api/documents/order", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ document_ids: next.map(d => d.id) })
        });
      }
      render();
    } catch (error) { notify(error.message, true); }
  });
  $("documentList").addEventListener("dragstart", event => {
    const row = event.target.closest(".document-row");
    if (!row) return;
    draggedId = row.dataset.id;
    row.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", draggedId);
  });
  $("documentList").addEventListener("dragend", () => {
    draggedId = null;
    document.querySelectorAll(".document-row").forEach(row => row.classList.remove("dragging", "drag-target"));
  });
  $("documentList").addEventListener("dragover", event => {
    event.preventDefault();
    const row = event.target.closest(".document-row");
    document.querySelectorAll(".document-row").forEach(item => item.classList.remove("drag-target"));
    if (row && row.dataset.id !== draggedId) row.classList.add("drag-target");
  });
  $("documentList").addEventListener("drop", async event => {
    event.preventDefault();
    const target = event.target.closest(".document-row");
    if (!target || !draggedId || target.dataset.id === draggedId) return;
    const ids = state.documents.map(d => d.id);
    const from = ids.indexOf(draggedId);
    const to = ids.indexOf(target.dataset.id);
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    try {
      state = await request("/api/documents/order", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: ids })
      });
      render();
    } catch (error) { notify(error.message, true); }
  });
  $("mergeButton").addEventListener("click", async () => {
    $("mergeButton").disabled = true;
    $("mergeButton").textContent = "Merging…";
    try {
      const result = await request("/api/merge", { method: "POST" });
      await refresh();
      notify(`PDF created (${formatBytes(result.size)}).`);
    } catch (error) {
      notify(error.message, true);
    } finally {
      $("mergeButton").textContent = "Merge into PDF";
      $("mergeButton").disabled = !(state.documents || []).length;
    }
  });
  $("mergedList").addEventListener("click", async event => {
    const button = event.target.closest("[data-merged-delete]");
    if (!button) return;
    try {
      state = await request(`/api/merged/${button.dataset.mergedDelete}`, { method: "DELETE" });
      render();
    } catch (error) { notify(error.message, true); }
  });
  $("refreshButton").addEventListener("click", refresh);
  const dropZone = $("dropZone");
  ["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => {
    event.preventDefault(); dropZone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => {
    event.preventDefault(); dropZone.classList.remove("dragover");
  }));
  dropZone.addEventListener("drop", event => setSelected(event.dataTransfer.files));
  refresh();
  setInterval(refresh, 30000);
})();
