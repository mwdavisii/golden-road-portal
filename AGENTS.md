<!-- golden-road:begin -->
## Golden road

`builder generate` writes this block. Don't edit it: put your own notes outside the golden-road markers.

- Don't edit the generated files (`Dockerfile`, `.dockerignore`, `deploy/base/`, `_platform/`, `.builder.lock`). Change `builder-spec.json` and run `builder generate .` instead.
- Call models only through the platform gateway, never a model provider directly. `builder guide` names the gateway host.
- Never make this repository public.
- Ask the user about data classification, roles and egress. Never decide them yourself.
- This app's data classification is `confidential`. The list of models approved for `confidential` data is not published yet: ask the platform team before you add a model.
- To package or update this app, use the golden-road skill. Install it from the golden-road landing page: <https://github.com/mwdavisii/golden-road-agents>.
<!-- golden-road:end -->
