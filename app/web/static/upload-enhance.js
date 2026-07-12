(() => {
  function initUploadEnhance() {
    const form = document.getElementById("upload-form");
    const dropzone = document.getElementById("uploadDropzone") || document.getElementById("upload-dropzone");
    const fileInput = document.getElementById("fileInput") || document.getElementById("file-input");
    const selectedList = document.getElementById("selectedFiles") || document.getElementById("selected-files");
    const uploadResults = document.getElementById("uploadResults") || document.getElementById("upload-results");
    const uploadMessage = document.getElementById("upload-message");
    const uploadButton = document.getElementById("uploadButton") || document.getElementById("upload-button");
    const clearButton = document.getElementById("clear-files");

    if (!form || !dropzone || !fileInput || !selectedList || !uploadResults || !uploadButton) return;

    form.classList.add("enhanced");
    let selectedFiles = [];
    let uploading = false;

    console.info("[LuomoFile] upload enhance initialized");

    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));

    const humanSize = (bytes) => {
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
      return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
    };

    const showMessage = (text, kind = "info") => {
      if (uploadMessage) {
        uploadMessage.hidden = !text;
        uploadMessage.className = `upload-message full ${kind}`;
        uploadMessage.textContent = text;
      }
      if (text) {
        uploadResults.dataset.lastMessage = text;
      }
    };

    const showError = (text) => {
      showMessage(text, "error");
      const node = document.createElement("div");
      node.className = "upload-result upload-error";
      node.innerHTML = `<div><strong>Upload</strong><small>Failed</small><div>${esc(text)}</div></div>`;
      uploadResults.appendChild(node);
    };

    const renderSelected = () => {
      uploadButton.disabled = uploading;
      if (clearButton) clearButton.disabled = uploading || selectedFiles.length === 0;
      if (selectedFiles.length === 0) {
        selectedList.innerHTML = `<div class="muted">No files selected.</div>`;
        return;
      }
      selectedList.innerHTML = selectedFiles.map((file, index) => `
        <div class="selected-file" data-index="${index}">
          <div>
            <strong>${esc(file.name)}</strong>
            <small>${humanSize(file.size)} / Ready</small>
          </div>
          <span class="upload-file-status">Selected</span>
          <div class="upload-progress"><span style="width: 0%"></span></div>
        </div>
      `).join("");
    };

    const setSelectedFiles = (fileList) => {
      if (uploading) return;
      selectedFiles = Array.from(fileList || []);
      console.info("[LuomoFile] files selected:", selectedFiles.length);
      uploadResults.innerHTML = "";
      showMessage("");
      renderSelected();
    };

    const selectedRow = (index) => selectedList.querySelector(`.selected-file[data-index="${index}"]`);

    const setRowStatus = (index, text, percent, error = false) => {
      const row = selectedRow(index);
      if (!row) return;
      const status = row.querySelector(".upload-file-status");
      const bar = row.querySelector(".upload-progress span");
      if (status) {
        status.textContent = text;
        status.classList.toggle("error", error);
      }
      if (bar) bar.style.width = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
    };

    const uploadUrl = () => form.getAttribute("action") || window.location.pathname;

    const buildFormData = (file) => {
      const data = new FormData();
      data.append("file", file, file.name);
      for (const element of Array.from(form.elements)) {
        if (!element.name || element.type === "file") continue;
        if ((element.type === "checkbox" || element.type === "radio") && !element.checked) continue;
        data.append(element.name, element.value);
      }
      if (!data.has("generate_link")) data.append("generate_link", "false");
      return data;
    };

    const detailUrl = (payload) => {
      if (!payload.file_id) return "";
      return `${form.dataset.detailBase || "/files/"}${encodeURIComponent(payload.file_id)}`;
    };

    const copyText = async (text, button) => {
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch (_) {
        window.prompt("Copy this link", text);
      }
    };

    const appendResult = (file, payload, ok, errorText = "") => {
      const publicUrl = payload.public_url || "";
      const detail = detailUrl(payload);
      const node = document.createElement("div");
      node.className = `upload-result ${ok ? "upload-success" : "upload-error"}`;
      node.innerHTML = `
        <div>
          <strong>${esc(file?.name || "Upload")}</strong>
          <small>${ok ? "Uploaded" : "Failed"}</small>
          <div>${ok ? (publicUrl ? esc(publicUrl) : "Saved privately") : esc(errorText || "Upload failed")}</div>
        </div>
        <div class="upload-result-actions"></div>
      `;
      const actions = node.querySelector(".upload-result-actions");
      if (ok && publicUrl) {
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "btn secondary";
        copy.textContent = "Copy Link";
        copy.addEventListener("click", () => copyText(publicUrl, copy));
        actions.appendChild(copy);

        const open = document.createElement("a");
        open.className = "btn secondary";
        open.href = publicUrl;
        open.target = "_blank";
        open.rel = "noopener";
        open.textContent = "Open";
        actions.appendChild(open);
      }
      if (ok && detail) {
        const details = document.createElement("a");
        details.className = "btn secondary";
        details.href = detail;
        details.textContent = "Details";
        actions.appendChild(details);
      }
      uploadResults.appendChild(node);
    };

    const uploadOne = (file, index) => new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      const url = uploadUrl();
      console.info("[LuomoFile] uploading to:", url);
      showMessage("Uploading...", "info");
      setRowStatus(index, "Uploading...", 1);
      xhr.upload.addEventListener("progress", (event) => {
        const percent = event.lengthComputable ? (event.loaded / event.total) * 100 : 5;
        showMessage("Uploading...", "info");
        setRowStatus(index, "Uploading...", Math.max(1, percent));
      });
      xhr.addEventListener("load", () => {
        let payload = {};
        try {
          payload = JSON.parse(xhr.responseText || "{}");
        } catch (_) {
          payload = {};
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          setRowStatus(index, "Uploaded", 100);
          appendResult(file, payload, true);
        } else {
          const detail = typeof payload.detail === "string" ? payload.detail : "";
          const errorText = detail || payload.message || payload.error || `Failed: Upload failed (${xhr.status})`;
          setRowStatus(index, "Failed", 0, true);
          appendResult(file, payload, false, errorText);
        }
        resolve();
      });
      xhr.addEventListener("error", () => {
        setRowStatus(index, "Failed", 0, true);
        appendResult(file, {}, false, "Failed: Network error while uploading. Please try again.");
        resolve();
      });
      try {
        xhr.open("POST", url, true);
        xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
        xhr.setRequestHeader("Accept", "application/json");
        xhr.send(buildFormData(file));
      } catch (error) {
        const message = `Failed: ${error?.message || "Could not start upload."}`;
        setRowStatus(index, message, 0, true);
        appendResult(file, {}, false, message);
        resolve();
      }
    });

    const handleUploadClick = async (event) => {
      if (event) event.preventDefault();
      console.info("[LuomoFile] upload button clicked");
      uploadResults.innerHTML = "";
      showMessage("Preparing upload...", "info");
      if (!selectedFiles || selectedFiles.length === 0) {
        showError("Please choose one or more files.");
        return;
      }
      if (uploading) return;
      uploading = true;
      renderSelected();
      try {
        for (let index = 0; index < selectedFiles.length; index += 1) {
          await uploadOne(selectedFiles[index], index);
        }
        showMessage("Uploaded", "success");
        selectedFiles = [];
        fileInput.value = "";
        renderSelected();
      } catch (error) {
        showError(`Failed: ${error?.message || "Unexpected upload error."}`);
      } finally {
        uploading = false;
        renderSelected();
      }
    };

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", (event) => setSelectedFiles(event.dataTransfer.files));
    fileInput.addEventListener("change", () => setSelectedFiles(fileInput.files));
    if (clearButton) {
      clearButton.addEventListener("click", () => {
        selectedFiles = [];
        fileInput.value = "";
        uploadResults.innerHTML = "";
        showMessage("");
        renderSelected();
      });
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      handleUploadClick(event);
    });
    uploadButton.addEventListener("click", handleUploadClick);

    renderSelected();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initUploadEnhance);
  } else {
    initUploadEnhance();
  }
})();
