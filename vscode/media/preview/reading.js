// Keeps the passage being read aloud in view in VS Code's markdown preview: the preview
// re-renders as the reading moves (the extension refreshes it), and fires
// vscode.markdown.updateContent each time.
(function () {
  function follow() {
    const passage = document.querySelector(".pear-reading");
    if (passage) passage.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  window.addEventListener("vscode.markdown.updateContent", follow);
  window.addEventListener("load", follow);
  follow();
})();
