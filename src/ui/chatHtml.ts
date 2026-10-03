// The chat webview's markup. No vscode import, so the DOM tests (test/unit/chat.dom.test.ts)
// render exactly what the panel serves.

export interface ChatHtmlOptions {
  nonce: string;
  // The webview's CSP source, for the stylesheet.
  cspSource: string;
  // A media/chat file → the URL the webview loads it from.
  src: (file: string) => string;
}

export function chatHtml({ nonce, cspSource, src }: ChatHtmlOptions): string {
  const csp = ["default-src 'none'", `style-src ${cspSource}`, `script-src 'nonce-${nonce}'`, "media-src blob:"].join(
    "; ",
  );
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="${src("chat.css")}">
  <title>Pear Review</title>
</head>
<body>
  <header id="hunk">No review running.</header>
  <div id="toolbar">
    <button data-command="startReview" id="start">Start review</button>
    <button data-command="prev" class="secondary" title="Previous change">Prev</button>
    <button data-command="next" class="secondary" title="Next change">Next</button>
    <button data-command="explain" id="explain" class="secondary" title="Have the reviewer explain this change">Explain</button>
  </div>
  <main id="transcript" aria-live="polite"></main>
  <div id="audio-blocked" hidden><button id="enable-audio">Click to enable spoken replies</button></div>
  <div id="audio-bar" hidden>
    <span id="audio-status">Speaking…</span>
    <button id="audio-pause" class="secondary small">Pause</button>
    <button id="audio-stop" class="secondary small">Stop</button>
  </div>
  <div id="target" hidden>
    <span id="target-label"></span>
    <button id="target-back" class="link">Back to review</button>
  </div>
  <div id="context" hidden>
    <span id="context-label"></span>
    <button id="context-clear" class="icon" title="Don't send this selection" aria-label="Don't send this selection">×</button>
  </div>
  <form id="composer">
    <button type="button" id="act" class="secondary" aria-pressed="false">Act Now</button>
    <button type="button" id="mic" data-command="toggleRecording" title="Push to talk (Ctrl+Alt+Space)">Mic</button>
    <textarea id="input" rows="2" placeholder="Ask about this change… (select lines in the editor to ask about them)"></textarea>
    <button type="submit" id="send">Send</button>
  </form>
  <script nonce="${nonce}" src="${src("blocks.js")}"></script>
  <script nonce="${nonce}" src="${src("chat.js")}"></script>
</body>
</html>`;
}
