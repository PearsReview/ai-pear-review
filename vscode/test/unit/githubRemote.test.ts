import assert from "node:assert/strict";
import { test } from "node:test";

import { parseGithubRemote } from "../../src/github/remote.ts";

void test("HTTPS, SSH and scp-style GitHub remotes all parse", () => {
  const expected = { owner: "acme", repo: "widgets" };
  for (const url of [
    "https://github.com/acme/widgets.git",
    "https://github.com/acme/widgets",
    "https://token@github.com/acme/widgets.git",
    "git@github.com:acme/widgets.git",
    "ssh://git@github.com/acme/widgets.git",
    "  https://GitHub.com/acme/widgets/  ",
  ]) {
    assert.deepEqual(parseGithubRemote(url), expected, url);
  }
});

void test("a dotted repository name keeps its dots", () => {
  assert.deepEqual(parseGithubRemote("git@github.com:acme/widgets.js.git"), { owner: "acme", repo: "widgets.js" });
});

void test("other hosts and local paths are not GitHub remotes", () => {
  for (const url of [
    "https://gitlab.com/acme/widgets.git",
    "git@ghe.example.com:acme/widgets.git",
    "C:/repos/widgets",
    "../widgets",
  ]) {
    assert.equal(parseGithubRemote(url), undefined, url);
  }
});
