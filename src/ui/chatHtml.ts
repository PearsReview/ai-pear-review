// The chat webview's markup. No vscode import, so the DOM tests (test/unit/chat.dom.test.ts)
// render exactly what the panel serves.

export interface ChatHtmlOptions {
  nonce: string;
  // The webview's CSP source, for the stylesheets and the icon font.
  cspSource: string;
  // A media/chat file → the URL the webview loads it from.
  src: (file: string) => string;
}

// An icon from VS Code's own codicon font (media/chat/codicons, copied at build).
const icon = (name: string): string => `<i class="codicon codicon-${name}" aria-hidden="true"></i>`;

export function chatHtml({ nonce, cspSource, src }: ChatHtmlOptions): string {
  const csp = [
    "default-src 'none'",
    `style-src ${cspSource}`,
    `font-src ${cspSource}`,
    `script-src 'nonce-${nonce}'`,
    "media-src blob:",
  ].join("; ");
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="${src("codicons/codicon.css")}">
  <link rel="stylesheet" href="${src("chat.css")}">
  <title>Pear Review</title>
</head>
<body>
  <header class="bar">
    <div id="hunk" class="hunk">No review running.</div>
    <div id="toolbar" class="toolbar">
      <button id="explain" class="icon-btn" data-command="explain" title="Explain this change" aria-label="Explain this change">${icon("sparkle")}</button>
    </div>
  </header>
  <main id="transcript" aria-live="polite"></main>
  <div id="audio-bar" class="strip" hidden>
    ${icon("unmute")}<span id="audio-status" class="grow">Speaking…</span>
    <button id="audio-pause" class="icon-btn" title="Pause" aria-label="Pause">${icon("debug-pause")}</button>
    <button id="audio-stop" class="icon-btn" title="Stop" aria-label="Stop">${icon("debug-stop")}</button>
  </div>
  <div id="target" class="strip" hidden>
    ${icon("file")}<span id="target-label" class="grow"></span>
    <button id="target-back" class="pill">${icon("arrow-left")}<span>Back to review</span></button>
  </div>
  <div id="context" class="strip" hidden>
    ${icon("quote")}<span id="context-label" class="grow"></span>
    <button id="context-clear" class="icon-btn" title="Don't send this selection" aria-label="Don't send this selection">${icon("close")}</button>
  </div>
  <form id="composer" class="composer">
    <textarea id="input" rows="2" placeholder="Ask about this change… (select lines in the editor to ask about them)"></textarea>
    <div class="composer-bar">
      <button type="button" id="act" class="pill toggle" aria-pressed="false">${icon("zap")}<span>Act Now</span></button>
      <span class="grow"></span>
      <button type="button" id="mic" class="icon-btn" data-command="toggleRecording" title="Push to talk (Ctrl+Alt+Space)" aria-label="Push to talk">${icon("mic")}</button>
      <button type="submit" id="send" class="icon-btn" title="Send" aria-label="Send">${icon("send")}</button>
    </div>
  </form>
  <script nonce="${nonce}" src="${src("blocks.js")}"></script>
  <script nonce="${nonce}" src="${src("chat.js")}"></script>
</body>
</html>`;
}
