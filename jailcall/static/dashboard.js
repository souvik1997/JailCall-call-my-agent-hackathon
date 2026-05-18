const POLL_MS = 800;

const nodes = {
  activeCall: document.querySelector("#active-call"),
  turnCount: document.querySelector("#turn-count"),
  matchCount: document.querySelector("#match-count"),
  chunkCount: document.querySelector("#chunk-count"),
  dispatchInbox: document.querySelector("#dispatch-inbox"),
  liveDot: document.querySelector("#live-dot"),
  liveLabel: document.querySelector("#live-label"),
  lastUpdated: document.querySelector("#last-updated"),
  callStatus: document.querySelector("#call-status"),
  latestQuery: document.querySelector("#latest-query"),
  transcript: document.querySelector("#transcript"),
  mossResults: document.querySelector("#moss-results"),
  toolList: document.querySelector("#tool-list"),
  chunks: document.querySelector("#chunks"),
  replies: document.querySelector("#replies"),
  replyCount: document.querySelector("#reply-count"),
  timeline: document.querySelector("#timeline"),
};

const renderKeys = {
  transcript: "",
  moss: "",
  tools: "",
  chunks: "",
  replies: "",
  timeline: "",
};

let activeTranscriptCallId = "";

function asText(value, fallback = "") {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (typeof value === "number") {
    return String(value);
  }
  return fallback;
}

function el(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text) {
    element.textContent = text;
  }
  return element;
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) {
    return "--:--";
  }
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function previewValue(value) {
  if (!value) {
    return "";
  }
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return previewValue(parsed);
    } catch {
      return value;
    }
  }
  if (Array.isArray(value)) {
    return `${value.length} result${value.length === 1 ? "" : "s"}`;
  }
  if (typeof value === "object") {
    const candidates = value.candidates;
    if (Array.isArray(candidates)) {
      return `${candidates.length} candidate firm${candidates.length === 1 ? "" : "s"}`;
    }
    return JSON.stringify(value);
  }
  return String(value);
}

function pickConversation(state) {
  const conversations = Array.isArray(state.conversations) ? state.conversations : [];
  return (
    conversations.find((conversation) => conversation.call_id === state.active_call_id) ||
    conversations[0] ||
    null
  );
}

function latestMossEvent(state, callId) {
  const events = Array.isArray(state.events) ? state.events : [];
  return [...events]
    .reverse()
    .find((event) => event.kind === "moss" && (!callId || event.call_id === callId));
}

function setEmpty(target, message) {
  const empty = el("div", "empty", message);
  target.replaceChildren(empty);
}

function isNearBottom(target) {
  return target.scrollHeight - target.scrollTop - target.clientHeight < 72;
}

function stableJson(value) {
  return JSON.stringify(value);
}

function renderMetrics(state, conversation, mossEvent) {
  const turns = Array.isArray(conversation?.transcript) ? conversation.transcript : [];
  const chunks = Array.isArray(conversation?.agentphone_chunks)
    ? conversation.agentphone_chunks
    : [];
  const candidates = Array.isArray(mossEvent?.payload?.candidates)
    ? mossEvent.payload.candidates
    : [];

  nodes.activeCall.textContent = asText(conversation?.call_id, "No call yet");
  nodes.turnCount.textContent = String(turns.length);
  nodes.matchCount.textContent = String(candidates.length);
  nodes.chunkCount.textContent = String(chunks.length);
  nodes.dispatchInbox.textContent = asText(state.dispatch_inbox, "Not configured");
  nodes.lastUpdated.textContent = `Updated ${formatTime(state.generated_at)}`;

  const fresh =
    conversation && Number.isFinite(conversation.updated_at)
      ? Date.now() / 1000 - conversation.updated_at < 15
      : false;
  nodes.liveDot.classList.toggle("live", Boolean(fresh));
  nodes.liveLabel.textContent = fresh ? "Live" : "Waiting";

  const status = asText(conversation?.status, "idle");
  nodes.callStatus.textContent = status;
  nodes.callStatus.classList.toggle("ended", status === "ended");
}

function renderTranscript(conversation) {
  const turns = Array.isArray(conversation?.transcript) ? conversation.transcript : [];
  const callId = asText(conversation?.call_id);
  const key = stableJson(
    turns.map((turn) => [turn.role, turn.text, Boolean(turn.interim), turn.source, turn.ts]),
  );
  if (renderKeys.transcript === key && activeTranscriptCallId === callId) {
    return;
  }

  const callChanged = activeTranscriptCallId !== callId;
  const shouldFollow = callChanged || isNearBottom(nodes.transcript);
  const previousScrollTop = nodes.transcript.scrollTop;

  if (turns.length === 0) {
    setEmpty(nodes.transcript, "Waiting for the first voice turn.");
    renderKeys.transcript = key;
    activeTranscriptCallId = callId;
    return;
  }

  const rendered = turns.map((turn) => {
    const role = asText(turn.role, "agent");
    const row = el("article", `turn ${role}${turn.interim ? " interim" : ""}`);
    row.append(
      el("span", "turn-role", turn.interim ? `${role} interim` : role),
      el("div", "turn-text", asText(turn.text, "")),
    );
    return row;
  });
  nodes.transcript.replaceChildren(...rendered);
  renderKeys.transcript = key;
  activeTranscriptCallId = callId;

  if (shouldFollow) {
    nodes.transcript.scrollTop = nodes.transcript.scrollHeight;
  } else {
    nodes.transcript.scrollTop = previousScrollTop;
  }
}

function renderMoss(mossEvent) {
  const payload = mossEvent?.payload || {};
  const query = asText(payload.query, "No query");
  const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  const key = stableJson({ query, candidates });
  if (renderKeys.moss === key) {
    return;
  }
  renderKeys.moss = key;

  nodes.latestQuery.textContent = query;
  if (candidates.length === 0) {
    setEmpty(nodes.mossResults, "No Moss search results yet.");
    return;
  }

  const rows = candidates.map((candidate, index) => {
    const row = el("article", "candidate");
    row.append(el("div", "candidate-name", `${index + 1}. ${asText(candidate.firm_name, "Firm")}`));

    const meta = el("div", "candidate-meta");
    const phone = asText(candidate.phone);
    const email = asText(candidate.email);
    if (phone) {
      meta.append(el("span", "tag phone", phone));
    }
    if (email) {
      meta.append(el("span", "tag email", email));
    }

    row.append(meta, el("p", "candidate-summary", asText(candidate.summary, "No summary.")));
    return row;
  });
  nodes.mossResults.replaceChildren(...rows);
}

function renderTools(conversation) {
  const tools = Array.isArray(conversation?.tools) ? conversation.tools : [];
  const key = stableJson(tools);
  if (renderKeys.tools === key) {
    return;
  }
  renderKeys.tools = key;

  if (tools.length === 0) {
    setEmpty(nodes.toolList, "No dispatch tool calls yet.");
    return;
  }

  const rows = [...tools].reverse().map((tool) => {
    const row = el("article", "tool-row");
    row.append(el("time", "tool-time", formatTime(tool.ts)));

    const main = el("div", "tool-main");
    const title = el("div", "tool-name");
    title.append(
      document.createTextNode(asText(tool.name, "tool")),
      el("span", "tool-status", ` ${asText(tool.status, "")}`),
    );

    const result = previewValue(tool.result) || previewValue(tool.args);
    main.append(title, el("div", "tool-result", result));
    row.append(main);
    return row;
  });
  nodes.toolList.replaceChildren(...rows);
}

function renderChunks(conversation) {
  const chunks = Array.isArray(conversation?.agentphone_chunks)
    ? conversation.agentphone_chunks
    : [];
  const key = stableJson(chunks);
  if (renderKeys.chunks === key) {
    return;
  }
  renderKeys.chunks = key;

  if (chunks.length === 0) {
    setEmpty(nodes.chunks, "No NDJSON response chunks yet.");
    return;
  }

  const rows = [...chunks].reverse().map((chunk) => {
    const row = el("article", "chunk-row");
    row.append(el("time", "chunk-time", formatTime(chunk.ts)));
    const main = el("div", "chunk-main");
    const label = chunk.interim ? "Interim spoken chunk" : "Spoken chunk";
    main.append(el("div", "tool-name", label), el("div", "chunk-text", asText(chunk.text)));
    row.append(main);
    return row;
  });
  nodes.chunks.replaceChildren(...rows);
}

function renderReplies(state) {
  const replies = Array.isArray(state.inbound_replies) ? state.inbound_replies : [];
  const key = stableJson(replies);
  if (renderKeys.replies === key) {
    return;
  }
  renderKeys.replies = key;

  nodes.replyCount.textContent = String(replies.length);

  if (replies.length === 0) {
    setEmpty(nodes.replies, "No attorney replies yet.");
    return;
  }

  const rows = replies.map((reply) => {
    const row = el("article", "reply-row");
    row.append(el("time", "reply-time", formatTime(reply.ts)));
    const main = el("div", "reply-main");
    main.append(
      el("div", "reply-subject", asText(reply.subject, "(no subject)")),
      el("div", "reply-from", `from ${asText(reply.sender, "unknown")}`),
      el("div", "reply-body", asText(reply.text, "(empty body)")),
    );
    row.append(main);
    return row;
  });
  nodes.replies.replaceChildren(...rows);
}

function renderTimeline(state) {
  const events = Array.isArray(state.events) ? state.events : [];
  const key = stableJson(events.slice(-50));
  if (renderKeys.timeline === key) {
    return;
  }
  renderKeys.timeline = key;

  if (events.length === 0) {
    setEmpty(nodes.timeline, "No events recorded yet.");
    return;
  }

  const rows = [...events]
    .reverse()
    .slice(0, 50)
    .map((event) => {
      const row = el("article", "timeline-row");
      row.append(el("time", "event-time", formatTime(event.ts)));
      const main = el("div", "event-main");
      main.append(
        el("div", "event-title", asText(event.title, event.kind)),
        el("div", "event-detail", asText(event.detail, "")),
      );
      row.append(main);
      return row;
    });
  nodes.timeline.replaceChildren(...rows);
}

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const state = await response.json();
    const conversation = pickConversation(state);
    const mossEvent = latestMossEvent(state, conversation?.call_id);

    renderMetrics(state, conversation, mossEvent);
    renderTranscript(conversation);
    renderMoss(mossEvent);
    renderTools(conversation);
    renderChunks(conversation);
    renderReplies(state);
    renderTimeline(state);
  } catch (error) {
    nodes.liveDot.classList.remove("live");
    nodes.liveLabel.textContent = "Disconnected";
    nodes.lastUpdated.textContent = error instanceof Error ? error.message : "Fetch failed";
  }
}

void refresh();
setInterval(() => {
  void refresh();
}, POLL_MS);
