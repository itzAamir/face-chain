"use strict";

const MAX_FILE_SIZE = 10 * 1024 * 1024;
const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const $ = (selector) => document.querySelector(selector);
const views = ["upload", "face", "search", "results", "error"];

let selectedFile = null;
let previewUrl = null;
let selectedFaceIndex = null;

function setView(name) {
  views.forEach((view) => { $(`#${view}-view`).hidden = view !== name; });
  $(".workspace-heading").hidden = name !== "upload";
}

function setActiveStep(stepNumber) {
  document.querySelectorAll(".step").forEach((step) => {
    const active = Number(step.dataset.step) === stepNumber;
    step.classList.toggle("active", active);
    if (active) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });
}

function formatBytes(bytes) {
  return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function updateContinueState() {
  $("#continue-button").disabled = !(selectedFile && $("#consent-input").checked);
  $("#selection-note").textContent = !selectedFile
    ? "Select a photo and confirm permission"
    : !$("#consent-input").checked ? "Confirm permission to continue" : "Ready to detect faces";
}

function clearSelection() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  selectedFile = null;
  selectedFaceIndex = null;
  $("#face-input").value = "";
  $("#image-preview").removeAttribute("src");
  $("#face-image").removeAttribute("src");
  $("#preview-card").hidden = true;
  $("#drop-zone").hidden = false;
  $("#consent-input").checked = false;
  $("#file-error").hidden = true;
  updateContinueState();
}

function selectFile(file) {
  $("#file-error").hidden = true;
  if (!ALLOWED_TYPES.has(file.type) || file.size > MAX_FILE_SIZE) {
    clearSelection();
    $("#file-error").textContent = !ALLOWED_TYPES.has(file.type)
      ? "Choose a JPG, PNG or WebP image." : "Choose an image smaller than 10 MB.";
    $("#file-error").hidden = false;
    return;
  }
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  selectedFile = file;
  previewUrl = URL.createObjectURL(file);
  $("#image-preview").src = previewUrl;
  $("#face-image").src = previewUrl;
  $("#file-name").textContent = file.name;
  $("#file-size").textContent = formatBytes(file.size);
  $("#drop-zone").hidden = true;
  $("#preview-card").hidden = false;
  updateContinueState();
}

async function apiRequest(url, formData) {
  const response = await fetch(url, { method: "POST", body: formData });
  let payload = null;
  try { payload = await response.json(); } catch { /* handled below */ }
  if (!response.ok) {
    const error = new Error(payload?.detail?.message || "The service returned an unexpected response.");
    error.code = payload?.detail?.code || "request_failed";
    throw error;
  }
  return payload;
}

function chooseFace(index) {
  selectedFaceIndex = index;
  document.querySelectorAll(".face-choice").forEach((button) => {
    const selected = Number(button.dataset.faceIndex) === index;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  $("#search-button").disabled = false;
}

function renderFacePicker(faces) {
  const overlay = $("#face-overlay");
  overlay.replaceChildren();
  faces.forEach((face) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "face-choice";
    button.dataset.faceIndex = String(face.index);
    button.setAttribute("aria-label", `Select detected face ${face.index + 1}`);
    button.setAttribute("aria-pressed", "false");
    Object.assign(button.style, {
      left: `${face.box.x * 100}%`, top: `${face.box.y * 100}%`,
      width: `${face.box.width * 100}%`, height: `${face.box.height * 100}%`,
    });
    const label = document.createElement("span");
    label.textContent = String(face.index + 1);
    button.append(label);
    button.addEventListener("click", () => chooseFace(face.index));
    overlay.append(button);
  });
  if (faces.length === 1) {
    $("#face-picker-title").textContent = "Face detected";
    $("#face-picker-copy").textContent = "The detected face is selected and ready to search.";
    chooseFace(faces[0].index);
  } else {
    $("#face-picker-title").textContent = "Choose the face to search";
    $("#face-picker-copy").textContent = `${faces.length} faces detected. Select one to continue.`;
    $("#search-button").disabled = true;
  }
}

function showPipelineError(error, title = "We could not complete the search") {
  $("#error-title").textContent = title;
  $("#error-message").textContent = error.message || "Try again with another image.";
  $("#retry-button").hidden = error.code === "no_face_detected";
  setView("error");
}

async function detectFaces() {
  if (!selectedFile || !$("#consent-input").checked) return;
  const button = $("#continue-button");
  button.disabled = true;
  button.textContent = "Detecting…";
  const form = new FormData();
  form.append("image", selectedFile);
  try {
    const payload = await apiRequest("/api/faces/detect", form);
    selectedFaceIndex = null;
    renderFacePicker(payload.faces);
    setView("face");
  } catch (error) {
    showPipelineError(error, error.code === "no_face_detected" ? "No face detected" : undefined);
  } finally {
    button.textContent = "Detect faces";
    updateContinueState();
  }
}

function buildResultCard(result) {
  const article = document.createElement("article");
  article.className = "result-card";
  const thumbnail = document.createElement("img");
  thumbnail.className = "result-thumbnail";
  thumbnail.src = result.thumbnail_data_url;
  thumbnail.alt = "Confirmed matching public image";
  const body = document.createElement("div");
  body.className = "result-body";
  const eyebrow = document.createElement("span");
  eyebrow.className = "result-eyebrow";
  eyebrow.textContent = `${result.source_kind === "social_post" ? "Social post" : "Web page"} · ${result.platform}`;
  const title = document.createElement("h3");
  title.textContent = result.page_title;
  const details = document.createElement("p");
  details.textContent = `${result.provider_match_type === "full" ? "Full" : "Partial"} image match · Face similarity ${Number(result.face_similarity).toFixed(3)}`;
  const link = document.createElement("a");
  link.className = "result-link";
  link.href = result.page_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = result.source_kind === "social_post" ? "Open post" : "Open page";
  body.append(eyebrow, title, details, link);
  article.append(thumbnail, body);
  return article;
}

function renderResults(payload) {
  $("#results-list").replaceChildren(...payload.results.map(buildResultCard));
  const social = payload.summary.confirmed_social_posts;
  const total = payload.summary.confirmed_results;
  if (payload.status === "matched") {
    $("#results-title").textContent = "Confirmed social match found";
    $("#results-summary").textContent = `${social} social post${social === 1 ? "" : "s"} confirmed from ${payload.summary.candidates_examined} candidate images.`;
  } else if (total > 0) {
    $("#results-title").textContent = "No confirmed social post";
    $("#results-summary").textContent = `${total} confirmed web match${total === 1 ? "" : "es"} found, but none is a recognized social-post URL.`;
  } else {
    $("#results-title").textContent = "No confirmed match found";
    $("#results-summary").textContent = "No downloadable result passed local face confirmation.";
  }
  setView("results");
}

async function runSearch() {
  if (!selectedFile || selectedFaceIndex === null) return;
  setActiveStep(2);
  setView("search");
  const form = new FormData();
  form.append("image", selectedFile);
  form.append("face_index", String(selectedFaceIndex));
  try { renderResults(await apiRequest("/api/search", form)); }
  catch (error) { showPipelineError(error); }
}

function startOver() {
  clearSelection();
  setActiveStep(1);
  setView("upload");
}

$("#face-input").addEventListener("change", () => {
  const [file] = $("#face-input").files;
  if (file) selectFile(file);
});
$("#consent-input").addEventListener("change", updateContinueState);
["dragenter", "dragover"].forEach((name) => $("#drop-zone").addEventListener(name, (event) => {
  event.preventDefault(); $("#drop-zone").classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => $("#drop-zone").addEventListener(name, (event) => {
  event.preventDefault(); $("#drop-zone").classList.remove("dragging");
}));
$("#drop-zone").addEventListener("drop", (event) => {
  const [file] = event.dataTransfer.files; if (file) selectFile(file);
});
$("#remove-file").addEventListener("click", clearSelection);
$("#continue-button").addEventListener("click", detectFaces);
$("#search-button").addEventListener("click", runSearch);
$("#change-photo-button").addEventListener("click", startOver);
$("#new-search-button").addEventListener("click", startOver);
$("#error-new-search-button").addEventListener("click", startOver);
$("#retry-button").addEventListener("click", () => selectedFaceIndex === null ? detectFaces() : runSearch());

async function checkStatus() {
  try {
    const response = await fetch("/api/status", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error();
    const status = await response.json();
    if (status.face_pipeline === "ready" && status.web_search === "ready") {
      $("#status-dot").className = "status-dot ready"; $("#status-label").textContent = "Search ready";
    } else if (status.face_pipeline !== "ready") {
      $("#status-dot").className = "status-dot error"; $("#status-label").textContent = "Face models missing";
    } else {
      $("#status-dot").className = "status-dot pending"; $("#status-label").textContent = "Search credentials needed";
    }
  } catch {
    $("#status-dot").className = "status-dot error"; $("#status-label").textContent = "Offline";
  }
}

checkStatus();
