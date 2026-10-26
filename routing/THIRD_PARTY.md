# Design references

Auto effort's request-middleware structure, once-per-turn classification and
content-free record were informed by these MIT-licensed implementations. No upstream installer or optional module is
included. Retained MIT notices are in `licenses/`.

- AlphaPerseii3000/jev-effort-router, revision 562dc2a27aa22977e0795809a17fbbbb1819f0ab.
  https://github.com/AlphaPerseii3000/jev-effort-router
- kerpopule/hermes-jev-skills, revision c52837522f575820c63d2ff608c6cb19bae96400.
  https://github.com/kerpopule/hermes-jev-skills

Auto effort uses OpenRouter's Decisions API, a protected OpenRouter credential,
Codex Responses `reasoning.effort` and Claude `output_config.effort`. It does not
import either upstream package.
