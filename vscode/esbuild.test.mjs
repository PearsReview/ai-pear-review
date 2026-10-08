// Bundles the integration suite (test/integration/suite) for the VS Code test
// instance, which loads CommonJS. runTest.ts itself runs straight from TypeScript.
import { readdirSync } from "node:fs";
import * as esbuild from "esbuild";

const suite = "test/integration/suite";
await esbuild.build({
  entryPoints: readdirSync(suite)
    .filter((f) => f === "index.ts" || f.endsWith(".test.ts"))
    .map((f) => `${suite}/${f}`),
  bundle: true,
  format: "cjs",
  platform: "node",
  target: "node20",
  outdir: "dist-test/suite",
  external: ["vscode", "mocha"],
  sourcemap: true,
  logLevel: "warning",
});
