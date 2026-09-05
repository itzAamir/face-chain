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
  $("#continue-button").disabled = !selectedFile;
  $("#selection-note").textContent = selectedFile ? "IMAGE LOADED" : "NO IMAGE SELECTED";
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
    $("#face-picker-title").textContent = "1 face detected";
    $("#face-picker-copy").textContent = "Detection 01 selected.";
    chooseFace(faces[0].index);
  } else {
    $("#face-picker-title").textContent = `${faces.length} faces detected`;
    $("#face-picker-copy").textContent = "Select a detection.";
    $("#search-button").disabled = true;
  }
}

function showPipelineError(error, title = "Search failed") {
  $("#error-title").textContent = title;
  $("#error-message").textContent = error.message || "Try again with another image.";
  $("#retry-button").hidden = error.code === "no_face_detected";
  setView("error");
}

async function detectFaces() {
  if (!selectedFile) return;
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
    button.textContent = "Detect";
    updateContinueState();
  }
}

const PROOF_STATES = {
  attested: { label: "Anchored on chain", tone: "" },
  already_attested: { label: "Already on chain", tone: "" },
  unavailable: { label: "Not anchored", tone: "absent" },
  disabled: { label: "Anchoring off", tone: "pending" },
};

// Every status the API can return, and how loudly to say it. `verified` and
// `image_changed` must never look alike.
const VERIFY_STATES = {
  verified: { label: "Verified", tone: "ok",
    fallback: "This record hashes to a digest recorded on chain." },
  digest_mismatch: { label: "Digest mismatch", tone: "bad",
    fallback: "This record does not hash to the digest it claims." },
  image_changed: { label: "Image changed", tone: "bad",
    fallback: "The record is on chain, but the post now serves different bytes." },
  not_attested: { label: "Not attested", tone: "bad",
    fallback: "This digest is not on the chain." },
  chain_reset: { label: "Chain reset", tone: "unknown",
    fallback: "This record was written against a different chain instance." },
  chain_unavailable: { label: "Chain unavailable", tone: "unknown",
    fallback: "The node could not be reached, so nothing can be concluded." },
};

function shorten(value, head = 10, tail = 8) {
  if (typeof value !== "string" || value.length <= head + tail + 1) return value ?? "—";
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

function formatBlockTime(seconds) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toISOString().replace("T", " ").replace(".000Z", "Z");
}

function proofRow(label, value, title) {
  const row = document.createElement("div");
  row.className = "proof-row";
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  if (title) dd.title = title;
  row.append(dt, dd);
  return row;
}

function copyButton(value) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "chip";
  button.textContent = "Copy";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Copy failed";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1500);
  });
  return button;
}

function buildProof(proof) {
  const section = document.createElement("div");
  section.className = "proof";

  const state = PROOF_STATES[proof.state] || PROOF_STATES.unavailable;
  const head = document.createElement("div");
  head.className = "proof-head";
  const badge = document.createElement("span");
  badge.className = `proof-state ${state.tone}`.trim();
  badge.textContent = state.label;
  head.append(badge);
  if (proof.chain_id) {
    const note = document.createElement("span");
    note.className = "proof-note";
    note.textContent = `chain ${proof.chain_id}`;
    head.append(note);
  }
  section.append(head);

  if (!proof.record_digest) {
    if (proof.reason) {
      const note = document.createElement("p");
      note.className = "proof-note";
      note.textContent = proof.reason;
      section.append(note);
    }
    return section;
  }

  const rows = document.createElement("dl");
  rows.className = "proof-rows";
  const digestRow = proofRow("RECORD", shorten(proof.record_digest, 12, 10), proof.record_digest);
  digestRow.append(copyButton(proof.record_digest));
  rows.append(digestRow);
  rows.append(proofRow("IMAGE", shorten(proof.image_digest, 12, 10), proof.image_digest));
  if (proof.record?.tx_hash) {
    rows.append(proofRow("TX", shorten(proof.record.tx_hash, 12, 10), proof.record.tx_hash));
  }
  if (proof.record?.timestamp) {
    rows.append(proofRow("BLOCK TIME", formatBlockTime(proof.record.timestamp)));
  }
  if (proof.contract_address) {
    rows.append(proofRow("CONTRACT", shorten(proof.contract_address, 10, 8), proof.contract_address));
  }
  section.append(rows);

  const actions = document.createElement("div");
  actions.className = "proof-actions";

  const download = document.createElement("a");
  download.className = "chip";
  download.href = `/api/evidence/${proof.record_digest}`;
  download.download = `${proof.record_digest}.json`;
  download.textContent = "Download record";
  actions.append(download);

  const check = document.createElement("button");
  check.type = "button";
  check.className = "chip";
  check.textContent = "Re-verify";
  check.addEventListener("click", () => verifyByDigest(proof.record_digest, check));
  actions.append(check);

  section.append(actions);
  return section;
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
  const sourceLabel = result.source_kind === "social_post"
    ? "Social post" : result.source_kind === "web_image" ? "Public image" : "Web page";
  eyebrow.textContent = `${sourceLabel} · ${result.platform}`;
  const title = document.createElement("h3");
  title.textContent = result.page_title;
  const details = document.createElement("p");
  const matchLabel = result.provider_match_type === "full"
    ? "Full image match"
    : result.provider_match_type === "partial" ? "Partial image match" : "Visual face match";
  const providerLabel = result.discovery_provider?.includes("serpapi") ? "Google Lens" : "Google Vision";
  details.textContent = `${matchLabel} · Face similarity ${Number(result.face_similarity).toFixed(3)} · ${providerLabel}`;
  const link = document.createElement("a");
  link.className = "result-link";
  link.href = result.page_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = result.source_kind === "social_post"
    ? "Open post" : result.source_kind === "web_image" ? "Open image" : "Open page";
  body.append(eyebrow, title, details, link);
  if (result.proof) body.append(buildProof(result.proof));
  article.append(thumbnail, body);
  return article;
}

function renderVerification(payload) {
  const output = $("#verify-output");
  const state = VERIFY_STATES[payload.status] || {
    label: payload.status || "Unknown", tone: "unknown",
    fallback: "The service returned an unrecognized status.",
  };
  output.className = `verify-output ${state.tone}`;
  output.replaceChildren();

  const verdict = document.createElement("span");
  verdict.className = "verify-verdict";
  verdict.textContent = state.label;
  output.append(verdict);

  const reason = document.createElement("p");
  reason.className = "verify-reason";
  reason.textContent = payload.reason || state.fallback;
  output.append(reason);

  const rows = document.createElement("dl");
  rows.className = "proof-rows";
  if (payload.record_digest) {
    rows.append(proofRow("RECORD", shorten(payload.record_digest, 12, 10), payload.record_digest));
  }
  if (payload.claimed_digest) {
    rows.append(proofRow("CLAIMED", shorten(payload.claimed_digest, 12, 10), payload.claimed_digest));
  }
  const onChain = payload.on_chain?.record;
  if (onChain?.exists) {
    rows.append(proofRow("ATTESTED BY", shorten(onChain.submitter, 10, 8), onChain.submitter));
    rows.append(proofRow("BLOCK TIME", formatBlockTime(onChain.timestamp)));
  }
  if (payload.refetch) {
    rows.append(payload.refetch.attempted
      ? proofRow("LIVE IMAGE", payload.refetch.matches ? "matches attested bytes" : "differs from attested bytes")
      : proofRow("LIVE IMAGE", "not fetched", payload.refetch.reason));
  }
  if (rows.childElementCount) output.append(rows);
  output.hidden = false;
  output.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showVerifyPending(message) {
  const output = $("#verify-output");
  output.className = "verify-output unknown";
  output.replaceChildren();
  const verdict = document.createElement("span");
  verdict.className = "verify-verdict";
  verdict.textContent = message;
  output.append(verdict);
  output.hidden = false;
}

async function submitVerification(form) {
  // A failed verification answers with 422; that is a verdict, not an error.
  const response = await fetch("/api/verify", { method: "POST", body: form });
  let payload = null;
  try { payload = await response.json(); } catch { /* handled below */ }
  if (payload?.status) return renderVerification(payload);
  renderVerification({
    status: "chain_unavailable",
    reason: payload?.detail?.message || "The service returned an unexpected response.",
  });
}

async function verifyByDigest(digest, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Checking…";
  showVerifyPending("Checking…");
  const form = new FormData();
  form.append("digest", digest);
  try { await submitVerification(form); }
  finally { button.disabled = false; button.textContent = label; }
}

async function verifyRecordFile(file) {
  if (!file) return;
  showVerifyPending("Checking…");
  const form = new FormData();
  form.append("record", file, file.name);
  if ($("#verify-refetch").checked) form.append("refetch", "true");
  await submitVerification(form);
}

function renderResults(payload) {
  $("#results-list").replaceChildren(...payload.results.map(buildResultCard));
  const social = payload.summary.confirmed_social_posts;
  const total = payload.summary.confirmed_results;
  const candidates = payload.summary.candidates_examined;
  const images = payload.summary.candidate_images_examined ?? candidates;
  if (payload.status === "matched") {
    $("#results-title").textContent = "Social match";
  } else if (total > 0) {
    $("#results-title").textContent = "Web matches only";
  } else {
    $("#results-title").textContent = "No match";
  }
  $("#results-summary").textContent = `${social} social · ${total} confirmed · ${candidates} candidates · ${images} images checked`;
  if (total > 0) setActiveStep(3);
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
  $("#verify-output").hidden = true;
  $("#verify-input").value = "";
  setView("upload");
}

$("#face-input").addEventListener("change", () => {
  const [file] = $("#face-input").files;
  if (file) selectFile(file);
});
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

$("#verify-input").addEventListener("change", () => {
  const [file] = $("#verify-input").files;
  verifyRecordFile(file);
});
["dragenter", "dragover"].forEach((name) => $("#verify-zone").addEventListener(name, (event) => {
  event.preventDefault(); $("#verify-zone").classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => $("#verify-zone").addEventListener(name, (event) => {
  event.preventDefault(); $("#verify-zone").classList.remove("dragging");
}));
$("#verify-zone").addEventListener("drop", (event) => {
  const [file] = event.dataTransfer.files;
  verifyRecordFile(file);
});

async function checkStatus() {
  const setRuntime = (selector, ready, readyLabel = "READY") => {
    const element = $(selector);
    element.textContent = ready ? readyLabel : "OFFLINE";
    element.classList.toggle("ready", ready);
    element.classList.toggle("offline", !ready);
  };
  try {
    const response = await fetch("/api/status", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error();
    const status = await response.json();
    setRuntime("#models-status", status.face_pipeline === "ready");
    setRuntime("#vision-status", status.search_providers?.google_web_detection === "configured");
    setRuntime("#lens-status", status.search_providers?.serpapi_google_lens === "configured");

    const chain = status.blockchain || {};
    const chainReady = chain.state === "ready";
    const chainElement = $("#chain-status");
    chainElement.textContent = chainReady
      ? (status.attestation === "enabled" ? "READY" : "READ ONLY")
      : chain.state === "not_configured" ? "NOT DEPLOYED" : "OFFLINE";
    chainElement.classList.toggle("ready", chainReady && status.attestation === "enabled");
    chainElement.classList.toggle("offline", !chainReady);
    chainElement.title = chain.contract_address
      ? `${chain.contract_address} on chain ${chain.chain_id}`
      : "";

    if (status.face_pipeline === "ready" && status.web_search === "ready") {
      $("#status-dot").className = "status-dot ready"; $("#status-label").textContent = "Runtime ready";
    } else if (status.face_pipeline !== "ready") {
      $("#status-dot").className = "status-dot error"; $("#status-label").textContent = "Face models missing";
    } else {
      $("#status-dot").className = "status-dot pending"; $("#status-label").textContent = "Search credentials needed";
    }
  } catch {
    setRuntime("#models-status", false);
    setRuntime("#vision-status", false);
    setRuntime("#lens-status", false);
    setRuntime("#chain-status", false);
    $("#status-dot").className = "status-dot error"; $("#status-label").textContent = "Offline";
  }
}

checkStatus();
