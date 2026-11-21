# Third-party notices

The Roboport Controller source is published by Gellou under the [MIT license](../../LICENSE). The components below carry their own terms.

## Included in the source archive

**CodeMirror 5.65.16** is copyright Marijn Haverbeke and other contributors, licensed under MIT. The JavaScript bundle, styles and Material Darker theme retain their upstream notices. Mattia Astorino is credited in the theme. Keep the [included license](vendor/codemirror/LICENSE) with redistributed copies. [CodeMirror upstream](https://github.com/codemirror/codemirror5/tree/5.65.16)

Roboport’s main interface uses fonts available on the viewer’s device. The README screenshots and preview card are rendered images of the panel. [Microsoft font use FAQ](https://learn.microsoft.com/en-us/typography/fonts/font-faq)

## Loaded by the browser

| Feature | Component and license |
| --- | --- |
| File editor | Microsoft Monaco Editor 0.50.0. [MIT license](https://github.com/microsoft/monaco-editor/blob/v0.50.0/LICENSE.txt) and [notices for components included in Monaco](https://github.com/microsoft/monaco-editor/blob/v0.50.0/ThirdPartyNotices.txt). |
| File editor icons | Codicons artwork by Microsoft and contributors. Roboport uses the icons as supplied with Monaco. [Codicons](https://github.com/microsoft/vscode-codicons) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Metrics charts | Chart.js 4.4.7 by the Chart.js contributors includes @kurkle/color by Jukka Kurkela. [Chart.js MIT license](https://github.com/chartjs/Chart.js/blob/v4.4.7/LICENSE.md) · [color library MIT license](https://github.com/kurkle/color/blob/v0.3.2/LICENSE.md) |
| Chart dates | chartjs-adapter-date-fns 3.0.0 by the Chart.js contributors includes date-fns by Sasha Koss and Lesha Koss. [Adapter MIT license](https://github.com/chartjs/chartjs-adapter-date-fns/blob/v3.0.0/LICENSE.md) · [date-fns MIT license](https://github.com/date-fns/date-fns/blob/v2.29.3/LICENSE.md) |
| API reference | FastAPI selects the browser bundles for Swagger UI and ReDoc. [Swagger UI Apache 2.0 license](https://github.com/swagger-api/swagger-ui/blob/master/LICENSE) · [Swagger UI 5 bundle notices](https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js.LICENSE.txt) · [ReDoc MIT license](https://github.com/Redocly/redoc/blob/main/LICENSE) · [ReDoc 2 bundle notices](https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js.LICENSE.txt) |

## Installed on the host

The installer fetches the Python packages listed in [requirements.txt](../../requirements.txt). Installed packages include their own license files.

| Packages | Terms |
| --- | --- |
| FastAPI | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Uvicorn, Jinja, Babel and psutil | BSD 3-Clause. [Uvicorn](https://github.com/encode/uvicorn/blob/master/LICENSE.md) · [Jinja](https://github.com/pallets/jinja/blob/main/LICENSE.txt) · [Babel](https://github.com/python-babel/babel/blob/master/LICENSE) · [psutil](https://github.com/giampaolo/psutil/blob/master/LICENSE). Babel’s CLDR data also carries a [Unicode license](https://www.unicode.org/license.txt). |
| python-multipart | [Apache 2.0](https://github.com/Kludex/python-multipart/blob/master/LICENSE.txt) |
| python-qrcode | BSD 3-Clause, with an earlier implementation by Kazuhiko Arase under MIT. DENSO WAVE INCORPORATED holds the registered QR Code trademark. [Package notices](https://github.com/lincolnloop/python-qrcode/blob/v8.2/LICENSE) · [DENSO WAVE FAQ](https://www.qrcode.com/en/faq.html?lang=en) |

Linux uses Docker Engine. The Windows installer offers Docker Desktop, which has separate subscription terms. [Docker Engine](https://docs.docker.com/engine/install/) · [Docker Desktop license](https://docs.docker.com/subscription-billing/desktop-license/)

## Factorio servers and mods

Roboport is an independent community project developed without affiliation with or endorsement by Wube Software Ltd. Factorio and its game assets belong to Wube. [Wube’s terms](https://www.factorio.com/terms-of-service) · [Factorio HTTP API guidelines](https://wiki.factorio.com/Factorio_HTTP_API_usage_guidelines)

The panel pulls the community-maintained `factoriotools/factorio` image when a server is set up. The container scripts use MIT. The included Factorio headless server is subject to Wube’s terms, which permit running a commercial game-server hosting service. Game binaries and mod archives are separate downloads. [Container project and license](https://github.com/factoriotools/factorio-docker/blob/master/LICENSE) · [Official headless download](https://www.factorio.com/download)

Mods downloaded from the Factorio Mod Portal retain their authors’ licenses. Check the particular mod before redistributing its archive. [Factorio Mod Portal](https://mods.factorio.com/) · [Mod license options](https://wiki.factorio.com/Mod_details_API)
