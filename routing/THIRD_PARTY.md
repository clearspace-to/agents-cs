# Design references

This focused implementation reuses the existing Clearspace Jev review tool. Its
request-middleware structure, once-per-turn routing and audit design were informed
by these MIT-licensed implementations. No upstream installer or optional module is
included. Retained MIT notices are in `licenses/`.

- AlphaPerseii3000/jev-effort-router, revision 562dc2a27aa22977e0795809a17fbbbb1819f0ab.
  https://github.com/AlphaPerseii3000/jev-effort-router
- kerpopule/hermes-jev-skills, revision c52837522f575820c63d2ff608c6cb19bae96400.
  https://github.com/kerpopule/hermes-jev-skills

Our router uses OpenRouter's Decisions API, a protected OpenRouter credential,
and Codex Responses `reasoning.effort`. It does not import either upstream package.
