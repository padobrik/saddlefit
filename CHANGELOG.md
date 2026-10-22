# Changelog

## [0.2.0](https://github.com/padobrik/saddlefit/compare/v0.1.3...v0.2.0) (2026-10-22)


### Features

* **cli:** add export --format skill ([#111](https://github.com/padobrik/saddlefit/issues/111)) ([62c68b8](https://github.com/padobrik/saddlefit/commit/62c68b86f64843220e1c496f07536d0883c60b17))
* **cli:** add report --final ([#110](https://github.com/padobrik/saddlefit/issues/110)) ([bce0280](https://github.com/padobrik/saddlefit/commit/bce02805f48b627281875d17183c4e816c2f50c8))
* **cli:** add the fit command ([#104](https://github.com/padobrik/saddlefit/issues/104)) ([e2048cc](https://github.com/padobrik/saddlefit/commit/e2048cccf50d4ffc4d961ecd45f2482fde9b99f3))
* **cli:** add the prune command ([#106](https://github.com/padobrik/saddlefit/issues/106)) ([41fe8c2](https://github.com/padobrik/saddlefit/commit/41fe8c28544146823a45ef0575f4a1c17f497ac1))
* **eval:** add batch and held-out gates and the tie-break ([#94](https://github.com/padobrik/saddlefit/issues/94)) ([dd70d0d](https://github.com/padobrik/saddlefit/commit/dd70d0df61deb00016ef971b45da2053d3415be4))
* **eval:** add report statistics and the delegation map ([#107](https://github.com/padobrik/saddlefit/issues/107)) ([8669cac](https://github.com/padobrik/saddlefit/commit/8669cac69470eb4b3d493d7f4e588a69bd531ddf))
* **eval:** log control evaluations and freeze the harness ([#109](https://github.com/padobrik/saddlefit/issues/109)) ([a12a88b](https://github.com/padobrik/saddlefit/commit/a12a88b4de78751b8ba0f57b04d95683601b74f1))
* **harness:** validate and apply proposals ([#95](https://github.com/padobrik/saddlefit/issues/95)) ([8eb1a43](https://github.com/padobrik/saddlefit/commit/8eb1a43d1268544f5cc59f0fc435d872bddf36b2))
* **loops:** ablate accepted changes and prune the useless ([#105](https://github.com/padobrik/saddlefit/issues/105)) ([1d6fe40](https://github.com/padobrik/saddlefit/commit/1d6fe40f7d9c8dff8f36d589f865beb10914d899))
* **loops:** add the middle loop round ([#103](https://github.com/padobrik/saddlefit/issues/103)) ([96b2f85](https://github.com/padobrik/saddlefit/commit/96b2f85feefd93dd29f2922891b6912d016e6e6b))
* **providers:** add the Messages API provider for the tutor ([#97](https://github.com/padobrik/saddlefit/issues/97)) ([2e7050a](https://github.com/padobrik/saddlefit/commit/2e7050a94034c0b610c2bf197dc78daa69ae1a01))
* **reports:** report progress since the baseline ([#108](https://github.com/padobrik/saddlefit/issues/108)) ([9f62d56](https://github.com/padobrik/saddlefit/commit/9f62d56f332c6ed5429ea602a086a05c85c340c4))
* **storage:** add change records for the library ([#102](https://github.com/padobrik/saddlefit/issues/102)) ([2c14ada](https://github.com/padobrik/saddlefit/commit/2c14ada335531ba55345f41e145acfc67f2f5f9e))
* **tutor:** diagnose failed runs into one proposal per call ([#98](https://github.com/padobrik/saddlefit/issues/98)) ([8a3739f](https://github.com/padobrik/saddlefit/commit/8a3739fdcb620205aab64ab4f6d963cc8b6d7722))
* **tutor:** record a reflection on each decision in EXPERIENCE.md ([#99](https://github.com/padobrik/saddlefit/issues/99)) ([d9590ee](https://github.com/padobrik/saddlefit/commit/d9590ee671683207adad520f8cc6bda36be72198))


### Bug Fixes

* **report:** skip run records cut off by a crash ([#112](https://github.com/padobrik/saddlefit/issues/112)) ([a3911f9](https://github.com/padobrik/saddlefit/commit/a3911f96edee210ce2132bf956d8c6a1df440d47))


### Documentation

* **adr:** free-text harness files are content, not formats ([#101](https://github.com/padobrik/saddlefit/issues/101)) ([6535d50](https://github.com/padobrik/saddlefit/commit/6535d50948aaa294534ce7f679588256659dbcc6))

## [0.1.3](https://github.com/padobrik/saddlefit/compare/v0.1.2...v0.1.3) (2026-10-21)


### Features

* **config:** add prompt-cache prices for the tutor ([#92](https://github.com/padobrik/saddlefit/issues/92)) ([0ba3bcb](https://github.com/padobrik/saddlefit/commit/0ba3bcb58b800bd281b612a0f0e74e5326b95f8c)), closes [#91](https://github.com/padobrik/saddlefit/issues/91)

## [0.1.2](https://github.com/padobrik/saddlefit/compare/v0.1.1...v0.1.2) (2026-10-21)


### Bug Fixes

* **config:** default to the sandbox image of the installed version ([#88](https://github.com/padobrik/saddlefit/issues/88)) ([1f3980b](https://github.com/padobrik/saddlefit/commit/1f3980b611c3895560b721d2e4e2788096c876ba))


### Documentation

* **adr:** a new default for a written-out field is not a format change ([#89](https://github.com/padobrik/saddlefit/issues/89)) ([dd3ac25](https://github.com/padobrik/saddlefit/commit/dd3ac25b1d1c95ffdc3ea19790a549d1897cd548))

## [0.1.1](https://github.com/padobrik/saddlefit/compare/v0.1.0...v0.1.1) (2026-10-21)


### Bug Fixes

* **ci:** publish the sandbox image for arm64 as well ([#85](https://github.com/padobrik/saddlefit/issues/85)) ([3a5bf93](https://github.com/padobrik/saddlefit/commit/3a5bf93e10e0753ebde93bb459e136cda5574244))
* **sandbox:** build per-repository environment images ([#84](https://github.com/padobrik/saddlefit/issues/84)) ([b3343b7](https://github.com/padobrik/saddlefit/commit/b3343b7ea89d316f2ec0657c593f2f60ec5cccc0))

## 0.1.0 (2026-10-21)


### Features

* **cli:** add baseline and report ([#82](https://github.com/padobrik/saddlefit/issues/82)) ([5a90709](https://github.com/padobrik/saddlefit/commit/5a907093174ca2dfc10abf39d2260e0ad68030b6))
* **cli:** add doctor with speed, tool-call probes and tier ([#77](https://github.com/padobrik/saddlefit/issues/77)) ([93e9409](https://github.com/padobrik/saddlefit/commit/93e940903454f54b872cf8c5227d5ce916b2cc1b))
* **cli:** add init and tasks mine, list and show ([#79](https://github.com/padobrik/saddlefit/issues/79)) ([d2c5ba5](https://github.com/padobrik/saddlefit/commit/d2c5ba5b04eef2f118d846b67170e1b9989ca5eb))
* **config:** add saddlefit.yaml model and loader ([#55](https://github.com/padobrik/saddlefit/issues/55)) ([1a9c31a](https://github.com/padobrik/saddlefit/commit/1a9c31ad843aaa4b9932806910793e15a9d634b7))
* **context:** assemble the harness context within budget ([#71](https://github.com/padobrik/saddlefit/issues/71)) ([4d741b5](https://github.com/padobrik/saddlefit/commit/4d741b561414ee48b055db4bc7b8d468a4d05beb))
* **eval:** add failure taxonomy and tamper detection ([#64](https://github.com/padobrik/saddlefit/issues/64)) ([0a7c3b4](https://github.com/padobrik/saddlefit/commit/0a7c3b46c592d6b1f38561129606b789a7ae133b))
* **eval:** add leak guard that aborts on canaries ([#65](https://github.com/padobrik/saddlefit/issues/65)) ([91ede13](https://github.com/padobrik/saddlefit/commit/91ede13b4efb16b9ab24efa5fafdf329dd287d9b))
* **eval:** add the evaluator with tamper checks ([#75](https://github.com/padobrik/saddlefit/issues/75)) ([e9b4297](https://github.com/padobrik/saddlefit/commit/e9b4297896f0bc7eecf669ac745588604157bc00))
* **harness:** add git-backed harness store ([#74](https://github.com/padobrik/saddlefit/issues/74)) ([29005fd](https://github.com/padobrik/saddlefit/commit/29005fd84106418ffb0656addc295b66c40a78dc))
* **harness:** add loop.yaml schema, clamping and allowlist ([#67](https://github.com/padobrik/saddlefit/issues/67)) ([fdf8a94](https://github.com/padobrik/saddlefit/commit/fdf8a94bdb4ed141dc0be8cdf10b0181483ce306))
* **logs:** add JSON-line logging with secret redaction ([#58](https://github.com/padobrik/saddlefit/issues/58)) ([22f9594](https://github.com/padobrik/saddlefit/commit/22f95944725a36a7023f9056b4dd22d8e782ae8b))
* **providers:** add ModelClient protocol and JSON tool parsing ([#57](https://github.com/padobrik/saddlefit/issues/57)) ([2cbf36c](https://github.com/padobrik/saddlefit/commit/2cbf36c9bbb5a334103f32911d9d9171c10dcc9e))
* **providers:** add OpenAI-compatible student provider ([#60](https://github.com/padobrik/saddlefit/issues/60)) ([79eb478](https://github.com/padobrik/saddlefit/commit/79eb478f3a259fbfcbe8fedca7ca5d2d68ecfb89))
* **providers:** add retries and scripted fake provider ([#59](https://github.com/padobrik/saddlefit/issues/59)) ([0a40318](https://github.com/padobrik/saddlefit/commit/0a40318aa4b9c00593c8ba84ba1405153aad917e))
* **providers:** pass reasoning effort to the runner ([#80](https://github.com/padobrik/saddlefit/issues/80)) ([e41765b](https://github.com/padobrik/saddlefit/commit/e41765bc7178fc9cadbf6cdf6639da80b904f6ff))
* **sandbox:** add container lifecycle with B11 limits ([#61](https://github.com/padobrik/saddlefit/issues/61)) ([2714dab](https://github.com/padobrik/saddlefit/commit/2714dabdf377cd40c846307f0213e06db45072a4))
* **storage:** add run records and schema migrations ([#66](https://github.com/padobrik/saddlefit/issues/66)) ([1f0a37b](https://github.com/padobrik/saddlefit/commit/1f0a37b5ec994004c67aeab750981cc1ae9b15ea))
* **student:** add the agent loop with finish gate and loop detection ([#76](https://github.com/padobrik/saddlefit/issues/76)) ([d713721](https://github.com/padobrik/saddlefit/commit/d7137210f4f4178fdddb2cce4f80fa83945cc9a6))
* **tasks:** add deterministic splits and the suite lock ([#62](https://github.com/padobrik/saddlefit/issues/62)) ([8002fd5](https://github.com/padobrik/saddlefit/commit/8002fd55e339d2444607d7d05a2a1205fdc5298e))
* **tasks:** add sealed store, canaries and import boundary ([#63](https://github.com/padobrik/saddlefit/issues/63)) ([1e61e84](https://github.com/padobrik/saddlefit/commit/1e61e84de678b901b348128577fb10566707f55e))
* **tasks:** mine viable tasks from git history ([#69](https://github.com/padobrik/saddlefit/issues/69)) ([8719e13](https://github.com/padobrik/saddlefit/commit/8719e1311522a39e2d04291f49a87a6776e2d49a))
* **tools:** add student tools that run in the sandbox ([#70](https://github.com/padobrik/saddlefit/issues/70)) ([306eedb](https://github.com/padobrik/saddlefit/commit/306eedbe3873f8e0fd84faa1e27b09a81b017fd5))


### Documentation

* add build specification ([1f9c25f](https://github.com/padobrik/saddlefit/commit/1f9c25f49fe4f300bbf34ef87f5ad6b012b45d34))
* add contributor, conduct and security docs ([6e8812a](https://github.com/padobrik/saddlefit/commit/6e8812a86976c43a690aff1c08a46108886f341a))
* add readme, architecture, ADR process and notes ([1cb2581](https://github.com/padobrik/saddlefit/commit/1cb25816ade20b1d0795a214732124666a9ea9b7))
* add the Saddlefit logo to the README ([#81](https://github.com/padobrik/saddlefit/issues/81)) ([b2a27da](https://github.com/padobrik/saddlefit/commit/b2a27daf00c50812587be2396a1ab733ee9f12e4))
* **adr:** define what reading sealed data means for evaluation ([#78](https://github.com/padobrik/saddlefit/issues/78)) ([b13b7af](https://github.com/padobrik/saddlefit/commit/b13b7afe854233f588cfe43250dbfc49313ac69c))
* **readme:** add loop and landscape diagrams ([#68](https://github.com/padobrik/saddlefit/issues/68)) ([f56be57](https://github.com/padobrik/saddlefit/commit/f56be57b2940471848843df6e5e88a78c4e49dd7))
