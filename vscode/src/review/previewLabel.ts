// Whether a markdown preview's tab label names a file. VS Code labels the preview
// "Preview README.md" in English, translated elsewhere ("[Preview] …" in some
// versions), so the label must be the file name itself or end with " <name>". Pure, so
// it's unit-tested (test/unit/previewLabel.test.ts).
export function labelNames(label: string, fileName: string): boolean {
  const trimmed = label.trim();
  return trimmed === fileName || trimmed.endsWith(` ${fileName}`);
}
