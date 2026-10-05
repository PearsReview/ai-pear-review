import assert from "node:assert/strict";
import { test } from "node:test";

import { enterpriseHost, GITHUB_COM, parseGithubRemote } from "../../src/github/remote.ts";

void test("HTTPS, SSH and scp-style GitHub remotes all parse", () => {
  const expected = { host: GITHUB_COM, owner: "acme", repo: "widgets" };
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
  assert.deepEqual(parseGithubRemote("git@github.com:acme/widgets.js.git"), {
    host: GITHUB_COM,
    owner: "acme",
    repo: "widgets.js",
  });
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

void test("a GitHub Enterprise Server host's API is under /api/v3, signed in as github-enterprise", () => {
  assert.deepEqual(enterpriseHost("https://GHE.example.com/"), {
    name: "ghe.example.com",
    api: "https://ghe.example.com/api/v3",
    authProvider: "github-enterprise",
  });
  assert.equal(enterpriseHost("https://ghe.example.com:8443")?.api, "https://ghe.example.com:8443/api/v3");
});

void test("a data-residency (ghe.com) host's API is on its api. subdomain", () => {
  assert.equal(enterpriseHost("https://acme.ghe.com")?.api, "https://api.acme.ghe.com");
});

void test("no enterprise host for an unset, malformed or github.com setting", () => {
  for (const uri of [undefined, "", "ghe.example.com", "ftp://ghe.example.com", "https://github.com"]) {
    assert.equal(enterpriseHost(uri), undefined, String(uri));
  }
});

void test("remotes on the enterprise host parse once it's configured", () => {
  const ghe = enterpriseHost("https://ghe.example.com")!;
  const hosts = [GITHUB_COM, ghe];
  for (const url of [
    "https://ghe.example.com/acme/widgets.git",
    "git@ghe.example.com:acme/widgets.git",
    "ssh://git@ghe.example.com:2222/acme/widgets.git",
  ]) {
    assert.deepEqual(parseGithubRemote(url, hosts), { host: ghe, owner: "acme", repo: "widgets" }, url);
  }
  assert.equal(parseGithubRemote("git@github.com:acme/widgets.git", hosts)?.host, GITHUB_COM);
  assert.equal(parseGithubRemote("git@other.example.com:acme/widgets.git", hosts), undefined);
});
