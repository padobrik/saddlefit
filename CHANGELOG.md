# Changelog

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
