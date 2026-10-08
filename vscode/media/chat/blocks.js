// Renders a reply's markdown blocks (markdown_speech.block_to_payload) into DOM.
// Ported from appendBlockBody/appendSpans in backend/static/js/md-preview.js, which
// can't be loaded here: that module imports the browser app's socket and state.
// Keep the two in step when the block payload changes.
// @ts-check
(function () {
  function appendSpans(el, spans) {
    for (const span of spans || []) {
      if (!span.text) continue;
      if (span.style === "plain") {
        el.appendChild(document.createTextNode(span.text));
        continue;
      }
      const node = document.createElement(span.style === "code" ? "code" : "span");
      node.className = "md-span md-" + span.style;
      node.textContent = span.text;
      // A tooltip, never an href: a model-written URL is not something to navigate to.
      if (span.style === "link" && span.href) node.title = span.href;
      el.appendChild(node);
    }
  }

  function appendBlockBody(el, block) {
    if (block.kind === "code") {
      const pre = document.createElement("pre");
      pre.className = "md-code-block";
      const code = document.createElement("code");
      code.textContent = block.code_text || "";
      if (block.code_lang) pre.dataset.lang = block.code_lang;
      pre.appendChild(code);
      el.appendChild(pre);
      return;
    }
    if (block.kind === "rule") return; // drawn with a CSS border
    if (block.kind === "table_row") {
      for (const cell of block.cells || []) {
        const span = document.createElement("span");
        span.className = "md-cell";
        span.textContent = cell;
        el.appendChild(span);
      }
      return;
    }
    if (block.kind === "list_item" && block.ordered && block.marker) {
      const marker = document.createElement("span");
      marker.className = "md-list-marker";
      marker.textContent = block.marker;
      el.appendChild(marker);
    }
    appendSpans(el, block.spans);
  }

  /** @param {HTMLElement} body @param {any[]} blocks */
  function renderBlocks(body, blocks) {
    for (const block of blocks) {
      const el = document.createElement("div");
      el.className = "turn-block md-" + block.kind.replace(/_/g, "-") + (block.level ? " md-level-" + block.level : "");
      if (block.kind === "list_item" && block.level) el.style.paddingLeft = `${1 + block.level}em`;
      if (block.kind === "table_row" && block.is_header) el.classList.add("md-header-row");
      appendBlockBody(el, block);
      body.appendChild(el);
    }
  }

  // The one global this file exports, read by chat.js (types: globals.d.ts).
  window.PearBlocks = { renderBlocks };
})();
