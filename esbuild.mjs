import { copyFileSync, mkdirSync } from "node:fs";
import * as esbuild from "esbuild";

// The chat webview's icons: VS Code's own codicon font, copied next to the chat's
// files because a webview may only load from its media folder.
mkdirSync("media/chat/codicons", { recursive: true });
for (const file of ["codicon.css", "codicon.ttf"]) {
  copyFileSync(`node_modules/@vscode/codicons/dist/${file}`, `media/chat/codicons/${file}`);
}

const production = process.argv.includes("--production");
const watch = process.argv.includes("--watch");

const ctx = await esbuild.context({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  format: "cjs",
  platform: "node",
  target: "node20",
  outfile: "dist/extension.js",
  external: ["vscode", "bufferutil", "utf-8-validate"],
  sourcemap: !production,
  minify: production,
  logLevel: "info",
});

if (watch) {
  await ctx.watch();
} else {
  await ctx.rebuild();
  await ctx.dispose();
}
