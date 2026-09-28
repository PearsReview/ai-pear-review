# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/PearsReview/ai-pear-review/security/advisories/new),
not in a public issue. Include what you found and how to reproduce it.
This is a beta maintained in spare time, so replies may take a few days.

## What the app assumes

- **It is a local, single-user tool.** The server has no authentication.
  It binds to `127.0.0.1` by default and checks the WebSocket `Origin`, so
  other web pages can't drive it. Setting `server.host` to anything other
  than a loopback address exposes it to your network; that is not a
  supported setup, and startup warns when you do it.
- **It can change your files.** Act Now writes to your working tree, but
  only after you confirm a preview. Look deeper runs read-only, in a
  temporary copy of the repo.
- **It sends code to the model you configure.** See
  [Privacy](README.md#privacy) in the README.

Only the latest release gets security fixes.
